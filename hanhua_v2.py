#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Antigravity 2.0 运行时汉化工具 V1.0 —— CDP 注入版

与旧版（AntigravityHanHua_GongJu.py）的区别：
- 旧版针对 1.x 解包结构（resources\\app\\out\\...\\workbench.html 文件注入 + checksum 同步），
  在 2.0 上失效（2.0 打包为 app.asar，界面改为本地 HTTP 服务，见 GitHub Issue #4）。
- 本版不修改任何安装文件：通过 --remote-debugging-port 启动 Antigravity，
  连 DevTools 协议（CDP）把翻译引擎注入渲染页面。

纯 Python 标准库 + 本机已装 websockets 库，无其它依赖。
"""
import os
import sys
import json
import time
import re
import argparse
import subprocess
import http.client
from pathlib import Path
import cdp_discovery
import cdp_targets
import privacy_policy

# ★★★ 配置 ★★★
DEFAULT_PORT = 9333
# Clash / 系统代理会劫持 127.0.0.1 的 CDP HTTP/WebSocket，必须直连
_LOCAL_NO_PROXY = "127.0.0.1,localhost,::1"


def _bypass_proxy_for_local_cdp():
    """本机 CDP 走直连，避免 HTTP(S)_PROXY 把 9333 打到代理。"""
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    for host in _LOCAL_NO_PROXY.split(","):
        if host not in parts:
            parts.append(host)
    merged = ",".join(parts)
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged


def _mask_proxy(url):
    """代理地址可能内嵌 user:pass@，日志常被贴到 issue，凭据打码。"""
    if not url:
        return None
    return re.sub(r"//([^:/@\s]+):([^@\s]+)@", r"//\1:***@", url)


def _log_proxy():
    http_p = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    https_p = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if http_p or https_p:
        print(f"[代理] HTTP_PROXY={_mask_proxy(http_p)} HTTPS_PROXY={_mask_proxy(https_p)}")
        print("[代理] 本机 CDP 已加入 NO_PROXY，不走代理")


def candidate_paths():
    """候选安装路径（按优先级）。

    Windows 用户级安装目录必须从 %LOCALAPPDATA% 取，不能硬编码 C:\\Users\\——
    用户配置文件目录不在 C 盘时（企业环境、多盘位机器）硬编码会直接找不到。
    """
    if sys.platform == 'darwin':
        return [
            '/Applications/Antigravity.app/Contents/MacOS/Antigravity',
            os.path.expanduser('~/Applications/Antigravity.app/Contents/MacOS/Antigravity'),
        ]
    local = os.environ.get('LOCALAPPDATA') or os.path.expanduser(r'~\AppData\Local')
    return [
        os.path.join(local, 'Programs', 'antigravity', 'Antigravity.exe'),
        r"D:\Antigravity\Antigravity.exe",
        r"C:\Program Files\Antigravity\Antigravity.exe",
        r"C:\Program Files (x86)\Antigravity\Antigravity.exe",
    ]


def normalize_text(text):
    """压缩空白 + 全角引号归一为半角（与旧版一致）"""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    return text


def load_dictionary():
    """合并 dicts/*.json 为一个 map（与旧版一致）"""
    total_map = {}
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dicts_dir = os.path.join(script_dir, 'dicts')
    if os.path.exists(dicts_dir):
        for filename in sorted(os.listdir(dicts_dir)):
            if filename.endswith(".json"):
                try:
                    with open(os.path.join(dicts_dir, filename), 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        for k, v in data.items():
                            norm_k = normalize_text(k)
                            if norm_k:
                                total_map[norm_k] = v
                except Exception as e:
                    print(f"  [跳过] {filename}: {e}")
    return total_map


def _load_engine_template():
    """Load the editable source asset, with PyInstaller's bundled-data fallback."""
    candidates = [Path(__file__).resolve().with_name("engine_template.js")]
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / "engine_template.js")
    for path in candidates:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            continue
    raise FileNotFoundError("engine_template.js is missing beside the application")


def build_engine_js(dict_map):
    """构造注入的翻译引擎 IIFE（复用旧版 MutationObserver 思路，禁区按 2.0 调整）

    2.0 是对话型 AI IDE，保护区重点是：代码/编辑器/输入区/对话消息内容。
    去掉旧版 Monaco 专属类（monaco-editor / suggest-widget / artifact-container 等）。
    """
    # 字典只序列化一次：allEntries 由 JS 从 map 现算即可。
    # 之前额外塞了一份排好序的 entries 数组，等于把整份字典编码两遍，
    # 注入 payload 白白翻倍（92 KB → 46 KB）。紧凑分隔符再省一截。
    dict_json = json.dumps(dict_map, ensure_ascii=False, separators=(',', ':'))

    js_source = _load_engine_template()
    return js_source.replace("PRIVACY_PLACEHOLDER", privacy_policy.build_policy_js()).replace("DICT_PLACEHOLDER", dict_json)


def find_antigravity_exe(install_dir=None):
    """定位 Antigravity 可执行文件，返回路径或 None"""
    if install_dir:
        cand = install_dir
        if os.path.isfile(cand):
            return cand
        for sub in [
            'Contents/MacOS/Antigravity',
            'Antigravity',
            'Antigravity.exe',
        ]:
            exe = os.path.join(cand, sub)
            if os.path.exists(exe):
                return exe
        return None
    for cand in candidate_paths():
        if os.path.exists(cand):
            return cand
    return None


def http_json(host, port, path):
    """GET http://host:port/path → dict/list，失败抛异常"""
    conn = http.client.HTTPConnection(host, port, timeout=3)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        return json.loads(body)
    finally:
        conn.close()


def launch_antigravity(exe, port):
    """启动 Antigravity（带调试端口），返回进程句柄"""
    print(f"[启动] {exe} --remote-debugging-port={port}")
    try:
        kwargs = {
            'stdout': subprocess.DEVNULL,
            'stderr': subprocess.DEVNULL,
        }
        if sys.platform == 'win32':
            kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['start_new_session'] = True
        return subprocess.Popen(
            [exe, f"--remote-debugging-port={port}"],
            **kwargs
        )
    except Exception as e:
        print(f"[错误] 启动失败: {e}")
        return None


def is_antigravity_running():
    """探测失败必须报错，不能误当作没有进程而重复启动。"""
    return bool(cdp_discovery.antigravity_pids())


def _confirm_restart(assume_yes):
    print("[警告] 重启将关闭当前所有 Antigravity 实例，未保存的编辑可能丢失。")
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        return input("保存工作后输入 RESTART 确认重启（其他输入取消）: ").strip() == "RESTART"
    except (EOFError, KeyboardInterrupt):
        return False


def prepare_instance(args):
    instance = cdp_discovery.find_running_instance(args.port)
    port = args.port or DEFAULT_PORT
    if args.force_restart and instance.pids:
        exe = find_antigravity_exe(args.install_dir)
        if not exe:
            raise cdp_discovery.DiscoveryError("未找到可执行文件，取消重启；请指定 --install-dir。")
        # 非当前已验证的 CDP 端口先检查，避免关掉应用才发现无法重启。
        if not instance.endpoint or instance.endpoint.port != port:
            cdp_discovery.require_free_port(port)
        if not _confirm_restart(args.yes):
            raise cdp_discovery.DiscoveryError("重启已取消，应用未被关闭。")
        cdp_discovery.stop_confirmed_instance(instance.pids)
    elif instance.endpoint:
        print(f"[复用] 已发现 Antigravity 调试端口 {instance.endpoint.port}，不重启应用。")
        return instance.endpoint.port, instance.page
    elif instance.pids:
        raise cdp_discovery.DiscoveryError(
            "Antigravity 已在运行，但未找到符合条件的调试端口。应用保持不变。\n"
            "       请用 --port 指定端口，或保存工作后运行 --force-restart 并确认。")
    elif args.no_launch:
        raise cdp_discovery.DiscoveryError("未找到可复用的 Antigravity 实例（--no-launch 不会启动应用）。")

    exe = find_antigravity_exe(args.install_dir)
    if not exe:
        raise cdp_discovery.DiscoveryError("未找到 Antigravity，请用 --install-dir 指定安装目录或 .app。")
    cdp_discovery.require_free_port(port)
    # 确认期间或发现之后若又有应用启动，不能再开重复实例。
    if cdp_discovery.antigravity_pids():
        raise cdp_discovery.DiscoveryError("检测到新的 Antigravity 实例，请重试以安全复用。")
    if launch_antigravity(exe, port) is None:
        raise cdp_discovery.DiscoveryError("Antigravity 启动失败，请检查安装路径与权限。")
    return port, None


def main():
    parser = argparse.ArgumentParser(description="Antigravity 2.0 运行时汉化工具（CDP 注入）")
    parser.add_argument("--install-dir", help="Antigravity 安装目录或 Antigravity.exe 路径（默认自动探测）")
    parser.add_argument("--port", type=int, help=f"指定调试端口；省略则自动发现，首次启动用 {DEFAULT_PORT}")
    parser.add_argument("--no-launch", action="store_true",
                        help="不启动 Antigravity，只连已有调试端口注入（需已带端口启动）")
    parser.add_argument("--watch", type=int,
                        help="注入后守护多少秒（页面重载会自动补注，0=不守护，默认 30）")
    diagnostic = parser.add_mutually_exclusive_group()
    diagnostic.add_argument("--status", action="store_true", help="只读查看版本、端口、窗口、引擎及词典状态")
    diagnostic.add_argument("--doctor", action="store_true", help="只读诊断并给出处理建议，不启动或修改应用")
    parser.add_argument("--autostart", choices=["on", "off", "status"],
                        help="自动注入开关：登录自启守护进程，只复用已有调试端口")
    parser.add_argument("--force-restart", action="store_true", help="保存工作后，确认关闭已有实例并重启")
    parser.add_argument("--yes", action="store_true", help="与 --force-restart 同用，明确确认可能丢失未保存内容")
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    if args.force_restart and (args.no_launch or args.autostart):
        parser.error("--force-restart 不能与 --no-launch / --autostart 同用")
    if args.yes and not args.force_restart:
        parser.error("--yes 只能与 --force-restart 同用")
    if args.watch is not None and args.watch < 0:
        parser.error("--watch 不能为负数")
    if args.status or args.doctor:
        if args.force_restart or args.yes or args.autostart or args.watch is not None or args.install_dir:
            parser.error("--status / --doctor 不能与重启、自启、--watch 或 --install-dir 同用")
        import diagnostics
        _bypass_proxy_for_local_cdp()
        sys.exit(diagnostics.run(port=args.port, doctor=args.doctor))
    if args.watch is None:
        args.watch = 30

    if args.autostart:
        # 委托给 shouhu.py（启动项指向它，开关逻辑也放在它那边）
        import shouhu
        {"on": shouhu.autostart_on, "off": shouhu.autostart_off,
         "status": shouhu.autostart_status}[args.autostart]()
        return

    print("=== Antigravity 2.0 运行时汉化工具（CDP 注入） ===")
    _bypass_proxy_for_local_cdp()
    _log_proxy()

    # 1. 加载字典
    dict_map = load_dictionary()
    print(f"[字典] 共 {len(dict_map)} 条翻译规则")
    if not dict_map:
        print("[错误] 未加载到任何字典，请检查 dicts/ 目录")
        sys.exit(1)

    # 只翻译经逐窗口身份检查的主界面；观察期内持续发现新窗口。
    try:
        port, _ = prepare_instance(args)
        print(f"[连接] 已验证调试端口 {port}，正在识别所有主界面窗口...")
        if args.watch > 0:
            print(f"[观察] 首个窗口成功后继续观察 {args.watch} 秒，为新窗口和重载页面补注。")
        report = cdp_targets.inject_windows(
            port, build_engine_js(dict_map), watch_seconds=args.watch)
    except cdp_discovery.DiscoveryError as exc:
        print(f"[错误] {exc}")
        sys.exit(1)
    reinjections = sum(max(0, count - 1) for count in report.injections.values())
    print(f"[窗口] 成功 {report.successful}；跳过 {report.skipped}；未完成 {report.failed}；补注 {reinjections}")
    for result in report.results:
        if result.status in ("error", "pending"):
            print(f"[窗口未完成] {result.reason}")
    if not report.ok:
        print("[错误] 没有已确认且注入成功的主窗口，或部分窗口未完成；未强行注入。")
        sys.exit(1)
    print("[成功] 所有当前已确认的主界面窗口均已注入；其他网页未处理。")
    print("[提示] 观察期结束后新开的窗口，请重跑启动器或使用安全守护。")


if __name__ == "__main__":
    main()
