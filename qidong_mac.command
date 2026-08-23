#!/bin/bash
DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
cd "$DIR"
exec ./qidong_mac.sh "$@"
