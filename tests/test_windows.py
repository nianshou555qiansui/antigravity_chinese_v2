"""Offline multi-window discovery, scheduling, and CDP response regressions."""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cdp_discovery as discovery
import cdp_targets as targets


def page(target_id, url="https://127.0.0.1:7555/"):
    return {
        "id": target_id,
        "type": "page",
        "title": "Antigravity",
        "url": url,
        "webSocketDebuggerUrl": f"ws://127.0.0.1:7542/devtools/page/{target_id}",
    }


def result(target_id, status, confirmed=True):
    return targets.TargetResult(target_id, status, confirmed=confirmed)


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if len(self.sleeps) >= 100:
            raise AssertionError("Window scheduling did not terminate")
        if seconds < 0:
            raise AssertionError("Window scheduling requested a negative sleep")
        self.sleeps.append(seconds)
        self.now += seconds


class WindowReportTests(unittest.TestCase):
    def test_counts_latest_confirmed_results_not_injection_history(self):
        report = targets.WindowReport(
            results=[result("a", "injected"), result("b", "alive"),
                     result("preview", "skipped", False)],
            injections={"a": 3, "closed-target": 1},
        )
        self.assertEqual(report.successful, 2)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.skipped, 1)
        self.assertTrue(report.ok)

    def test_no_target_skipped_or_unconfirmed_status_is_not_success(self):
        for entries in (
            [], [result("preview", "skipped", False)],
            [result("a", "injected", False)], [result("a", "alive", False)],
        ):
            with self.subTest(entries=entries):
                report = targets.WindowReport(results=entries)
                self.assertEqual(report.successful, 0)
                self.assertFalse(report.ok)

    def test_partial_errors_and_unready_main_windows_are_not_ok(self):
        for status in ("error", "pending"):
            with self.subTest(status=status):
                report = targets.WindowReport(results=[result("a", "alive"), result("b", status)])
                self.assertEqual(report.successful, 1)
                self.assertEqual(report.failed, 1)
                self.assertFalse(report.ok)


class WindowSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.enterContext(patch.object(targets.time, "monotonic", self.clock.monotonic))
        self.enterContext(patch.object(targets.time, "sleep", self.clock.sleep))
        self.find = self.enterContext(patch.object(discovery, "find_running_instance"))
        self.translate = self.enterContext(patch.object(targets, "translate_pages"))
        self.rounds = []

    def scenario(self, stages):
        index = 0

        def find(port):
            nonlocal index
            pages, _ = stages[min(index, len(stages) - 1)]
            return discovery.Instance({23}, discovery.Endpoint("127.0.0.1", port), pages=pages)

        def translate(pages, engine_js, force_ids=(), **kwargs):
            nonlocal index
            expected_pages, results = stages[min(index, len(stages) - 1)]
            self.assertEqual(pages, expected_pages)
            self.assertEqual(engine_js, "fixture-engine")
            self.rounds.append((tuple(p["id"] for p in pages), set(force_ids)))
            index += 1
            return results

        self.find.side_effect = find
        self.translate.side_effect = translate

    def inject(self, **kwargs):
        return targets.inject_windows(7542, "fixture-engine", **kwargs)

    def test_two_main_windows_and_skipped_page_single_round(self):
        pages = [page("a"), page("b"), page("external", "https://example.invalid/")]
        self.scenario([(pages, [result("a", "injected"), result("b", "injected"),
                                result("external", "skipped", False)])])
        report = self.inject(watch_seconds=0, ready_timeout=0)
        self.assertTrue(report.ok)
        self.assertEqual((report.successful, report.skipped), (2, 1))
        self.assertEqual(report.injections, {"a": 1, "b": 1})
        self.assertEqual(len(self.rounds), 1)
        self.find.assert_called_once_with(7542)

    def test_pending_initial_window_waits_then_injects(self):
        pages = [page("a")]
        self.scenario([
            (pages, [result("a", "pending")]),
            (pages, [result("a", "injected")]),
        ])
        report = self.inject(watch_seconds=0, ready_timeout=3)
        self.assertTrue(report.ok)
        self.assertEqual(report.injections, {"a": 1})
        self.assertEqual(self.rounds, [(("a",), {"a"}), (("a",), {"a"})])
        self.assertGreater(self.clock.now, 100)
        self.assertLessEqual(self.clock.now, 103)

    def test_watch_zero_still_waits_for_other_confirmed_initial_window(self):
        pages = [page("a"), page("b")]
        self.scenario([
            (pages, [result("a", "injected"), result("b", "pending")]),
            (pages, [result("a", "alive"), result("b", "injected")]),
        ])
        report = self.inject(watch_seconds=0, ready_timeout=3)
        self.assertTrue(report.ok)
        self.assertEqual(report.successful, 2)
        self.assertEqual(report.injections, {"a": 1, "b": 1})
        self.assertEqual(self.rounds[1][1], {"b"})

    def test_all_skipped_pages_do_not_consume_startup_timeout(self):
        self.scenario([([page("preview")], [result("preview", "skipped", False)])])
        report = self.inject(watch_seconds=0, ready_timeout=60)
        self.assertFalse(report.ok)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.injections, {})
        self.assertEqual(self.clock.now, 100)
        self.assertEqual(len(self.rounds), 1)

    def test_no_target_single_round_is_not_success(self):
        self.scenario([([], [])])
        report = self.inject(watch_seconds=0, ready_timeout=0)
        self.assertFalse(report.ok)
        self.assertEqual(report.results, [])
        self.assertEqual(report.injections, {})
        self.assertEqual(len(self.rounds), 1)

    def test_shared_watch_deadline_is_not_multiplied_by_window_count(self):
        pages = [page("a"), page("b")]
        self.scenario([
            (pages, [result("a", "injected"), result("b", "injected")]),
            (pages, [result("a", "alive"), result("b", "alive")]),
        ])
        report = self.inject(watch_seconds=3, ready_timeout=0)
        self.assertTrue(report.ok)
        self.assertEqual(self.clock.now, 103)
        self.assertEqual(report.injections, {"a": 1, "b": 1})
        self.assertEqual([forced for _, forced in self.rounds], [{"a", "b"}, set(), set()])
        self.assertEqual(self.find.call_count, len(self.rounds))
        self.assertTrue(all(call.args == (7542,) for call in self.find.call_args_list))

    def test_new_window_is_discovered_during_watch_and_forced_once(self):
        a, b = page("a"), page("b")
        self.scenario([
            ([a], [result("a", "injected")]),
            ([a, b], [result("a", "alive"), result("b", "injected")]),
            ([a, b], [result("a", "alive"), result("b", "alive")]),
        ])
        report = self.inject(watch_seconds=3, ready_timeout=0)
        self.assertTrue(report.ok)
        self.assertEqual(report.successful, 2)
        self.assertEqual(report.injections, {"a": 1, "b": 1})
        self.assertEqual([forced for _, forced in self.rounds], [{"a"}, {"b"}, set()])

    def test_closed_target_disappears_from_final_failures(self):
        a, b = page("a"), page("b")
        self.scenario([
            ([a, b], [result("a", "error"), result("b", "injected")]),
            ([b], [result("b", "alive")]),
        ])
        report = self.inject(watch_seconds=3, ready_timeout=3)
        self.assertTrue(report.ok)
        self.assertEqual([r.target_id for r in report.results], ["b"])
        self.assertEqual(report.injections, {"b": 1})

    def test_rebuilt_target_gets_its_own_initial_refresh(self):
        self.scenario([
            ([page("old")], [result("old", "injected")]),
            ([page("new")], [result("new", "injected")]),
            ([page("new")], [result("new", "alive")]),
        ])
        report = self.inject(watch_seconds=3, ready_timeout=0)
        self.assertTrue(report.ok)
        self.assertEqual([forced for _, forced in self.rounds], [{"old"}, {"new"}, set()])
        self.assertEqual(report.injections, {"old": 1, "new": 1})
        self.assertEqual(report.successful, 1)

    def test_reload_reinjects_without_repeating_force_refresh(self):
        pages = [page("a")]
        self.scenario([
            (pages, [result("a", "injected")]),
            (pages, [result("a", "injected")]),
            (pages, [result("a", "alive")]),
        ])
        report = self.inject(watch_seconds=3, ready_timeout=0)
        self.assertTrue(report.ok)
        self.assertEqual(report.injections, {"a": 2})
        self.assertEqual([forced for _, forced in self.rounds], [{"a"}, set(), set()])

    def test_transient_failure_recovers_and_keeps_refresh_until_success(self):
        pages = [page("a"), page("b")]
        self.scenario([
            (pages, [result("a", "injected"), result("b", "error", False)]),
            (pages, [result("a", "alive"), result("b", "injected")]),
            (pages, [result("a", "alive"), result("b", "alive")]),
        ])
        report = self.inject(watch_seconds=3, ready_timeout=3)
        self.assertTrue(report.ok)
        self.assertEqual(report.failed, 0)
        self.assertEqual([forced for _, forced in self.rounds], [{"a", "b"}, {"b"}, set()])

    def test_partial_failure_and_permanent_pending_finish_at_shared_deadline(self):
        for status in ("error", "pending"):
            with self.subTest(status=status):
                self.clock.now = 100
                self.clock.sleeps.clear()
                self.rounds.clear()
                pages = [page("a"), page("b")]
                self.scenario([
                    (pages, [result("a", "injected"), result("b", status)]),
                    (pages, [result("a", "alive"), result("b", status)]),
                ])
                report = self.inject(watch_seconds=0, ready_timeout=3)
                self.assertFalse(report.ok)
                self.assertEqual((report.successful, report.failed), (1, 1))
                self.assertEqual(self.clock.now, 103)
                self.assertEqual(report.injections, {"a": 1})

    def test_only_missing_never_forces_healthy_or_new_targets(self):
        a, b = page("a"), page("b")
        self.scenario([
            ([a], [result("a", "alive")]),
            ([a, b], [result("a", "alive"), result("b", "injected")]),
            ([a, b], [result("a", "alive"), result("b", "alive")]),
        ])
        report = self.inject(watch_seconds=3, ready_timeout=0, only_missing=True)
        self.assertTrue(report.ok)
        self.assertEqual(report.injections, {"b": 1})
        self.assertTrue(all(not forced for _, forced in self.rounds))

    def test_discovery_identity_loss_aborts_before_another_translation(self):
        self.find.side_effect = [
            discovery.Instance({23}, discovery.Endpoint("127.0.0.1", 7542), pages=[page("a")]),
            discovery.DiscoveryError("listener identity changed"),
        ]
        self.translate.return_value = [result("a", "injected")]
        with self.assertRaises(discovery.DiscoveryError):
            self.inject(watch_seconds=3, ready_timeout=0)
        self.assertEqual(self.find.call_count, 2)
        self.translate.assert_called_once()

    def test_target_navigation_to_skipped_page_is_not_stale_success(self):
        self.scenario([
            ([page("a")], [result("a", "injected")]),
            ([page("a", "https://example.invalid/")], [result("a", "skipped", False)]),
        ])
        report = self.inject(watch_seconds=3, ready_timeout=0)
        self.assertFalse(report.ok)
        self.assertEqual(report.successful, 0)
        self.assertEqual(report.injections, {"a": 1})


class TranslatePagesTests(unittest.TestCase):
    def test_timed_out_windows_do_not_starve_healthy_tail_or_leave_tasks(self):
        import json

        for hanging_count in (4, 9):
            with self.subTest(hanging_count=hanging_count):
                timeout = 0.02
                pending_ids = {f"pending-{i}" for i in range(hanging_count)}
                pages = [page(name) for name in sorted(pending_ids)] + [page("healthy-tail")]
                opened = set()
                cancelled = set()
                closed = set()
                active = set()
                peak = 0
                real_run = asyncio.run

                class Connection:
                    def __init__(self, target_id):
                        self.target_id = target_id

                    async def __aenter__(inner):
                        nonlocal peak
                        opened.add(inner.target_id)
                        active.add(inner.target_id)
                        peak = max(peak, len(active))
                        return inner

                    async def __aexit__(inner, *exc):
                        await asyncio.sleep(0)
                        active.remove(inner.target_id)
                        closed.add(inner.target_id)
                        return False

                    async def send(inner, message):
                        self.assertEqual(json.loads(message)["method"], "Runtime.evaluate")

                    async def recv(inner):
                        if inner.target_id == "healthy-tail":
                            return json.dumps({"id": 1, "result": {"result": {
                                "value": {"status": "injected", "confirmed": True}}}})
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError:
                            cancelled.add(inner.target_id)
                            raise

                def connect(url, **kwargs):
                    self.assertEqual(kwargs["open_timeout"], timeout)
                    return Connection(url.rsplit("/", 1)[-1])

                def run_with_cleanup_check(coroutine):
                    async def inspect_return():
                        results = await asyncio.wait_for(coroutine, timeout=2)
                        # Verify cleanup before asyncio.run can hide leaked tasks at shutdown.
                        self.assertFalse(active)
                        self.assertEqual(closed, opened)
                        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})
                        return results

                    return real_run(inspect_return())

                with patch.dict(sys.modules, {"websockets": SimpleNamespace(connect=connect)}), patch.object(
                        targets.asyncio, "run", side_effect=run_with_cleanup_check):
                    results = targets.translate_pages(pages, "fixture-engine", timeout=timeout)

                self.assertEqual(opened, pending_ids | {"healthy-tail"})
                self.assertEqual(cancelled, pending_ids)
                self.assertEqual(peak, 4)
                self.assertEqual([item.target_id for item in results], [item["id"] for item in pages])
                self.assertEqual(results[-1], result("healthy-tail", "injected"))
                for item in results[:-1]:
                    self.assertEqual(item, targets.TargetResult(item.target_id, "error", "target-timeout"))

    def test_concurrent_targets_are_bounded_and_failures_are_isolated(self):
        active = 0
        peak = 0
        calls = []

        async def translate(target, engine_js, force, timeout):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            calls.append((target["id"], engine_js, force, timeout))
            await asyncio.sleep(0)
            active -= 1
            return result(target["id"], "error" if target["id"] == "2" else "injected")

        pages = [page(str(i)) for i in range(7)]
        with patch.object(targets, "_translate_target", side_effect=translate):
            results = targets.translate_pages(pages, "fixture-engine", {"0", "5"}, timeout=2)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        self.assertEqual([r.target_id for r in results], [str(i) for i in range(7)])
        self.assertEqual([r.status for r in results].count("injected"), 6)
        self.assertEqual({target_id for target_id, _, force, _ in calls if force}, {"0", "5"})
        self.assertTrue(all(engine == "fixture-engine" and timeout == 2 for _, engine, _, timeout in calls))

    def test_empty_pages_do_not_open_connections(self):
        with patch.object(targets, "_translate_target") as translate:
            self.assertEqual(targets.translate_pages([], "fixture-engine"), [])
        translate.assert_not_called()


class TargetConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ws = SimpleNamespace(send=AsyncMock(), recv=AsyncMock())

        class Connection:
            async def __aenter__(inner):
                return self.ws

            async def __aexit__(inner, *exc):
                return False

        self.connect = Mock(return_value=Connection())
        self.enterContext(patch.dict(sys.modules, {"websockets": SimpleNamespace(connect=self.connect)}))

    async def translate(self, **kwargs):
        return await targets._translate_target(page("a"), "fixture-engine", **kwargs)

    def reply(self, value):
        import json
        self.ws.recv.side_effect = None
        self.ws.recv.return_value = json.dumps({"id": 1, "result": {"result": {"value": value}}})

    async def test_one_guarded_evaluation_no_document_start_script(self):
        import json
        self.ws.recv.side_effect = [
            json.dumps({"method": "Page.frameNavigated", "params": {}}),
            json.dumps({"id": 99, "result": {}}),
            json.dumps({"id": 1, "result": {"result": {"value": {"status": "injected", "confirmed": True}}}}),
        ]
        actual = await self.translate(force=True, timeout=1)
        self.assertEqual(actual, result("a", "injected"))
        self.ws.send.assert_awaited_once()
        sent = json.loads(self.ws.send.call_args.args[0])
        self.assertEqual(sent["method"], "Runtime.evaluate")
        self.assertTrue(sent["params"]["returnByValue"])
        expression = sent["params"]["expression"]
        self.assertIn("fixture-engine", expression)
        self.assertIn("const expectedOrigin", expression)
        self.assertNotIn("Page.addScriptToEvaluateOnNewDocument", expression)
        self.connect.assert_called_once_with(page("a")["webSocketDebuggerUrl"],
                                             open_timeout=1, close_timeout=1, max_size=1024 * 1024)

    async def test_status_and_confirmation_propagate(self):
        for status, confirmed in (("alive", True), ("pending", True), ("pending", False),
                                  ("skipped", False), ("error", True)):
            with self.subTest(status=status, confirmed=confirmed):
                self.reply({"status": status, "confirmed": confirmed, "reason": "fixture-reason"})
                actual = await self.translate(force=False, timeout=1)
                self.assertEqual(actual, targets.TargetResult("a", status, "fixture-reason", confirmed))

    async def test_missing_or_invalid_confirmation_cannot_report_success(self):
        for status in ("injected", "alive"):
            for confirmation in (None, False, 1, "true"):
                with self.subTest(status=status, confirmation=confirmation):
                    self.reply({"status": status, "confirmed": confirmation})
                    actual = await self.translate(force=False, timeout=1)
                    self.assertEqual(actual.status, "error")
                    self.assertEqual(actual.reason, "unconfirmed-success")
                    self.assertFalse(actual.confirmed)

    async def test_invalid_values_and_javascript_errors_fail_closed(self):
        import json
        for value in (None, True, "injected", {}, {"status": "ready"}):
            with self.subTest(value=value):
                self.reply(value)
                actual = await self.translate(force=False, timeout=1)
                self.assertEqual(actual.status, "error")
                self.assertEqual(actual.reason, "invalid-probe-result")
        for reply in ({"id": 1, "error": {"message": "fixture failure"}},
                      {"id": 1, "result": {"exceptionDetails": {"text": "fixture failure"}}}):
            with self.subTest(reply=reply):
                self.ws.recv.return_value = json.dumps(reply)
                actual = await self.translate(force=False, timeout=1)
                self.assertEqual(actual.status, "error")
                self.assertEqual(actual.reason, "evaluation-failed")

    async def test_closed_target_and_unresponsive_target_are_isolated_errors(self):
        self.connect.side_effect = OSError("fixture private connection detail")
        actual = await self.translate(force=False, timeout=1)
        self.assertEqual((actual.status, actual.reason), ("error", "OSError"))
        self.connect.side_effect = None

        async def unresponsive():
            await asyncio.Event().wait()

        self.ws.recv.side_effect = unresponsive
        actual = await self.translate(force=False, timeout=0.01)
        self.assertEqual((actual.status, actual.reason), ("error", "target-timeout"))


if __name__ == "__main__":
    unittest.main()
