"""Browser regression checks using a synthetic, user-data-free HTML fixture.

The test is optional for local environments without Playwright. CI installs the
test dependency and Chromium so these checks run there instead of silently
depending on an Antigravity installation or a live account.
"""

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).with_name("browser_fixture.html")
sys.path.insert(0, str(ROOT))

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised by minimal contributors' installs
    sync_playwright = None

import hanhua_v2


DICTIONARY = {
    "Hello": "你好",
    "Save": "保存",
    "File": "文件",
    "Shadow Label": "影子标签",
    "Dynamic": "动态",
}


@unittest.skipUnless(sync_playwright is not None, "Playwright is not installed")
class BrowserFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = hanhua_v2.build_engine_js(DICTIONARY)

    def setUp(self):
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(headless=True)
        except Exception as error:  # missing browser executable is an environment skip
            self.playwright.stop()
            self.skipTest(f"Playwright Chromium is unavailable: {error}")
        self.page = self.browser.new_page()
        self.page.goto(FIXTURE.resolve().as_uri(), wait_until="domcontentloaded")

    def tearDown(self):
        if getattr(self, "browser", None) is not None:
            self.browser.close()
        if getattr(self, "playwright", None) is not None:
            self.playwright.stop()

    @staticmethod
    def _wait(page, expression):
        page.wait_for_function(expression, timeout=5000)

    def test_async_mutations_shadow_dom_and_input_privacy(self):
        page = self.page
        page.evaluate(self.engine)
        self._wait(page, "document.querySelector('#static-text').textContent === '你好'")
        self._wait(page, "document.querySelector('#save-button').textContent === '保存'")
        self._wait(page, "document.querySelector('#save-button').getAttribute('aria-label') === '保存'")
        self._wait(page, "document.querySelector('#shadow-host').shadowRoot.textContent.includes('影子标签')")

        # Multiple mutations in one browser task are delivered as a single observer batch.
        page.evaluate("""
            const button = document.querySelector('#boundary-button');
            button.classList.add('terminal');
            button.firstChild.nodeValue = 'File';
            button.setAttribute('title', 'File');
        """)
        self._wait(page, "document.querySelector('#boundary-button').textContent === 'File'")
        self.assertEqual(page.locator("#boundary-button").get_attribute("title"), "File")

        self.assertEqual(page.locator("#safe-input").input_value(), "typed sample")
        self.assertEqual(page.locator("#safe-input").get_attribute("placeholder"), "文件")
        self.assertEqual(page.locator("#safe-input").get_attribute("title"), "Save")
        self.assertEqual(page.locator("#protected-message").text_content(), "Hello")
        self.assertEqual(page.locator("#terminal-message").text_content(), "Hello")

        page.evaluate("document.querySelector('#dynamic-root').append(document.createTextNode('Dynamic'))")
        self._wait(page, "document.querySelector('#dynamic-root').textContent === '动态'")
        page.evaluate("document.querySelector('#late-button').setAttribute('aria-label', 'Save')")
        self._wait(page, "document.querySelector('#late-button').getAttribute('aria-label') === '保存'")

        # characterData updates are observed after a React-like text replacement.
        page.evaluate("document.querySelector('#static-text').firstChild.nodeValue = 'Hello'")
        self._wait(page, "document.querySelector('#static-text').textContent === '你好'")

        page.evaluate("""
            const button = document.querySelector('#boundary-button');
            button.classList.remove('terminal');
            button.firstChild.nodeValue = 'Save';
            button.setAttribute('title', 'Save');
        """)
        self._wait(page, "document.querySelector('#boundary-button').textContent === '保存'")
        self._wait(page, "document.querySelector('#boundary-button').title === '保存'")

    def test_new_shadow_root_and_reload_can_be_reinjected(self):
        page = self.page
        page.evaluate(self.engine)
        page.evaluate("""
            const host = document.createElement('div');
            host.id = 'late-shadow-host';
            document.querySelector('#fixture-root').append(host);
            host.attachShadow({mode: 'open'}).innerHTML = '<span>Shadow Label</span>';
        """)
        self._wait(page, "document.querySelector('#late-shadow-host').shadowRoot.textContent.includes('影子标签')")

        page.reload(wait_until="domcontentloaded")
        # A real navigation destroys the old engine; production code reinjects after
        # identity validation, so this fixture performs the explicit equivalent.
        page.evaluate(self.engine)
        self._wait(page, "document.querySelector('#static-text').textContent === '你好'")
        self._wait(page, "document.querySelector('#shadow-host').shadowRoot.textContent.includes('影子标签')")

    def test_2321_dynamic_controls_and_user_content_boundaries(self):
        page = self.page
        cases = [
            ("Resets in 1d 3h", "将在 1 天 3 小时 后重置"),
            ("Downloads, 42% complete", "下载，已完成 42%"),
            ("Resume 2 comment drafts", "继续编辑 2 条评论草稿"),
            ("3 MCP servers failed to start", "3 个 MCP 服务器启动失败"),
            ("2 Help · 1 Side Question", "2 个帮助项 · 1 个侧边问题"),
            ("View 2 help items and 1 side question", "查看 2 个帮助项和 1 个侧边问题"),
            ("12 / 128 rows", "12 / 128 行"),
        ]
        markers = ["comment-card-text", "automation-name", "automation-instructions-text",
                   "coordinator-worker-card", "coordinator-welcome", "transcript-line-text",
                   "side-question-answer", "file-title-name", "webview-download-open",
                   "webview-prompt-message", "ssh-connect-host-option", "csv-scroll-viewport",
                   "pdf-text-layer", "notebook-viewer", "file-viewer-root"]
        page.evaluate("""({cases, markers}) => {
            const root = document.querySelector('#fixture-root');
            for (const [index, [raw]] of cases.entries()) {
                const button = document.createElement('button');
                button.id = 'version-control-' + index;
                button.title = raw;
                button.textContent = raw;
                root.append(button);
            }
            for (const [index, marker] of markers.entries()) {
                const parent = document.createElement('div');
                if (marker === 'file-viewer-root') parent.className = marker;
                else parent.dataset.testid = marker;
                const button = document.createElement('button');
                button.id = 'version-content-' + index;
                button.textContent = 'File';
                button.title = 'Save';
                parent.append(button);
                root.append(parent);
            }
        }""", {"cases": cases, "markers": markers})
        page.evaluate(self.engine)
        for index, (_, expected) in enumerate(cases):
            self.assertEqual(page.locator(f"#version-control-{index}").text_content(), expected)
            self.assertEqual(page.locator(f"#version-control-{index}").get_attribute("title"), expected)
        for index in range(len(markers)):
            self.assertEqual(page.locator(f"#version-content-{index}").text_content(), "File")
            self.assertEqual(page.locator(f"#version-content-{index}").get_attribute("title"), "Save")

        # React-style updates must keep working after a fresh engine replaces the old one.
        page.evaluate(self.engine)
        page.evaluate("document.querySelector('#version-control-0').firstChild.nodeValue = 'Resets in 7m'")
        self._wait(page, "document.querySelector('#version-control-0').textContent === '将在 7 分钟 后重置'")
        page.evaluate("document.querySelector('#version-content-0').firstChild.nodeValue = 'Save'")
        self.assertEqual(page.locator("#version-content-0").text_content(), "Save")


if __name__ == "__main__":
    unittest.main()
