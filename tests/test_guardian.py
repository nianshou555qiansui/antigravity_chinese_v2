"""Offline watchdog state, scheduling, lifecycle, and wrapper regressions."""

import asyncio
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import AsyncMock, Mock, call, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cdp_discovery as discovery
import cdp_targets as targets
import guard_runtime as runtime
import shouhu


ENGINE = "fixture-engine"
PRIVATE = "private-user:private-password@private.invalid/private-conversation"


def page(target_id):
    return {"id": target_id, "url": "https://127.0.0.1:7555/",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:7542/devtools/page/{target_id}"}


def instance(*target_ids, port=7542):
    return discovery.Instance({23}, discovery.Endpoint("127.0.0.1", port),
                              pages=[page(target_id) for target_id in target_ids])


def result(target_id, status="alive", *, confirmed=True, reason=""):
    return targets.TargetResult(target_id, status, reason, confirmed)


class FakePool:
    def __init__(self):
        self.reconcile = AsyncMock()
        self.close = AsyncMock()
        self.enters = 0
        self.exits = 0

    async def __aenter__(self):
        self.enters += 1
        return self

    async def __aexit__(self, *exc):
        self.exits += 1
        await self.close()


class GuardianStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = FakePool()
        self.discover = Mock(return_value=instance("main"))
        self.translate = AsyncMock(return_value=[result("main")])
        self.messages = []
        self.guard = runtime.GuardController(
            ENGINE, self.pool, discover=self.discover, translate=self.translate,
            on_status=self.messages.append,
        )
        self.launch = self.enterContext(patch.object(shouhu.hh, "launch_antigravity"))
        self.stop = self.enterContext(patch.object(discovery, "stop_confirmed_instance"))

    def tearDown(self):
        self.launch.assert_not_called()
        self.stop.assert_not_called()

    async def test_waiting_states_do_not_translate_unavailable_targets(self):
        fixtures = [
            (discovery.Instance(set()), "waiting-app"),
            (discovery.Instance({23}), "waiting-port"),
            (instance(), "waiting-window"),
        ]
        for value, expected in fixtures:
            with self.subTest(state=expected):
                self.discover.return_value = value
                snapshot = await self.guard.step()
                self.assertEqual(snapshot, runtime.GuardSnapshot(expected, 2))
                self.pool.reconcile.assert_awaited_with(value)
        self.translate.assert_not_awaited()
        self.assertEqual(self.discover.call_args_list, [call(None)] * 3)

    async def test_wait_backoff_caps_at_thirty_without_repeated_logs(self):
        self.discover.return_value = discovery.Instance(set())
        snapshots = [await self.guard.step() for _ in range(8)]
        self.assertEqual([item.delay for item in snapshots], [2, 4, 8, 16, 30, 30, 30, 30])
        self.assertTrue(all(item.state == "waiting-app" for item in snapshots))
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.pool.reconcile.await_count, 8)

    async def test_changing_wait_state_resets_backoff(self):
        self.discover.side_effect = [
            discovery.Instance(set()), discovery.Instance(set()),
            discovery.Instance({23}), discovery.Instance({23}),
            instance(), instance(), discovery.Instance(set()),
        ]
        snapshots = [await self.guard.step() for _ in range(7)]
        self.assertEqual([item.delay for item in snapshots], [2, 4, 2, 4, 2, 4, 2])
        self.assertEqual(len(self.messages), 4)

    async def test_active_uses_five_seconds_and_never_forces_healthy_engine(self):
        self.translate.return_value = [
            result("main"), result("second", "injected"),
            result("preview", "skipped", confirmed=False),
        ]
        snapshot = await self.guard.step()
        self.assertEqual(snapshot, runtime.GuardSnapshot("active", 5, 2, 1, 0))
        self.translate.assert_awaited_once_with(self.discover.return_value.pages, ENGINE, pool=self.pool)
        self.pool.close.assert_not_awaited()

    async def test_fresh_discovery_and_reconciliation_precede_each_translation(self):
        events = []
        fixtures = [instance("main"), instance("main", "new", port=7444)]

        def discover(port):
            value = fixtures[len([event for event in events if event[0] == "discover"])]
            events.append(("discover", port))
            return value

        async def reconcile(value):
            events.append(("reconcile", value))

        async def translate(pages, engine, *, pool):
            events.append(("translate", pages, engine, pool))
            return [result(item["id"]) for item in pages]

        self.guard.port = 7444
        self.guard.discover = discover
        self.pool.reconcile.side_effect = reconcile
        self.guard.translate = translate
        await self.guard.step()
        await self.guard.step()
        self.assertEqual([event[0] for event in events], ["discover", "reconcile", "translate"] * 2)
        self.assertEqual(events[0], ("discover", 7444))
        self.assertIs(events[1][1], fixtures[0])
        self.assertEqual(events[2][1:], (fixtures[0].pages, ENGINE, self.pool))
        self.assertIs(events[4][1], fixtures[1])
        self.assertEqual(events[5][1:], (fixtures[1].pages, ENGINE, self.pool))

    async def test_discovery_runs_off_event_loop(self):
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        entered = asyncio.Event()
        release = threading.Event()
        worker_threads = []

        def discover(port):
            worker_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise AssertionError("Event loop did not release the discovery worker")
            return instance("main")

        self.guard.discover = discover
        task = asyncio.create_task(self.guard.step())
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            self.assertFalse(task.done())
            self.assertNotEqual(worker_threads, [loop_thread])
        finally:
            release.set()
        self.assertEqual((await asyncio.wait_for(task, timeout=2)).state, "active")

    async def test_loading_stays_at_two_seconds_then_recovers(self):
        self.translate.side_effect = [
            [result("main", "pending", reason="hub-mount-loading")],
            [result("main", "pending", reason="hub-mount-loading")],
            [result("main", "injected")],
        ]
        snapshots = [await self.guard.step() for _ in range(3)]
        self.assertEqual([item.state for item in snapshots], ["waiting-window", "waiting-window", "active"])
        self.assertEqual([item.delay for item in snapshots], [2, 2, 5])
        self.assertEqual([item.failed for item in snapshots], [1, 1, 0])
        self.assertEqual(len(self.messages), 2)

    async def test_partial_pending_or_target_error_is_degraded_not_active(self):
        for status in ("pending", "error"):
            with self.subTest(status=status):
                self.translate.return_value = [result("main"), result("second", status)]
                snapshot = await self.guard.step()
                expected_reason = "target-failed" if status == "error" else "hub-mount-loading"
                self.assertEqual(snapshot, runtime.GuardSnapshot("degraded", 2, 1, 0, 1, expected_reason))
        self.pool.close.assert_not_awaited()

    async def test_all_target_errors_are_degraded_without_leaking_reasons(self):
        self.translate.return_value = [result("main", "error", confirmed=False, reason=PRIVATE)]
        snapshot = await self.guard.step()
        self.assertEqual(snapshot, runtime.GuardSnapshot("degraded", 2, 0, 0, 1, "target-failed"))
        self.assertNotIn(PRIVATE, "\n".join(self.messages))

    async def test_known_target_failures_are_deduplicated_sorted_and_sanitized(self):
        self.translate.return_value = [
            result("main", "error", reason="target-timeout"),
            result("second", "error", reason="backoff-connection-closed"),
            result("third", "error", reason="target-timeout"),
            result("fourth", "error", reason=PRIVATE),
        ]
        snapshot = await self.guard.step()
        self.assertEqual(snapshot.reason, "backoff-connection-closed,target-failed,target-timeout")
        self.assertNotIn(PRIVATE, "\n".join(self.messages))

    async def test_all_skipped_targets_wait_with_backoff(self):
        self.translate.return_value = [result("preview", "skipped", confirmed=False)]
        snapshots = [await self.guard.step() for _ in range(3)]
        self.assertEqual([item.state for item in snapshots], ["waiting-window"] * 3)
        self.assertEqual([item.delay for item in snapshots], [2, 4, 8])
        self.assertTrue(all(item.successful == 0 and item.skipped == 1 for item in snapshots))
        self.assertEqual(len(self.messages), 1)

    async def test_unconfirmed_success_is_not_an_active_guard(self):
        self.translate.return_value = [result("main", "alive", confirmed=False)]
        snapshot = await self.guard.step()
        self.assertEqual(snapshot.state, "waiting-window")
        self.assertEqual(snapshot.successful, 0)

    async def test_recovery_resets_error_backoff(self):
        self.discover.side_effect = [
            discovery.DiscoveryError(PRIVATE), discovery.DiscoveryError(PRIVATE),
            instance("main"), discovery.DiscoveryError(PRIVATE),
        ]
        snapshots = [await self.guard.step() for _ in range(4)]
        self.assertEqual([item.state for item in snapshots], ["error", "error", "active", "error"])
        self.assertEqual([item.delay for item in snapshots], [2, 4, 5, 2])
        self.assertEqual(self.pool.close.await_count, 3)
        self.translate.assert_awaited_once()
        self.assertNotIn(PRIVATE, "\n".join(self.messages))

    async def test_loading_also_resets_previous_wait_backoff(self):
        self.discover.side_effect = [instance(), instance(), instance("main"), instance()]
        self.translate.return_value = [result("main", "pending")]
        snapshots = [await self.guard.step() for _ in range(4)]
        self.assertEqual([item.delay for item in snapshots], [2, 4, 2, 2])

    async def test_discovery_failure_closes_pool_before_error_and_cannot_use_stale_pages(self):
        await self.guard.step()
        events = []
        self.pool.close.side_effect = lambda: events.append("closed")
        self.guard.on_status = lambda message: events.append(message)
        self.discover.side_effect = discovery.DiscoveryError(PRIVATE, reason="ambiguous-ports")
        snapshot = await self.guard.step()
        self.assertEqual(snapshot.state, "error")
        self.assertEqual(snapshot.reason, "ambiguous-ports")
        self.assertEqual(events[0], "closed")
        self.assertEqual(self.translate.await_count, 1)
        self.assertEqual(self.pool.reconcile.await_count, 1)
        self.assertNotIn(PRIVATE, " ".join(events))

    async def test_only_public_discovery_reason_codes_reach_status(self):
        for code, expected in (("ambiguous-ports", "ambiguous-ports"),
                               ("target-list-unavailable", "target-list-unavailable"),
                               (PRIVATE, "discovery-failed")):
            with self.subTest(code=code):
                self.discover.side_effect = discovery.DiscoveryError(PRIVATE, reason=code)
                snapshot = await self.guard.step()
                self.assertEqual(snapshot.reason, expected)
        self.assertNotIn(PRIVATE, "\n".join(self.messages))
        self.translate.assert_not_awaited()

    async def test_reconcile_or_translation_failure_closes_pool_and_is_sanitized(self):
        for failing in (self.pool.reconcile, self.translate):
            with self.subTest(stage=failing):
                self.pool.reconcile.side_effect = None
                self.translate.side_effect = None
                failing.side_effect = RuntimeError(PRIVATE)
                before = self.pool.close.await_count
                snapshot = await self.guard.step()
                self.assertEqual(snapshot.state, "error")
                self.assertEqual(snapshot.reason, "guard-failed")
                self.assertEqual(self.pool.close.await_count, before + 1)
        self.assertNotIn(PRIVATE, "\n".join(self.messages))

    async def test_cancelled_translation_propagates_instead_of_entering_retry_loop(self):
        self.translate.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.guard.step()
        self.assertEqual(self.messages, [])

    async def test_identical_status_is_silent_but_window_counts_and_state_changes_log(self):
        self.translate.side_effect = [
            [result("main", "injected")], [result("main")],
            [result("main"), result("new", "injected")],
            [result("main"), result("new")],
            [result("main"), result("new", "error")],
        ]
        for _ in range(5):
            await self.guard.step()
        self.assertEqual(len(self.messages), 3)
        self.assertIn("active", self.messages[0])
        self.assertIn("active", self.messages[1])
        self.assertIn("degraded", self.messages[2])


class GuardianLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_watch_reuses_one_pool_and_cancellation_closes_it(self):
        pool = FakePool()
        factory = Mock(return_value=pool)
        discover = Mock(return_value=instance("main"))
        translate = AsyncMock(return_value=[result("main")])
        delays = []
        messages = []

        async def sleep(seconds):
            delays.append(seconds)
            if len(delays) == 3:
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await runtime.watch(ENGINE, port=7542, pool_factory=factory, discover=discover,
                                translate=translate, on_status=messages.append, sleep=sleep)
        factory.assert_called_once_with()
        self.assertEqual((pool.enters, pool.exits), (1, 1))
        pool.close.assert_awaited_once_with()
        self.assertEqual(discover.call_args_list, [call(7542)] * 3)
        self.assertEqual(translate.await_count, 3)
        self.assertTrue(all(item.kwargs == {"pool": pool} for item in translate.await_args_list))
        self.assertEqual(delays, [5, 5, 5])
        self.assertEqual(len(messages), 1)

    async def test_error_recovery_reconciles_before_reusing_same_pool(self):
        pool = FakePool()
        discover = Mock(side_effect=[discovery.DiscoveryError(PRIVATE), instance("main")])
        translate = AsyncMock(return_value=[result("main", "injected")])
        events = []
        pool.close.side_effect = lambda: events.append("close")
        pool.reconcile.side_effect = lambda value: events.append("reconcile")
        delays = []

        async def sleep(seconds):
            delays.append(seconds)
            if len(delays) == 2:
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await runtime.watch(ENGINE, pool_factory=lambda: pool, discover=discover,
                                translate=translate, on_status=lambda message: None, sleep=sleep)
        self.assertEqual(delays, [2, 5])
        self.assertEqual(events, ["close", "reconcile", "close"])
        translate.assert_awaited_once_with(pool.reconcile.await_args.args[0].pages, ENGINE, pool=pool)

    async def test_cancelled_target_request_closes_watch_pool(self):
        pool = FakePool()
        with self.assertRaises(asyncio.CancelledError):
            await runtime.watch(ENGINE, pool_factory=lambda: pool,
                                discover=Mock(return_value=instance("main")),
                                translate=AsyncMock(side_effect=asyncio.CancelledError),
                                on_status=lambda message: None)
        self.assertEqual(pool.exits, 1)
        pool.close.assert_awaited_once_with()

    async def test_sleep_failure_still_closes_pool(self):
        pool = FakePool()
        with self.assertRaisesRegex(RuntimeError, "fixture sleep failure"):
            await runtime.watch(ENGINE, pool_factory=lambda: pool,
                                discover=Mock(return_value=instance("main")),
                                translate=AsyncMock(return_value=[result("main")]),
                                on_status=lambda message: None,
                                sleep=AsyncMock(side_effect=RuntimeError("fixture sleep failure")))
        pool.close.assert_awaited_once_with()

    async def test_status_sink_failure_does_not_leave_watch_pool_open(self):
        pool = FakePool()
        with self.assertRaisesRegex(RuntimeError, "fixture log failure"):
            await runtime.watch(ENGINE, pool_factory=lambda: pool,
                                discover=Mock(return_value=instance("main")),
                                translate=AsyncMock(return_value=[result("main")]),
                                on_status=Mock(side_effect=RuntimeError("fixture log failure")))
        self.assertEqual(pool.exits, 1)
        self.assertGreaterEqual(pool.close.await_count, 1)


class GuardianWrapperTests(unittest.TestCase):
    def setUp(self):
        self.load = self.enterContext(patch.object(shouhu.hh, "load_dictionary", return_value={"File": "file"}))
        self.build = self.enterContext(patch.object(shouhu.hh, "build_engine_js", return_value=ENGINE))
        self.watch = self.enterContext(patch.object(runtime, "watch", new_callable=AsyncMock))
        self.log = self.enterContext(patch.object(shouhu, "log"))

    def test_wrapper_builds_engine_once_and_delegates_one_async_loop(self):
        shouhu.watch(7444)
        self.load.assert_called_once_with()
        self.build.assert_called_once_with({"File": "file"})
        self.watch.assert_awaited_once_with(ENGINE, port=7444, on_status=self.log)

    def test_empty_dictionary_does_not_start_watch(self):
        self.load.return_value = {}
        shouhu.watch()
        self.build.assert_not_called()
        self.watch.assert_not_awaited()
        self.log.assert_called_once()

    def test_keyboard_interrupt_is_handled_without_rebuilding_or_restart(self):
        self.watch.side_effect = KeyboardInterrupt
        with patch.object(shouhu.hh, "launch_antigravity") as launch, patch.object(
                discovery, "stop_confirmed_instance") as stop:
            shouhu.watch()
        self.load.assert_called_once_with()
        self.build.assert_called_once()
        self.watch.assert_awaited_once()
        self.log.assert_called_once()
        launch.assert_not_called()
        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
