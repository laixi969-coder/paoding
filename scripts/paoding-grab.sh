#!/bin/bash
# @raycast.schemaVersion 1
# @raycast.title 庖丁收录当前页面
# @raycast.mode compact
# @raycast.packageName 庖丁
# @raycast.description 将浏览器当前页面送入庖丁任务队列
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
exec .venv/bin/paoding grab
