"""shouhu.py — 自动注入守护进程（可选功能）。

登录自启（hanhua_v2.py --autostart on）后常驻后台：
  - 检测到带调试端口的 Antigravity → 引擎不在就注入（复用 cdp_inject，不另写注入路径）
  - 检测到无调试端口的 Antigravity（正常双击启动）→ 关掉并带端口重新拉起

安全阀（防杀重启死循环）：
  - 自己拉起进程后 LAUNCH_WAIT 秒内不再杀（等端口出现）
  - 端口活着（哪怕页面没就绪）绝不杀
  - 两次杀重启至少间隔 KILL_GRACE 秒
  - 只累计「失败」的杀重启（注入成功即清零），FAIL_WINDOW 内第
    FAIL_LIMIT 次连续失败 → 守护自动退出并写日志
"""
import argparse
import json
import os
import subprocess
import sys
import time

import hanhua_v2 as hh

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_PATH = os.path.join(SCRIPT_DIR, 'shouhu.lock')
LOG_PATH = os.path.join(SCRIPT_DIR, 'shouhu.log')
LOG_MAX = 200 * 1024
LNK_NAME = 'Antigravity HanHua Guard.lnk'

# pythonw 没有控制台，子进程（tasklist/taskkill/powershell）会各自弹黑窗，
# 轮询每 2 秒一次就成了无限闪屏。CREATE_NO_WINDOW 是唯一正确的组合。
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

POLL = 0.3
LAUNCH_WAIT = 30.0
KILL_GRACE = 15.0
FAIL_WINDOW = 600.0
FAIL_LIMIT = 3

# ---------- 进程存在检查：WinAPI 直查（~1ms），不起 tasklist 子进程 ----------
if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes

    _psapi = ctypes.WinDLL('psapi')
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def is_running_fast():
        """有没有叫 Antigravity.exe 的进程。轮询每 0.3 秒跑一次，
        必须零子进程零窗口：tasklist 一次几十毫秒，这里 ~1ms。"""
        arr = (wintypes.DWORD * 2048)()
        needed = wintypes.DWORD(0)
        if not _psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
            return hh.is_antigravity_running()  # API 失败退回子进程方案
        count = needed.value // ctypes.sizeof(wintypes.DWORD)
        for i in range(count):
            pid = arr[i]
            if pid == 0:
                continue
            h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                continue
            try:
                buf = ctypes.create_unicode_buffer(1024)
                sz = wintypes.DWORD(1024)
                if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz)):
                    if buf.value.lower().endswith('\\antigravity.exe'):
                        return True
            finally:
                _kernel32.CloseHandle(h)
        return False
else:
    is_running_fast = hh.is_antigravity_running

VERBOSE = False


def log(msg):
    line = time.strftime('[%Y-%m-%d %H:%M:%S] ') + msg
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX:
            with open(LOG_PATH, 'w', encoding='utf-8') as f:
                f.write('')  # 超限截断，重新记
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass
    if VERBOSE:
        print(line, flush=True)


def pid_alive(pid):
    """tasklist 输出 GBK，按字节匹配避免解码崩溃（同 is_antigravity_running 的坑）。"""
    try:
        out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'],
                             capture_output=True, timeout=5,
                             creationflags=NO_WINDOW).stdout or b''
        return str(pid).encode() in out
    except Exception:
        return False


def acquire_lock():
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, encoding='utf-8') as f:
                old = int(f.read().strip() or 0)
            if old and old != os.getpid() and pid_alive(old):
                print(f"[守护] 已有实例在运行（PID {old}），本实例退出。")
                return False
        except (OSError, ValueError):
            pass  # 锁文件损坏视为无锁
    with open(LOCK_PATH, 'w', encoding='utf-8') as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        if os.path.exists(LOCK_PATH):
            with open(LOCK_PATH, encoding='utf-8') as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(LOCK_PATH)
    except OSError:
        pass


def kill_antigravity():
    subprocess.run(['taskkill', '/F', '/T', '/IM', 'Antigravity.exe'],
                   capture_output=True, timeout=15, creationflags=NO_WINDOW)
    # 等进程真的消失再重启（通常 0.2~0.4 秒），别写死 sleep 2
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not is_running_fast():
            break
        time.sleep(0.15)


def engine_alive(page):
    """查页面里 window.__ag_hanhua_engine__ 是否存活。任何异常按不存活处理。"""
    import asyncio
    import websockets

    async def run():
        async with websockets.connect(page['webSocketDebuggerUrl'], open_timeout=3,
                                      max_size=1024 * 1024) as ws:
            await ws.send(json.dumps({
                'id': 1, 'method': 'Runtime.evaluate',
                'params': {'expression': '!!window.__ag_hanhua_engine__',
                           'returnByValue': True}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get('id') == 1:
                    return msg.get('result', {}).get('result', {}).get('value') is True

    try:
        return asyncio.run(run())
    except Exception:
        return False


def port_answers(port):
    """调试端口有没有在听（页面就绪与否不管）。端口活着说明应用启动成功，绝不杀。"""
    try:
        hh.http_json('127.0.0.1', port, '/json/version')
        return True
    except Exception:
        return False


def watch(port):
    pending_until = 0.0     # 自己拉起的进程在等端口出现，期间绝不杀
    last_kill = 0.0
    kill_times = []         # 只累计「失败」的杀重启：注入成功即清零
    exe = hh.find_antigravity_exe()

    while True:
        try:
            page = hh.wait_for_debug_port(port, timeout=0.5)
            if page:
                pending_until = 0.0
                kill_times = []  # 端口通、页面在——上一轮（若有）显然成功了
                if not engine_alive(page):
                    log(f"注入引擎（端口 {port}）")
                    hh.cdp_inject(page, hh.build_engine_js(hh.load_dictionary()),
                                  watch_seconds=8)
            elif port_answers(port):
                pending_until = 0.0  # 端口活着但页面没就绪，等它，不杀
            elif pending_until > time.time():
                pass  # 等自己拉起的端口
            elif is_running_fast():
                now = time.time()
                if now - last_kill < KILL_GRACE:
                    pass  # 冷却期内不动
                else:
                    if not exe:
                        exe = hh.find_antigravity_exe()
                    if not exe:
                        log("未找到 Antigravity，60 秒后重试")
                        time.sleep(60)
                        continue
                    log("检测到无调试端口的实例，关闭并带端口重启")
                    kill_antigravity()
                    if hh.launch_antigravity(exe, port) is not None:
                        pending_until = now + LAUNCH_WAIT
                        last_kill = now
                        kill_times.append(now)
                        kill_times = [t for t in kill_times if now - t < FAIL_WINDOW]
                        if len(kill_times) >= FAIL_LIMIT:
                            log(f"{FAIL_WINDOW/60:.0f} 分钟内已杀重启 {len(kill_times)} 次，"
                                "疑似启动异常，守护退出。请查看应用能否正常启动，"
                                "处理后用 --autostart on 重新开启。")
                            return
                    else:
                        log("重启失败，冷却后重试")
                        last_kill = now
        except KeyboardInterrupt:
            log("收到中断，守护退出")
            return
        except Exception as e:
            log(f"循环异常（继续运行）: {type(e).__name__}: {e}")
        time.sleep(POLL)


# ---------- --autostart 开关（由 hanhua_v2.py 调用） ----------

def startup_lnk_path():
    base = os.environ.get('APPDATA') or os.path.expanduser('~/AppData/Roaming')
    return os.path.join(base, 'Microsoft', 'Windows', 'Start Menu',
                        'Programs', 'Startup', LNK_NAME)


def autostart_on():
    pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    if not os.path.exists(pythonw):
        print(f"[错误] 未找到 {pythonw}，无法创建无窗口守护。")
        sys.exit(1)
    lnk = startup_lnk_path()
    target = os.path.join(SCRIPT_DIR, 'shouhu.py')
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        "$s.TargetPath='{pyw}';"
        "$s.Arguments='\"{target}\"';"
        "$s.WorkingDirectory='{wd}';"
        "$s.Description='Antigravity HanHua Guard';"
        "$s.Save()".format(lnk=lnk, pyw=pythonw, target=target, wd=SCRIPT_DIR)
    )
    r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                       capture_output=True, timeout=30, creationflags=NO_WINDOW)
    if r.returncode != 0:
        print(f"[错误] 创建启动项失败: {r.stderr.decode('gbk', 'replace')}")
        sys.exit(1)
    print(f"[√] 已开启自动注入：登录后自动守护（{lnk}）")
    print("    反重力无论怎么启动，都会自动带调试端口重启并注入；")
    print("    正常双击启动时会先关再开（约 2 秒），未保存的编辑会丢。")
    print("    立即启动守护：pythonw shouhu.py（或重启电脑生效）")


def autostart_off():
    lnk = startup_lnk_path()
    removed = False
    if os.path.exists(lnk):
        os.remove(lnk)
        removed = True
    stopped = stop_running()
    if removed or stopped:
        print("[√] 已关闭自动注入" + ("（启动项已移除）" if removed else "")
              + ("（守护进程已停止）" if stopped else ""))
    else:
        print("[提示] 自动注入本来就没开")


def stop_running():
    """停掉在跑的守护进程，返回是否真的停了一个。"""
    if not os.path.exists(LOCK_PATH):
        return False
    try:
        with open(LOCK_PATH, encoding='utf-8') as f:
            pid = int(f.read().strip() or 0)
        if pid and pid != os.getpid() and pid_alive(pid):
            subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                           capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
            return True
    except (OSError, ValueError):
        pass
    return False


def autostart_status():
    lnk = startup_lnk_path()
    print(f"启动项: {'已开启' if os.path.exists(lnk) else '未开启'}")
    alive = False
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, encoding='utf-8') as f:
                pid = int(f.read().strip() or 0)
            alive = pid and pid_alive(pid)
            print(f"守护进程: {'运行中（PID %d）' % pid if alive else '未运行'}")
        except (OSError, ValueError):
            print("守护进程: 未运行（锁文件损坏）")
    else:
        print("守护进程: 未运行")
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
        print("日志尾部:")
        for line in lines[-5:]:
            print("  " + line)


def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="Antigravity 汉化守护进程")
    parser.add_argument('--port', type=int, default=hh.DEFAULT_PORT)
    parser.add_argument('--verbose', action='store_true', help='前台运行并打印日志')
    parser.add_argument('--autostart', choices=['on', 'off', 'status'],
                        help='开关登录自启（也可用 hanhua_v2.py --autostart）')
    args = parser.parse_args()

    if args.autostart:
        {'on': autostart_on, 'off': autostart_off,
         'status': autostart_status}[args.autostart]()
        return

    VERBOSE = args.verbose
    if not acquire_lock():
        sys.exit(0)
    log(f"守护启动（端口 {args.port}，PID {os.getpid()}）")
    try:
        watch(args.port)
    finally:
        release_lock()
        log("守护退出")


if __name__ == '__main__':
    main()
