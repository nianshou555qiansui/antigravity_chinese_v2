"""Offline dictionary and injected-engine regression checks."""

import json
import pathlib
import shutil
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hanhua_v2


class TranslationTests(unittest.TestCase):
    def test_dictionary_files(self):
        raw_keys = {}
        normalized_keys = {}
        files = sorted((ROOT / "dicts").glob("*.json"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(file=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertIsInstance(json.loads(source), dict)
                # Preserve pairs so duplicate keys cannot be silently overwritten.
                pairs = json.loads(source, object_pairs_hook=list)
                for key, value in pairs:
                    self.assertIsInstance(key, str)
                    self.assertIsInstance(value, str, f"{path.name}: {key!r}")
                    self.assertTrue(key.strip(), f"{path.name}: empty key")
                    self.assertTrue(value.strip(), f"{path.name}: {key!r} has empty value")
                    normalized = hanhua_v2.normalize_text(key)
                    self.assertTrue(normalized, f"{path.name}: empty normalized key")
                    self.assertNotIn(key, raw_keys, f"{key!r} also in {raw_keys.get(key)}")
                    self.assertNotIn(
                        normalized,
                        normalized_keys,
                        f"{key!r} collides with {normalized_keys.get(normalized)}",
                    )
                    raw_keys[key] = path.name
                    normalized_keys[normalized] = (path.name, key)
        self.assertEqual(len(normalized_keys), len(hanhua_v2.load_dictionary()))

    def test_short_key_case_consistency(self):
        folded = {}
        for key, value in hanhua_v2.load_dictionary().items():
            if len(key) < 30:
                lower = key.lower()
                if lower in folded:
                    self.assertEqual(value, folded[lower], f"Ambiguous case match: {key}")
                folded[lower] = value

    def test_windows_launcher_is_ascii(self):
        (ROOT / "qidong.bat").read_bytes().decode("ascii")

    def test_entire_engine_in_node(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the offline engine test")
        dictionary = hanhua_v2.load_dictionary()
        self.assertTrue(dictionary)
        norm_samples = []
        raw_keys = [
            key
            for path in sorted((ROOT / "dicts").glob("*.json"))
            for key in json.loads(path.read_text(encoding="utf-8"))
        ]
        for key in raw_keys:
            normalized = hanhua_v2.normalize_text(key)
            value = dictionary[normalized]
            variant = key.replace("'", "\u2019").replace('"', "\u201c")
            norm_samples.append([f" \t{variant}\r\n", value, normalized])
        for raw in (
            "\u0085File\u0085",
            "\u001cFile\u001d",
            "\u001eNew\u0085Conversation\u001f",
            "\u00a0File\u2028",
            "\ufeffFile\ufeff",
            "\u200bFile\u200b",
            "\u201cFile\u201d",
        ):
            normalized = hanhua_v2.normalize_text(raw)
            norm_samples.append([raw, dictionary.get(normalized, raw), normalized])
        payload = {
            "engine": hanhua_v2.build_engine_js(dictionary),
            "dictionary": dictionary,
            "normSamples": norm_samples,
        }
        process = subprocess.run(
            [node, str(ROOT / "tests" / "engine.test.js")],
            input=json.dumps(payload, ensure_ascii=True),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=90,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)


if __name__ == "__main__":
    unittest.main()
