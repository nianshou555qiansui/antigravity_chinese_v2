"""Effective dictionary identity and runtime reinjection, without live application access."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import diagnostics
import hanhua_v2


class DictionaryFingerprintTests(unittest.TestCase):
    def test_reordering_does_not_change_identity(self):
        first = {"Save": "保存", "File": "文件"}
        second = {"File": "文件", "Save": "保存"}
        self.assertEqual(hanhua_v2.dictionary_fingerprint(first), hanhua_v2.dictionary_fingerprint(second))

    def test_equal_entry_counts_with_different_keys_or_translations_have_different_identities(self):
        first = hanhua_v2.dictionary_fingerprint({"File": "文件"})
        self.assertNotEqual(first, hanhua_v2.dictionary_fingerprint({"File": "文件更新"}))
        self.assertNotEqual(first, hanhua_v2.dictionary_fingerprint({"Save": "文件"}))
        self.assertNotEqual(first, hanhua_v2.dictionary_fingerprint({"File": " 文件 "}))

    def test_diagnosis_matches_runtime_loading_after_key_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dictionaries = root / "dicts"
            dictionaries.mkdir()
            (dictionaries / "ui.json").write_text(
                json.dumps({" Don’t   close ": "保持  空白", " File ": "文件"}, ensure_ascii=False), encoding="utf-8")
            report = diagnostics.inspect_dictionaries(dictionaries)
            with patch.object(hanhua_v2, "__file__", str(root / "hanhua_v2.py")):
                loaded = hanhua_v2.load_dictionary()
            self.assertTrue(report.ok)
            self.assertEqual(loaded, {"Don't close": "保持  空白", "File": "文件"})
            self.assertEqual(report.fingerprint, hanhua_v2.dictionary_fingerprint(loaded))

    def test_template_placeholder_in_dictionary_content_is_preserved(self):
        dictionary = {"FINGERPRINT_PLACEHOLDER": "保持原文"}
        engine = hanhua_v2.build_engine_js(dictionary)
        self.assertIn('"FINGERPRINT_PLACEHOLDER":"保持原文"', engine)

    def test_runtime_reinjection_replaces_the_fingerprint_even_with_equal_counts(self):
        dictionaries = [{"File": "文件"}, {"File": "文件更新"}, {"Save": "保存"}]
        cases = [{"engine": hanhua_v2.build_engine_js(dictionary),
                  "fingerprint": hanhua_v2.dictionary_fingerprint(dictionary)} for dictionary in dictionaries]
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required")
        source = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {makeHarness} = require('./tests/engine.test.js');
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
const h = makeHarness();
let previous = null;
for (const fixture of cases) {
    h.inject(fixture.engine);
    const engine = h.window.__ag_hanhua_engine__;
    assert.equal(engine.state.entries, 1);
    assert.equal(engine.state.dictionaryFingerprint, fixture.fingerprint);
    assert.equal(engine.state.started, true);
    if (previous) {
        assert.equal(previous.state.stopped, true);
        assert.notEqual(engine.state.dictionaryFingerprint, previous.state.dictionaryFingerprint);
    }
    previous = engine;
}
assert.equal(h.intervals.size, 1, 'reinjection retains one live interval');
"""
        process = subprocess.run([node, "-e", source], input=json.dumps(cases), text=True,
                                 encoding="utf-8", capture_output=True, timeout=30, cwd=ROOT, check=False)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)


if __name__ == "__main__":
    unittest.main()
