"""Bounded, reusable CDP connections admitted by verified discovery snapshots."""

import asyncio
from dataclasses import dataclass, field
import errno
import json
import time


_REASONS = frozenset({
    "target-timeout", "connection-closed", "connection-refused", "invalid-response",
    "missing-websockets", "revalidation-required",
})


class CdpError(RuntimeError):
    def __init__(self, reason):
        if not isinstance(reason, str) or (reason not in _REASONS and not (
                isinstance(reason, str) and reason.startswith("backoff-")
                and reason[8:] in _REASONS)):
            reason = "connection-closed"
        self.reason = reason
        super().__init__(reason)


@dataclass
class _Session:
    key: tuple
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    websocket: object = None
    operation: object = None
    closing: object = None
    counter: int = 0
    valid: bool = True
    failures: int = 0
    retry_at: float = 0
    last_reason: str = ""
    failed_epoch: int = -1


def _page_key(page):
    if not isinstance(page, dict):
        return None
    values = tuple(page.get(name) for name in ("id", "url", "webSocketDebuggerUrl"))
    if not all(isinstance(value, str) and value for value in values):
        return None
    return values


class CdpPool:
    """A pool never discovers, launches, or translates pages on its own."""

    def __init__(self, timeout=5, connector=None, clock=None):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self._close_timeout = min(timeout, 1)
        self._connector = connector
        self._clock = clock or time.monotonic
        self._identity = None
        self._verified = {}
        self._sessions = {}
        self._epoch = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def reconcile(self, instance):
        """Replace admission data; only a fresh snapshot permits failed reconnects."""
        endpoint = instance.endpoint
        identity = (frozenset(instance.pids),
                    (endpoint.host, endpoint.port) if endpoint is not None else None)
        verified = {}
        if endpoint is not None and instance.pids:
            for page in instance.pages:
                key = _page_key(page)
                if key is not None and key[0] not in verified:
                    verified[key[0]] = key
        self._epoch += 1
        retired = []
        for target_id, session in list(self._sessions.items()):
            if identity != self._identity or verified.get(target_id) != session.key:
                session.valid = False
                retired.append(session)
                del self._sessions[target_id]
        self._identity = identity
        self._verified = verified
        await self._retire(retired)

    def _admitted(self, session):
        return (session.valid and self._verified.get(session.key[0]) == session.key
                and self._sessions.get(session.key[0]) is session)

    async def _close_socket(self, session):
        closing = session.closing
        if closing is None:
            websocket, session.websocket = session.websocket, None
            if websocket is None:
                return
            closing = session.closing = asyncio.create_task(self._shutdown_socket(websocket))
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
            raise
        finally:
            if closing.done() and session.closing is closing:
                session.closing = None

    async def _shutdown_socket(self, websocket):
        close_task = asyncio.create_task(websocket.close())
        try:
            # Older clients can require several close_timeout phases to finish.
            await asyncio.wait_for(asyncio.shield(close_task), timeout=3 * self._close_timeout + 0.1)
        except asyncio.CancelledError:
            self._abort(websocket)
        except Exception:
            self._abort(websocket)
        finally:
            if not close_task.done():
                close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)

    @staticmethod
    def _abort(websocket):
        transport = getattr(websocket, "transport", None)
        abort = getattr(transport, "abort", None)
        if callable(abort):
            try:
                abort()
            except Exception:
                pass

    async def _retire(self, sessions):
        operations = []
        for session in sessions:
            operation = session.operation
            if operation is not None and not operation.done():
                operation.cancel()
                operations.append(operation)
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)
        if sessions:
            await asyncio.gather(*(self._close_socket(session) for session in sessions))

    async def close(self):
        sessions = list(self._sessions.values())
        for session in sessions:
            session.valid = False
        self._sessions.clear()
        self._verified.clear()
        self._identity = None
        self._epoch += 1
        await self._retire(sessions)

    async def discard(self, page):
        key = _page_key(page)
        if key is None or self._verified.get(key[0]) != key:
            return
        self._verified.pop(key[0], None)
        session = self._sessions.pop(key[0], None)
        if session is not None:
            session.valid = False
            await self._retire([session])

    async def _request(self, session, expression):
        if session.websocket is None:
            connector = self._connector
            if connector is None:
                try:
                    import websockets
                    connector = websockets.connect
                except ImportError:
                    raise CdpError("missing-websockets") from None
            session.websocket = await connector(
                session.key[2], open_timeout=self.timeout, close_timeout=self._close_timeout,
                max_size=1024 * 1024,
            )
        if not self._admitted(session):
            raise CdpError("revalidation-required")
        session.counter += 1
        request_id = session.counter
        await session.websocket.send(json.dumps({
            "id": request_id, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        while True:
            try:
                reply = json.loads(await session.websocket.recv())
            except (ValueError, TypeError):
                raise CdpError("invalid-response") from None
            if not isinstance(reply, dict):
                raise CdpError("invalid-response")
            if "id" not in reply:
                if isinstance(reply.get("method"), str):
                    continue
                raise CdpError("invalid-response")
            if type(reply["id"]) is not int:
                raise CdpError("invalid-response")
            if reply["id"] != request_id:
                continue
            if not (isinstance(reply.get("result"), dict) or isinstance(reply.get("error"), dict)):
                raise CdpError("invalid-response")
            if not self._admitted(session):
                raise CdpError("revalidation-required")
            return reply

    async def evaluate(self, page, expression):
        key = _page_key(page)
        if key is None or self._verified.get(key[0]) != key:
            raise CdpError("revalidation-required")
        session = self._sessions.get(key[0])
        if session is None:
            session = self._sessions[key[0]] = _Session(key)
        # A cancelled waiter must not close another request's connection.
        try:
            await asyncio.wait_for(session.lock.acquire(), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise CdpError("target-timeout") from None
        try:
            if not self._admitted(session):
                raise CdpError("revalidation-required")
            if session.last_reason:
                if self._clock() < session.retry_at:
                    raise CdpError("backoff-" + session.last_reason)
                if self._epoch <= session.failed_epoch:
                    raise CdpError("revalidation-required")
            operation = asyncio.create_task(self._request(session, expression))
            session.operation = operation
            try:
                reply = await asyncio.wait_for(operation, timeout=self.timeout)
            except asyncio.CancelledError:
                session.failed_epoch = self._epoch
                session.last_reason = "connection-closed"
                await self._close_socket(session)
                raise
            except Exception as exc:
                if isinstance(exc, CdpError):
                    reason = exc.reason
                elif isinstance(exc, asyncio.TimeoutError):
                    reason = "target-timeout"
                elif isinstance(exc, ConnectionRefusedError) or (
                        isinstance(exc, OSError) and exc.errno in (errno.ECONNREFUSED, 10061)):
                    reason = "connection-refused"
                else:
                    reason = "connection-closed"
                session.failures += 1
                session.last_reason = reason
                session.failed_epoch = self._epoch
                session.retry_at = self._clock() + min(2 ** min(session.failures - 1, 5), 30)
                await self._close_socket(session)
                raise CdpError(reason) from None
            finally:
                if session.operation is operation:
                    session.operation = None
            session.failures = 0
            session.last_reason = ""
            session.retry_at = 0
            session.failed_epoch = -1
            return reply
        finally:
            session.lock.release()
