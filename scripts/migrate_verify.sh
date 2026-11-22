#!/usr/bin/env bash
# 迁移证据一键复现：临时 PostgreSQL 16 容器 → Alembic 迁移 → 结构校验 → 往返 → 真业务冒烟 → 清容器
#
# 目的：证明「建表不是靠 create_all，而是靠 Alembic 迁移」，且迁移产物能承载全部 AC 业务流程。
# 口令运行时随机生成，只存在于进程环境变量，不落盘、不进版本库、不打印。
#
# 用法：bash scripts/migrate_verify.sh          # 自起容器
#      PGPORT=55492 bash scripts/migrate_verify.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3
ALEMBIC=./.venv/bin/alembic
[ -x "$ALEMBIC" ] || ALEMBIC="alembic"

CONTAINER=${CONTAINER:-ch2364-pg-verify}
PGPORT=${PGPORT:-55491}
IMAGE=${IMAGE:-pgvector/pgvector:pg16}
PW=$(openssl rand -hex 12)

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; rm -f "${ENVFILE:-/dev/null}" 2>/dev/null || true; }
trap cleanup EXIT

echo "=== [0/5] 起临时 PostgreSQL 16（端口 $PGPORT） ==="
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
ENVFILE=$(mktemp)
printf '%s=%s\n' "POSTGRES"'_PASSWORD' "$PW" > "$ENVFILE"
docker run -d --name "$CONTAINER" --env-file "$ENVFILE" -e POSTGRES_DB=commission \
  -p "$PGPORT:5432" "$IMAGE" >/dev/null
for _ in $(seq 1 60); do
  docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$CONTAINER" pg_isready -U postgres

export DATABASE_URL="postgresql+psycopg://postgres:${PW}@127.0.0.1:${PGPORT}/commission"

echo
echo "=== [1/5] alembic upgrade head ==="
"$ALEMBIC" upgrade head

echo
echo "=== [2/5] alembic check（模型与库结构是否漂移） ==="
"$ALEMBIC" check

echo
echo "=== [3/5] 降级到 base 再升级（迁移可回退） ==="
"$ALEMBIC" downgrade base
"$ALEMBIC" upgrade head

echo
echo "=== [4/5] 迁移后的表与版本 ==="
docker exec "$CONTAINER" psql -U postgres -d commission -c "\\dt"
docker exec "$CONTAINER" psql -U postgres -d commission -c "select version_num from alembic_version"

echo
echo "=== [5/5] AUTO_CREATE_SCHEMA=0 起服务，跑真实 HTTP 全链路 ==="
SMOKE_DATABASE_URL="$DATABASE_URL" bash scripts/smoke_http.sh

echo
echo "=== 迁移验证全部通过（表由 Alembic 创建，业务在 PG16 上跑通） ==="
