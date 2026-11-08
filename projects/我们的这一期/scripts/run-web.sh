#!/usr/bin/env bash
set -euo pipefail
PRODUCT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$PRODUCT_DIR/frontend"
case "${1:-normal}" in
  normal) export BACKEND_URL='http://127.0.0.1:8788' BACKEND_MODE=normal; WEB_PORT=3000 ;;
  demo) export BACKEND_URL='http://127.0.0.1:8790' BACKEND_MODE=demo; WEB_PORT=3001 ;;
  *) printf '%s\n' '使用 bash scripts/run-web.sh 或 bash scripts/run-web-demo.sh。' >&2; exit 1 ;;
esac
export APP_ORIGIN="http://127.0.0.1:$WEB_PORT" NEXT_TELEMETRY_DISABLED=1
if [[ ! -x node_modules/.bin/next ]]; then
  printf '%s\n' '缺少网页依赖。请在 frontend 目录运行 npm ci 后重试。' >&2; exit 1
fi
if [[ ! -f .next/BUILD_ID ]]; then npm run build; fi
printf '正式网页：%s\n先保持对应后端服务运行。\n' "$APP_ORIGIN"
exec npm run start -- --port "$WEB_PORT"
