#!/usr/bin/env bash
# S6-COMM-016 一键验证：单测 + 真实 HTTP 冒烟（不需要 Docker / 不需要外部服务）
set -euo pipefail
cd "$(dirname "$0")"

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3

echo "=== 1/2 单元测试（AC1-AC4 全覆盖） ==="
$PY -m pytest -q

echo
echo "=== 2/2 真实 HTTP 端到端冒烟 ==="
bash scripts/smoke_http.sh
