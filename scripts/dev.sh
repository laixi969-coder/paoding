#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
uv sync --frozen
if [ ! -f .env ]; then cp .env.example .env; fi
docker compose up -d --wait postgres
uv run paoding init
echo "初始化完成。在项目目录运行 'uv run paoding open' 即可打开工作台。"
