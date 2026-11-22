#!/usr/bin/env bash
# 起真实服务 → 打完整链路 → 断言 → 收工。
#
# 两种模式：
#   1) 默认（无 SMOKE_DATABASE_URL）：自建 SQLite 文件库，表由模型 create_all 创建。
#   2) SMOKE_DATABASE_URL=postgresql+psycopg://... ：连真实 PostgreSQL，
#      并要求表由 Alembic 迁移创建（AUTO_CREATE_SCHEMA=0，不自动建表）。
set -euo pipefail
cd "$(dirname "$0")/.."

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3
PORT=${PORT:-8137}

if [ -n "${SMOKE_DATABASE_URL:-}" ]; then
  MODE="PostgreSQL（Alembic 迁移建表，AUTO_CREATE_SCHEMA=0）"
  DB_URL="$SMOKE_DATABASE_URL"
  export AUTO_CREATE_SCHEMA=0
  CLEANUP_DB=""
else
  MODE="SQLite（模型 create_all 建表）"
  DB_URL="sqlite+pysqlite:///./smoke.db"
  CLEANUP_DB="./smoke.db"
  rm -f "$CLEANUP_DB"
fi
export DATABASE_URL="$DB_URL"

echo "[smoke] 模式: $MODE"

$PY -m uvicorn commission_corrections.api:app --host 127.0.0.1 --port "$PORT" --log-level warning &
SVPID=$!
cleanup() {
  kill "$SVPID" 2>/dev/null || true
  wait "$SVPID" 2>/dev/null || true
  if [ -n "${CLEANUP_DB:-}" ]; then rm -f "$CLEANUP_DB"; fi
  return 0
}
trap cleanup EXIT

for _ in $(seq 1 90); do
  if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

$PY scripts/smoke_client.py --port "$PORT" --db ./smoke.db
