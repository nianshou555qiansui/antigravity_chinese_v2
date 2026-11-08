"""可选的安全注入守护：只复用已验证的调试端口，不启动或重启应用。"""
import argparse
import asyncio
import os
import subprocess
import sys
import time

import hanhua_v2 as hh
import guard_runtime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_PATH = os.path.join(SCRIPT_DIR, 'shouhu.lock')
LOG_PATH = os.path.join(SCRIPT_DIR, 'shouhu.log')
LOG_MAX = 200 * 1024
LNK_NAME = 'Antigravity HanHua Guard.lnk'

# pythonw 没有控制台，子进程（tasklist/taskkill/powershell）会各自弹黑窗，
# 后台轮询不能闪黑窗。CREATE_NO_WINDOW 是唯一正确的组合。
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

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


def watch(port=None):
    dictionary = hh.load_dictionary()
    if not dictionary:
        log("词典为空，守护未启动；请运行 --doctor 检查。")
        return
    engine = hh.build_engine_js(dictionary)
    try:
        asyncio.run(guard_runtime.watch(engine, port=port, on_status=log))
    except KeyboardInterrupt:
        log("收到中断，守护退出")


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
    print("    自动发现已有调试端口并注入；不会关闭、启动或重启 Antigravity。")
    print("    没有调试端口时，请保存工作后手动运行 hanhua_v2.py --force-restart。")
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
    parser.add_argument('--port', type=int, help='仅连接指定端口；省略则自动发现')
    parser.add_argument('--verbose', action='store_true', help='前台运行并打印日志')
    parser.add_argument('--autostart', choices=['on', 'off', 'status'],
                        help='开关登录自启（也可用 hanhua_v2.py --autostart）')
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error('--port 必须在 1 到 65535 之间')

    if args.autostart:
        {'on': autostart_on, 'off': autostart_off,
         'status': autostart_status}[args.autostart]()
        return

    VERBOSE = args.verbose
    # 守护是独立入口，不走 hanhua_v2.main()：自己把本机 CDP 加进 NO_PROXY，
    # 否则用户的 HTTP_PROXY 会让 websockets 把 ws://127.0.0.1 打进代理而连不上。
    hh._bypass_proxy_for_local_cdp()
    if not acquire_lock():
        sys.exit(0)
    log(f"安全守护启动（端口 {args.port or '自动发现'}，PID {os.getpid()}）")
    try:
        watch(args.port)
    finally:
        release_lock()
        log("守护退出")


if __name__ == '__main__':
    main()
