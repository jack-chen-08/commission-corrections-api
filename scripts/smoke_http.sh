#!/usr/bin/env bash
# 起真实服务 → 打完整链路 → 断言 → 收工。不依赖 Docker、不依赖外部数据库。
set -euo pipefail
cd "$(dirname "$0")/.."

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3
PORT=${PORT:-8137}
DB=./smoke.db

rm -f "$DB"
export DATABASE_URL="sqlite+pysqlite:///$DB"

$PY -m uvicorn commission_corrections.api:app --host 127.0.0.1 --port "$PORT" --log-level warning &
SVPID=$!
cleanup() { kill "$SVPID" 2>/dev/null || true; wait "$SVPID" 2>/dev/null || true; rm -f "$DB"; }
trap cleanup EXIT

for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

$PY scripts/smoke_client.py --port "$PORT" --db "$DB"
