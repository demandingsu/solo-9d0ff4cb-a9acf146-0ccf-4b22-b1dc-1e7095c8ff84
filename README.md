# 基金会在途款核对系统

FastAPI + SQLite：录入银行流水，按月底重算逐划款在途余额，封存每月一份底稿，
月结后补传/更正流水时逐笔对照封存底稿与当前重算结论。

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

## 目录结构

```
app/
  config.py    # DB 路径（环境变量 TRANSIT_DB_PATH）、方向常量
  schemas.py   # Pydantic 请求/响应模型，输入校验
  compute.py   # 核算纯函数：最高修订、月底截止、待核、差异对照
  database.py  # SQLite 建表、事务/快照、录入、封存、查询
  main.py      # FastAPI 路由（/docs 可交互操作）
tests/
  test_api.py      # 49 项端到端断言（TestClient）
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
```

## 测试

```bash
pip install -r requirements-test.txt
python3 tests/test_api.py        # 49 项断言，含跨月补传、改归属、冲突、待核
python3 tests/smoke_http.py      # 需先启动服务，走真实 HTTP
```

## 设计要点

- **一致性**：写入用 `BEGIN IMMEDIATE` 事务（嵌套时自动降级 SAVEPOINT）；重算/封存/对照用只读快照事务，保证同一次请求只看到同一次已提交数据。
- **可追溯**：全部修订物理留存；封存时把依据流水完整复制进 `paper_flow_evidence`，后续任何补传/更正都不影响已签字底稿。
- **金额安全**：全程整数分，杜绝浮点误差。
- **无 ORM 依赖**：标准库 sqlite3 + 手写 SQL，表结构透明可审。

## 待办 / 可扩展

- 环境无 Docker 守护进程，Compose 文件仅做静态校验，未实跑容器；部署前执行一次 `docker compose up --build` 验证。
- 缺认证鉴权，生产部署需加 API Key/OIDC 与网络访问控制。
- 目前单文件 SQLite；高并发或多实例部署时评估 WAL 之上的写串行化或迁移 PostgreSQL。
- 未做分页，单次录入上限 1 万条；海量历史数据需补分页与批量导入文件入口。
- 待核结论仅标记不阻断，后续可加审批流/核销流。
