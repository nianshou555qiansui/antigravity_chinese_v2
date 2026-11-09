"""Stable, synthetic engine-scan performance baseline.

This never connects to Antigravity or a live page. It measures only initial
translation of generated DOM trees and uses a broad timeout so CI timing noise
does not turn into a flaky regression gate.
"""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hanhua_v2


class PerformanceBaselineTests(unittest.TestCase):
    def test_synthetic_scan_baseline(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the synthetic performance baseline")
        cases = []
        for name, labels, dictionary_entries in (
            ("small", 128, 16),
            ("medium", 1024, 64),
            ("large", 4096, 256),
        ):
            dictionary = {
                f"Label {index}": f"标签 {index}" for index in range(dictionary_entries)
            }
            cases.append({
                "name": name,
                "nodes": labels,
                "dictionary_entries": dictionary_entries,
                "engine": hanhua_v2.build_engine_js(dictionary),
            })
        process = subprocess.run(
            [node, str(ROOT / "tests" / "performance_baseline.js")],
            input=json.dumps({"cases": cases}, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report.get("schema"), 1)
        self.assertEqual([case["name"] for case in report["cases"]], ["small", "medium", "large"])
        for case in report["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(case["translated"], case["labels"])
                self.assertEqual(case["elements"], 1 + case["labels"] * 2)
                self.assertEqual(case["changed_nodes"], max(1, case["labels"] // 10))
                for metric in ("initial_ms", "mutation_batch_ms", "attribute_batch_ms",
                               "idle_attribute_fallback_ms"):
                    self.assertGreaterEqual(case[metric], 0)
                    self.assertLess(case[metric], 15000)
                self.assertEqual(case["idle_text_reads"], 0,
                                 "idle attribute fallback must not rescan text nodes")
        # Keep the report visible in verbose CI logs without writing a file.
        print("performance baseline:", json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
