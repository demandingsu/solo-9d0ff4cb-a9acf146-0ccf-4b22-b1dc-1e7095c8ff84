# 基金会在途款核对系统

FastAPI + SQLite：录入银行流水，按月底重算逐划款在途余额，封存每月一份底稿，
月结后补传/更正流水时逐笔对照封存底稿与当前重算结论。
另提供**到账时效明细分页**：财务按付款月分批复核银行到账时效。

## 业务规则

1. **流水字段**：流水号、修订号、划款号、入账日、收付方向（`pay` 付款 / `receive` 到账）、金额（以分计的正整数）。
2. **修订机制**：同一流水号取最高修订号为有效版本；旧版不删，置 `is_active=0` 并记录 `superseded_by_batch`。可更正划款号与入账日。
3. **冲突退回**：同流水号同修订号但内容不一致，返回 409，**整批回滚**（批内自查 + 库内唯一约束 `UNIQUE(flow_no, revision)` 双保险）。同号同修订同内容重报幂等。
4. **核算口径**：核算某月底只取有效流水中 `post_date <= 月底` 的记录；按划款号分组，`在途 = 付款合计 − 到账合计`。一次划款一次付出、可分笔到账。
5. **待核三类**（单列 `pending_transfers`）：
   - `missing_payment` 缺付款：只有到账没有付款；
   - `over_received` 超额到账：到账合计 > 付款合计；
   - `duplicate_payment` 重复付款：同一划款号出现多笔付款。
6. **封存**：每月只封存一份（`working_papers.month_end` 唯一），内容含逐笔在途结论、待核标记、note、每个划款的流水依据、全部依据流水快照。封存后旧数据永不改写。
7. **对照**：`/papers/{id}/compare` 在**同一个只读快照事务**里读取封存依据与当前数据并分别重算，输出变动流水（added/removed/revision_changed/content_changed）与划款结论字段级差异。封存、对照各自只取同一次已提交数据。
8. **截止口径一致**：重算与封存一样只取不晚于该月底的入账记录；跨月补传不会改变历史月底数字。

## 到账时效明细（按付款月复核）

面向财务按月复核“钱何时到账”的台账，规则如下：

1. **纳入范围**：只列**恰好一笔有效付款**的划款；缺付款、重复付款的划款仍由 `/compute`
   的待核口径处理，不进入本明细。
2. **待核（`pending`）**：到账合计**超过**付款额（`over_received`），或任一有效到账日
   **早于**付款日（`received_before_payment`）。待核时不给结清日/未到账金额。
3. **结清（`settled`）**：到账合计等于付款额，`settled_date` 取**最后到账日**。
4. **未足额（`outstanding`）**：到账合计小于付款额，`outstanding_cents` 列未到账金额。
5. **附依据**：每条明细带付款流水号/修订号（`payment_flow_no`/`payment_revision`）与
   全部有效到账流水（`receipt_flows`，含流水号、修订号、入账日、金额）。
6. **维护方式**：明细随流水在**同一事务**内增量更新；每批只重算“有效流水确实变化”的划款。
   改归属时原、新划款都重算；改付款月时该划款自动从原月移出、进入新月。
   录入失败整批回滚，明细沿用原结果。
7. **分页与轮次固定**：`/timing-details` 直接读已维护的明细。首页返回 `as_of`（本页固定的
   明细版本批次）与 `next_cursor`；续页**只需回传 `cursor`**，游标已内含批次与筛选口径，
   因此重试、重启服务后仍沿原结果续读，后续录入不会改变本轮翻页。
8. **旧库升级**：启动时若为无明细表的旧库，按当前有效流水一次性补齐明细，并写入基线
   版本；升级幂等，之后录入继续走增量维护。

## 目录结构

```
app/
  config.py    # DB 路径（环境变量 TRANSIT_DB_PATH）、方向常量
  schemas.py   # Pydantic 请求/响应模型，输入校验
  compute.py   # 核算纯函数：最高修订、月底截止、待核、到账时效明细、差异对照
  database.py  # SQLite 建表、事务/快照、录入、明细增量维护、封存、分页查询
  main.py      # FastAPI 路由（/docs 可交互操作）
tests/
  test_api.py      # 55 项端到端断言（TestClient）
  test_timing.py   # 65 项到账时效明细断言：分页/轮次固定/跨月更正/改归属/旧库升级
  smoke_http.py    # 对真实 uvicorn 进程的 HTTP 冒烟（仅标准库）
```

## 用 Docker Compose 启动

```bash
docker compose up -d --build
# 打开 http://localhost:8000/docs （根路径 / 自动跳转 /docs）
docker compose logs -f
docker compose down          # 保留数据卷 transit-data
```

 SQLite 文件位于命名卷 `transit-data:/data/transit.db`，容器重建不丢数据。

## 本地直接运行（无需 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
TRANSIT_DB_PATH=./data/transit.db uvicorn app.main:app --reload --port 8000
```

## 接口一览（/docs 在线试）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/flows/import` | 整批录入流水；冲突 409 整批退回 |
| GET  | `/flows` | 查流水，默认仅有效版本，`active_only=false` 看全部修订留痕 |
| GET  | `/compute?month_end=2026-10-31` | 按当前流水重算该月底（日期自动归一为当月最后一天） |
| POST | `/papers/seal?month_end=2026-10-31` | 封存月底稿，每月一份，重复封存 409 |
| GET  | `/papers` | 底稿清单 |
| GET  | `/papers/{paper_id}` | 底稿详情：逐笔结论 + 冻结的流水依据快照 |
| GET  | `/papers/{paper_id}/compare` | 封存 vs 当前重算的逐笔对照，列出变动流水 |
| GET  | `/timing-details` | 到账时效明细分页：按付款日、划款号排序，固定轮次可续读 |
| GET  | `/health` | 健康检查 |

## 操作示例

```bash
# 录入
curl -X POST localhost:8000/flows/import -H 'Content-Type: application/json' -d '{
  "flows": [
    {"flow_no":"F001","revision":1,"transfer_no":"T1","post_date":"2026-10-05",
     "direction":"pay","amount_cents":1000000}
  ]}'
# 重算
curl 'localhost:8000/compute?month_end=2026-10-31'
# 封存
curl -X POST 'localhost:8000/papers/seal?month_end=2026-10-31'
# 补传/更正后对照
curl 'localhost:8000/papers/1/compare'
# 到账时效明细：首页（默认最新批次，每页 20）
curl 'localhost:8000/timing-details?page_size=20'
# 续页：把上一页返回的 next_cursor 原样回传即可，无需再带 as_of
curl 'localhost:8000/timing-details?page_size=20&cursor=<next_cursor>'
# 按付款月/结论筛选
curl 'localhost:8000/timing-details?payment_month=2026-09&status=pending'
```

## 测试

```bash
pip install -r requirements-test.txt
python3 tests/test_api.py        # 55 项断言，含跨月补传、改归属、冲突、待核
python3 tests/test_timing.py     # 65 项到账时效明细断言：分页、轮次固定、跨月更正、旧库升级
python3 tests/smoke_http.py      # 需先启动服务，走真实 HTTP
```

## 设计要点

- **一致性**：写入用 `BEGIN IMMEDIATE` 事务（嵌套时自动降级 SAVEPOINT）；重算/封存/对照用只读快照事务，保证同一次请求只看到同一次已提交数据。明细在同一写事务内随流水提交，失败整批回滚。
- **可追溯**：全部修订物理留存；封存时把依据流水完整复制进 `paper_flow_evidence`，后续任何补传/更正都不影响已签字底稿。
- **可复核的分页**：`timing_detail_history` 记录每个批次下明细的版本，`/timing-details` 按 `as_of` 批次取当期版本，因此同一轮翻页不受后续录入影响，服务重启后游标仍有效。
- **增量维护**：只重算本批真正改变有效流水的划款（新生效流水 + 被顶替的旧有效流水所属划款），重报/乱序低修订不触发重算。
- **金额安全**：全程整数分，杜绝浮点误差。
- **无 ORM 依赖**：标准库 sqlite3 + 手写 SQL，表结构透明可审。

## 待办 / 可扩展

- 本环境无 Docker 守护进程，`Dockerfile` / `docker-compose.yml` 仅做静态校验与本地等价启动验证，未实跑容器；部署前执行一次 `docker compose up -d --build` 并打开 `/docs` 复核。
- 明细历史表随批次增长，目前未设归档策略；长期运行需按保留期清理或迁移 PostgreSQL。
- 缺认证鉴权，生产部署需加 API Key/OIDC 与网络访问控制。
- 目前单文件 SQLite；高并发或多实例部署时评估 WAL 之上的写串行化或迁移 PostgreSQL。
- 单次录入上限仍为 1 万条；海量历史数据可再加批量导入文件入口（明细分页已完成）。
- 待核结论仅标记不阻断，后续可加审批流/核销流。
