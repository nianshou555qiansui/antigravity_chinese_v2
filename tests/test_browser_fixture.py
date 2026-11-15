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

    def test_audited_settings_tooltips_and_reused_portal_privacy(self):
        page = self.page
        page.evaluate("""() => {
            const root = document.querySelector('#fixture-root');
            root.insertAdjacentHTML('beforeend', `
                <div class="agy-card"><div class="w-full"><div class="px-3 pb-2 -mt-1 w-full">
                    <span class="flex items-center gap-1 text-sm text-muted-foreground">
                        <span>Keyboard shortcuts</span>
                        <span id="keyboard-anchor" data-tooltip-id=":r2t7:-2"
                            class="cursor-pointer inline-flex items-center hover:text-foreground"></span>
                    </span>
                </div></div></div>
                <div data-testid="permissions-menu" role="menu">
                    <div data-testid="permissions-preset-option-turbo" role="menuitem">
                        <span data-testid="permissions-default-tag">
                            <div id="default-anchor" data-tooltip-id="fixture-default" aria-describedby="default-tip"
                                class="bg-secondary opacity-70 flex items-center gap-1 rounded-full px-1.5 text-sm"></div>
                        </span>
                    </div>
                </div>
                <div id="keyboard-tip" role="tooltip" class="compact-tooltip">
                    <span id="shortcut-one">Enter Queues after the turn</span>
                    <span id="shortcut-two">Alt+Enter Sends immediately</span>
                    <span id="shortcut-three">Alt+Enter On empty prompt, sends next in queue</span>
                </div>
                <div id="default-tip" role="tooltip" class="compact-tooltip">Your This Computer setting</div>
                <button id="file-anchor" data-tooltip-id="fixture-file" aria-describedby="file-tip"></button>
                <div id="file-tip" role="tooltip" class="compact-tooltip">File</div>
            `);
            window.fixturePrivateTipReads = 0;
            const text = document.querySelector('#file-tip').firstChild;
            const descriptor = Object.getOwnPropertyDescriptor(Node.prototype, 'nodeValue');
            Object.defineProperty(text, 'nodeValue', {
                get() { window.fixturePrivateTipReads++; return descriptor.get.call(this); },
                set(value) { descriptor.set.call(this, value); },
            });
        }""")
        page.evaluate(hanhua_v2.build_engine_js(hanhua_v2.load_dictionary()))
        self._wait(page, "document.querySelector('#default-tip').textContent === '你的此计算机设置'")
        self.assertEqual(page.locator('#shortcut-one').text_content(), 'Enter Queues after the turn')
        page.evaluate("document.querySelector('#keyboard-anchor').setAttribute('aria-describedby', 'keyboard-tip')")
        self._wait(page, "document.querySelector('#shortcut-one').textContent === 'Enter 排队等待当前轮次结束后发送'")
        self.assertEqual(page.locator('#shortcut-two').text_content(), 'Alt+Enter 立即发送')
        self.assertEqual(page.locator('#shortcut-three').text_content(), 'Alt+Enter 输入为空时，发送队列中的下一条')

        page.evaluate("document.querySelector('#default-tip').firstChild.nodeValue = 'Your Remote setting'")
        self._wait(page, "document.querySelector('#default-tip').textContent === '你的远程设置'")
        page.evaluate("document.querySelector('#default-tip').firstChild.nodeValue = 'Create a new project from one or more folders. setting'")
        self.assertEqual(page.locator('#default-tip').text_content(), 'Create a new project from one or more folders. setting')
        self.assertEqual(page.evaluate('window.fixturePrivateTipReads'), 0)
        self.assertEqual(page.locator('#file-tip').text_content(), 'File')

        page.evaluate("""() => {
            document.querySelector('#keyboard-anchor').removeAttribute('aria-describedby');
            document.querySelector('#file-anchor').setAttribute('aria-describedby', 'keyboard-tip');
            document.querySelector('#shortcut-one').firstChild.nodeValue = 'File';
        }""")
        self.assertEqual(page.locator('#shortcut-one').text_content(), 'File')
        page.evaluate(hanhua_v2.build_engine_js(hanhua_v2.load_dictionary()))
        self.assertEqual(page.locator('#shortcut-one').text_content(), 'File')
        self.assertEqual(page.evaluate('window.fixturePrivateTipReads'), 0)

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
