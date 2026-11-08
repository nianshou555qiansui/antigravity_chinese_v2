"""Shared privacy policy and real injected-engine checks with a synthetic DOM."""

import json
import pathlib
import shutil
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hanhua_v2
import privacy_policy


class PrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise AssertionError("Node.js is required for the offline privacy tests")
        dictionary = {
            "File": "\u6587\u4ef6",
            "Save": "\u4fdd\u5b58",
            "Pinned": "\u5df2\u56fa\u5b9a",
            "Create a new project from one or more folders.": "\u4ece\u6587\u4ef6\u5939\u521b\u5efa\u9879\u76ee\u3002",
        }
        cls.payload = json.dumps({
            "engine": hanhua_v2.build_engine_js(dictionary),
            "dictionary": dictionary,
            "policy": privacy_policy.build_policy_js(),
            "sources": list(privacy_policy.SOURCES),
            "staticLabels": privacy_policy.STATIC_LABELS,
        }, ensure_ascii=True)

    def run_case(self, name):
        process = subprocess.run(
            [self.node, str(ROOT / "tests" / "privacy.test.js"), name],
            input=self.payload,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=20,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(process.stdout.strip(), f"ok: privacy {name}")

    def test_policy_hard_boundaries_and_control_metadata(self):
        self.run_case("boundaries")

    def test_real_engine_never_translates_hard_text_or_attributes(self):
        self.run_case("hard")

    def test_input_placeholders_do_not_allow_user_titles_or_values(self):
        self.run_case("attributes")

    def test_shadow_depth_limits_and_cycles_fail_closed(self):
        self.run_case("shadow")

    def test_boundary_changes_and_reparenting_recheck_admission(self):
        self.run_case("mutation")

    def test_collection_requires_static_marker_and_exact_value(self):
        self.run_case("collection")

    def test_misses_are_bounded_source_counts_without_context(self):
        self.run_case("misses")


if __name__ == "__main__":
    unittest.main()
