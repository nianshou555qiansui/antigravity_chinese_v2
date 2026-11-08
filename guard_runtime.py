"""Persistent, non-launching watchdog with explicit states and bounded retries."""

import asyncio
from dataclasses import dataclass

import cdp_discovery
import cdp_targets
from cdp_connection import CdpPool


@dataclass(frozen=True)
class GuardSnapshot:
    state: str
    delay: float
    successful: int = 0
    skipped: int = 0
    failed: int = 0
    reason: str = ""


class GuardController:
    def __init__(self, engine_js, pool, *, port=None, discover=None, translate=None, on_status=print):
        self.engine_js = engine_js
        self.pool = pool
        self.port = port
        self.discover = discover or cdp_discovery.find_running_instance
        self.translate = translate or cdp_targets.translate_pages_async
        self.on_status = on_status
        self.previous = None
        self.wait_state = None
        self.wait_count = 0

    def _snapshot(self, state, *, report=None, reason="", backoff=False):
        if backoff:
            self.wait_count = min(5, self.wait_count + 1) if state == self.wait_state else 1
            self.wait_state = state
            delay = min(30, 2 ** self.wait_count)
        else:
            self.wait_count = 0
            self.wait_state = None
            delay = 5 if state == "active" else 2
        snapshot = GuardSnapshot(state, delay,
                                 report.successful if report else 0,
                                 report.skipped if report else 0,
                                 report.failed if report else 0, reason)
        key = (snapshot.state, snapshot.successful, snapshot.skipped, snapshot.failed, snapshot.reason)
        if key != self.previous:
            self.on_status(f"状态 {state}：主窗口 {snapshot.successful}，跳过 {snapshot.skipped}，"
                           f"未完成 {snapshot.failed}" + (f"；{reason}" if reason else ""))
            self.previous = key
        return snapshot

    async def step(self):
        report = None
        reason = ""
        backoff = False
        try:
            instance = await asyncio.to_thread(self.discover, self.port)
            await self.pool.reconcile(instance)
            if not instance.pids:
                state, backoff = "waiting-app", True
            elif not instance.endpoint:
                state, backoff = "waiting-port", True
            elif not instance.pages:
                state, backoff = "waiting-window", True
            else:
                results = await self.translate(instance.pages, self.engine_js, pool=self.pool)
                report = cdp_targets.WindowReport(results)
                if report.ok:
                    state = "active"
                elif report.failed:
                    state = "degraded" if report.successful else "waiting-window"
                    errors = [item for item in results if item.status == "error"]
                    if errors:
                        state = "degraded"
                    allowed = {"target-timeout", "connection-closed", "connection-refused", "invalid-response",
                               "missing-websockets", "evaluation-failed", "invalid-probe-result",
                               "revalidation-required", "unconfirmed-success", "connection-failed"}
                    allowed |= {"backoff-" + code for code in allowed}
                    reasons = {item.reason if item.reason in allowed else "target-failed" for item in errors}
                    reason = ",".join(sorted(reasons)) if errors else "hub-mount-loading"
                else:
                    state, backoff = "waiting-window", True
        except cdp_discovery.DiscoveryError as exc:
            await self.pool.close()
            reason = exc.reason if exc.reason in ("ambiguous-ports", "target-list-unavailable") else "discovery-failed"
            state, backoff = "error", True
        except Exception:
            await self.pool.close()
            state, reason, backoff = "error", "guard-failed", True
        return self._snapshot(state, report=report, reason=reason, backoff=backoff)


async def watch(engine_js, port=None, on_status=print, *, pool_factory=None, discover=None,
                translate=None, sleep=None):
    """One event loop owns all sessions; cancellation always closes them."""
    sleep = sleep or asyncio.sleep
    async with (pool_factory or CdpPool)() as pool:
        controller = GuardController(engine_js, pool, port=port, discover=discover,
                                     translate=translate, on_status=on_status)
        while True:
            snapshot = await controller.step()
            await sleep(snapshot.delay)
