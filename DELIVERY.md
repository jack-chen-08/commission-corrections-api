# 交付说明 · 提成人工调整与重算接口

| 项 | 内容 |
| --- | --- |
| 工单 | 提成规则表模块（Stage vi 陪跑与二期第一批） |
| 交付物 | 可运行 git 仓库（FastAPI + SQLAlchemy 2 + Alembic + PostgreSQL 16） |
| 仓库路径 | `commission-corrections-api` |
| 接口前缀 | `/api/admin/commission/corrections` |
| 测试结果 | **15 passed in 1.88s**（真实执行，见 `EVIDENCE/pytest_output.txt`） |
| 数据库 | PostgreSQL 16.16（pgvector/pgvector:pg16 容器，healthy） |
| 迁移 | `0001_init`，可 `downgrade base` 后重新 `upgrade head` |

---

## 1. 交付清单

| 路径 | 说明 |
| --- | --- |
| `app/api/routes_corrections.py` | 11 个 HTTP 端点（10 个业务端点 + 健康检查） |
| `app/service/corrections.py` | 业务规则：金额与月份校验、双人审批、自批拦截、锁定月差额、未锁定月替代、月结合计、审计写入 |
| `app/models.py` | `contracts` / `payments` / `commission_records` / `month_locks` / `audit_logs`，统一审计列，金额 `BIGINT` 分 |
| `app/schemas.py` | 请求模型：整数金额（strict）、说明 ≤300 字、原因类别四枚举、`YYYY-MM` 校验 |
| `app/iam.py` | S2-IAM 接口桩：请求头解析身份、角色等级（业务员 1 → 平台管理员 5）、组织范围 |
| `app/calculator.py` | 提成规则计算桩，返回 `(amount_cents, trace)` |
| `app/audit.py` | 审计日志写入（前后金额、操作人、审批人、明细 JSONB） |
| `app/config.py` / `app/db.py` / `app/main.py` | 环境变量配置、会话工厂、应用装配 |
| `alembic/versions/0001_init.py` | 初始迁移，含 CHECK 约束与索引 |
| `tests/conftest.py` | 迁移建库、逐用例清表、身份头与数据工厂（全部虚构） |
| `tests/test_corrections.py` | 15 个用例，4 条 AC 各有显式命名用例（AC4 两条：调整审计 + 重算审计） |
| `scripts/demo.sh` + `app/seed_demo.py` | curl 端到端演示（虚构企业「青禾示例科技有限公司」） |
| `README.md` | 起库、迁移、跑测试、跑演示的完整命令；对接点与已知边界 |
| `EVIDENCE/` | 4 份真实执行证据 |

---

## 2. 实际执行记录

环境：WSL Ubuntu，Python 3.12.13，Docker 29.1.3，Compose 2.40.3。

### 2.1 起库

```bash
docker compose up -d
docker ps --filter name=commission-pg
```

真实输出（`EVIDENCE/env_and_db.txt` 第 1–3 行）：

```
NAMES       IMAGE                    STATUS                        PORTS
commission-pg   pgvector/pgvector:pg16   Up About a minute (healthy)   0.0.0.0:55432->5432/tcp, [::]:55432->5432/tcp
```

数据库版本：

```
PostgreSQL 16.16 (Debian 16.16-1.pgdg12+1) on x86_64-pc-linux-gnu, compiled by gcc (Debian 12.2.0-14+deb12u1) 12.2.0, 64-bit
```

### 2.2 迁移

```bash
.venv/bin/alembic upgrade head
.venv/bin/alembic current
```

真实输出：

```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001_init, 初始表结构：合同/回款只读副本、提成台账、月结锁、审计日志。
0001_init (head)
```

迁移可逆性验证（`EVIDENCE/alembic_cycle.txt`）：

```bash
.venv/bin/alembic downgrade base
.venv/bin/alembic upgrade head
```

真实输出：

```
INFO  [alembic.runtime.migration] Running downgrade 0001_init -> , 初始表结构：合同/回款只读副本、提成台账、月结锁、审计日志。
INFO  [alembic.runtime.migration] Running upgrade  -> 0001_init, 初始表结构：合同/回款只读副本、提成台账、月结锁、审计日志。
```

建表结果（6 张表）：

```
 public | alembic_version    | table | comm
 public | audit_logs         | table | comm
 public | commission_records | table | comm
 public | contracts          | table | comm
 public | month_locks        | table | comm
 public | payments           | table | comm
```

### 2.3 测试

```bash
.venv/bin/pytest -v --durations=5
```

真实输出全文：

```
============================= test session starts ==============================
platform linux -- Python 3.12.13, pytest-8.3.4, pluggy-1.7.0 -- commission-corrections-api/.venv/bin/python3.12
cachedir: .pytest_cache
rootdir: commission-corrections-api
configfile: pytest.ini
testpaths: tests
plugins: anyio-4.16.0
collecting ... collected 15 items

tests/test_corrections.py::test_ac1_pending_adjustment_excluded_from_monthly_total PASSED [  6%]
tests/test_corrections.py::test_ac2_applicant_cannot_approve_own_adjustment_returns_403 PASSED [ 13%]
tests/test_corrections.py::test_ac3_recalculate_locked_month_creates_delta_and_keeps_original_locked PASSED [ 20%]
tests/test_corrections.py::test_ac4_audit_log_contains_before_after_amounts_and_approver PASSED [ 26%]
tests/test_corrections.py::test_ac4_recalc_audit_log_contains_before_after_amounts PASSED [ 33%]
tests/test_corrections.py::test_recalculate_unlocked_month_voids_original_and_supersedes PASSED [ 40%]
tests/test_corrections.py::test_adjustment_amount_limit_and_negative_amount PASSED [ 46%]
tests/test_corrections.py::test_adjustment_note_length_limit PASSED      [ 53%]
tests/test_corrections.py::test_reject_keeps_record_out_of_monthly_total PASSED [ 60%]
tests/test_corrections.py::test_adjustment_trace_records_manual_input_and_approval_chain PASSED [ 66%]
tests/test_corrections.py::test_org_scope_isolation PASSED               [ 73%]
tests/test_corrections.py::test_rank_requirements PASSED                 [ 80%]
tests/test_corrections.py::test_adjustment_into_locked_month_is_rejected PASSED [ 86%]
tests/test_corrections.py::test_recalculate_without_active_record_returns_409 PASSED [ 93%]
tests/test_corrections.py::test_month_lock_endpoint_requires_admin PASSED [100%]

============================= slowest 5 durations ==============================
0.21s setup    tests/test_corrections.py::test_ac1_pending_adjustment_excluded_from_monthly_total
0.09s setup    tests/test_corrections.py::test_rank_requirements
0.08s call     tests/test_corrections.py::test_ac1_pending_adjustment_excluded_from_monthly_total
0.08s setup    tests/test_corrections.py::test_recalculate_unlocked_month_voids_original_and_supersedes
0.08s setup    tests/test_corrections.py::test_reject_keeps_record_out_of_monthly_total
============================== 15 passed in 1.88s ==============================
```

### 2.4 curl 端到端演示

```bash
bash scripts/demo.sh
```

完整 251 行输出见 `EVIDENCE/curl_demo.txt`，关键片段：

审批前 2026-09 月结（待审批调整不计入 `total_cents`，单列于 `pending_*`）：

```json
{
    "period_month": "2026-09",
    "locked": false,
    "total_cents": 10000,
    "active_count": 1,
    "pending_total_cents": 320000,
    "pending_count": 1
}
```

申请人自批（同时持 `sales_rep,channel_supervisor`）：

```
HTTP 403
{
    "detail": {
        "code": "SELF_APPROVAL_FORBIDDEN",
        "message": "不得审批本人提交的调整，需第二人审批"
    }
}
```

注意请求者角色等级已满足招商主管，拦截依据是「自批」而非等级不足；同请求换第二人审批即返回 200。

审批后 2026-09 合计 330000 分（原 10000 + 调整 320000）；锁定 2026-09 后重算，回款 100000 → 150000 分（返回体节选关键字段）：

```json
{
  "mode": "delta_in_current_period",
  "delta_cents": 5000,
  "original": {"status": "active", "period_month": "2026-09", "amount_cents": 10000, "month_locked": true, "voided_reason": null},
  "result_record": {"kind": "adjustment", "origin": "recalculation_delta", "status": "active", "amount_cents": 5000, "period_month": "2026-11", "month_locked": false}
}
```

重算后两个月的月结口径：

```
2026-09：locked=true,  total_cents=330000（不变）
2026-11：locked=false, total_cents=5000  （差额）
```

审计日志：

```
{"action": "commission.adjustment.approve", "before_amount_cents": 0, "after_amount_cents": 320000, "actor_id": "66666666-...", "approver_id": "a1a1a1a1-..."}
{"action": "commission.adjustment.create",  "before_amount_cents": 0, "after_amount_cents": 320000, "actor_id": "66666666-...", "approver_id": null}
{"action": "commission.recalculate",        "before_amount_cents": 10000, "after_amount_cents": 15000, "actor_id": "a1a1a1a1-...", "approver_id": null}
```

---

## 3. 验收标准对照（AC → 用例 → 证据）

| AC | 要求 | 用例名 | 结果 | 证据文件 |
| --- | --- | --- | --- | --- |
| AC1 | 未审批的调整不计入月结单合计 | `test_ac1_pending_adjustment_excluded_from_monthly_total` | 通过 | `tests/test_corrections.py:53`；`EVIDENCE/pytest_output.txt:11`；演示 `EVIDENCE/curl_demo.txt:71-92` |
| AC2 | 申请人审批自己的调整返回 403 | `test_ac2_applicant_cannot_approve_own_adjustment_returns_403` | 通过 | `tests/test_corrections.py:84`；`EVIDENCE/pytest_output.txt:12`；演示 `EVIDENCE/curl_demo.txt:94-102` |
| AC3 | 已锁定月份的记录重算后生成差额调整记录，原记录保持 locked | `test_ac3_recalculate_locked_month_creates_delta_and_keeps_original_locked` | 通过 | `tests/test_corrections.py:118`；`EVIDENCE/pytest_output.txt:13`；演示 `EVIDENCE/curl_demo.txt:164-227` |
| AC4 | 审计日志包含调整前后金额与审批人 | `test_ac4_audit_log_contains_before_after_amounts_and_approver`、`test_ac4_recalc_audit_log_contains_before_after_amounts`（真执行一次重算并断言 before/after） | 通过 | `tests/test_corrections.py:169`、`:216`；`EVIDENCE/pytest_output.txt:15-16`；演示 `EVIDENCE/curl_demo.txt:229-234` |

### AC1 断言要点

- 建 10000 分基础记录 → 提交 250000 分调整后，`total_cents` 仍为 10000，`pending_total_cents=250000`、`pending_count=1`。
- 第二人审批后 `total_cents=1250000`、`pending_count=0`。

### AC2 断言要点

- 申请人 `X-User-Id` 与提交人相同，角色头为 `sales_rep,channel_supervisor`（等级已够）。
- `approve` 与 `reject` 均返回 403 `SELF_APPROVAL_FORBIDDEN`。
- 记录仍为 `pending`，`approval_chain` 只有 `submitted`；换第二人审批可正常通过。

### AC3 断言要点

- 2026-09 已锁定，回款 100000 → 150000 分，费率 10%（`rate_bp=1000`），重算后 15000 分。
- `mode=delta_in_current_period`，`delta_cents=5000`。
- 原记录：`status=active`、`voided_reason=null`、`amount_cents` 不变、`month_locked=true`；2026-09 合计仍 10000。
- 差额记录：`kind=adjustment`、`origin=recalculation_delta`、`period_month=2026-11`、`trace.original_record_id` 指向原记录；2026-11 合计 5000。

### AC4 断言要点

- 查询 `action=commission.adjustment.approve` 的审计条目：`before_amount_cents=0`、`after_amount_cents=501234`、`actor_id=申请人`、`approver_id=审批人`。
- `detail` 含 `period_total_before_cents`、`period_total_after_cents`、`counted_in_monthly_close=true`、`comment`。
- 另校验 `create` 条目的 `approver_id` 为空，且库内该实体仅有 create/approve 两条。

---

## 4. 附加场景覆盖（非 AC，防回归）

| 用例名 | 验证内容 |
| --- | --- |
| `test_recalculate_unlocked_month_voids_original_and_supersedes` | 未锁定月重算：原作废（`voided_reason=recalculated`）、新记录 `supersedes_id` 指向原记录、合计只算新记录 |
| `test_adjustment_amount_limit_and_negative_amount` | 上限 +1 分返回 422 并回传 `limit_cents`；0 返回 422；`-5000000` 可正常提交 |
| `test_adjustment_note_length_limit` | 304 字 422、空白 422、整 300 字通过 |
| `test_reject_keeps_record_out_of_monthly_total` | 驳回后 `status=voided`、不计入合计、重复驳回 409、审计前后金额 120000 → 0 |
| `test_adjustment_trace_records_manual_input_and_approval_chain` | trace 为 `manual_input`，`calculation.applied=false`，无 `rule_version`/`steps`，审批链 `submitted → approved` |
| `test_org_scope_isolation` | 跨组织合同 404；跨组织读取他人调整 404 |
| `test_rank_requirements` | 业务员重算 403、招商专员审批 403 |
| `test_adjustment_into_locked_month_is_rejected` | 向已锁定月新增调整 409 `PERIOD_LOCKED` |
| `test_recalculate_without_active_record_returns_409` | 无生效记录重算 409 `NO_ACTIVE_RECORD` |
| `test_month_lock_endpoint_requires_admin` | 招商主管锁月 403、平台管理员 201，且月结口径 `locked=true` |

---

## 5. 接口与规则实现摘要

- **金额**：`amount_cents` 以 `int(strict=True)` 接收，`BIGINT` 存储，全链路无浮点；上限读取 `COMM_ADJUST_MAX_CENTS`（默认 5000000），按绝对值判断。
- **说明**：去空格后非空且字符数 ≤300；`reason_category` 限定四枚举，数据库层另有 CHECK 约束。
- **双人审批**：`status=pending` 不参与合计；审批人等级 ≥ 招商主管；审批人 id 与 `created_by` 相同即 403。
- **锁定月**：`is_period_locked` 是唯一判断入口；锁定月禁新增调整、禁审批入账；重算走差额路径，原记录金额与状态均不变。
- **重算**：以 `payment_id` 找唯一的 active `kind=base` 记录；未锁定则作废并新建 `supersedes_id`；差额为 0 时只写审计不建记录。
- **审计**：每次 create/approve/reject/recalculate 都落 `audit_logs`，含前后金额、操作人、审批人、月份合计快照与原因，支持按实体与动作查询。
- **组织隔离**：所有读写按 `principal.org_id` 过滤，跨组织资源统一 404。

---

## 6. 桩与对接点（正式环境需替换）

| 依赖 | 当前状态 | 替换位置 |
| --- | --- | --- |
| S2-IAM | **桩**：信任 `X-User-Id / X-Org-Id / X-Site-Id / X-Roles` 请求头，未验签 | `app/iam.py: principal_from_headers` |
| S2-AUDIT | **本地落库**：写 `audit_logs` 并提供查询接口，未投递审计服务 | `app/audit.py: write_audit` |
| 提成规则表计算 | **桩**：按合同费率 `commission_rate_bp` 线性计算 | `app/calculator.py: compute_commission` |
| 合同 / 回款数据 | **本地只读副本**，由 `app/seed_demo.py` 写入，未接上游同步 | 上游事件/同步任务写 `contracts`、`payments` |
| 月结锁定 | 本模块自管 `month_locks`，未接财务系统 | `app/service/corrections.py: is_period_locked` |

替换契约：`Principal` 结构、`compute_commission` 的 `(int, dict)` 返回值、`write_audit` 的参数签名保持不变，业务层无需改动。

---

## 7. 已知短板

1. **IAM 未验签**：请求头桩可被伪造，生产必须由网关剥离外部同名头并改为令牌校验；越权测试只覆盖 `org_id` 维度，未覆盖 `site_id` 细粒度数据范围。
2. **审计未投递 S2-AUDIT**：目前仅本地库，缺少 outbox 表、异步投递与失败重试；审计服务不可用时不会阻塞主流程，但也不会补投。
3. **审批为单人复核**：未实现多级会签、审批超时、撤回；`approval_chain` 结构已预留步骤字段，可扩展。
4. **重算粒度为单笔回款**：批量更正需上层循环调用；未做并发互斥锁，同一 `payment_id` 并发重算可能产生两条替代记录（数据库未加唯一约束），生产建议加 `SELECT ... FOR UPDATE` 或分布式锁。
5. **额度控制仅单条**：未实现同合同同月累计调整上限、按机构配置的分级审批阈值。
6. **月份口径依赖配置**：`COMM_CURRENT_PERIOD` 固定注入，不随日历自动滚动；跨月未重跑配置会导致差额进入旧月份。
7. **未覆盖的性能项**：月结合计为实时聚合查询，大数据量下需物化视图或预聚合；当前无压测数据。
8. **演示脚本会重置数据**：`scripts/demo.sh` 使用固定 UUID 的虚构数据，重复执行会覆盖同 org 的演示合同/回款记录，仅适用于本地演示库。

---

## 8. 数据与保密

- 全部数据为虚构：企业名「青禾示例科技有限公司」，合同号 `FICT-DEMO-2026-0001` / `FICT-2026-0001`，人名与 UUID 均为构造值。
- 交付物不含客户名称、真实企业信息、真实个人信息。
- 无真实连接串与密钥入库；配置仅从环境变量读取，`.env` 已被忽略。
- 依赖清单不含任何向境外发送数据的第三方 SDK。

## 9. 返工回应（针对上一轮 3 条意见）

| # | 上一轮意见 | 本轮状态 | 落地位置 / 验证 |
| --- | --- | --- | --- |
| 1 | 重算必须先取合同/回款现值，不能拿申请时的快照算；需有「更正后结果确实改变」的测试 | **已闭环** | `app/service/corrections.py` 的 `recalculate()` 首段按 `contract_id` 重新 `SELECT contracts / payments`，只用申请里的 `reason / new_amount` 作为更正值；用例 `test_ac3_recalculate_locked_month_creates_delta_and_keeps_original_locked` 把 `payments.paid_amount_cents` 由 100000 改为 150000 后重算，断言 `delta_cents == 5000`、原记录仍为锁定状态。 |
| 2 | `id` 用 UUID、各表统一 `site_id / created_at / created_by / updated_at / deleted_at`，`month_lock` 表补齐 | **已闭环** | `app/models.py`：5 张业务表 `contracts / payments / month_locks / commission_records / audit_logs` 主键均 `uuid.UUID`，公共字段由 `TimestampMixin` + 显式 `site_id / deleted_at` 提供（`month_locks` 已补 `site_id`、`created_by`、`deleted_at`）；`alembic/versions/0001_init.py` 逐列与模型对齐，`\dt` 实测 6 张表（含 `alembic_version`）。 |
| 3 | `test_ac4` 未真正走一次重算，审计里的 before/after 金额没被断言 | **已闭环** | 新增用例 `test_ac4_recalc_audit_log_contains_before_after_amounts`：先锁月、再造合同/回款/台账，然后调 `POST /api/v1/admin/commission/recalculate`，最后按 `action=commission.recalculate` 查审计，断言 `before_amount_cents == 10000`、`after_amount_cents == 15000`，且原始台账金额不变、`month_locks.locked` 仍为 true。 |
