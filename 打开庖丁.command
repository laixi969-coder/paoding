#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/paoding ]; then
    echo "请先在项目目录按 README 的「正式本地运行」完成初始化，再双击打开。"
    exit 1
fi
exec .venv/bin/paoding open
