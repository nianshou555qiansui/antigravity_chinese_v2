"""Persistent CDP sessions are tested without processes or network sockets."""

import asyncio
import builtins
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cdp_connection import CdpError, CdpPool
from cdp_discovery import Endpoint, Instance


def page(target_id="a", **changes):
    return dict({
        "id": target_id,
        "url": "https://127.0.0.1:7555/",
        "webSocketDebuggerUrl": f"ws://127.0.0.1:7542/devtools/page/{target_id}",
    }, **changes)


def snapshot(pages=None, pids=None, endpoint=None):
    return Instance({23} if pids is None else pids,
                    Endpoint("127.0.0.1", 7542) if endpoint is None else endpoint,
                    pages=[page()] if pages is None else pages)


class Socket:
    def __init__(self):
        self.sent = []
        self.responses = asyncio.Queue()
        self.closed = 0
        self.aborted = 0
        self.hang = False
        self.close_hang = False
        self.close_cancelled = False
        self.recv_cancelled = False
        self.receiving = asyncio.Event()
        self.closing = asyncio.Event()
        self.transport = SimpleNamespace(abort=self.abort)

    def abort(self):
        self.aborted += 1

    async def send(self, source):
        request = json.loads(source)
        self.sent.append(request)
        if not self.hang:
            await self.responses.put(json.dumps({"id": request["id"], "result": {"result": {
                "value": {"request": request["id"]}}}}))

    async def recv(self):
        self.receiving.set()
        try:
            response = await self.responses.get()
            if isinstance(response, BaseException):
                raise response
            return response
        except asyncio.CancelledError:
            self.recv_cancelled = True
            raise

    async def close(self):
        self.closed += 1
        self.closing.set()
        if self.close_hang:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class PoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.sockets = []
        self.connects = []
        self.next_sockets = []

        async def connect(url, **kwargs):
            self.connects.append((url, kwargs))
            value = self.next_sockets.pop(0) if self.next_sockets else Socket()
            if isinstance(value, BaseException):
                raise value
            self.sockets.append(value)
            return value

        self.pool = CdpPool(timeout=0.02, connector=connect, clock=self.clock)
        self.addAsyncCleanup(self.pool.close)

    async def error(self, reason, operation):
        with self.assertRaises(CdpError) as caught:
            await operation
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), reason)

    async def test_requires_verified_snapshot_and_exact_page_membership(self):
        await self.error("revalidation-required", self.pool.evaluate(page(), "fixture"))
        await self.pool.reconcile(snapshot())
        for changed in (page("other"), page(url="https://127.0.0.1:7555/other"),
                        page(webSocketDebuggerUrl="ws://127.0.0.1:7542/devtools/page/other")):
            await self.error("revalidation-required", self.pool.evaluate(changed, "fixture"))
        self.assertFalse(self.connects)

    async def test_reuses_connection_unique_ids_and_raw_result(self):
        await self.pool.reconcile(snapshot())
        first = await self.pool.evaluate(page(), "first")
        await self.pool.reconcile(snapshot())
        second = await self.pool.evaluate(page(), "second")
        self.assertEqual(len(self.connects), 1)
        self.assertEqual(first["id"], 1)
        self.assertEqual(second["id"], 2)
        sent = self.sockets[0].sent
        self.assertEqual([entry["id"] for entry in sent], [1, 2])
        self.assertEqual([entry["params"]["expression"] for entry in sent], ["first", "second"])
        self.assertTrue(all(entry["params"]["returnByValue"] for entry in sent))
        self.assertTrue(all(entry["method"] == "Runtime.evaluate" for entry in sent))

    async def test_serial_requests_do_not_compete_for_recv(self):
        self.pool.timeout = 1
        await self.pool.reconcile(snapshot())
        results = await asyncio.gather(*(self.pool.evaluate(page(), str(i)) for i in range(8)))
        self.assertEqual(len(self.sockets), 1)
        self.assertEqual([reply["id"] for reply in results], list(range(1, 9)))

    async def test_ignores_events_and_other_request_ids(self):
        websocket = Socket()
        await websocket.responses.put('{"method":"Page.frameNavigated","params":{}}')
        await websocket.responses.put('{"id":0,"result":{}}')
        self.next_sockets.append(websocket)
        await self.pool.reconcile(snapshot())
        reply = await self.pool.evaluate(page(), "fixture")
        self.assertEqual(reply["id"], 1)

    async def test_invalid_responses_are_safe_and_close_session(self):
        for source in ("not JSON", "[]", "{}", '{"id":true,"result":{}}',
                       '{"id":1,"result":null}', '{"id":1,"error":"private"}'):
            with self.subTest(source=source):
                await self.pool.close()
                websocket = Socket()
                await websocket.responses.put(source)
                self.next_sockets.append(websocket)
                await self.pool.reconcile(snapshot())
                await self.error("invalid-response", self.pool.evaluate(page(), "fixture"))
                self.assertEqual(websocket.closed, 1)

    async def test_cdp_protocol_error_response_is_returned_without_reconnect(self):
        websocket = Socket()
        await websocket.responses.put('{"id":1,"error":{"code":-32000,"message":"fixture"}}')
        self.next_sockets.append(websocket)
        await self.pool.reconcile(snapshot())
        reply = await self.pool.evaluate(page(), "fixture")
        self.assertEqual(reply["error"]["code"], -32000)
        self.assertEqual(websocket.closed, 0)

    async def test_failure_requires_new_snapshot_even_after_delay(self):
        self.next_sockets.append(ConnectionRefusedError("private address"))
        await self.pool.reconcile(snapshot())
        await self.error("connection-refused", self.pool.evaluate(page(), "fixture"))
        await self.error("backoff-connection-refused", self.pool.evaluate(page(), "fixture"))
        self.clock.now += 1
        await self.error("revalidation-required", self.pool.evaluate(page(), "fixture"))
        self.assertEqual(len(self.connects), 1)
        await self.pool.reconcile(snapshot())
        await self.pool.evaluate(page(), "fixture")
        self.assertEqual(len(self.connects), 2)

    async def test_exponential_backoff_is_per_target_capped_and_reset_by_success(self):
        pages = [page("a"), page("b")]
        for delay in (1, 2, 4, 8, 16, 30, 30):
            await self.pool.reconcile(snapshot(pages))
            self.next_sockets.append(ConnectionRefusedError("private detail"))
            await self.error("connection-refused", self.pool.evaluate(page("a"), "fixture"))
            await self.pool.reconcile(snapshot(pages))
            self.clock.now += delay - 0.01
            await self.error("backoff-connection-refused", self.pool.evaluate(page("a"), "fixture"))
            self.clock.now += 0.01
        await self.pool.reconcile(snapshot(pages))
        await self.pool.evaluate(page("a"), "recovered")
        self.sockets[-1].hang = True
        await self.error("target-timeout", self.pool.evaluate(page("a"), "fixture"))
        self.clock.now += 1
        await self.pool.reconcile(snapshot(pages))
        await self.pool.evaluate(page("a"), "new-success")
        await self.pool.evaluate(page("b"), "unaffected")

    async def test_open_and_evaluation_timeouts_close_and_do_not_leak_tasks(self):
        websocket = Socket()
        websocket.hang = True
        self.next_sockets.append(websocket)
        await self.pool.reconcile(snapshot())
        before = asyncio.all_tasks()
        await self.error("target-timeout", self.pool.evaluate(page(), "fixture"))
        self.assertTrue(websocket.recv_cancelled)
        self.assertEqual(websocket.closed, 1)
        self.assertEqual(asyncio.all_tasks(), before)

        opened = asyncio.Event()
        cancelled = asyncio.Event()

        async def connect(*args, **kwargs):
            opened.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with CdpPool(timeout=0.01, connector=connect) as pool:
            await pool.reconcile(snapshot())
            await self.error("target-timeout", pool.evaluate(page(), "fixture"))
        self.assertTrue(opened.is_set())
        self.assertTrue(cancelled.is_set())

    async def test_one_failed_window_does_not_drop_other_healthy_session(self):
        pages = [page("a"), page("b")]
        self.next_sockets.extend([ConnectionRefusedError("private"), Socket()])
        await self.pool.reconcile(snapshot(pages))
        await self.error("connection-refused", self.pool.evaluate(pages[0], "fixture"))
        await self.pool.evaluate(pages[1], "first")
        healthy = self.sockets[-1]
        await self.pool.reconcile(snapshot(pages))
        await self.pool.evaluate(pages[1], "second")
        self.assertEqual(healthy.closed, 0)
        self.assertEqual(len(healthy.sent), 2)

    async def test_snapshot_change_retires_url_endpoint_process_or_removed_target(self):
        for changed in (snapshot([]), snapshot([page(url="https://127.0.0.1:7555/new")]),
                        snapshot([page(webSocketDebuggerUrl="ws://127.0.0.1:7542/devtools/page/new")]),
                        snapshot(pids={23, 24}), snapshot(endpoint=Endpoint("::1", 7542))):
            with self.subTest(changed=changed):
                await self.pool.close()
                await self.pool.reconcile(snapshot())
                await self.pool.evaluate(page(), "fixture")
                websocket = self.sockets[-1]
                await self.pool.reconcile(changed)
                self.assertEqual(websocket.closed, 1)

    async def test_discard_needs_reconcile_and_close_can_be_reused(self):
        await self.pool.reconcile(snapshot())
        await self.pool.evaluate(page(), "first")
        first = self.sockets[-1]
        await self.pool.discard(page())
        self.assertEqual(first.closed, 1)
        await self.error("revalidation-required", self.pool.evaluate(page(), "fixture"))
        await self.pool.reconcile(snapshot())
        await self.pool.evaluate(page(), "second")
        second = self.sockets[-1]
        await self.pool.close()
        await self.pool.close()
        self.assertEqual(second.closed, 1)
        await self.error("revalidation-required", self.pool.evaluate(page(), "fixture"))
        await self.pool.reconcile(snapshot())
        await self.pool.evaluate(page(), "third")
        self.assertEqual(len(self.sockets), 3)

    async def test_cancelled_request_closes_but_cancelled_waiter_does_not(self):
        websocket = Socket()
        websocket.hang = True
        self.next_sockets.append(websocket)
        await self.pool.reconcile(snapshot())
        self.pool.timeout = 1
        operation = asyncio.create_task(self.pool.evaluate(page(), "blocked"))
        await websocket.receiving.wait()
        waiter = asyncio.create_task(self.pool.evaluate(page(), "waiter"))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(websocket.closed, 0)
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertTrue(websocket.recv_cancelled)
        self.assertEqual(websocket.closed, 1)
        self.clock.now += 1
        await self.error("revalidation-required", self.pool.evaluate(page(), "fixture"))

    async def test_reconcile_cancels_inflight_target_and_cleans_connection(self):
        websocket = Socket()
        websocket.hang = True
        self.next_sockets.append(websocket)
        await self.pool.reconcile(snapshot())
        self.pool.timeout = 1
        operation = asyncio.create_task(self.pool.evaluate(page(), "blocked"))
        await websocket.receiving.wait()
        await self.pool.reconcile(snapshot([]))
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertEqual(websocket.closed, 1)
        self.assertTrue(websocket.recv_cancelled)

    async def test_reconcile_waits_for_cleanup_already_owned_by_cancelled_request(self):
        websocket = Socket()
        websocket.hang = True
        websocket.close_hang = True
        self.next_sockets.append(websocket)
        await self.pool.reconcile(snapshot())
        self.pool.timeout = 1
        self.pool._close_timeout = 0.005
        operation = asyncio.create_task(self.pool.evaluate(page(), "blocked"))
        await websocket.receiving.wait()
        operation.cancel()
        await websocket.closing.wait()
        retire = asyncio.create_task(self.pool.reconcile(snapshot([])))
        await asyncio.sleep(0)
        self.assertFalse(retire.done(), "reconcile must await the other owner's socket cleanup")
        await retire
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertEqual(websocket.closed, 1)
        self.assertEqual(websocket.aborted, 1)
        self.assertTrue(websocket.close_cancelled)

    async def test_hung_close_aborts_transport_and_collects_close_task(self):
        websocket = Socket()
        websocket.close_hang = True
        self.next_sockets.append(websocket)
        await self.pool.reconcile(snapshot())
        await self.pool.evaluate(page(), "fixture")
        self.pool._close_timeout = 0.005
        before = asyncio.all_tasks()
        await self.pool.close()
        self.assertEqual(websocket.aborted, 1)
        self.assertTrue(websocket.close_cancelled)
        self.assertEqual(asyncio.all_tasks(), before)

    async def test_cancelling_close_aborts_and_propagates_without_leak(self):
        websocket = Socket()
        websocket.close_hang = True
        self.next_sockets.append(websocket)
        await self.pool.reconcile(snapshot())
        await self.pool.evaluate(page(), "fixture")
        operation = asyncio.create_task(self.pool.close())
        await websocket.closing.wait()
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertEqual(websocket.aborted, 1)
        self.assertTrue(websocket.close_cancelled)

    async def test_missing_dependency_and_exception_details_are_sanitized(self):
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "websockets":
                raise ImportError("private module path")
            return real_import(name, *args, **kwargs)

        async with CdpPool(timeout=0.01) as pool:
            await pool.reconcile(snapshot())
            with patch("builtins.__import__", side_effect=guarded_import):
                await self.error("missing-websockets", pool.evaluate(page(), "fixture"))
        for reason in ("private error", "backoff-private error", None, {}):
            self.assertEqual(CdpError(reason).reason, "connection-closed")


if __name__ == "__main__":
    unittest.main()
