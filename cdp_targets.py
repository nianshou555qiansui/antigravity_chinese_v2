"""Verify each Hub document and translate all windows on one verified CDP endpoint."""

import asyncio
from dataclasses import dataclass, field
import json
import time
from urllib.parse import urlsplit

import cdp_discovery


ENGINE_SCOPE = "antigravity-main-v1"
MAIN_PAGE_STATE = r"""() => {
    try {
        if (window !== window.top) return {status: 'skipped', reason: 'subframe'};
        const path = decodeURIComponent(location.pathname);
        if (/^\/(artifact|terminal-standalone|settings-standalone)/.test(path)
            || /^\/static(\/|$)/.test(path)) {
            return {status: 'skipped', reason: 'standalone-or-preview'};
        }
        const bridge = window.electronNative;
        if (!bridge || typeof bridge.minimize !== 'function' || typeof bridge.maximize !== 'function'
            || typeof window.nativeStorage?.getItems !== 'function') {
            return {status: 'skipped', reason: 'not-hub-preload'};
        }
        const config = window.__APP_CONFIG__;
        if (!config && document.readyState === 'loading') {
            return {status: 'pending', reason: 'bootstrap-loading'};
        }
        if (typeof config?.productName !== 'string' || config.productName.toLowerCase() !== 'antigravity'
            || typeof config.appVersion !== 'string') {
            return {status: 'skipped', reason: 'not-antigravity-bootstrap'};
        }
        const root = document.getElementById('root');
        const hasBundle = Array.from(document.scripts).some(script => {
            if (!script.src) return false;
            const url = new URL(script.src, location.href);
            return url.origin === location.origin && url.pathname === '/main.js';
        });
        if (!root || !root.childElementCount || !hasBundle || document.readyState === 'loading') {
            return {status: 'pending', reason: 'hub-mount-loading', confirmed: true};
        }
        return {status: 'ready', confirmed: true};
    } catch (_) {
        return {status: 'skipped', reason: 'identity-unavailable'};
    }
}"""


def guarded_expression(page, engine_js, force=False):
    url = urlsplit(page["url"])
    origin = f"{url.scheme}://{url.netloc}"
    return """(() => {
        const expectedOrigin = ORIGIN;
        const identify = STATE;
        const inspect = () => {
            if (location.origin !== expectedOrigin) return {status: 'skipped', reason: 'navigation'};
            return identify();
        };
        const state = inspect();
        if (state.status !== 'ready') return state;
        const previous = window.__ag_hanhua_engine__;
        if (!FORCE && previous?.scope === SCOPE && previous.active?.()) {
            return {status: 'alive', confirmed: true};
        }
        const __agTrustedPage = () => inspect().status === 'ready';
        ENGINE
        const current = window.__ag_hanhua_engine__;
        return current?.scope === SCOPE && current.active?.()
            ? {status: 'injected', confirmed: true}
            : {status: 'error', reason: 'engine-not-active', confirmed: true};
    })()""".replace("ORIGIN", json.dumps(origin)).replace("STATE", MAIN_PAGE_STATE).replace(
        "FORCE", "true" if force else "false").replace("SCOPE", json.dumps(ENGINE_SCOPE)).replace("ENGINE", engine_js)


@dataclass
class TargetResult:
    target_id: str
    status: str
    reason: str = ""
    confirmed: bool = False


@dataclass
class WindowReport:
    results: list = field(default_factory=list)
    injections: dict = field(default_factory=dict)

    @property
    def successful(self):
        return sum(result.confirmed and result.status in ("injected", "alive") for result in self.results)

    @property
    def failed(self):
        return sum(result.status in ("error", "pending") for result in self.results)

    @property
    def skipped(self):
        return sum(result.status == "skipped" for result in self.results)

    @property
    def ok(self):
        return self.successful > 0 and self.failed == 0


async def _translate_target(page, engine_js, force, timeout):
    import websockets

    target_id = page["id"]

    async def execute():
        async with websockets.connect(page["webSocketDebuggerUrl"], open_timeout=timeout,
                                      close_timeout=1, max_size=1024 * 1024) as ws:
            await ws.send(json.dumps({
                "id": 1, "method": "Runtime.evaluate",
                "params": {"expression": guarded_expression(page, engine_js, force),
                           "returnByValue": True},
            }))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") != 1:
                    continue
                result = reply.get("result", {})
                if reply.get("error") or result.get("exceptionDetails"):
                    return TargetResult(target_id, "error", "evaluation-failed")
                value = result.get("result", {}).get("value")
                if not isinstance(value, dict) or value.get("status") not in {
                        "injected", "alive", "pending", "skipped", "error"}:
                    return TargetResult(target_id, "error", "invalid-probe-result")
                if value["status"] in ("injected", "alive") and value.get("confirmed") is not True:
                    return TargetResult(target_id, "error", "unconfirmed-success")
                return TargetResult(target_id, value["status"], value.get("reason", ""),
                                    value.get("confirmed") is True)

    try:
        return await asyncio.wait_for(execute(), timeout=timeout)
    except TimeoutError:
        return TargetResult(target_id, "error", "target-timeout")
    except Exception as exc:
        return TargetResult(target_id, "error", type(exc).__name__)


def translate_pages(pages, engine_js, force_ids=(), timeout=5):
    async def run():
        semaphore = asyncio.Semaphore(4)

        async def limited(page):
            async with semaphore:
                return await _translate_target(page, engine_js, page["id"] in force_ids, timeout)

        return await asyncio.gather(*(limited(page) for page in pages))

    return asyncio.run(run())


def inject_windows(port, engine_js, watch_seconds=30, ready_timeout=60, only_missing=False):
    """Use one watch deadline for all targets; rediscover and revalidate every round."""
    report = WindowReport()
    refreshed = set()
    ready_deadline = time.monotonic() + max(0, ready_timeout)
    watch_deadline = None
    while True:
        instance = cdp_discovery.find_running_instance(port)
        pages = instance.pages
        force_ids = set() if only_missing else {page["id"] for page in pages} - refreshed
        report.results = translate_pages(pages, engine_js, force_ids)
        for result in report.results:
            if result.confirmed and result.status in ("injected", "alive"):
                refreshed.add(result.target_id)
            if result.confirmed and result.status == "injected":
                report.injections[result.target_id] = report.injections.get(result.target_id, 0) + 1
        if report.results and all(result.status == "skipped" for result in report.results) and watch_deadline is None:
            return report
        now = time.monotonic()
        if report.successful and watch_deadline is None:
            watch_deadline = now + max(0, watch_seconds)
        deadline = watch_deadline if watch_deadline is not None else ready_deadline
        if report.failed and now < ready_deadline:
            deadline = max(deadline, ready_deadline)
        if now >= deadline:
            return report
        time.sleep(min(1.5, deadline - now))
