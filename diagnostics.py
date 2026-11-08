"""Read-only, privacy-preserving diagnostics for local dictionaries and CDP."""

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import cdp_discovery
import cdp_targets


DICT_DIR = Path(__file__).resolve().parent / "dicts"
CONNECTION_REASONS = {
    "target-timeout", "connection-closed", "connection-refused", "invalid-response",
    "missing-websockets", "revalidation-required", "identity-changed", "pool-closed",
}
PAGE_REASONS = {
    "subframe", "standalone-or-preview", "not-hub-preload", "bootstrap-loading",
    "not-antigravity-bootstrap", "hub-mount-loading", "identity-unavailable", "navigation",
}
DISCOVERY_REASONS = {"discovery-failed", "ambiguous-ports", "target-list-unavailable"}


class ObjectPairs(list):
    pass


@dataclass
class DictionaryFile:
    name: str
    entries: int = 0
    errors: list = field(default_factory=list)


@dataclass
class DictionaryReport:
    files: list = field(default_factory=list)
    entries: int = 0
    errors: list = field(default_factory=list)

    @property
    def ok(self):
        return bool(self.entries and self.files) and not self.errors and not any(
            item.errors for item in self.files)


@dataclass
class WindowStatus:
    number: int
    state: str
    entries: int | None = None
    reason: str = ""


def inspect_dictionaries(directory=None):
    from hanhua_v2 import normalize_text

    directory = DICT_DIR if directory is None else Path(directory)
    report = DictionaryReport()
    raw_seen = set()
    normalized_seen = set()
    valid_keys = set()
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        report.errors.append("dictionary-read-error")
        return report
    if not paths:
        report.errors.append("dictionary-files-missing")
        return report
    for path in paths:
        item = DictionaryFile(path.name)
        report.files.append(item)
        try:
            pairs = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=ObjectPairs)
        except (OSError, UnicodeError):
            item.errors.append("dictionary-read-error")
            continue
        except (ValueError, RecursionError):
            item.errors.append("invalid-json")
            continue
        if not isinstance(pairs, ObjectPairs):
            item.errors.append("dictionary-not-object")
            continue
        for key, value in pairs:
            if not isinstance(key, str):
                item.errors.append("invalid-key-type")
                continue
            normalized = normalize_text(key)
            if key in raw_seen:
                item.errors.append("duplicate-raw-key")
            raw_seen.add(key)
            if normalized in normalized_seen:
                item.errors.append("duplicate-normalized-key")
            normalized_seen.add(normalized)
            if not normalized:
                item.errors.append("empty-key")
                continue
            if not isinstance(value, str):
                item.errors.append("invalid-value-type")
                continue
            if not value.strip():
                item.errors.append("empty-value")
                continue
            item.entries += 1
            valid_keys.add(normalized)
        item.errors = sorted(set(item.errors))
    report.entries = len(valid_keys)
    if not report.entries:
        report.errors.append("dictionary-empty")
    return report


def diagnostic_expression(page):
    url = urlsplit(page["url"])
    origin = json.dumps(f"{url.scheme}://{url.netloc}")
    scope = json.dumps(cdp_targets.ENGINE_SCOPE)
    return """(() => {
        try {
            if (location.origin !== EXPECTED_ORIGIN) return {status: 'skipped', reason: 'navigation'};
            const identify = IDENTIFY;
            const identity = identify();
            if (identity.status !== 'ready') return identity;
            const ownData = (object, key) => {
                if (object === null || (typeof object !== 'object' && typeof object !== 'function')) return undefined;
                const descriptor = Object.getOwnPropertyDescriptor(object, key);
                return descriptor && Object.prototype.hasOwnProperty.call(descriptor, 'value')
                    ? descriptor.value : undefined;
            };
            const marker = Object.getOwnPropertyDescriptor(window, '__ag_hanhua_engine__');
            if (!marker || (Object.prototype.hasOwnProperty.call(marker, 'value') && marker.value == null)) {
                return {status: 'ready', confirmed: true, engine: 'missing'};
            }
            const engine = ownData(window, '__ag_hanhua_engine__');
            const snapshot = ownData(engine, 'state');
            const started = ownData(snapshot, 'started');
            const stopped = ownData(snapshot, 'stopped');
            const entries = ownData(snapshot, 'entries');
            if (ownData(engine, 'scope') !== EXPECTED_SCOPE || typeof started !== 'boolean'
                || typeof stopped !== 'boolean' || !Number.isSafeInteger(entries) || entries < 0) {
                return {status: 'ready', confirmed: true, engine: 'unknown'};
            }
            return {status: 'ready', confirmed: true,
                engine: stopped ? 'stopped' : started ? 'running' : 'not-started', entries};
        } catch (_) {
            return {status: 'error', reason: 'probe-unavailable'};
        }
    })()""".replace("EXPECTED_ORIGIN", origin).replace(
        "IDENTIFY", cdp_targets.MAIN_PAGE_STATE).replace("EXPECTED_SCOPE", scope)


def _safe_reason(value, allowed, default):
    return value if isinstance(value, str) and value in allowed else default


def _connection_reason(error):
    reason = getattr(error, "reason", None)
    allowed = CONNECTION_REASONS | {"backoff-" + item for item in CONNECTION_REASONS}
    return _safe_reason(reason, allowed, "connection-failed")


def _parse_reply(number, reply):
    if not isinstance(reply, dict):
        return WindowStatus(number, "error", reason="invalid-probe-result")
    result = reply.get("result")
    if reply.get("error") or not isinstance(result, dict) or result.get("exceptionDetails"):
        return WindowStatus(number, "error", reason="evaluation-failed")
    remote = result.get("result")
    value = remote.get("value") if isinstance(remote, dict) else None
    if not isinstance(value, dict):
        return WindowStatus(number, "error", reason="invalid-probe-result")
    state = value.get("status")
    if state in ("pending", "skipped"):
        reason = _safe_reason(value.get("reason"), PAGE_REASONS, "invalid-probe-result")
        if reason in ("identity-unavailable", "invalid-probe-result"):
            return WindowStatus(number, "error", reason=reason)
        return WindowStatus(number, state, reason=reason)
    if state != "ready" or value.get("confirmed") is not True:
        return WindowStatus(number, "error", reason="invalid-probe-result")
    engine = value.get("engine")
    if engine in ("missing", "unknown"):
        return WindowStatus(number, engine)
    entries = value.get("entries")
    if (engine not in ("running", "stopped", "not-started") or type(entries) is not int
            or not 0 <= entries <= 9007199254740991):
        return WindowStatus(number, "error", reason="invalid-probe-result")
    return WindowStatus(number, engine, entries)


async def _inspect_windows(instance):
    from cdp_connection import CdpPool

    async with CdpPool() as pool:
        await pool.reconcile(instance)
        semaphore = asyncio.Semaphore(4)

        async def inspect(number, page):
            async with semaphore:
                try:
                    reply = await pool.evaluate(page, diagnostic_expression(page))
                    return _parse_reply(number, reply)
                except Exception as exc:
                    return WindowStatus(number, "error", reason=_connection_reason(exc))

        return await asyncio.gather(*(inspect(number, page) for number, page in enumerate(instance.pages, 1)))


def _display_filename(name):
    return "".join(char if char.isprintable() else "?" for char in name)


def _window_label(status, entries):
    labels = {
        "running": "引擎运行快照正常", "stopped": "引擎已停止", "not-started": "引擎尚未启动",
        "missing": "未发现翻译引擎", "unknown": "引擎状态未知（旧版或快照不可读）",
        "pending": "主界面尚未就绪", "skipped": "非主界面，已跳过", "error": "检测失败",
    }
    if status.state == "running" and status.entries != entries:
        return "引擎词条数量与本地词典不同"
    return labels[status.state]


def run(port=None, doctor=False):
    print("=== Antigravity 只读诊断 ===" if doctor else "=== Antigravity 状态（只读） ===")
    dictionaries = inspect_dictionaries()
    print(f"词典：{len(dictionaries.files)} 个文件，{dictionaries.entries} 条；"
          f"{'校验通过' if dictionaries.ok else '需要处理'}")
    for error in dictionaries.errors:
        print(f"  词典错误：{error}")
    for item in dictionaries.files:
        if doctor or item.errors:
            suffix = ", ".join(item.errors) if item.errors else "ok"
            print(f"  {_display_filename(item.name)}：{item.entries} 条；{suffix}")

    reason = ""
    instance = None
    windows = []
    try:
        instance = cdp_discovery.find_running_instance(port)
    except Exception as exc:
        reason = _safe_reason(getattr(exc, "reason", None), DISCOVERY_REASONS, "discovery-failed")
    if instance is None:
        print(f"连接：无法确认（{reason}）")
    elif instance.endpoint is None:
        reason = "no-cdp" if instance.pids else "not-running"
        print("应用：正在运行，但未发现可用的已验证调试端口" if instance.pids else "应用：未运行")
    else:
        version = getattr(instance, "version", "")
        version = version if isinstance(version, str) and re.fullmatch(r"[0-9]+(?:[.][0-9]+)+", version) else "未知"
        if version == "未知":
            reason = "version-unavailable"
        print(f"版本：{version}；调试端口：{instance.endpoint.port}")
        print(f"目标：{len(instance.pages)} 个可安全探测的候选（不代表主界面已确认）")
        if instance.pages:
            try:
                windows = asyncio.run(_inspect_windows(instance))
            except Exception as exc:
                reason = _connection_reason(exc)
                print(f"连接：检测未完成（{reason}）")
        else:
            reason = "no-targets"
        for status in windows:
            detail = f"；词条 {status.entries}" if doctor and status.entries is not None else ""
            detail += f"；{status.reason}" if status.reason else ""
            print(f"  #{status.number}：{_window_label(status, dictionaries.entries)}{detail}")
        running = sum(status.state == "running" and status.entries == dictionaries.entries for status in windows)
        skipped = sum(status.state == "skipped" for status in windows)
        print(f"窗口：运行正常 {running}；跳过 {skipped}；需处理 {len(windows) - running - skipped}")

    healthy = bool(windows) and any(status.state == "running" for status in windows)
    healthy = healthy and all(status.state == "skipped" or (
        status.state == "running" and status.entries == dictionaries.entries) for status in windows)
    healthy = bool(healthy and dictionaries.ok and not reason)
    print("结果：通过（仅检查快照，未改动应用）" if healthy else "结果：需处理或尚未就绪（未改动应用）")
    if doctor and not healthy:
        if not dictionaries.ok:
            print("建议：按错误码修复本地词典格式或重复键，再重新诊断。")
        if reason == "not-running":
            print("建议：先手动打开 Antigravity，再运行诊断；本命令不会启动应用。")
        elif reason == "no-cdp":
            print("建议：确认应用调试端口；必要时保存工作后手动选择安全启动或确认重启。")
        elif reason == "ambiguous-ports":
            print("建议：使用 --port 明确选择要诊断的调试实例。")
        elif reason:
            print("建议：等待应用界面加载完成后重试，并确认本机调试端口可访问。")
        if any(status.state in ("missing", "unknown", "stopped", "not-started") or (
                status.state == "running" and status.entries != dictionaries.entries) for status in windows):
            print("建议：确认工作已保存后，手动运行 --no-launch 重新注入；诊断不会自动注入。")
        if windows and all(status.state == "skipped" for status in windows):
            print("建议：打开 Antigravity 主界面后重试；独立网页与预览不会作为主窗口。")
        if any(status.state in ("pending", "error") for status in windows):
            print("建议：待相应窗口加载完成后重试；连接失败不会影响其他窗口的只读检查。")
    return 0 if healthy else 1
