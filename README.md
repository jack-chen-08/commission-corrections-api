# 提成人工调整与重算接口（双人审批、留痕）

会员空间平台 · 提成规则表模块（Stage vi 陪跑与二期第一批）。

实现两类更正操作，统一前缀 `/api/admin/commission/corrections`：

1. **人工调整**：对某合同的某业务员追加 `kind=adjustment` 的提成记录，金额可正可负（整数分，单条绝对值上限由环境变量 `COMM_ADJUST_MAX_CENTS` 控制，默认 5000000 分）。必填原因类别（规则表外特批 / 数据录入错误 / 业务员归属变更 / 其他）与说明（≤300 字）。提交后 `status=pending`，需第二人（招商主管或以上）审批后才生效，生效前不计入月结单。
2. **重算**：回款或合同字段被更正后，按 `payment_id` 重新执行提成计算。原记录未锁定月份则作废（`voided_reason=recalculated`）并生成 `supersedes_id` 指向原记录的新记录；原记录月份已锁定则不作废，改为在**当前未锁定月份**生成差额调整记录。

两类操作均写审计日志（调整前后金额、操作人、审批人）。

---

## 1. 技术栈

| 项 | 版本 |
| --- | --- |
| Python | 3.12（要求 3.11+） |
| FastAPI | 0.115.6 |
| SQLAlchemy | 2.0.36（2.x 声明式） |
| Alembic | 1.14.0 |
| PostgreSQL | 16（`pgvector/pgvector:pg16` 镜像） |
| 驱动 | psycopg 3.2.3（binary） |
| 测试 | pytest 8.3.4 + httpx 0.28.1（TestClient） |

金额全部以「分」为单位的整数存储（`BIGINT`），代码中无浮点金额运算。

---

## 2. 目录结构

```
.
├── alembic/
│   ├── env.py
│   └── versions/0001_init.py          # 初始表结构（可 downgrade 到 base）
├── app/
│   ├── api/
│   │   ├── deps.py                    # 依赖：数据库会话、当前身份
│   │   └── routes_corrections.py      # 路由层，仅做参数绑定与委派
│   ├── service/
│   │   └── corrections.py             # 全部业务规则（调整/审批/重算/月结/审计查询）
│   ├── audit.py                       # 审计日志写入
│   ├── calculator.py                  # 提成规则计算桩（S2 规则表的对接点）
│   ├── config.py                      # 环境变量配置
│   ├── db.py                          # engine / session 工厂
│   ├── iam.py                         # S2-IAM 接口桩（角色等级、组织范围）
│   ├── main.py                        # FastAPI 应用装配
│   ├── models.py                      # 合同/回款只读副本、提成台账、月结锁、审计日志
│   ├── schemas.py                     # 请求模型与校验（金额上限、说明字数、原因类别）
│   └── seed_demo.py                   # 虚构演示数据
├── scripts/demo.sh                    # curl 端到端演示脚本
├── tests/
│   ├── conftest.py                    # 迁移建库、事务级清表、身份头与数据工厂
│   └── test_corrections.py            # 15 个用例（含 4 条 AC 的显式命名用例）
├── EVIDENCE/                          # 真实执行证据（测试、迁移、演示、环境）
├── docker-compose.yml
├── requirements.txt
├── pytest.ini
├── alembic.ini
├── .env.example
├── README.md
└── DELIVERY.md
```

---

## 3. 快速开始（命令可直接复制执行）

以下命令均在仓库根目录执行：

```bash
cd commission-corrections-api
```

### 3.1 起数据库（PostgreSQL 16，端口 55432）

```bash
docker compose up -d
docker compose ps
```

首次启动会自动建库 `commission`，健康检查通过后状态为 `healthy`。

### 3.2 建虚拟环境并装依赖

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3.3 配置环境变量

配置只从环境变量读取，代码与仓库中不含任何真实连接串或密钥。本地开发可直接导出：

```bash
export DATABASE_URL="postgresql+psycopg://comm:comm_local_pw@127.0.0.1:55432/commission"
export COMM_ADJUST_MAX_CENTS=5000000
export COMM_CURRENT_PERIOD=2026-11
export APP_ENV=local
```

也可以复制模板后由 shell 加载：

```bash
cp .env.example .env
set -a; . ./.env; set +a
```

> `.env` 已在 `.gitignore` 中，不会提交。生产环境的 `DATABASE_URL`、数据库口令由部署平台注入，`COMM_ADJUST_MAX_CENTS` 按机构风控要求配置。

### 3.4 执行迁移

```bash
.venv/bin/alembic upgrade head
.venv/bin/alembic current
```

回滚与重建（可选，用于验证迁移可逆）：

```bash
.venv/bin/alembic downgrade base
.venv/bin/alembic upgrade head
```

### 3.5 跑测试

```bash
.venv/bin/pytest -v
```

测试会先自动执行 `alembic upgrade head`（见 `tests/conftest.py` 的 session 级 fixture），每个用例前清空业务表，全部使用虚构数据，不依赖外网。

### 3.6 启动 API 并跑 curl 演示

终端 A：

```bash
export DATABASE_URL="postgresql+psycopg://comm:comm_local_pw@127.0.0.1:55432/commission"
export COMM_CURRENT_PERIOD=2026-11
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8010
```

终端 B：

```bash
cd commission-corrections-api
bash scripts/demo.sh
```

演示脚本依次执行：造虚构数据 → 提交调整 → 查看审批前月结（pending 不计入）→ 申请人自批（403）→ 第二人审批 → 查看审批后月结 → 管理员锁定 2026-09 → 更正回款后重算 → 锁定月合计不变、差额进 2026-11 → 查询两类审计日志。完整输出见 `EVIDENCE/curl_demo.txt`。

---

## 4. 环境变量

| 变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `DATABASE_URL` | 是 | 无 | SQLAlchemy 连接串，缺失时启动即报 `ConfigError` |
| `COMM_ADJUST_MAX_CENTS` | 否 | `5000000` | 单条人工调整金额绝对值上限（分），必须为正整数 |
| `COMM_CURRENT_PERIOD` | 否 | 系统当月 | 「当前未锁定月份」的口径，格式 `YYYY-MM`；锁定月重算的差额进这个月 |
| `APP_ENV` | 否 | `local` | 环境标识，仅用于日志与返回体标识 |

---

## 5. 数据模型

所有业务表统一含 `id(UUID)`、`org_id`、`site_id`、`created_at`、`created_by`、`updated_at`、`deleted_at`（软删除）。

| 表 | 用途 | 关键字段 |
| --- | --- | --- |
| `contracts` | 合同只读副本（由合同服务同步） | `contract_no`、`salesperson_id`、`amount_cents`、`commission_rate_bp`、`signed_at` |
| `payments` | 回款只读副本（由回款服务同步） | `contract_id`、`paid_amount_cents`、`paid_at` |
| `commission_records` | 提成台账 | `kind`(base/adjustment)、`origin`(calculation/manual/recalculation_delta)、`amount_cents`、`status`(pending/active/voided)、`voided_reason`、`supersedes_id`、`reason_category`、`note`、`trace`(JSONB)、`approval_chain`(JSONB)、`period_month` |
| `month_locks` | 月结锁定，`org_id + period_month` 唯一 | `locked`、`locked_at`、`locked_by` |
| `audit_logs` | 审计日志 | `entity_type`、`entity_id`、`action`、`before_amount_cents`、`after_amount_cents`、`actor_id`、`approver_id`、`detail`(JSONB) |

`trace` 的语义区分：

- 人工调整：`origin=manual_input`，`calculation.applied=false`，记录申请人输入（合同、业务员、金额、原因类别、说明）与 `approval_chain`，**不写**规则版本与计算步骤。
- 规则计算/重算：`origin=rule_calculation`，记录规则版本、命中规则与计算步骤。
- 锁定月差额：`origin=recalculation_delta`，记录 `original_record_id`、原金额、重算金额、差额、原月份与计入月份。

---

## 6. 接口清单

前缀 `PREFIX=/api/admin/commission/corrections`。

| 方法 | 路径 | 说明 | 最低角色 |
| --- | --- | --- | --- |
| POST | `/adjustments` | 提交人工调整，返回 201，`status=pending` | 业务员 |
| POST | `/adjustments/{id}/approve` | 审批通过，`status=active` 并计入月结 | 招商主管 |
| POST | `/adjustments/{id}/reject` | 驳回，`status=voided`、`voided_reason=rejected` | 招商主管 |
| GET | `/adjustments` | 按状态列出本组织调整 | 业务员 |
| GET | `/adjustments/{id}` | 调整详情（含 trace、审批链） | 业务员 |
| POST | `/recalculate` | 按 `payment_id` 重算，返回 201 | 招商主管 |
| GET | `/records/{id}` | 任意提成记录详情 | 业务员 |
| GET | `/monthly-summary?year=&month=` | 月结口径合计（区分 active / pending / voided） | 业务员 |
| GET | `/audit-logs?entity_id=&action=` | 审计日志查询 | 业务员 |
| POST | `/month-locks` | 设置/解除月结锁定 | 平台管理员 |
| GET | `/health` | 健康检查 | 无 |

### 6.1 提交人工调整

```bash
curl -sS -X POST "http://127.0.0.1:8010/api/admin/commission/corrections/adjustments" \
  -H "X-User-Id: 66666666-6666-4666-8666-666666666666" \
  -H "X-Org-Id: 33333333-3333-4333-8333-333333333333" \
  -H "X-Site-Id: 44444444-4444-4444-8444-444444444444" \
  -H "X-Roles: sales_rep" \
  -H "Content-Type: application/json" \
  -d '{
        "contract_id": "77777777-7777-4777-8777-777777777777",
        "salesperson_id": "55555555-5555-4555-8555-555555555555",
        "payment_id": "88888888-8888-4888-8888-888888888888",
        "amount_cents": 320000,
        "reason_category": "数据录入错误",
        "note": "虚构演示：财务复核发现首期回款少记，按复核单补差 3200 元。",
        "period_month": "2026-09"
      }'
```

校验规则：

- `amount_cents` 必须是整数（`strict=True`，字符串与小数一律 422），`0` 返回 422 `AMOUNT_ZERO`，绝对值超上限返回 422 `AMOUNT_EXCEEDS_LIMIT` 并回传 `limit_cents`。
- `note` 去空格后不得为空，长度（字符数）≤300。
- `reason_category` 只接受四个枚举值。
- 目标月份已锁定返回 409 `PERIOD_LOCKED`。
- 合同不属于当前 `org_id` 返回 404 `CONTRACT_NOT_FOUND`（不泄露跨组织资源是否存在）。

### 6.2 审批与驳回

```bash
# 第二人审批（招商主管）
curl -sS -X POST "$PREFIX/adjustments/$ADJ_ID/approve" \
  -H "X-User-Id: a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1" \
  -H "X-Org-Id: $ORG_ID" -H "X-Site-Id: $SITE_ID" -H "X-Roles: channel_supervisor" \
  -H "Content-Type: application/json" -d '{"comment":"财务复核单齐备，同意补差"}'

# 驳回
curl -sS -X POST "$PREFIX/adjustments/$ADJ_ID/reject" \
  -H "X-User-Id: a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1" \
  -H "X-Org-Id: $ORG_ID" -H "X-Site-Id: $SITE_ID" -H "X-Roles: channel_supervisor" \
  -H "Content-Type: application/json" -d '{"reason":"缺少财务复核单"}'
```

- 申请人审批/驳回自己的调整，即使其角色等级足够，也返回 **403 `SELF_APPROVAL_FORBIDDEN`**（记录不可被本人前置状态绕过）。
- 非 `pending` 状态重复处理返回 409 `NOT_PENDING`。
- 待审批期间目标月份被锁定，审批返回 409 `PERIOD_LOCKED`，不允许补记入已结账月份。

### 6.3 重算

```bash
curl -sS -X POST "$PREFIX/recalculate" \
  -H "X-User-Id: a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1" \
  -H "X-Org-Id: $ORG_ID" -H "X-Site-Id: $SITE_ID" -H "X-Roles: channel_supervisor" \
  -H "Content-Type: application/json" \
  -d '{"payment_id":"88888888-8888-4888-8888-888888888888","correction_reason":"回款金额录入更正：1000 元 → 1500 元"}'
```

返回体 `mode` 二选一：

| 场景 | `mode` | 原记录 | 新记录 |
| --- | --- | --- | --- |
| 原记录月份**未**锁定 | `replaced` | `status=voided`、`voided_reason=recalculated`，`trace.void` 记录作废人与原因 | `kind=base`，`supersedes_id=原记录 id`，计入原月份 |
| 原记录月份**已**锁定 | `delta_in_current_period` | 保持 `active`、金额不变、`month_locked=true` | `kind=adjustment`、`origin=recalculation_delta`，金额为差额，计入 `COMM_CURRENT_PERIOD` 月份 |

其他约束：重算最低角色招商主管（否则 403 `RECALC_RANK_TOO_LOW`）；该回款没有生效的 base 记录返回 409 `NO_ACTIVE_RECORD`；差额为 0 时不生成记录，仅写审计（`detail.mode=no_change`）；原月份与当前月份都被锁定时返回 409 `CURRENT_PERIOD_LOCKED`。

### 6.4 月结口径

`GET /monthly-summary?year=2026&month=11` 返回：

```json
{
  "org_id": "...",
  "period_month": "2026-11",
  "locked": false,
  "total_cents": 5000,
  "active_count": 1,
  "pending_total_cents": 0,
  "pending_count": 0,
  "voided_total_cents": 0,
  "voided_count": 0,
  "by_status": {"active": {"count": 1, "amount_cents": 5000}}
}
```

`total_cents` 只累加 `status=active` 且未软删除的记录，`pending` 单独列示、绝不并入合计，`voided` 也不计入。

### 6.5 审计动作

| action | 触发点 | before → after |
| --- | --- | --- |
| `commission.adjustment.create` | 提交调整 | 0 → 调整金额（`approver_id` 为空，`counted_in_monthly_close=false`） |
| `commission.adjustment.approve` | 审批通过 | 0 → 调整金额（含 `approver_id`、`period_total_before/after_cents`、`comment`） |
| `commission.adjustment.reject` | 驳回 | 调整金额 → 0（含 `approver_id`、驳回原因） |
| `commission.recalculate` | 重算 | 原记录金额 → 重算后金额（含 `mode`、`delta_cents`、原/新记录 id、`voided_reason`） |

---

## 7. 对接点

本仓库按统一约定先用接口桩跑通，替换点均集中在单文件内，业务层无需改动。

| 依赖 | 当前实现 | 正式对接方式 |
| --- | --- | --- |
| **S2-IAM**（身份、角色、数据范围） | `app/iam.py` 从请求头 `X-User-Id` / `X-Org-Id` / `X-Site-Id` / `X-Roles` 构造 `Principal`，角色等级表内置（业务员 1 → 平台管理员 5） | 把 `principal_from_headers` 换成 S2-IAM 的令牌校验与数据范围解析，返回同结构 `Principal`；`require_rank`、组织过滤逻辑保留。请求头桩仅供本地联调，生产网关必须剥离外部传入的同名头 |
| **S2-AUDIT**（审计日志） | `app/audit.py` 写本地 `audit_logs` 表，接口可查 | 在同一事务内追加投递 S2-AUDIT（建议 outbox 表 + 异步投递，避免审计服务抖动影响主流程）；字段已对齐：`entity_type/entity_id/action/before_amount_cents/after_amount_cents/actor_id/approver_id/detail` |
| **提成规则表计算** | `app/calculator.py` 按合同费率 `commission_rate_bp` 对回款金额计算，返回金额与规则 trace | 替换 `compute_commission(contract, payment)` 内部实现，调用 S2 规则表引擎；返回 `(amount_cents:int, trace:dict)` 契约不变 |
| **合同 / 回款数据** | 本地 `contracts`、`payments` 只读副本，由 `app/seed_demo.py` 或上游同步任务写入 | 对接合同服务、回款服务的变更事件或同步任务；本模块只读，不修改上游数据。回款更正后由上游触发 `POST /recalculate` |
| **月结锁定** | 本模块 `month_locks` 表 + `POST /month-locks`（平台管理员） | 若月结由财务系统统一控制，改为读取其锁定状态；`is_period_locked` 是唯一判断入口 |

---

## 8. 测试

```bash
.venv/bin/pytest -v
```

- 共 15 个用例，覆盖 4 条验收标准 + 10 个边界场景（未锁定月重算、金额上限与负数、说明字数、驳回、trace 语义、组织隔离、角色等级、锁定月禁新增、无生效记录、月锁权限）。
- 测试库与开发库共用同一 PostgreSQL 实例，`conftest.py` 在每个用例前 `TRUNCATE` 业务表，用例之间互不影响。
- 证据：`EVIDENCE/pytest_output.txt`（真实输出，15 passed in 1.88s）、`EVIDENCE/env_and_db.txt`、`EVIDENCE/alembic_cycle.txt`、`EVIDENCE/curl_demo.txt`。

验收标准与用例名的对照见 `DELIVERY.md`。

---

## 9. 已知边界

- 审批为单人复核（第二人），未实现多级会签；如需多级，可在 `approval_chain` 上扩展状态机。
- 金额上限按「单条绝对值」校验，未做「同一合同同月累计上限」的机构级额度控制。
- 重算以 `payment_id` 为粒度，一次一个回款；批量重算需在上层编排多次调用。
- 差额记录写入当前未锁定月份，月份口径由 `COMM_CURRENT_PERIOD` 决定，不按服务器时区自动滚动。
- 审计日志目前同步写本地库；投递 S2-AUDIT 的 outbox 与重试尚未实现（见第 7 节）。
- IAM 为请求头桩，未做令牌签名校验、租户越权穿透测试仅覆盖 `org_id` 维度。

---

## 10. 数据与保密

- 仓库内全部为虚构数据：企业名「青禾示例科技有限公司」、合同号 `FICT-*`、UUID 均为人工构造，不含任何真实个人或企业信息。
- 不含客户名称、不含真实连接串与密钥；`DATABASE_URL` 与口令仅通过环境变量注入，`.env` 不入库。
- 未引入任何向境外发送数据的第三方 SDK；依赖仅 FastAPI / SQLAlchemy / Alembic / psycopg / pydantic / pytest / httpx。
