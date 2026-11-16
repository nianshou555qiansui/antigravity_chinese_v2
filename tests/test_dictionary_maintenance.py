"""Fixed UI expectations and reviewed alias coverage, independent of the current dictionary."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hanhua_v2


CRITICAL_UI_CASES = [
    ("General", "常规"),
    ("Knowledge Work", "知识工作"),
    ("Simplified interface for research, writing, and everyday tasks.", "适用于研究、写作和日常任务的简洁界面。"),
    ("Software Engineering", "软件工程"),
    ("Full interface for coding, developer tools, and managing agents.", "包含编程、开发工具和智能体管理功能的完整界面。"),
    ("Permissions", "权限"),
    ("Permission Preset", "权限预设"),
    ("Default", "默认"),
    ("Auto", "自动"),
    ("Default and Auto need the terminal sandbox, which isn't available on this host.",
     "“默认”和“自动”模式需要终端沙箱，但当前主机无法使用该功能。"),
    ("Execution", "执行"),
    ("Keyboard shortcuts", "键盘快捷键"),
    ("Voice Mode", "语音模式"),
    ("Voice", "语音"),
    ("Change", "更换"),
    ("Conversation History", "对话历史"),
    ("Scheduled Tasks", "定时任务"),
    ("Open File", "打开文件"),
    ("Show in Finder", "在访达中显示"),
    ("Select Line", "选择行"),
    ("Clear Terminal", "清空终端"),
]


class DictionaryMaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dictionary = hanhua_v2.load_dictionary()
        cls.manifest = json.loads((ROOT / "docs" / "translation_sources.json").read_text(encoding="utf-8"))
        cls.reviews = cls.manifest["reviews"]
        cls.engine = hanhua_v2.build_engine_js(cls.dictionary)

    def run_node(self, cases):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required")
        source = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {makeHarness} = require('./tests/engine.test.js');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const h = makeHarness();
const fixtures = payload.cases.map(([raw, expected]) => {
    const button = h.document.body.appendChild(new h.ElementStub('button', {title: raw, 'aria-label': raw}));
    const text = button.appendChild(new h.TextStub(raw));
    return {button, text, raw, expected};
});
const protectedRoot = h.document.body.appendChild(new h.ElementStub('div', {class: 'terminal'}));
const privateTexts = payload.cases.map(([raw]) => {
    const text = protectedRoot.appendChild(new h.TextStub(raw));
    Object.defineProperty(text, 'nodeValue', {
        get() { throw Error('Protected text must not be read'); },
        set() { throw Error('Protected text must not be written'); },
    });
    return {text, raw};
});
for (let pass = 0; pass < 2; pass++) {
    h.inject(payload.engine);
    h.tick(5000);
    for (const {button, text, expected, raw} of fixtures) {
        assert.equal(text.value, expected, 'fixed text expectation: ' + raw);
        assert.equal(button.getAttribute('title'), expected, 'fixed title expectation: ' + raw);
        assert.equal(button.getAttribute('aria-label'), expected, 'fixed aria expectation: ' + raw);
    }
    for (const {text, raw} of privateTexts) assert.equal(text.value, raw, 'protected content remains unchanged');
}
for (const {text, raw, expected} of fixtures) {
    text.nodeValue = raw;
    assert.equal(text.value, expected, 'characterData update keeps the reviewed alias covered');
}
assert.equal(h.intervals.size, 1);
"""
        process = subprocess.run([node, "-e", source], input=json.dumps({"engine": self.engine, "cases": cases}),
                                 text=True, encoding="utf-8", capture_output=True, timeout=30, cwd=ROOT, check=False)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    def test_reviewed_removals_have_present_equal_translation_replacements(self):
        self.assertEqual(len(self.reviews), 42)
        files = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in (ROOT / "dicts").glob("*.json")}
        for review in self.reviews:
            with self.subTest(key=review["key"]):
                self.assertEqual(review["action"], "remove-equivalent")
                self.assertNotIn(review["key"], files[review["file"]])
                replacement = review["replacement"]
                self.assertEqual(files[replacement["file"]][replacement["key"]], review["translation"])
                old = hanhua_v2.normalize_text(review["key"])
                kept = hanhua_v2.normalize_text(replacement["key"])
                self.assertTrue(old.isascii() and kept.isascii())
                self.assertLess(len(old), 30)
                self.assertLess(len(kept), 30)
                self.assertEqual(old.lower(), kept.lower())
                self.assertTrue(review["reason"])
                self.assertEqual(review["verification"], "offline-engine")

    def test_all_removed_aliases_remain_translated_for_text_attributes_and_reinjection(self):
        cases = []
        for review in self.reviews:
            for variant in (review["key"], review["key"].lower(), review["key"].upper(),
                            "  " + review["key"] + "  "):
                cases.append((variant, review["translation"]))
        self.run_node(cases)

    def test_fixed_critical_ui_expectations_survive_dictionary_cleanup(self):
        self.run_node(CRITICAL_UI_CASES)

    def test_configuration_layout_labels_remain_in_the_dictionary(self):
        layouts = self.manifest["sources"][0]
        self.assertEqual(layouts["id"], "project-4k-mode-copy")
        expected = dict(CRITICAL_UI_CASES)
        self.assertEqual(len(layouts["keys"]), 4)
        for key in layouts["keys"]:
            self.assertEqual(self.dictionary[key], expected[key])

    def test_common_dictionary_ownership_and_legacy_menu_coverage_are_retained(self):
        common = json.loads((ROOT / "dicts" / "common.json").read_text(encoding="utf-8"))
        self.assertEqual(common["Scheduled Tasks"], "定时任务")
        self.assertEqual(common["Conversation History"], "对话历史")
        for key in ("Show in Finder", "Select Line", "Clear Terminal"):
            self.assertIn(key, self.dictionary)


if __name__ == "__main__":
    unittest.main()
