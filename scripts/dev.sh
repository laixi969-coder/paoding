#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
uv sync --frozen
if [ ! -f .env ]; then cp .env.example .env; fi
docker compose up -d --wait postgres
uv run paoding init
echo "Ready. Run 'uv run paoding serve' and 'uv run paoding worker' in separate terminals."
