#!/usr/bin/env bash
set -euo pipefail
PRODUCT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$PRODUCT_DIR/scripts/run-web.sh" demo
