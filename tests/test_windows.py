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


def result(target_id, status, confirmed=True, reason=""):
    return targets.TargetResult(target_id, status, reason=reason, confirmed=confirmed)


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


class WindowSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.pool = SimpleNamespace(reconcile=AsyncMock())
        self.find = self.enterContext(patch.object(discovery, "find_running_instance"))
        self.translate = self.enterContext(patch.object(targets, "translate_pages_async"))
        self.rounds = []

    def scenario(self, stages):
        index = 0

        def find(port):
            nonlocal index
            pages, _ = stages[min(index, len(stages) - 1)]
            return discovery.Instance({23}, discovery.Endpoint("127.0.0.1", port), pages=pages)

        async def translate(pages, engine_js, force_ids=(), **kwargs):
            nonlocal index
            expected_pages, results = stages[min(index, len(stages) - 1)]
            self.assertEqual(pages, expected_pages)
            self.assertEqual(engine_js, "fixture-engine")
            self.rounds.append((tuple(p["id"] for p in pages), set(force_ids)))
            index += 1
            return results

        self.find.side_effect = find
        self.translate.side_effect = translate

    async def inject(self, **kwargs):
        async def sleep(seconds):
            self.clock.sleep(seconds)

        return await targets._inject_windows(7542, "fixture-engine", pool=self.pool,
                                             clock=self.clock.monotonic, sleep=sleep, **kwargs)

    async def test_two_main_windows_and_skipped_page_single_round(self):
        pages = [page("a"), page("b"), page("external", "https://example.invalid/")]
        self.scenario([(pages, [result("a", "injected"), result("b", "injected"),
                                result("external", "skipped", False)])])
        report = await self.inject(watch_seconds=0, ready_timeout=0)
        self.assertTrue(report.ok)
        self.assertEqual((report.successful, report.skipped), (2, 1))
        self.assertEqual(report.injections, {"a": 1, "b": 1})
        self.assertEqual(len(self.rounds), 1)
        self.find.assert_called_once_with(7542)

    async def test_pending_initial_window_waits_then_injects(self):
        pages = [page("a")]
        self.scenario([
            (pages, [result("a", "pending")]),
            (pages, [result("a", "injected")]),
        ])
        report = await self.inject(watch_seconds=0, ready_timeout=3)
        self.assertTrue(report.ok)
        self.assertEqual(report.injections, {"a": 1})
        self.assertEqual(self.rounds, [(("a",), {"a"}), (("a",), {"a"})])
        self.assertGreater(self.clock.now, 100)
        self.assertLessEqual(self.clock.now, 103)

    async def test_watch_zero_still_waits_for_other_confirmed_initial_window(self):
        pages = [page("a"), page("b")]
        self.scenario([
            (pages, [result("a", "injected"), result("b", "pending")]),
            (pages, [result("a", "alive"), result("b", "injected")]),
        ])
        report = await self.inject(watch_seconds=0, ready_timeout=3)
        self.assertTrue(report.ok)
        self.assertEqual(report.successful, 2)
        self.assertEqual(report.injections, {"a": 1, "b": 1})
        self.assertEqual(self.rounds[1][1], {"b"})

    async def test_all_skipped_pages_do_not_consume_startup_timeout(self):
        self.scenario([([page("preview")], [result("preview", "skipped", False)])])
        report = await self.inject(watch_seconds=0, ready_timeout=60)
        self.assertFalse(report.ok)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.injections, {})
        self.assertEqual(self.clock.now, 100)
        self.assertEqual(len(self.rounds), 1)

    async def test_launched_hub_waits_through_transient_preload_skips(self):
        main = page("main")
        self.scenario([
            ([main], [result("main", "skipped", False, "not-hub-preload")]),
            ([main], [result("main", "skipped", False, "not-antigravity-bootstrap")]),
            ([main], [result("main", "injected")]),
        ])
        report = await self.inject(watch_seconds=0, ready_timeout=3, wait_for_main=True)
        self.assertTrue(report.ok)
        self.assertEqual(report.injections, {"main": 1})
        self.assertEqual(len(self.rounds), 3)
        self.assertLessEqual(self.clock.now, 103)

    async def test_launched_hub_waits_through_transient_preview_classification(self):
        preview = page("main", "http://127.0.0.1:7542/static/loading.html")
        main = page("main")
        self.scenario([
            ([preview], [result("main", "skipped", False, "standalone-or-preview")]),
            ([main], [result("main", "injected")]),
        ])
        report = await self.inject(watch_seconds=0, ready_timeout=3, wait_for_main=True)
        self.assertTrue(report.ok)
        self.assertEqual(report.injections, {"main": 1})
        self.assertEqual(len(self.rounds), 2)

    async def test_launched_hub_waits_for_targets_to_appear(self):
        main = page("main")
        self.scenario([
            ([], []),
            ([main], [result("main", "injected")]),
        ])
        report = await self.inject(watch_seconds=0, ready_timeout=3, wait_for_main=True)
        self.assertTrue(report.ok)
        self.assertEqual(report.injections, {"main": 1})
        self.assertEqual(len(self.rounds), 2)

    async def test_launched_preview_stays_uninjected_until_startup_deadline(self):
        self.scenario([([page("preview")], [
            result("preview", "skipped", False, "standalone-or-preview")])])
        report = await self.inject(watch_seconds=0, ready_timeout=2, wait_for_main=True)
        self.assertFalse(report.ok)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.injections, {})
        self.assertEqual(self.clock.now, 102)
        self.assertEqual(len(self.rounds), 3)

    async def test_no_target_single_round_is_not_success(self):
        self.scenario([([], [])])
        report = await self.inject(watch_seconds=0, ready_timeout=0)
        self.assertFalse(report.ok)
        self.assertEqual(report.results, [])
        self.assertEqual(report.injections, {})
        self.assertEqual(len(self.rounds), 1)

    async def test_shared_watch_deadline_is_not_multiplied_by_window_count(self):
        pages = [page("a"), page("b")]
        self.scenario([
            (pages, [result("a", "injected"), result("b", "injected")]),
            (pages, [result("a", "alive"), result("b", "alive")]),
        ])
        report = await self.inject(watch_seconds=3, ready_timeout=0)
        self.assertTrue(report.ok)
        self.assertEqual(self.clock.now, 103)
        self.assertEqual(report.injections, {"a": 1, "b": 1})
        self.assertEqual([forced for _, forced in self.rounds], [{"a", "b"}, set(), set()])
        self.assertEqual(self.find.call_count, len(self.rounds))
        self.assertTrue(all(call.args == (7542,) for call in self.find.call_args_list))

    async def test_new_window_is_discovered_during_watch_and_forced_once(self):
        a, b = page("a"), page("b")
        self.scenario([
            ([a], [result("a", "injected")]),
            ([a, b], [result("a", "alive"), result("b", "injected")]),
            ([a, b], [result("a", "alive"), result("b", "alive")]),
        ])
        report = await self.inject(watch_seconds=3, ready_timeout=0)
        self.assertTrue(report.ok)
        self.assertEqual(report.successful, 2)
        self.assertEqual(report.injections, {"a": 1, "b": 1})
        self.assertEqual([forced for _, forced in self.rounds], [{"a"}, {"b"}, set()])

    async def test_closed_target_disappears_from_final_failures(self):
        a, b = page("a"), page("b")
        self.scenario([
            ([a, b], [result("a", "error"), result("b", "injected")]),
            ([b], [result("b", "alive")]),
        ])
        report = await self.inject(watch_seconds=3, ready_timeout=3)
        self.assertTrue(report.ok)
        self.assertEqual([r.target_id for r in report.results], ["b"])
        self.assertEqual(report.injections, {"b": 1})

    async def test_rebuilt_target_gets_its_own_initial_refresh(self):
        self.scenario([
            ([page("old")], [result("old", "injected")]),
            ([page("new")], [result("new", "injected")]),
            ([page("new")], [result("new", "alive")]),
        ])
        report = await self.inject(watch_seconds=3, ready_timeout=0)
        self.assertTrue(report.ok)
        self.assertEqual([forced for _, forced in self.rounds], [{"old"}, {"new"}, set()])
        self.assertEqual(report.injections, {"old": 1, "new": 1})
        self.assertEqual(report.successful, 1)

    async def test_reload_reinjects_without_repeating_force_refresh(self):
        pages = [page("a")]
        self.scenario([
            (pages, [result("a", "injected")]),
            (pages, [result("a", "injected")]),
            (pages, [result("a", "alive")]),
        ])
        report = await self.inject(watch_seconds=3, ready_timeout=0)
        self.assertTrue(report.ok)
        self.assertEqual(report.injections, {"a": 2})
        self.assertEqual([forced for _, forced in self.rounds], [{"a"}, set(), set()])

    async def test_transient_failure_recovers_and_keeps_refresh_until_success(self):
        pages = [page("a"), page("b")]
        self.scenario([
            (pages, [result("a", "injected"), result("b", "error", False)]),
            (pages, [result("a", "alive"), result("b", "injected")]),
            (pages, [result("a", "alive"), result("b", "alive")]),
        ])
        report = await self.inject(watch_seconds=3, ready_timeout=3)
        self.assertTrue(report.ok)
        self.assertEqual(report.failed, 0)
        self.assertEqual([forced for _, forced in self.rounds], [{"a", "b"}, {"b"}, set()])

    async def test_partial_failure_and_permanent_pending_finish_at_shared_deadline(self):
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
                report = await self.inject(watch_seconds=0, ready_timeout=3)
                self.assertFalse(report.ok)
                self.assertEqual((report.successful, report.failed), (1, 1))
                self.assertEqual(self.clock.now, 103)
                self.assertEqual(report.injections, {"a": 1})

    async def test_only_missing_never_forces_healthy_or_new_targets(self):
        a, b = page("a"), page("b")
        self.scenario([
            ([a], [result("a", "alive")]),
            ([a, b], [result("a", "alive"), result("b", "injected")]),
            ([a, b], [result("a", "alive"), result("b", "alive")]),
        ])
        report = await self.inject(watch_seconds=3, ready_timeout=0, only_missing=True)
        self.assertTrue(report.ok)
        self.assertEqual(report.injections, {"b": 1})
        self.assertTrue(all(not forced for _, forced in self.rounds))

    async def test_discovery_identity_loss_aborts_before_another_translation(self):
        self.find.side_effect = [
            discovery.Instance({23}, discovery.Endpoint("127.0.0.1", 7542), pages=[page("a")]),
            discovery.DiscoveryError("listener identity changed"),
        ]
        self.translate.return_value = [result("a", "injected")]
        with self.assertRaises(discovery.DiscoveryError):
            await self.inject(watch_seconds=3, ready_timeout=0)
        self.assertEqual(self.find.call_count, 2)
        self.translate.assert_called_once()

    async def test_target_navigation_to_skipped_page_is_not_stale_success(self):
        self.scenario([
            ([page("a")], [result("a", "injected")]),
            ([page("a", "https://example.invalid/")], [result("a", "skipped", False)]),
        ])
        report = await self.inject(watch_seconds=3, ready_timeout=0)
        self.assertFalse(report.ok)
        self.assertEqual(report.successful, 0)
        self.assertEqual(report.injections, {"a": 1})


class TranslatePagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_timed_out_windows_do_not_starve_healthy_tail_or_leave_tasks(self):
        import json
        from cdp_connection import CdpPool

        for hanging_count in (4, 9):
            with self.subTest(hanging_count=hanging_count):
                timeout = 0.02
                pending_ids = {f"pending-{i}" for i in range(hanging_count)}
                pages = [page(name) for name in sorted(pending_ids)] + [page("healthy-tail")]
                opened, cancelled, closed, active = set(), set(), set(), set()
                peak = 0

                class Connection:
                    def __init__(self, target_id):
                        self.target_id = target_id
                        self.request_id = None

                    async def send(inner, message):
                        nonlocal peak
                        request = json.loads(message)
                        self.assertEqual(request["method"], "Runtime.evaluate")
                        inner.request_id = request["id"]
                        active.add(inner.target_id)
                        peak = max(peak, len(active))

                    async def recv(inner):
                        if inner.target_id == "healthy-tail":
                            active.remove(inner.target_id)
                            return json.dumps({"id": inner.request_id, "result": {"result": {
                                "value": {"status": "injected", "confirmed": True}}}})
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError:
                            active.remove(inner.target_id)
                            cancelled.add(inner.target_id)
                            raise

                    async def close(inner):
                        await asyncio.sleep(0)
                        closed.add(inner.target_id)

                async def connect(url, **kwargs):
                    self.assertEqual(kwargs["open_timeout"], timeout)
                    target_id = url.rsplit("/", 1)[-1]
                    opened.add(target_id)
                    return Connection(target_id)

                before = asyncio.all_tasks()
                async with CdpPool(timeout=timeout, connector=connect) as pool:
                    await pool.reconcile(discovery.Instance({23}, discovery.Endpoint("127.0.0.1", 7542),
                                                           pages=pages))
                    results = await asyncio.wait_for(targets.translate_pages_async(
                        pages, "fixture-engine", {"healthy-tail"}, pool=pool), timeout=2)
                    self.assertFalse(active)
                    self.assertEqual(cancelled, pending_ids)
                    self.assertEqual(asyncio.all_tasks(), before)
                self.assertEqual(opened, pending_ids | {"healthy-tail"})
                self.assertEqual(closed, opened)
                self.assertEqual(peak, 4)
                self.assertEqual([item.target_id for item in results], [item["id"] for item in pages])
                self.assertEqual(results[-1], result("healthy-tail", "injected"))
                for item in results[:-1]:
                    self.assertEqual(item, targets.TargetResult(item.target_id, "error", "target-timeout"))

    async def test_concurrent_targets_are_bounded_and_failures_are_isolated(self):
        active = 0
        peak = 0
        calls = []
        pool = object()

        async def translate(target, engine_js, force, actual_pool):
            nonlocal active, peak
            self.assertIs(actual_pool, pool)
            active += 1
            peak = max(peak, active)
            calls.append((target["id"], engine_js, force))
            await asyncio.sleep(0)
            active -= 1
            return result(target["id"], "error" if target["id"] == "2" else "injected")

        pages = [page(str(i)) for i in range(7)]
        with patch.object(targets, "_translate_target", side_effect=translate):
            results = await targets.translate_pages_async(pages, "fixture-engine", {"0", "5"}, pool=pool)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        self.assertEqual([r.target_id for r in results], [str(i) for i in range(7)])
        self.assertEqual([r.status for r in results].count("injected"), 6)
        self.assertEqual({target_id for target_id, _, force in calls if force}, {"0", "5"})
        self.assertTrue(all(engine == "fixture-engine" for _, engine, _ in calls))

    async def test_empty_pages_do_not_open_connections(self):
        with patch.object(targets, "_translate_target") as translate:
            self.assertEqual(await targets.translate_pages_async([], "fixture-engine", pool=object()), [])
        translate.assert_not_called()


class TargetConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = SimpleNamespace(evaluate=AsyncMock())

    async def translate(self, force=False):
        return await targets._translate_target(page("a"), "fixture-engine", force, self.pool)

    def reply(self, value):
        self.pool.evaluate.side_effect = None
        self.pool.evaluate.return_value = {"result": {"result": {"value": value}}}

    async def test_one_forced_guarded_evaluation_no_document_start_script(self):
        self.reply({"status": "injected", "confirmed": True})
        actual = await self.translate(force=True)
        self.assertEqual(actual, result("a", "injected"))
        self.pool.evaluate.assert_awaited_once()
        actual_page, expression = self.pool.evaluate.call_args.args
        self.assertEqual(actual_page, page("a"))
        self.assertIn("fixture-engine", expression)
        self.assertIn("const expectedOrigin", expression)
        self.assertNotIn("Page.addScriptToEvaluateOnNewDocument", expression)

    async def test_healthy_poll_uses_small_probe_without_engine(self):
        self.reply({"status": "alive", "confirmed": True})
        self.assertEqual(await self.translate(), result("a", "alive"))
        self.pool.evaluate.assert_awaited_once()
        self.assertNotIn("fixture-engine", self.pool.evaluate.call_args.args[1])
        self.assertIn("const expectedOrigin", self.pool.evaluate.call_args.args[1])

    async def test_confirmed_missing_sends_second_fully_guarded_expression(self):
        self.pool.evaluate.side_effect = [
            {"result": {"result": {"value": {"status": "missing", "confirmed": True}}}},
            {"result": {"result": {"value": {"status": "injected", "confirmed": True}}}},
        ]
        self.assertEqual(await self.translate(), result("a", "injected"))
        self.assertEqual(self.pool.evaluate.await_count, 2)
        first, second = [entry.args[1] for entry in self.pool.evaluate.await_args_list]
        self.assertNotIn("fixture-engine", first)
        self.assertIn("fixture-engine", second)
        self.assertIn("const expectedOrigin", first)
        self.assertIn("const expectedOrigin", second)

    async def test_unconfirmed_missing_never_runs_full_engine(self):
        self.reply({"status": "missing", "confirmed": False})
        actual = await self.translate()
        self.assertEqual(actual.status, "error")
        self.pool.evaluate.assert_awaited_once()
        self.assertNotIn("fixture-engine", self.pool.evaluate.call_args.args[1])

    async def test_status_and_confirmation_propagate(self):
        for status, confirmed in (("alive", True), ("pending", True), ("pending", False),
                                  ("skipped", False), ("error", True)):
            with self.subTest(status=status, confirmed=confirmed):
                self.reply({"status": status, "confirmed": confirmed, "reason": "hub-mount-loading"})
                actual = await self.translate()
                self.assertEqual(actual, targets.TargetResult("a", status, "hub-mount-loading", confirmed))

    async def test_missing_or_invalid_confirmation_cannot_report_success(self):
        for status in ("injected", "alive"):
            for confirmation in (None, False, 1, "true"):
                with self.subTest(status=status, confirmation=confirmation):
                    self.reply({"status": status, "confirmed": confirmation})
                    actual = await self.translate()
                    self.assertEqual(actual.status, "error")
                    self.assertEqual(actual.reason, "unconfirmed-success")
                    self.assertFalse(actual.confirmed)

    async def test_invalid_values_and_javascript_errors_fail_closed(self):
        for value in (None, True, "injected", {}, {"status": "ready"}):
            with self.subTest(value=value):
                self.reply(value)
                actual = await self.translate()
                self.assertEqual(actual.status, "error")
                self.assertEqual(actual.reason, "invalid-probe-result")
        for reply in ({"error": {"message": "private fixture failure"}},
                      {"result": {"exceptionDetails": {"text": "private fixture failure"}}}):
            with self.subTest(reply=reply):
                self.pool.evaluate.return_value = reply
                actual = await self.translate()
                self.assertEqual(actual.status, "error")
                self.assertEqual(actual.reason, "evaluation-failed")

    async def test_unknown_or_unavailable_identity_cannot_hide_partial_failure(self):
        for reason in ("private reason", {"private": "detail"}, ["private"], "identity-unavailable"):
            with self.subTest(reason=reason):
                self.reply({"status": "skipped", "reason": reason})
                actual = await self.translate()
                expected_reason = "identity-unavailable" if reason == "identity-unavailable" else "unknown-probe-reason"
                self.assertEqual((actual.status, actual.reason), ("error", expected_reason))
                self.assertFalse(targets.WindowReport([result("healthy", "alive"), actual]).ok)

    async def test_transport_errors_are_safe_and_cancellation_propagates(self):
        from cdp_connection import CdpError

        for reason in ("connection-closed", "target-timeout", "backoff-connection-refused"):
            with self.subTest(reason=reason):
                self.pool.evaluate.side_effect = CdpError(reason)
                actual = await self.translate()
                self.assertEqual((actual.status, actual.reason), ("error", reason))
        self.pool.evaluate.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.translate()


class CliPoolLifecycleTests(unittest.TestCase):
    def test_sync_cli_reuses_one_pool_for_multiple_rounds_and_closes_it(self):
        from cdp_connection import CdpPool
        import json

        sockets = []
        stages = []

        class Connection:
            def __init__(self):
                self.messages = []
                self.closed = 0
                self.last_request = None

            async def send(self, source):
                self.last_request = json.loads(source)
                self.messages.append(self.last_request)

            async def recv(self):
                count = len(self.messages)
                value = {"status": "injected" if count == 1 else "alive", "confirmed": True}
                return json.dumps({"id": self.last_request["id"], "result": {"result": {"value": value}}})

            async def close(self):
                self.closed += 1

        async def connect(*args, **kwargs):
            websocket = Connection()
            sockets.append(websocket)
            return websocket

        def discover(port):
            return discovery.Instance({23}, discovery.Endpoint("127.0.0.1", port), pages=[page("a")])

        clock = Clock()
        original = targets._inject_windows

        async def inject(*args, **kwargs):
            async def sleep(seconds):
                clock.sleep(seconds)

            report = await original(*args, **kwargs, clock=clock.monotonic, sleep=sleep)
            stages.append(report)
            return report

        with patch.object(targets, "CdpPool", side_effect=lambda: CdpPool(connector=connect)), patch.object(
                discovery, "find_running_instance", side_effect=discover), patch.object(
                targets, "_inject_windows", side_effect=inject):
            report = targets.inject_windows(7542, "fixture-engine", watch_seconds=3, ready_timeout=0)
        self.assertTrue(report.ok)
        self.assertEqual(len(stages), 1)
        self.assertEqual(len(sockets), 1)
        self.assertEqual(len(sockets[0].messages), 3)
        self.assertEqual(sockets[0].closed, 1)
        self.assertIn("fixture-engine", sockets[0].messages[0]["params"]["expression"])
        self.assertNotIn("fixture-engine", sockets[0].messages[1]["params"]["expression"])


if __name__ == "__main__":
    unittest.main()
