"""Read-only diagnosis and privacy regressions with no live application access."""

import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cdp_discovery as discovery
import diagnostics
import hanhua_v2 as hh


SECRET = "SECRET_ACCOUNT_PRIVATE_URL_CSRF"


def reply(state="running", entries=1, **extra):
    value = {"status": "ready", "confirmed": True, "engine": state, "entries": entries}
    value.update(extra)
    return {"result": {"result": {"value": value}}}


def instance(pages=1, endpoint=True, pids=True, version="2.29.0"):
    targets = [{
        "id": f"{SECRET}-{number}", "title": SECRET,
        "url": f"https://127.0.0.1:7555/?private={SECRET}",
        "webSocketDebuggerUrl": f"ws://127.0.0.1:7542/devtools/page/{SECRET}-{number}",
    } for number in range(pages)]
    return SimpleNamespace(
        pids={23} if pids else set(),
        endpoint=discovery.Endpoint("127.0.0.1", 7542) if endpoint else None,
        pages=targets, version=version,
    )


class SafeError(Exception):
    def __init__(self, reason):
        super().__init__(SECRET)
        self.reason = reason


class FakePool:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.reconciled = []
        self.evaluations = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def reconcile(self, snapshot):
        self.reconciled.append(snapshot)

    async def evaluate(self, page, expression):
        if not self.reconciled:
            raise AssertionError("Probe before verified reconciliation")
        self.evaluations.append((page, expression))
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        return result


class MemoryDictionary:
    def __init__(self, case):
        self.directory = ROOT / "test-dictionary-fixture"
        self.files = {}
        case.enterContext(patch.object(Path, "glob", self.glob))
        files = self.files

        def read(path, *args, **kwargs):
            value = files[path.name]
            if isinstance(value, Exception):
                raise value
            return value

        case.enterContext(patch.object(Path, "read_text", read))

    def glob(self, pattern):
        return [self.directory / name for name in sorted(self.files)]

class DictionaryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryDictionary(self)
        self.directory = self.memory.directory

    def write(self, filename, source):
        self.memory.files[filename] = source

    def test_valid_files_are_counted_without_normalizing_values(self):
        self.write("b.json", '{"Save": "save-value"}')
        self.write("a.json", '{"  File  ": "file-value"}')
        report = diagnostics.inspect_dictionaries(self.directory)
        self.assertTrue(report.ok)
        self.assertEqual(report.entries, 2)
        self.assertEqual([item.name for item in report.files], ["a.json", "b.json"])

    def test_raw_and_normalized_duplicates_inside_and_across_files_fail(self):
        self.write("a.json", '{"File":"a","File":"b"," Save ":"c","Save":"d"}')
        self.write("b.json", '{"File":"e","Save\\t":"f"}')
        report = diagnostics.inspect_dictionaries(self.directory)
        self.assertFalse(report.ok)
        self.assertEqual(report.entries, 2)
        for item in report.files:
            self.assertIn("duplicate-raw-key", item.errors)
            self.assertIn("duplicate-normalized-key", item.errors)

    def test_curly_quote_normalization_detects_collisions(self):
        self.write("a.json", json.dumps({"Don\u2019t": "one", "Don't": "two"}))
        report = diagnostics.inspect_dictionaries(self.directory)
        self.assertEqual(report.files[0].errors, ["duplicate-normalized-key"])
        self.assertFalse(report.ok)

    def test_non_objects_empty_keys_values_and_wrong_value_types_fail(self):
        cases = [
            ("[]", "dictionary-not-object"),
            ('"text"', "dictionary-not-object"),
            ('{"  ":"x"}', "empty-key"),
            ('{"File":"  "}', "empty-value"),
            ('{"File":false}', "invalid-value-type"),
            ('{"File":{}}', "invalid-value-type"),
            ('{"File":[]}', "invalid-value-type"),
            ('{"File":NaN}', "invalid-value-type"),
        ]
        for source, error in cases:
            with self.subTest(source=source):
                self.write("a.json", source)
                report = diagnostics.inspect_dictionaries(self.directory)
                self.assertFalse(report.ok)
                self.assertIn(error, report.files[0].errors)
                self.assertEqual(report.entries, 0)

    def test_invalid_json_encoding_and_read_errors_expose_only_codes(self):
        self.write("a.json", '{"' + SECRET)
        report = diagnostics.inspect_dictionaries(self.directory)
        self.assertEqual(report.files[0].errors, ["invalid-json"])
        self.memory.files["a.json"] = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")
        report = diagnostics.inspect_dictionaries(self.directory)
        self.assertEqual(report.files[0].errors, ["dictionary-read-error"])
        with patch.object(Path, "read_text", side_effect=PermissionError(SECRET)):
            report = diagnostics.inspect_dictionaries(self.directory)
        self.assertEqual(report.files[0].errors, ["dictionary-read-error"])

    def test_empty_missing_directory_and_empty_dictionary_fail(self):
        report = diagnostics.inspect_dictionaries(self.directory)
        self.assertEqual(report.errors, ["dictionary-files-missing"])
        report = diagnostics.inspect_dictionaries(self.directory / "missing")
        self.assertFalse(report.ok)
        self.write("a.json", "{}")
        report = diagnostics.inspect_dictionaries(self.directory)
        self.assertFalse(report.ok)
        self.assertIn("dictionary-empty", report.errors)


class DiagnosisRunTests(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryDictionary(self)
        self.directory = self.memory.directory
        self.memory.files["ui.json"] = '{"File":"file-value"}'
        self.enterContext(patch.object(diagnostics, "DICT_DIR", self.directory))
        self.find = self.enterContext(patch.object(discovery, "find_running_instance", return_value=instance()))
        self.pool = FakePool([reply()])
        self.pool_factory = Mock(side_effect=lambda: self.pool)
        self.enterContext(patch.dict(sys.modules, {"cdp_connection": SimpleNamespace(CdpPool=self.pool_factory)}))
        for owner, name in (
            (hh, "build_engine_js"), (hh, "load_dictionary"), (hh, "prepare_instance"),
            (hh, "launch_antigravity"), (hh.cdp_targets, "inject_windows"),
            (discovery, "stop_confirmed_instance"), (discovery, "require_free_port"),
        ):
            self.enterContext(patch.object(owner, name, side_effect=AssertionError("Mutating path used")))

    def run_diagnosis(self, **kwargs):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = diagnostics.run(**kwargs)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(SECRET, output)
        self.assertNotIn("https://", output)
        self.assertNotIn("ws://", output)
        self.assertNotIn(str(self.directory), output)
        self.assertNotIn("file-value", output)
        return code, output

    def test_healthy_status_and_doctor_are_read_only_and_use_numbered_targets(self):
        for doctor in (False, True):
            with self.subTest(doctor=doctor):
                self.pool = FakePool([reply()])
                code, output = self.run_diagnosis(doctor=doctor, port=7542)
                self.assertEqual(code, 0)
                self.assertIn("#1", output)
                self.assertIn("2.29.0", output)
                self.assertIn("7542", output)
                self.find.assert_called_with(7542)
                self.assertTrue(self.pool.closed)
                self.assertEqual(self.pool.reconciled, [self.find.return_value])
                self.assertEqual(len(self.pool.evaluations), 1)
                expression = self.pool.evaluations[0][1]
                self.assertNotIn(SECRET, expression)
                self.assertNotIn(".active(", expression)
                self.assertNotIn(".disconnect(", expression)
                self.assertNotIn(".observe(", expression)
                self.assertEqual(self.memory.files["ui.json"], '{"File":"file-value"}')

    def test_no_process_no_port_or_no_target_is_not_ready_without_probe(self):
        for snapshot in (
            instance(pages=0, endpoint=False, pids=False),
            instance(pages=0, endpoint=False),
            instance(pages=0),
        ):
            with self.subTest(snapshot=snapshot):
                self.find.return_value = snapshot
                code, _ = self.run_diagnosis(doctor=True)
                self.assertEqual(code, 1)
        self.pool_factory.assert_not_called()

    def test_missing_legacy_stopped_or_different_dictionary_count_need_attention(self):
        for state, entries in (("missing", 1), ("unknown", 1), ("stopped", 1), ("not-started", 1), ("running", 2)):
            with self.subTest(state=state, entries=entries):
                self.pool = FakePool([reply(state, entries)])
                code, _ = self.run_diagnosis(doctor=True)
                self.assertEqual(code, 1)

    def test_other_pages_can_be_skipped_but_errors_or_pending_prevent_success(self):
        self.find.return_value = instance(pages=2)
        for status, expected in (("skipped", 0), ("pending", 1), ("error", 1)):
            with self.subTest(status=status):
                reason = "not-hub-preload" if status == "skipped" else "hub-mount-loading"
                other = {"result": {"result": {"value": {"status": status, "reason": reason}}}}
                self.pool = FakePool([reply(), other])
                code, output = self.run_diagnosis()
                self.assertEqual(code, expected)
                self.assertIn("#2", output)
                self.assertEqual(len(self.pool.evaluations), 2)
        self.find.return_value = instance()
        self.pool = FakePool([{ "result": {"result": {"value": {"status": "skipped", "reason": "not-hub-preload"}}}}])
        self.assertEqual(self.run_diagnosis(doctor=True)[0], 1)

    def test_unknown_identity_is_not_silently_skipped_beside_healthy_window(self):
        self.find.return_value = instance(pages=2)
        for reason in ("identity-unavailable", SECRET):
            with self.subTest(reason=reason):
                other = {"result": {"result": {"value": {"status": "skipped", "reason": reason}}}}
                self.pool = FakePool([reply(), other])
                self.assertEqual(self.run_diagnosis()[0], 1)

    def test_target_timeout_does_not_suppress_remaining_windows(self):
        self.find.return_value = instance(pages=2)
        self.pool = FakePool([SafeError("target-timeout"), reply()])
        code, output = self.run_diagnosis(doctor=True)
        self.assertEqual(code, 1)
        self.assertIn("target-timeout", output)
        self.assertIn("#2", output)
        self.assertTrue(self.pool.closed)

    def test_unknown_discovery_connection_and_version_strings_are_redacted(self):
        for reason in ("ambiguous-ports", "target-list-unavailable", SECRET):
            with self.subTest(reason=reason):
                self.find.side_effect = SafeError(reason)
                self.assertEqual(self.run_diagnosis(doctor=True)[0], 1)
        self.find.side_effect = None
        self.find.return_value = instance(version=SECRET)
        self.pool = FakePool([SafeError(SECRET)])
        self.assertEqual(self.run_diagnosis(doctor=True)[0], 1)
        self.pool = FakePool([reply()])
        code, output = self.run_diagnosis()
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, output)

    def test_bad_dictionary_reports_filename_and_codes_without_source(self):
        self.memory.files["ui.json"] = '{"' + SECRET
        code, output = self.run_diagnosis(doctor=True)
        self.assertEqual(code, 1)
        self.assertIn("ui.json", output)
        self.assertIn("invalid-json", output)

    def test_malformed_unconfirmed_or_javascript_error_replies_fail_closed(self):
        cases = [
            None, {}, {"error": {"message": SECRET}},
            {"result": {"exceptionDetails": {"text": SECRET}}},
            {"result": {"result": {"value": SECRET}}},
            reply(confirmed=False), reply(confirmed=1), reply(entries=True), reply(entries=-1),
            reply(entries=9007199254740992), reply(state=SECRET),
        ]
        for response in cases:
            with self.subTest(response=response):
                self.pool = FakePool([response])
                self.assertEqual(self.run_diagnosis()[0], 1)

    def test_pool_reconcile_failure_is_redacted_and_closes_without_evaluating(self):
        async def rejected(snapshot):
            raise SafeError("revalidation-required")

        self.pool.reconcile = rejected
        code, output = self.run_diagnosis(doctor=True)
        self.assertEqual(code, 1)
        self.assertIn("revalidation-required", output)
        self.assertEqual(self.pool.evaluations, [])
        self.assertTrue(self.pool.closed)

    def test_many_candidate_probes_have_bounded_concurrency_and_stable_numbers(self):
        self.find.return_value = instance(pages=9)
        active = 0
        peak = 0
        pool = self.pool

        async def evaluate(page, expression):
            import asyncio
            nonlocal active, peak
            self.assertTrue(pool.reconciled)
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return reply()

        self.pool.evaluate = evaluate
        code, output = self.run_diagnosis()
        self.assertEqual(code, 0)
        self.assertEqual(peak, 4)
        self.assertEqual(active, 0)
        for number in range(1, 10):
            self.assertIn(f"#{number}", output)

    def test_doctor_is_more_detailed_without_changing_health_result(self):
        self.pool = FakePool([reply("missing")])
        status_code, status = self.run_diagnosis()
        self.pool = FakePool([reply("missing")])
        doctor_code, doctor = self.run_diagnosis(doctor=True)
        self.assertEqual((status_code, doctor_code), (1, 1))
        self.assertGreater(len(doctor), len(status))


class DiagnosticExpressionTests(unittest.TestCase):
    def test_probe_runs_in_node_without_calls_or_private_data(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for diagnostic expression tests")
        source = diagnostics.diagnostic_expression(instance().pages[0])
        completed = subprocess.run(
            [node, str(ROOT / "tests" / "diagnostics.test.js")],
            input=json.dumps({"expression": source, "scope": hh.cdp_targets.ENGINE_SCOPE}),
            text=True, encoding="utf-8", capture_output=True, timeout=20, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
