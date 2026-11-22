# S6-COMM-016 · 提成人工调整与重算接口

会员空间平台提成结算的可更正链路：**人工调整走双人复核**、**重算全程可追溯**、
**锁账月份不篡改历史（差额制）**、**每一笔更正都留审计**。

- 技术栈：Python 3.12 · FastAPI · SQLAlchemy 2.0 · Pydantic v2 · Alembic（schema 迁移）· PostgreSQL 16（本地/测试用 SQLite）
- 金额：一律「分」为单位的整数，无浮点、无 Decimal、无真除法
- 与 S6-COMM-001（`compute_gross` 超额累进阶梯 + 折扣系数）共用同一套取整口径，本仓以 `commission_corrections/engine.py` 内置该纯函数，避免跨服务调用带来的口径漂移

---

## 1. 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
bash run_tests.sh              # 单测 17 例 + 真实 HTTP 端到端冒烟 11 项断言（SQLite）
bash scripts/migrate_verify.sh # 迁移证据：临时 PG16 容器 → Alembic → 全链路冒烟（需 Docker）
uvicorn commission_corrections.api:app --reload --port 8000   # 起服务
```

零外部依赖：不需要 Docker、不需要 Redis、不需要跑上游服务。默认 SQLite 文件库，
生产把 `DATABASE_URL` 换成 PostgreSQL 16 即可，代码无需改动。

### 数据库迁移（Alembic）

客户统一技术约定要求 schema 由 Alembic 管理（不是 `create_all`）。连接串只从环境变量读，
`alembic.ini` 里不写任何凭证：

```bash
export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/commission"
alembic upgrade head        # 建表（含 alembic_version 版本表）
alembic current             # 当前版本
alembic check               # 模型与库结构是否漂移（CI 可作门禁）
alembic downgrade base      # 回退全部迁移
```

- 初始迁移：`alembic/versions/0001_init_commission_corrections.py`（3 张业务表 + 2 个索引 + 1 个唯一约束）
- 服务启动时默认 `AUTO_CREATE_SCHEMA=1`（本地 sqlite 便利）；**生产/迁移模式设
  `AUTO_CREATE_SCHEMA=0`**，表全部来自迁移，启动不再隐式建表。
- 一键复现证据：`bash scripts/migrate_verify.sh` —— 起临时 PostgreSQL 16 容器，
  跑 `upgrade head` → `alembic check` → 降级/升级往返 → `AUTO_CREATE_SCHEMA=0` 起服务打 11 项真实 HTTP 断言，
  输出留档 `docs/migration_verify_output.txt`（口令运行时随机生成，不落盘、不进仓库）。

---

## 2. 验收项对照（工单 AC → 实现 → 证据）

| 工单 AC | 实现位置 | 自动化证据 |
|---|---|---|
| **AC1** 未审批的调整不得计入月结单合计数 | `service.monthly_statement()` 只聚合 `status='approved' AND voided_at IS NULL` 的行；`pending`/`rejected`/`voided` 天然被排除，并单独披露 `excluded_pending_cents` 便于对账 | `test_ac1_pending_adjustment_not_counted_in_monthly_statement`、`test_ac1_rejected_adjustment_never_counted` |
| **AC2** 审批人不得审批自己提交的调整（403） | `service.approve_adjustment()` / `reject_adjustment()` 中 `record.created_by == actor.user_id` **先于**角色校验判定，直接 `403 forbidden_self_approval`（有审批权也不能自审） | `test_ac2_applicant_cannot_approve_own_adjustment`、`test_ac2_applicant_cannot_reject_own_adjustment`、`test_ac2_underprivileged_role_cannot_approve` |
| **AC3** 已锁定月份重算后生成差额调整记录，原记录保持 `locked` | `service.recalculate()`：`is_month_locked()` 为真时**不改原记录一个字**（`locked` 保持 `True`、`voided_at` 保持 `None`、`amount_cents` 不变），另建 `kind='adjustment'` 的差额记录落到当前未锁月，`supersedes_id` 指回原记录 | `test_ac3_locked_month_recalc_creates_delta_and_keeps_original_locked`、`test_ac3_unlocked_month_recalc_voids_original_and_supersedes` |
| **AC4** 审计日志包含调整前后金额与审批人 | `AuditLog` 表 `before_amount_cents` / `after_amount_cents` / `actor_id` / `approver_id`；审批、驳回、作废、重算四条路径全部写入，且审计行只增不改（无 update 路径） | `test_ac4_audit_log_has_before_after_amounts_and_approver`、`test_ac4_recalc_audit_records_old_and_new_amount` |

真实服务端到端（`scripts/smoke_http.sh`，起 uvicorn 后走 HTTP）：

```
[PASS] 月结合计 = 0  pending=100000
[PASS] 自审返回 403  code=forbidden_self_approval
[PASS] 审计含前后金额与审批人
[PASS] 差额记录 20000-15000=5000 分  month=2026-11 kind=adjustment
[PASS] 原记录 locked=True 且未作废且金额未改
```

---

## 3. 接口

前缀 `/api/admin/commission/corrections`。身份与数据范围由请求头注入
（`X-User-Id` / `X-User-Role` / `X-Org-Id`），对应 S2-IAM 的本地桩 `iam.py`——
真实 IAM 落地后只替换该文件，service 与测试不动。

| 方法 | 路径 | 说明 | 关键错误码 |
|---|---|---|---|
| POST | `/adjustments` | 提交人工调整 → `pending`，不生效 | 422 `limit_exceeded`（超出 `COMM_ADJUST_MAX_CENTS`）、409 `invalid_state`（目标月已锁账） |
| POST | `/adjustments/{id}/approve` | 第二人审批生效 | 403 `forbidden_self_approval`、403 `forbidden_role`、409 `invalid_state`、404 `not_found` |
| POST | `/adjustments/{id}/reject` | 第二人驳回 | 同上 |
| POST | `/recalculate` | 按回款 `payment_id` 重算 | 404 `not_found`、409 `invalid_state`（缺输入快照） |
| GET | `/monthly-statement?month=YYYY-MM` | 月结单（只算生效记录） | — |
| POST | `/month-locks?month=YYYY-MM` | 月结锁账（主管以上） | 403 `forbidden_role`、409 `invalid_state` |

提交调整：

```http
POST /api/admin/commission/corrections/adjustments
X-User-Id: sales-01
X-User-Role: sales
X-Org-Id: org-1

{
  "contract_id": "c-1001",
  "salesperson_id": "sp-01",
  "payment_id": "pay-1001",
  "amount_cents": 100000,
  "reason_category": "rule_override",
  "note": "客户口头承诺特殊折扣，主管已批",
  "month": "2026-11"
}
```

`reason_category` 为四值枚举：`rule_override`（规则表外特批）/ `data_entry_error`（录入错误）/
`owner_change`（业务员归属变更）/ `other`。备注 1–300 字，不接受空。

---

## 4. 数据模型

| 表 | 作用 | 要点 |
|---|---|---|
| `commission_record` | 提成记录（`kind=system` 规则结果 / `kind=adjustment` 人工调整与重算差额） | `amount_cents` 整数分、可负；`status` ∈ pending/approved/rejected/voided；`locked` 锁账标记；`supersedes_id` 版本链；`input_snapshot` JSON 保存重算所需的阶梯/折扣/基数快照；统一 `org_id`/`site_id`/`created_by`/`updated_at`/`deleted_at` |
| `commission_month_lock` | 月度锁账 | `(org_id, month)` 唯一；锁账时把该月生效记录标 `locked=True` |
| `commission_audit_log` | 审计日志 | 前后金额 + 操作人 + 审批人 + detail JSON；只增不改 |

**为什么把 `input_snapshot` 存在记录里**：重算若依赖上游阶梯表的「当前值」，一旦规则调整，
历史记录会被算成另一个数，账就再也对不回来。存快照后，重算只重放当时的输入，
结果差异全部来自引擎版本而非数据漂移——这是可追溯的前提。

---

## 5. 关键设计决策

1. **「不计入」用状态过滤实现，不用冲销分录。** 月结单是可重算视图，`pending` 调整从未进入
   合计，审批通过后自然进入；驳回不留残影。避免了「先记再冲」造成合计抖动与对账口径分裂。
2. **自审拦截优先于角色校验。** 若先判角色，主管自提自批会「因有权限而通过」——这是最典型的内控漏洞。
   顺序反过来，`created_by == actor` 一律 403，与角色无关。
3. **锁账月不修改历史。** 锁定意味着对外已出的报表不可变更；重算结论以**差额调整**落到当前未锁月，
   原记录金额、`locked`、`voided_at` 全部不动（AC3）。未锁账月才允许「作废原记录 + 新记录 supersedes」。
4. **重算结论无需二次审批，但必须留痕。** 差额记录的 `approved_by='system:recalculate'`、
   `trace` 里带完整 `trace_steps`（逐档切段明细），审计里记 `recalculate_delta` 与前后金额。
   人工调整与系统结论走两条不同的信任路径，不混用。
5. **金额全整数分 + 引擎内一次取整。** 与 S6-COMM-001 同一口径：`Fraction` 精确累进，
   函数末尾 `ROUND_HALF_UP` 只取整一次，避免分段取整的累计误差。
6. **`org_id` 强隔离。** 所有查询带 `org_id`，`_get_scoped()` 对不同 org 的记录一律返回 404
   （不是 403，避免探测他人记录是否存在）。
7. **软删除 + 审计只增。** `deleted_at` 保留业务记录历史；审计表无 update/delete 路径。
8. **配置只从环境变量读。** `DATABASE_URL`、`COMM_ADJUST_MAX_CENTS` 均不落代码、不进仓库。

---

## 6. 并发与一致性

- 单条调整的状态机是单向的（pending → approved / rejected），重复审批返回 `409 invalid_state`，
  不会重复计入合计。
- SQLite 单写者模型下测试稳定；**生产 PostgreSQL 上对同一 `payment_id` 的并发重算建议加
  `SELECT ... FOR UPDATE`（或 advisory lock）**，当前实现未加行锁——已列入后续项，
  若月结高峰存在并发重算，建议优先补这一块。
- 事务边界：每个 HTTP 请求一个事务，异常全回滚（`get_db` 依赖），不会出现「审计写了、记录没改」。

---

## 7. 目录

```
commission_corrections/
  api.py        FastAPI 路由与错误翻译（DomainError → JSON + code）
  service.py    业务层：调整/审批/驳回/重算/月结/锁账（无 HTTP 依赖）
  models.py     SQLAlchemy 模型（3 张表）
  schemas.py    Pydantic 请求响应模型
  iam.py        身份角色与 org 范围（S2-IAM 本地桩，可替换）
  engine.py     compute_gross 纯函数（与 S6-COMM-001 同口径）
alembic/        迁移环境 env.py（读 DATABASE_URL）+ versions/0001_init_commission_corrections.py
alembic.ini     仅保留 Alembic 配置，sqlalchemy.url 留空（凭证只走环境变量）
tests/          17 例单测，AC1–AC4 逐条命名
scripts/        smoke_http.sh（起真实服务打全链路）+ smoke_client.py + migrate_verify.sh（迁移证据）
run_tests.sh    一键：单测 + 冒烟
```

## 8. 已知边界

- 未做行级并发锁（见第 6 节），高并发重算场景需补。
- 差额调整不回溯重算历史月份的阶梯：差额 = 新口径结果 − 原记录金额，不做逐档重排。
- 未提供审计导出接口与前端页面（工单范围外，按需另立）。
- IAM 为本地桩，角色阶梯 `sales < sales_supervisor < org_admin`，与平台现有角色表需在接入时对齐。
