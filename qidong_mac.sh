#!/bin/bash
# Antigravity 2.0 HanHua Launcher (macOS)
# Close an instance that has no debug port, then start + inject.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

echo
echo "=========================================="
echo "  Antigravity 2.0 汉化启动器 (macOS)"
echo "=========================================="

export NO_PROXY="127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"

if pgrep -x Antigravity >/dev/null 2>&1; then
    if curl -s -m 1 "http://127.0.0.1:9333/json" >/dev/null 2>&1; then
        echo "[提示] 已有带调试端口的实例，直接注入..."
    else
        echo "[1/3] 正在关闭未开调试端口的 Antigravity..."
        echo "      （未保存的编辑会丢失）"
        killall Antigravity >/dev/null 2>&1 || true
        sleep 2
    fi
fi

echo
echo "[2/3] 启动 Antigravity 并注入翻译引擎..."
echo "      请保持本窗口打开，直到出现 [成功]。"
python3 "$DIR/hanhua_v2.py" "$@"

echo
echo "[3/3] Done!"
echo "[Note] 翻译引擎已在内存中。关掉本窗口不影响汉化；下次启动需重新运行。"
echo
