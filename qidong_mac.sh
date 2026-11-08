#!/bin/bash
# Antigravity 2.0 HanHua Launcher (macOS)
# Reuse a verified instance, or start one when none is running.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

echo
echo "=========================================="
echo "  Antigravity 2.0 汉化启动器 (macOS)"
echo "=========================================="

echo
echo "[1/2] 安全探测 Antigravity 并注入翻译引擎..."
echo "      请保持本窗口打开，直到出现 [成功]。"
if python3 "$DIR/hanhua_v2.py" "$@"; then
    echo "[2/2] Done!"
else
    result=$?
    echo "[错误] 命令失败或需要处理，请查看上方提示。"
    exit "$result"
fi

echo
echo "[Note] 命令已完成，注入或诊断结果请以上方输出为准。"
echo
