#!/usr/bin/env bash
set -euo pipefail
PRODUCT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$PRODUCT_DIR/backend"
if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' '缺少 backend/.venv；请先安装项目本地依赖。' >&2
  exit 1
fi
exec .venv/bin/python "$PRODUCT_DIR/scripts/demo-server.py"
