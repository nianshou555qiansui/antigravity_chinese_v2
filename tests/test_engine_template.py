"""Resource lookup tests for the standalone JavaScript engine template."""

import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hanhua_v2


class EngineTemplateTests(unittest.TestCase):
    def test_source_tree_template_is_loaded_and_substituted(self):
        generated = hanhua_v2.build_engine_js({"Synthetic label": "合成标签"})
        self.assertIn("Synthetic label", generated)
        self.assertIn("合成标签", generated)
        self.assertIn("const privacy =", generated)
        self.assertNotIn("DICT_PLACEHOLDER", generated)
        self.assertNotIn("PRIVACY_PLACEHOLDER", generated)

    def test_packaged_resource_fallback_is_used_when_source_resource_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            packaged = Path(directory) / "engine_template.js"
            packaged.write_text("DICT_PLACEHOLDER|PRIVACY_PLACEHOLDER", encoding="utf-8")
            with patch.object(hanhua_v2, "__file__", str(Path(directory) / "_MEIPASS" / "hanhua_v2.py")):
                with patch.object(hanhua_v2.sys, "_MEIPASS", directory, create=True):
                    self.assertEqual(hanhua_v2._load_engine_template(), "DICT_PLACEHOLDER|PRIVACY_PLACEHOLDER")


if __name__ == "__main__":
    unittest.main()
