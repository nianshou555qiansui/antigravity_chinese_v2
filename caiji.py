#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect bounded UI statistics from verified Antigravity main windows.

The default report contains no UI text. Explicit --include-ui-text exports only
audited static labels to pending.json, never to the console. --misses reads only
the current engine's numeric statistics; legacy raw-text collections are refused.
"""

import argparse
import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

import cdp_discovery
from cdp_connection import CdpError, CdpPool
import cdp_targets
import privacy_policy


ROOT = Path(__file__).resolve().parent
SOURCES = privacy_policy.SOURCES
STATIC_TEXT = frozenset(text for labels in privacy_policy.STATIC_LABELS.values() for text in labels)
MAX_COUNT = 2 ** 53 - 1
MAX_NODES = 200_000
MAX_CANDIDATES = 1000
MAX_TEXT_LENGTH = 120
MAX_OUTPUT_BYTES = 128 * 1024
WITHHELD = ("blocked", "sensitive", "unclassified")
CONNECTION_REASONS = {
    "target-timeout", "connection-closed", "connection-refused", "invalid-response",
    "missing-websockets", "revalidation-required",
}
SAFE_REASONS = CONNECTION_REASONS | {"backoff-" + name for name in CONNECTION_REASONS} | {
    "discovery-failed", "ambiguous-ports", "target-list-unavailable", "not-running", "no-cdp",
    "no-targets", "no-main-window", "identity-unavailable", "bootstrap-loading", "hub-mount-loading",
    "evaluation-failed", "invalid-probe-result", "collection-unavailable", "collection-limit",
    "engine-upgrade-required", "collection-failed",
}
SKIP_REASONS = {
    "navigation", "subframe", "standalone-or-preview", "not-hub-preload", "not-antigravity-bootstrap",
}


def _new_report(include_ui_text=False, misses=False):
    return {
        "schema": 1,
        "mode": "misses" if misses else "pending",
        "text_included": bool(include_ui_text and not misses),
        "windows": {"checked": 0, "skipped": 0, "failed": 0},
        "sources": dict.fromkeys(SOURCES, 0),
        "withheld": dict.fromkeys(WITHHELD, 0),
        "candidates": [],
    }


@dataclass
class CollectionResult:
    report: dict
    reasons: tuple = field(default_factory=tuple)

    @property
    def ok(self):
        return bool(self.report["windows"]["checked"] and not self.report["windows"]["failed"]
                    and not self.reasons)


def _verified_expression(page, body):
    url = urlsplit(page["url"])
    origin = json.dumps(f"{url.scheme}://{url.netloc}")
    return """(() => {
        try {
            if (location.origin !== EXPECTED_ORIGIN) return {status: 'skipped', reason: 'navigation'};
            const identify = IDENTIFY;
            const identity = identify();
            if (identity.status !== 'ready') return identity;
            BODY
        } catch (_) {
            return {status: 'error', reason: 'collection-unavailable'};
        }
    })()""".replace("EXPECTED_ORIGIN", origin).replace("IDENTIFY", cdp_targets.MAIN_PAGE_STATE).replace(
        "BODY", body)


def collection_expression(page, include_ui_text=False):
    body = r"""
        const policy = POLICY;
        const includeText = INCLUDE_TEXT;
        const sources = Object.fromEntries(policy.sources.map(name => [name, 0]));
        const withheld = {blocked: 0, sensitive: 0, unclassified: 0};
        const candidates = new Map();
        const increment = (object, key) => { object[key] = Math.min(MAX_COUNT, object[key] + 1); };
        function observe(node, attribute = null) {
            const allowed = attribute === null ? policy.allowsText(node) : policy.allowsAttribute(node, attribute);
            if (!allowed) { increment(withheld, 'blocked'); return; }
            const source = policy.collectionSource(node, attribute);
            if (!source || !Object.prototype.hasOwnProperty.call(sources, source)) {
                increment(withheld, 'unclassified');
                return;
            }
            if (attribute !== null && !node.hasAttribute(attribute)) return;
            increment(sources, source);
            if (!includeText) return;
            const raw = attribute === null ? node.nodeValue : node.getAttribute(attribute);
            if (!policy.usableText(raw) || !policy.acceptsStatic(node, attribute, raw)) {
                increment(withheld, 'sensitive');
                return;
            }
            const text = raw.replace(/\s+/g, ' ').trim();
            if (text.length > MAX_TEXT_LENGTH) { increment(withheld, 'sensitive'); return; }
            const key = JSON.stringify([source, text]);
            const previous = candidates.get(key);
            if (previous) increment(previous, 'count');
            else {
                if (candidates.size >= MAX_CANDIDATES) throw Error('limit');
                candidates.set(key, {source, text, count: 1});
            }
        }
        const pending = [];
        const visited = new Set();
        let scheduled = 0;
        const enqueue = node => {
            if (!node) return true;
            if (scheduled >= MAX_NODES) return false;
            pending.push(node);
            scheduled++;
            return true;
        };
        if (!enqueue(document.documentElement)) return {status: 'error', reason: 'collection-limit'};
        while (pending.length) {
            const node = pending.pop();
            if (!node || visited.has(node)) continue;
            visited.add(node);
            if (node.nodeType === 3) { observe(node); continue; }
            if (node.nodeType === 1) {
                for (const attribute of policy.attributes) {
                    if (policy.allowsAttribute(node, attribute)) observe(node, attribute);
                }
                if (!policy.allowsText(node)) { increment(withheld, 'blocked'); continue; }
                if (node.shadowRoot && !enqueue(node.shadowRoot)) {
                    return {status: 'error', reason: 'collection-limit'};
                }
            }
            const children = node.childNodes || [];
            for (const child of children) {
                if (!enqueue(child)) return {status: 'error', reason: 'collection-limit'};
            }
        }
        return {status: 'ready', confirmed: true, schema: 1, mode: 'pending',
            text_included: includeText, sources, withheld, candidates: [...candidates.values()]};
    """.replace("POLICY", privacy_policy.build_policy_js()).replace(
        "INCLUDE_TEXT", "true" if include_ui_text else "false").replace(
        "MAX_COUNT", str(MAX_COUNT)).replace("MAX_NODES", str(MAX_NODES)).replace(
        "MAX_TEXT_LENGTH", str(MAX_TEXT_LENGTH)).replace(
        "MAX_CANDIDATES", str(MAX_CANDIDATES))
    return _verified_expression(page, body)


def misses_expression(page):
    body = r"""
        const ownData = (object, key) => {
            if (object === null || (typeof object !== 'object' && typeof object !== 'function')) return undefined;
            const descriptor = Object.getOwnPropertyDescriptor(object, key);
            return descriptor && Object.prototype.hasOwnProperty.call(descriptor, 'value')
                ? descriptor.value : undefined;
        };
        const engine = ownData(window, '__ag_hanhua_engine__');
        const misses = ownData(engine, 'misses');
        const rawSources = ownData(misses, 'sources');
        if (ownData(engine, 'scope') !== ENGINE_SCOPE || ownData(misses, 'schema') !== 1) {
            return {status: 'error', reason: 'engine-upgrade-required'};
        }
        const sources = {};
        for (const source of SOURCES) {
            const value = ownData(rawSources, source);
            if (!Number.isSafeInteger(value) || value < 0) {
                return {status: 'error', reason: 'engine-upgrade-required'};
            }
            sources[source] = value;
        }
        return {status: 'ready', confirmed: true, schema: 1, mode: 'misses', text_included: false,
            sources, withheld: {blocked: 0, sensitive: 0, unclassified: 0}, candidates: []};
    """.replace("ENGINE_SCOPE", json.dumps(cdp_targets.ENGINE_SCOPE)).replace("SOURCES", json.dumps(SOURCES))
    return _verified_expression(page, body)


def _counter(value):
    return type(value) is int and 0 <= value <= MAX_COUNT


def _safe_reason(value, fallback="collection-failed"):
    return value if isinstance(value, str) and value in SAFE_REASONS else fallback


def _parse_reply(reply, include_ui_text=False, misses=False):
    if not isinstance(reply, dict) or reply.get("error"):
        return None, "evaluation-failed"
    result = reply.get("result")
    if not isinstance(result, dict) or result.get("exceptionDetails"):
        return None, "evaluation-failed"
    remote = result.get("result")
    value = remote.get("value") if isinstance(remote, dict) else None
    if not isinstance(value, dict):
        return None, "invalid-probe-result"
    status = value.get("status")
    if status == "skipped":
        reason = value.get("reason")
        return ("skipped", "") if isinstance(reason, str) and reason in SKIP_REASONS else (
            None, _safe_reason(reason, "identity-unavailable"))
    if status in ("pending", "error"):
        return None, _safe_reason(value.get("reason"))
    expected = _new_report(include_ui_text, misses)
    if (status != "ready" or value.get("confirmed") is not True
            or type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("mode") != expected["mode"]
            or type(value.get("text_included")) is not bool
            or value["text_included"] != expected["text_included"]):
        return None, "invalid-probe-result"
    for name, keys in (("sources", SOURCES), ("withheld", WITHHELD)):
        counters = value.get(name)
        if not isinstance(counters, dict) or not all(_counter(counters.get(key)) for key in keys):
            return None, "invalid-probe-result"
        expected[name] = {key: counters[key] for key in keys}
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        return None, "invalid-probe-result"
    if expected["text_included"]:
        for candidate in candidates:
            if not isinstance(candidate, dict):
                return None, "invalid-probe-result"
            source, text, count = (candidate.get(key) for key in ("source", "text", "count"))
            if (not isinstance(source, str) or source not in SOURCES or not isinstance(text, str)
                    or not 0 < len(text) <= MAX_TEXT_LENGTH or text not in STATIC_TEXT
                    or not _counter(count) or count == 0):
                return None, "invalid-probe-result"
            expected["candidates"].append({"source": source, "text": text, "count": count})
    return expected, ""


async def _collect_verified(instance, include_ui_text=False, misses=False):
    semaphore = asyncio.Semaphore(4)
    async with CdpPool() as pool:
        await pool.reconcile(instance)

        async def inspect(page):
            async with semaphore:
                try:
                    expression = misses_expression(page) if misses else collection_expression(page, include_ui_text)
                    return _parse_reply(await pool.evaluate(page, expression), include_ui_text, misses)
                except CdpError as error:
                    return None, _safe_reason(error.reason)
                except Exception:
                    return None, "collection-failed"

        return await asyncio.gather(*(inspect(page) for page in instance.pages))


def _serialize(report):
    data = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("collection-limit")
    return data


def collect(port=None, include_ui_text=False, misses=False):
    report = _new_report(include_ui_text, misses)
    if include_ui_text and misses:
        return CollectionResult(report, ("collection-failed",))
    try:
        instance = cdp_discovery.find_running_instance(port)
    except Exception as error:
        return CollectionResult(report, (_safe_reason(getattr(error, "reason", None), "discovery-failed"),))
    if instance.endpoint is None:
        return CollectionResult(report, ("no-cdp" if instance.pids else "not-running",))
    if not instance.pages:
        return CollectionResult(report, ("no-targets",))
    try:
        results = asyncio.run(_collect_verified(instance, include_ui_text, misses))
    except Exception:
        return CollectionResult(report, ("collection-failed",))
    reasons = set()
    candidates = {}
    for value, reason in results:
        if value == "skipped":
            report["windows"]["skipped"] += 1
            continue
        if value is None:
            report["windows"]["failed"] += 1
            reasons.add(reason)
            continue
        report["windows"]["checked"] += 1
        for name in ("sources", "withheld"):
            for key in report[name]:
                report[name][key] = min(MAX_COUNT, report[name][key] + value[name][key])
        for candidate in value["candidates"]:
            key = (candidate["source"], candidate["text"])
            candidates[key] = min(MAX_COUNT, candidates.get(key, 0) + candidate["count"])
    if len(candidates) > MAX_CANDIDATES:
        reasons.add("collection-limit")
    else:
        report["candidates"] = [
            {"source": source, "text": text, "count": count}
            for (source, text), count in sorted(candidates.items(), key=lambda item: (SOURCES.index(item[0][0]), item[0][1]))
        ]
    if not report["windows"]["checked"] and not reasons:
        reasons.add("no-main-window")
    try:
        _serialize(report)
    except (ValueError, UnicodeError):
        reasons.add("collection-limit")
        report["candidates"] = []
    return CollectionResult(report, tuple(sorted(reasons)))


def _save_report(report, filename):
    temporary = None
    try:
        data = _serialize(report)
        with tempfile.NamedTemporaryFile(mode="wb", dir=ROOT, prefix=f".{filename}.", suffix=".tmp", delete=False) as file:
            temporary = file.name
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, ROOT / filename)
        temporary = None
        return True
    except (OSError, ValueError, UnicodeError):
        return False
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _port(value):
    try:
        number = int(value)
        if not 1 <= number <= 65535:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("端口必须为 1 至 65535 的整数。") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Antigravity 只读界面来源统计（默认不导出原文）")
    parser.add_argument("--port", type=_port, default=None, help="明确选择已验证调试端口；默认自动发现")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--misses", action="store_true", help="仅导出新版引擎未命中的来源统计")
    mode.add_argument("--include-ui-text", action="store_true", help="仅本地保存已审计静态 UI 标签，提交前仍须人工核查")
    args = parser.parse_args(argv)
    if args.include_ui_text:
        print("[提醒] 仅本地保存已审计静态 UI 候选；过滤不能替代人工核查，不要提交采集产物。", file=sys.stderr)
    from hanhua_v2 import _bypass_proxy_for_local_cdp
    _bypass_proxy_for_local_cdp()
    result = collect(args.port, args.include_ui_text, args.misses)
    windows = result.report["windows"]
    print(f"[窗口] 已检查 {windows['checked']}；已跳过 {windows['skipped']}；未完成 {windows['failed']}")
    print(f"[来源] 共 {sum(result.report['sources'].values())} 次许可来源观测；不是唯一英文文案数")
    if not result.ok:
        for reason in result.reasons:
            print(f"[状态] {_safe_reason(reason)}")
        if "engine-upgrade-required" in result.reasons:
            print("[建议] 旧引擎原文记录不会读取；请手动运行 python hanhua_v2.py --no-launch 后重试。")
        print("失败，原有文件未覆盖。")
        return 1
    filename = "misses.json" if args.misses else "pending.json"
    if not _save_report(result.report, filename):
        print("[错误] 无法保存统计。失败，原有文件未覆盖。")
        return 1
    if args.include_ui_text:
        print(f"[候选] 本地保存 {len(result.report['candidates'])} 条已审计静态 UI 标签；未在控制台输出原文")
    else:
        print("[隐私] 本次文件仅保存来源统计，不包含界面原文。")
    print(f"[输出] {filename}（本机采集产物，不要提交）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
