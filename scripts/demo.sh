#!/usr/bin/env bash
# 本地演示：人工调整（双人审批）+ 已锁定月份的重算差额。
# 前置：docker compose up -d && alembic upgrade head && uvicorn 已启动。
# 用法：bash scripts/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8010}"
PREFIX="$BASE/api/admin/commission/corrections"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pretty() { "$PYTHON_BIN" -m json.tool --no-ensure-ascii; }
field() { "$PYTHON_BIN" -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }

echo "== 0. 造演示数据（虚构企业：青禾示例科技有限公司 / 合同 FICT-DEMO-2026-0001）"
eval "$(cd "$ROOT" && "$PYTHON_BIN" -m app.seed_demo seed)"
echo "ORG_ID=$ORG_ID CONTRACT_ID=$CONTRACT_ID PAYMENT_ID=$PAYMENT_ID 原始提成记录=$RECORD_ID (2026-09, 10000 分)"

APPLICANT_HEADERS=(-H "X-User-Id: 66666666-6666-4666-8666-666666666666"
                   -H "X-Org-Id: $ORG_ID" -H "X-Site-Id: $SITE_ID" -H "X-Roles: sales_rep")
APPLICANT_DUAL_HEADERS=(-H "X-User-Id: 66666666-6666-4666-8666-666666666666"
                        -H "X-Org-Id: $ORG_ID" -H "X-Site-Id: $SITE_ID" -H "X-Roles: sales_rep,channel_supervisor")
APPROVER_HEADERS=(-H "X-User-Id: a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1"
                  -H "X-Org-Id: $ORG_ID" -H "X-Site-Id: $SITE_ID" -H "X-Roles: channel_supervisor")
ADMIN_HEADERS=(-H "X-User-Id: b2b2b2b2-b2b2-4b2b-8b2b-b2b2b2b2b2b2"
               -H "X-Org-Id: $ORG_ID" -H "X-Site-Id: $SITE_ID" -H "X-Roles: platform_admin")

echo
echo "== 1. 申请人提交人工调整 +320000 分（原因：数据录入错误）"
curl -sS -X POST "$PREFIX/adjustments" "${APPLICANT_HEADERS[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"contract_id\":\"$CONTRACT_ID\",\"salesperson_id\":\"$SALESPERSON_ID\",\"payment_id\":\"$PAYMENT_ID\",\"amount_cents\":320000,\"reason_category\":\"数据录入错误\",\"note\":\"虚构演示：财务复核发现首期回款少记，按复核单补差 3200 元。\",\"period_month\":\"2026-09\"}" \
  -o "$TMP/adj.json" -w "HTTP %{http_code}\n"
pretty < "$TMP/adj.json"
ADJ_ID="$(field "$TMP/adj.json" id)"

echo
echo "== 2. 审批前月结单口径（2026-09）：pending 调整不计入 total_cents"
curl -sS "$PREFIX/monthly-summary?year=2026&month=9" "${APPLICANT_HEADERS[@]}" | pretty

echo
echo "== 3. 申请人本人（同时具备招商主管角色）审批自己的调整（应 403 SELF_APPROVAL_FORBIDDEN）"
curl -sS -X POST "$PREFIX/adjustments/$ADJ_ID/approve" "${APPLICANT_DUAL_HEADERS[@]}" \
  -H "Content-Type: application/json" -d '{"comment":"自己批自己"}' \
  -o "$TMP/self.json" -w "HTTP %{http_code}\n"
pretty < "$TMP/self.json"

echo
echo "== 4. 招商主管（第二人）审批通过"
curl -sS -X POST "$PREFIX/adjustments/$ADJ_ID/approve" "${APPROVER_HEADERS[@]}" \
  -H "Content-Type: application/json" -d '{"comment":"财务复核单齐备，同意补差"}' \
  -o "$TMP/approved.json" -w "HTTP %{http_code}\n"
"$PYTHON_BIN" - "$TMP/approved.json" <<'PY'
import json, sys
body = json.load(open(sys.argv[1]))
print(json.dumps({k: body[k] for k in ("id", "status", "amount_cents", "period_month",
                                       "month_locked", "reason_category", "approval_chain")},
                 ensure_ascii=False, indent=2))
PY

echo
echo "== 5. 审批后月结单口径（2026-09）：total_cents 已包含调整"
curl -sS "$PREFIX/monthly-summary?year=2026&month=9" "${APPLICANT_HEADERS[@]}" | pretty

echo
echo "== 6. 平台管理员锁定 2026-09 月结"
curl -sS -X POST "$PREFIX/month-locks" "${ADMIN_HEADERS[@]}" \
  -H "Content-Type: application/json" -d '{"period_month":"2026-09","locked":true}' \
  -o "$TMP/lock.json" -w "HTTP %{http_code}\n"
pretty < "$TMP/lock.json"

echo
echo "== 7. 回款金额被更正：100000 → 150000 分，然后重算该 payment"
cd "$ROOT" && "$PYTHON_BIN" -m app.seed_demo correct 150000
curl -sS -X POST "$PREFIX/recalculate" "${APPROVER_HEADERS[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"payment_id\":\"$PAYMENT_ID\",\"correction_reason\":\"回款金额录入更正：1000 元 → 1500 元\"}" \
  -o "$TMP/recalc.json" -w "HTTP %{http_code}\n"
"$PYTHON_BIN" - "$TMP/recalc.json" <<'PY'
import json, sys
body = json.load(open(sys.argv[1]))
print(json.dumps({
    "mode": body["mode"],
    "delta_cents": body["delta_cents"],
    "current_period": body["current_period"],
    "original": {k: body["original"][k] for k in ("id", "status", "period_month", "amount_cents", "month_locked", "voided_reason")},
    "result_record": {k: body["result_record"][k] for k in
                      ("id", "kind", "origin", "status", "amount_cents", "period_month", "month_locked", "note")},
}, ensure_ascii=False, indent=2))
PY

echo
echo "== 8. 锁定月 2026-09 合计不变；差额进入当前未锁定月 2026-11"
echo "-- 2026-09 --"; curl -sS "$PREFIX/monthly-summary?year=2026&month=9" "${APPLICANT_HEADERS[@]}" | pretty
echo "-- 2026-11 --"; curl -sS "$PREFIX/monthly-summary?year=2026&month=11" "${APPLICANT_HEADERS[@]}" | pretty

echo
echo "== 9. 审计日志（该调整：调整前后金额 / 操作人 / 审批人）"
curl -sS "$PREFIX/audit-logs?entity_id=$ADJ_ID" "${APPROVER_HEADERS[@]}" -o "$TMP/audit.json"
"$PYTHON_BIN" - "$TMP/audit.json" <<'PY'
import json, sys
for item in json.load(open(sys.argv[1]))["items"]:
    print(json.dumps({k: item[k] for k in ("action", "before_amount_cents", "after_amount_cents",
                                           "actor_id", "approver_id")}, ensure_ascii=False))
PY

echo
echo "== 10. 重算审计日志（before=原金额 10000，after=重算后 15000）"
curl -sS "$PREFIX/audit-logs?action=commission.recalculate" "${APPROVER_HEADERS[@]}" -o "$TMP/audit2.json"
"$PYTHON_BIN" - "$TMP/audit2.json" <<'PY'
import json, sys
for item in json.load(open(sys.argv[1]))["items"]:
    print(json.dumps({k: item[k] for k in ("action", "before_amount_cents", "after_amount_cents",
                                           "actor_id", "approver_id")}, ensure_ascii=False))
    print(json.dumps(item["detail"], ensure_ascii=False, indent=2))
PY
echo
echo "演示结束。"
