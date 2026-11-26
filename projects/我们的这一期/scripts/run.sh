#!/usr/bin/env bash
set -euo pipefail
PRODUCT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$PRODUCT_DIR/backend"
export LOCAL_DEMO_ENTRY="${LOCAL_DEMO_ENTRY:-1}"
if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' '缺少 backend/.venv；请先按产品 README 安装本地依赖。' >&2
  exit 1
fi
.venv/bin/python "$PRODUCT_DIR/scripts/setup-local.py"
exec .venv/bin/python -m uvicorn --factory app.main:production_app --host 127.0.0.1 --port "${PORT:-8788}" --workers 1 --no-access-log
