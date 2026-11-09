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


if __name__ == "__main__":
    unittest.main()
