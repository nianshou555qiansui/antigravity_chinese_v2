#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Antigravity 2.0 界面文本采集器

连 CDP 端口，抓取当前页面的所有英文 UI 文本（含 shadow DOM、ARIA 标签），
过滤掉邮箱/URL/路径/时间戳/单字符等非 UI 文案，输出到 pending.json。

注意：过滤是尽力而为，不是保证——JS 侧 isUsableEnglish 与 Python 侧 SENSITIVE
两道过滤都可能在界面改版后失效。pending.json 是某台机器某一次的界面快照，
可能仍夹带会话标题等本机数据，已在 .gitignore 里排除，不要提交。

用法：python caiji.py [--port 9333]           # 扫当前页面未翻译英文 → pending.json
      python caiji.py --misses [--port 9333]  # 拉取引擎记下的未命中英文 → misses.json
"""
import os
import sys
import json
import re
import argparse
import http.client

# 二次过滤（Python 侧兜底）：JS 侧 isUsableEnglish 漏网的个人数据在这里再拦一道，
# 宁可错杀不可漏放——拦下的内容不落盘也不上屏
SENSITIVE = re.compile(
    r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}'   # 邮箱
    r'|https?://'                                        # URL
    r'|[A-Za-z]:[\\/]'                                   # C:\ 绝对路径
    r'|[~/][A-Za-z0-9._-]*/'                             # /home/... ~/... 路径
)

DEFAULT_PORT = 9333


def http_json(host, port, path):
    conn = http.client.HTTPConnection(host, port, timeout=3)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return json.loads(resp.read())
    finally:
        conn.close()


def get_page(port):
    """取真正的主界面页。

    必须筛掉 data: 加载页——冷启动时它先出现，连上去只会抓到空白。
    连接失败时给出可读的原因而不是裸 traceback。
    """
    try:
        pages = http_json('127.0.0.1', port, '/json')
    except (OSError, ValueError) as e:
        print(f"[错误] 连不上调试端口 {port}: {e}")
        return None
    return next((p for p in pages
                 if p.get('type') == 'page' and p.get('url', '').startswith('http')), None)


MISS_EXPR = r"""
(() => {
    try {
        const m = window.__ag_hanhua_engine__ && window.__ag_hanhua_engine__.misses;
        return m ? [...m] : null;
    } catch (e) { return null; }
})()
"""


def collect_misses(port):
    """拉取引擎的词条漂移日志（注入后界面上出现过但没翻出来的英文）。

    需要新版引擎；引擎未注入或版本过旧返回 None。
    """
    import asyncio
    import websockets

    page = get_page(port)
    if not page:
        return None
    ws_url = page['webSocketDebuggerUrl']

    async def run():
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                      "params": {"expression": MISS_EXPR,
                                                 "returnByValue": True}}))
            while True:
                resp = json.loads(await ws.recv())
                if resp.get('id') == 1:
                    break
            val = (resp.get('result', {}).get('result', {}) or {}).get('value')
            return val if isinstance(val, list) else None

    return asyncio.run(run())


def collect(port):
    import asyncio
    import websockets

    page = get_page(port)
    if not page:
        return None

    scan_expr = r"""
(() => {
    const seen = new Set();
    const attrs = new Set();

    const isUsableEnglish = (s) => {
        s = s.trim();
        if (!s || s.length < 3 || s.length > 120) return false;   // 单字符按键提示不是 UI 文案
        if (!/[A-Za-z]/.test(s)) return false;
        if (/[一-鿿]/.test(s)) return false;
        if (!/[A-Za-z]{2,}/.test(s)) return false;                // 至少要有一个像单词的东西
        // 个人数据：邮箱、URL、绝对路径 —— 采到就会跟着 pending.json 泄露出去
        if (/@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/.test(s)) return false;   // 邮箱
        if (/^https?:\/\//.test(s)) return false;                     // URL
        if (/^[A-Za-z]:[\\/]/.test(s)) return false;                  // C:\... 绝对路径
        if (/^[~/][A-Za-z0-9._-]*\//.test(s)) return false;           // /home/... ~/... 路径
        // 纯数字/时间/百分比/样式
        if (/^[\d\s:./%+-]+$/.test(s)) return false;
        if (/^\d+[dhms]$/i.test(s)) return false;                     // 4d、1h 时间戳
        if (/[{};]/.test(s)) return false;                            // CSS/代码片段
        if (/^[\w-]+\.(tsx?|jsx?|py|json|css|html|md|go|rs|toml|ya?ml)$/.test(s)) return false;
        return true;
    };

    function walk(root) {
        if (!root) return;
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let n;
        while (n = walker.nextNode()) {
            const t = (n.nodeValue || '').replace(/\s+/g, ' ').trim();
            if (isUsableEnglish(t)) seen.add(t);
        }
        for (const el of root.querySelectorAll('*')) {
            for (const a of ['placeholder', 'title', 'aria-label',
                             'data-title', 'data-tooltip-content']) {
                const v = el.getAttribute(a);
                if (v && isUsableEnglish(v)) attrs.add(v.trim());
            }
            if (el.shadowRoot) walk(el.shadowRoot);
        }
    }
    walk(document.documentElement);
    return { texts: [...seen], attrs: [...attrs] };
})()
"""

    ws_url = page['webSocketDebuggerUrl']

    async def run():
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                      "params": {"expression": scan_expr, "returnByValue": True}}))
            # CDP 会穿插推送无 id 的事件消息，循环跳过、只认自己这条请求的回包
            while True:
                resp = json.loads(await ws.recv())
                if resp.get('id') == 1:
                    break
            if resp.get('result', {}).get('result', {}).get('value'):
                return resp['result']['result']['value']
            return None

    return asyncio.run(run())


def _bypass_proxy_for_local_cdp():
    """本机 CDP 走直连。caiji 是独立入口，不走 hanhua_v2.main()，必须自己调。

    websockets 的 proxy 参数默认 True，会读 HTTP_PROXY/no_proxy；用户把代理写进
    系统环境变量时，连 ws://127.0.0.1 会被打进代理而失败（实测 ConnectionRefused）。
    """
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    hosts = [h.strip() for h in existing.split(",") if h.strip()]
    for host in ("127.0.0.1", "localhost", "::1"):
        if host not in hosts:
            hosts.append(host)
    merged = ",".join(hosts)
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged


def main():
    parser = argparse.ArgumentParser(description="Antigravity 2.0 界面文本采集器")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--misses", action="store_true",
                        help="拉取引擎记录的未命中英文（官方更新后查词条漂移），写入 misses.json")
    args = parser.parse_args()

    _bypass_proxy_for_local_cdp()

    if args.misses:
        print(f"[连接] 拉取端口 {args.port} 引擎的未命中日志...")
        data = collect_misses(args.port)
        if data is None:
            print("[错误] 引擎未注入或版本过旧（misses 日志需要新版引擎）。")
            print("       先运行 python hanhua_v2.py 注入后再试。")
            sys.exit(1)
        dropped = sum(1 for s in data if SENSITIVE.search(s))
        if dropped:
            print(f"[过滤] 二次过滤拦下 {dropped} 条疑似个人数据（不显示、不落盘）")
            data = [s for s in data if not SENSITIVE.search(s)]
        data = sorted(set(data), key=len)
        print(f"[未命中] 共 {len(data)} 条英文没翻出来")
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'misses.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[输出] 已写入 {out_path}（已在 .gitignore 里，不要提交）")
        for item in data:
            print(f"  {item}")
        return

    print(f"[连接] 采集端口 {args.port} 的当前页面英文文本...")
    data = collect(args.port)
    if data is None:
        print("[错误] 未获取到数据。确认 Antigravity 正带调试端口运行？")
        sys.exit(1)

    # 合并去重，按长度排序；二次过滤在写盘/上屏之前做
    all_items = sorted(set(data['texts'] + data['attrs']), key=len)
    dropped = sum(1 for s in all_items if SENSITIVE.search(s))
    if dropped:
        print(f"[过滤] 二次过滤拦下 {dropped} 条疑似个人数据（不显示、不落盘）")
        all_items = [s for s in all_items if not SENSITIVE.search(s)]
    print(f"[采集] 共 {len(all_items)} 条未翻译英文")

    # 写入 pending.json
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pending.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)
    print(f"[输出] 已写入 {out_path}")
    print("\n--- 采集到的文本 ---")
    for item in all_items:
        print(f"  {item}")


if __name__ == "__main__":
    main()
