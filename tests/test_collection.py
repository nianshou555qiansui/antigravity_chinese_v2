"""Offline collector, output-boundary, and read-only JavaScript tests."""

import asyncio
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

import caiji
import cdp_discovery
from cdp_connection import CdpError
import cdp_targets
import hanhua_v2 as hh


SECRET = "SECRET_ACCOUNT_PRIVATE_URL_CSRF"


def instance(count=1, *, endpoint=True, pids=True):
    return SimpleNamespace(
        pids={42} if pids else set(),
        endpoint=cdp_discovery.Endpoint("127.0.0.1", 7542) if endpoint else None,
        pages=[{
            "id": f"{SECRET}-{number}", "title": SECRET,
            "url": f"https://127.0.0.1:7555/?private={SECRET}",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:7542/devtools/page/{SECRET}-{number}",
        } for number in range(count)],
        version="2.29.0",
    )


def response(value):
    return {"result": {"result": {"value": value}}}


def ready(*, include=False, misses=False, sources=None, candidates=None, **extra):
    value = caiji._new_report(include, misses)
    value.pop("windows")
    value.update(status="ready", confirmed=True)
    value["sources"].update(sources or {})
    value["candidates"] = candidates or []
    value.update(extra)
    return response(value)


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
            raise AssertionError("Evaluation before verified admission")
        self.evaluations.append((page, expression))
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        return result


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = instance()
        self.find = self.enterContext(patch.object(cdp_discovery, "find_running_instance", return_value=self.snapshot))
        self.pool = FakePool([ready()])
        self.factory = self.enterContext(patch.object(caiji, "CdpPool", side_effect=lambda: self.pool))
        for owner, name in (
            (hh, "build_engine_js"), (hh, "load_dictionary"), (hh, "prepare_instance"),
            (hh, "launch_antigravity"), (cdp_targets, "inject_windows"),
            (cdp_discovery, "stop_confirmed_instance"),
        ):
            self.enterContext(patch.object(owner, name, side_effect=AssertionError("Unexpected mutation")))

    def test_auto_discovery_reconciles_before_read_and_closes_without_mutation(self):
        result = caiji.collect()
        self.assertTrue(result.ok)
        self.find.assert_called_once_with(None)
        self.assertEqual(self.pool.reconciled, [self.snapshot])
        self.assertTrue(self.pool.closed)
        self.assertEqual(len(self.pool.evaluations), 1)
        expression = self.pool.evaluations[0][1]
        self.assertIn(cdp_targets.MAIN_PAGE_STATE, expression)
        self.assertNotIn(SECRET, expression)
        self.assertNotIn(".active(", expression)
        self.assertEqual(result.report["windows"], {"checked": 1, "skipped": 0, "failed": 0})
        self.assertEqual(result.report["candidates"], [])

    def test_explicit_port_is_forwarded_without_fallback(self):
        self.find.side_effect = cdp_discovery.DiscoveryError(SECRET)
        result = caiji.collect(9444)
        self.find.assert_called_once_with(9444)
        self.factory.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.reasons, ("discovery-failed",))
        self.assertNotIn(SECRET, repr(result))

    def test_discovery_unknown_running_no_cdp_and_empty_targets_fail_closed(self):
        for snapshot, reason in (
            (instance(endpoint=False, pids=False), "not-running"),
            (instance(endpoint=False), "no-cdp"),
            (instance(count=0), "no-targets"),
        ):
            with self.subTest(reason=reason):
                self.find.return_value = snapshot
                result = caiji.collect()
                self.assertFalse(result.ok)
                self.assertEqual(result.reasons, (reason,))
        self.factory.assert_not_called()

    def test_multiple_windows_merge_numeric_sources_and_canonical_candidates(self):
        self.find.return_value = instance(3)
        candidate = {"source": "text", "text": "New Conversation", "count": 2, "private": SECRET}
        self.pool = FakePool([
            ready(include=True, sources={"text": 2}, candidates=[candidate], private=SECRET),
            ready(include=True, sources={"text": 3, "title": 1}, candidates=[candidate]),
            response({"status": "skipped", "reason": "standalone-or-preview", "title": SECRET}),
        ])
        result = caiji.collect(include_ui_text=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.report["windows"], {"checked": 2, "skipped": 1, "failed": 0})
        self.assertEqual(result.report["sources"]["text"], 5)
        self.assertEqual(result.report["candidates"], [{"source": "text", "text": "New Conversation", "count": 4}])
        self.assertNotIn(SECRET, caiji._serialize(result.report).decode())
        self.assertTrue(self.pool.closed)

    def test_default_and_misses_drop_unexpected_text_and_unknown_fields(self):
        for misses in (False, True):
            with self.subTest(misses=misses):
                self.pool = FakePool([ready(
                    misses=misses, candidates=[{"text": SECRET}], raw=SECRET,
                    sources={"text": 7, SECRET: SECRET},
                    withheld={"blocked": 1, "sensitive": 2, "unclassified": 3, SECRET: SECRET},
                )])
                result = caiji.collect(misses=misses)
                self.assertTrue(result.ok)
                self.assertEqual(result.report["candidates"], [])
                self.assertEqual(set(result.report["sources"]), set(caiji.SOURCES))
                self.assertNotIn(SECRET, caiji._serialize(result.report).decode())

    def test_per_window_failures_do_not_cancel_other_windows(self):
        self.find.return_value = instance(5)
        self.pool = FakePool([
            CdpError("target-timeout"),
            response({"status": "pending", "reason": "hub-mount-loading"}),
            response({"status": "skipped", "reason": "identity-unavailable"}),
            RuntimeError(SECRET),
            ready(sources={"title": 2}),
        ])
        result = caiji.collect()
        self.assertFalse(result.ok)
        self.assertEqual(len(self.pool.evaluations), 5)
        self.assertEqual(result.report["windows"], {"checked": 1, "skipped": 0, "failed": 4})
        self.assertEqual(result.report["sources"]["title"], 2)
        self.assertEqual(set(result.reasons), {
            "target-timeout", "hub-mount-loading", "identity-unavailable", "collection-failed",
        })
        self.assertNotIn(SECRET, repr(result))
        self.assertTrue(self.pool.closed)

    def test_node_budget_failure_makes_partial_multiwindow_scan_unsavable(self):
        self.find.return_value = instance(2)
        self.pool = FakePool([
            response({"status": "error", "reason": "collection-limit", "candidates": [{"text": SECRET}]}),
            ready(include=True, candidates=[{"source": "text", "text": "New Conversation", "count": 1}]),
        ])
        result = caiji.collect(include_ui_text=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.reasons, ("collection-limit",))
        self.assertEqual(result.report["windows"], {"checked": 1, "skipped": 0, "failed": 1})
        self.assertEqual(result.report["candidates"], [{"source": "text", "text": "New Conversation", "count": 1}])
        self.assertNotIn(SECRET, repr(result))

    def test_all_unrelated_targets_are_not_success(self):
        self.pool = FakePool([response({"status": "skipped", "reason": "not-hub-preload"})])
        result = caiji.collect()
        self.assertFalse(result.ok)
        self.assertEqual(result.reasons, ("no-main-window",))
        self.assertEqual(result.report["windows"]["skipped"], 1)

    def test_unknown_error_reason_is_replaced_not_echoed(self):
        self.pool = FakePool([response({"status": "error", "reason": SECRET})])
        result = caiji.collect()
        self.assertEqual(result.reasons, ("collection-failed",))
        self.assertNotIn(SECRET, repr(result))

    def test_bounded_parallel_probes_reach_every_window(self):
        self.find.return_value = instance(9)
        active = peak = 0

        async def evaluate(page, expression):
            nonlocal active, peak
            self.assertTrue(self.pool.reconciled)
            self.pool.evaluations.append((page, expression))
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return ready()

        self.pool.evaluate = evaluate
        result = caiji.collect()
        self.assertTrue(result.ok)
        self.assertEqual((peak, active), (4, 0))
        self.assertEqual(len(self.pool.evaluations), 9)

    def test_counters_saturate_at_safe_integer(self):
        self.find.return_value = instance(2)
        self.pool = FakePool([ready(sources={"text": caiji.MAX_COUNT}) for _ in range(2)])
        result = caiji.collect()
        self.assertTrue(result.ok)
        self.assertEqual(result.report["sources"]["text"], caiji.MAX_COUNT)


class ReplyBoundaryTests(unittest.TestCase):
    def test_invalid_protocol_envelopes_are_refused(self):
        for value in (
            None, [], {}, {"error": {"message": SECRET}},
            {"result": {"exceptionDetails": {"text": SECRET}}},
            {"result": {"result": {"value": [SECRET]}}},
        ):
            with self.subTest(value=value):
                parsed, reason = caiji._parse_reply(value)
                self.assertIsNone(parsed)
                self.assertIn(reason, ("evaluation-failed", "invalid-probe-result"))
                self.assertNotIn(SECRET, reason)

    def test_node_budget_error_is_fixed_and_preserves_no_partial_candidate_reply(self):
        parsed, reason = caiji._parse_reply(response({
            "status": "error", "reason": "collection-limit", "candidates": [{"text": SECRET}],
        }), include_ui_text=True)
        self.assertIsNone(parsed)
        self.assertEqual(reason, "collection-limit")
        self.assertNotIn(SECRET, reason)

    def test_wrong_schema_mode_confirmed_and_count_types_are_refused(self):
        for key, invalid in (
            ("schema", True), ("schema", 2), ("mode", "other"),
            ("confirmed", 1), ("status", "unknown"), ("text_included", 0),
            ("sources", {source: True for source in caiji.SOURCES}),
            ("sources", {source: -1 for source in caiji.SOURCES}),
            ("sources", {source: caiji.MAX_COUNT + 1 for source in caiji.SOURCES}),
            ("sources", {source: 1.0 for source in caiji.SOURCES}),
            ("sources", {}), ("withheld", {}), ("candidates", {}),
        ):
            with self.subTest(key=key, invalid=invalid):
                reply = ready()
                reply["result"]["result"]["value"][key] = invalid
                parsed, reason = caiji._parse_reply(reply)
                self.assertIsNone(parsed)
                self.assertEqual(reason, "invalid-probe-result")

    def test_opt_in_accepts_only_bounded_canonical_static_values(self):
        invalid = [
            {"source": "text", "text": SECRET, "count": 1},
            {"source": "data-title", "text": "Rename", "count": 1},
            {"source": "text", "text": " Rename ", "count": 1},
            {"source": "text", "text": "New\tConversation", "count": 1},
            {"source": "text", "text": "Rename", "count": True},
            {"source": "text", "text": "Rename", "count": 0},
            {"source": "text", "text": "Rename", "count": -1},
            {"source": "text", "text": "Rename", "count": caiji.MAX_COUNT + 1},
            {"source": "text", "text": "X" * 121, "count": 1},
            SECRET,
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                parsed, reason = caiji._parse_reply(ready(include=True, candidates=[candidate]), True)
                self.assertIsNone(parsed)
                self.assertEqual(reason, "invalid-probe-result")
        parsed, reason = caiji._parse_reply(ready(include=True, candidates=[{
            "source": "aria-label", "text": "Rename", "count": 1, "account": SECRET,
        }]), True)
        self.assertFalse(reason)
        self.assertEqual(parsed["candidates"], [{"source": "aria-label", "text": "Rename", "count": 1}])

    def test_candidate_and_serialized_output_caps_fail_closed(self):
        parsed, reason = caiji._parse_reply(ready(candidates=[SECRET] * 1001))
        self.assertIsNone(parsed)
        self.assertEqual(reason, "invalid-probe-result")
        report = caiji._new_report(True)
        report["candidates"] = [{"source": "text", "text": "Rename", "count": 1}] * 2000
        with self.assertRaises(ValueError):
            caiji._serialize(report)


class CollectionCliTests(unittest.TestCase):
    def setUp(self):
        self.report = caiji._new_report()
        self.report["windows"]["checked"] = 1
        self.result = caiji.CollectionResult(self.report)
        self.collect = self.enterContext(patch.object(caiji, "collect", side_effect=lambda *args: self.result))
        self.save = self.enterContext(patch.object(caiji, "_save_report", return_value=True))
        self.bypass = self.enterContext(patch.object(hh, "_bypass_proxy_for_local_cdp"))

    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = caiji.main(args)
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_success_outputs_only_counts_and_fixed_relative_filename(self):
        code, stdout, stderr = self.run_cli([])
        self.assertEqual(code, 0)
        self.collect.assert_called_once_with(None, False, False)
        self.save.assert_called_once_with(self.report, "pending.json")
        self.bypass.assert_called_once()
        self.assertIn("pending.json", stdout)
        self.assertIn("统计", stdout)
        self.assertNotIn(str(ROOT), stdout + stderr)
        self.assertNotIn(SECRET, stdout + stderr)
        self.assertFalse(stderr)

    def test_include_prints_warning_but_never_prints_any_candidates(self):
        self.report["text_included"] = True
        self.report["candidates"] = [{"source": "text", "text": "New Conversation", "count": 1}]
        code, stdout, stderr = self.run_cli(["--include-ui-text", "--port", "9444"])
        self.assertEqual(code, 0)
        self.collect.assert_called_once_with(9444, True, False)
        self.assertIn("人工核查", stderr)
        self.assertNotIn("New Conversation", stdout + stderr)
        self.assertNotIn(str(ROOT), stdout + stderr)

    def test_misses_saves_only_stats_to_fixed_misses_name(self):
        self.report["mode"] = "misses"
        code, stdout, stderr = self.run_cli(["--misses"])
        self.assertEqual(code, 0)
        self.collect.assert_called_once_with(None, False, True)
        self.save.assert_called_once_with(self.report, "misses.json")
        self.assertIn("misses.json", stdout)

    def test_partial_failure_preserves_previous_file_and_has_no_false_privacy_claim(self):
        self.report["windows"]["failed"] = 1
        self.result = caiji.CollectionResult(self.report, ("target-timeout",))
        code, stdout, stderr = self.run_cli([])
        self.assertEqual(code, 1)
        self.save.assert_not_called()
        self.assertIn("原有文件未覆盖", stdout)
        self.assertNotIn("不包含界面原文", stdout)

    def test_old_engine_refusal_gives_fixed_manual_reinjection_advice(self):
        self.result = caiji.CollectionResult(self.report, ("engine-upgrade-required",))
        code, stdout, stderr = self.run_cli(["--misses"])
        self.assertEqual(code, 1)
        self.assertIn("--no-launch", stdout)
        self.assertIn("原文记录不会读取", stdout)
        self.save.assert_not_called()

    def test_save_error_preserves_old_file_and_does_not_claim_success(self):
        self.save.return_value = False
        code, stdout, stderr = self.run_cli([])
        self.assertEqual(code, 1)
        self.assertIn("原有文件未覆盖", stdout)
        self.assertNotIn("[输出]", stdout)
        self.assertNotIn("不包含界面原文", stdout)

    def test_invalid_port_and_incompatible_modes_exit_before_discovery(self):
        for args in (
            ["--port", "0"], ["--port", "65536"], ["--port", "bad"],
            ["--misses", "--include-ui-text"],
        ):
            with self.subTest(args=args):
                code, stdout, stderr = self.run_cli(args)
                self.assertEqual(code, 2)
        self.collect.assert_not_called()
        self.save.assert_not_called()
        self.bypass.assert_not_called()


class AtomicOutputTests(unittest.TestCase):
    def setUp(self):
        self.report = caiji._new_report()
        self.file = Mock()
        self.file.name = str(ROOT / ".pending.json.fixture.tmp")
        self.file.fileno.return_value = 23
        self.file.__enter__ = Mock(return_value=self.file)
        self.file.__exit__ = Mock(return_value=False)
        self.temp = self.enterContext(patch.object(caiji.tempfile, "NamedTemporaryFile", return_value=self.file))
        self.fsync = self.enterContext(patch.object(caiji.os, "fsync"))
        self.replace = self.enterContext(patch.object(caiji.os, "replace"))
        self.unlink = self.enterContext(patch.object(caiji.os, "unlink"))

    def test_success_flushes_and_atomically_replaces_in_repository(self):
        self.assertTrue(caiji._save_report(self.report, "pending.json"))
        self.file.write.assert_called_once_with(caiji._serialize(self.report))
        self.file.flush.assert_called_once()
        self.fsync.assert_called_once_with(23)
        self.replace.assert_called_once_with(self.file.name, ROOT / "pending.json")
        self.unlink.assert_not_called()
        self.assertEqual(self.temp.call_args.kwargs["dir"], ROOT)
        self.assertFalse(self.temp.call_args.kwargs["delete"])

    def test_write_or_replace_failure_never_overwrites_and_cleans_temporary(self):
        for failure in ("write", "replace"):
            with self.subTest(failure=failure):
                self.file.write.side_effect = OSError(SECRET) if failure == "write" else None
                self.replace.side_effect = OSError(SECRET) if failure == "replace" else None
                self.replace.reset_mock()
                self.unlink.reset_mock()
                self.assertFalse(caiji._save_report(self.report, "pending.json"))
                self.unlink.assert_called_once_with(self.file.name)
                if failure == "write":
                    self.replace.assert_not_called()

    def test_oversized_report_does_not_create_any_file(self):
        with patch.object(caiji, "MAX_OUTPUT_BYTES", 1):
            self.assertFalse(caiji._save_report(self.report, "pending.json"))
        self.temp.assert_not_called()
        self.replace.assert_not_called()
        self.unlink.assert_not_called()


class CollectionExpressionTests(unittest.TestCase):
    def test_expressions_in_node_enforce_policy_without_mutation_or_private_reads(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for collector tests")
        page = instance().pages[0]
        with patch.object(caiji, "MAX_NODES", 5):
            limited = caiji.collection_expression(page)
        completed = subprocess.run(
            [node, str(ROOT / "tests" / "collection.test.js")],
            input=json.dumps({
                "stats": caiji.collection_expression(page),
                "include": caiji.collection_expression(page, True),
                "limited": limited,
                "misses": caiji.misses_expression(page),
                "scope": cdp_targets.ENGINE_SCOPE,
                "sources": caiji.SOURCES,
            }),
            text=True, encoding="utf-8", capture_output=True, timeout=20, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
