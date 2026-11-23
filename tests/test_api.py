"""端到端测试：跨月补传、改归属、冲突退回、待核、封存不可变。

直接用 uvicorn 同款 ASGI TestClient 起服务，SQLite 指向临时文件。
运行：python -I tests/test_api.py（需 PYTHONPATH 指向依赖目录）
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

# 隔离模式 (-I) 下手动注入依赖与项目根
for p in ("/tmp/pylibs", str(Path(__file__).resolve().parents[1])):
    if p not in sys.path:
        sys.path.insert(0, p)

# 必须在 import app 之前指定数据库
_TMP = tempfile.mkdtemp(prefix="transit_test_")
os.environ["TRANSIT_DB_PATH"] = str(Path(_TMP) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import compute, database as db  # noqa: E402
from app.config import PAY, RECEIVE  # noqa: E402

client = TestClient(app)
client.__enter__()  # 触发 lifespan 建表；测试脚本退出时进程结束即可
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def flow(flow_no, revision, transfer_no, post_date, direction, amount):
    return {
        "flow_no": flow_no, "revision": revision, "transfer_no": transfer_no,
        "post_date": post_date, "direction": direction, "amount_cents": amount,
    }


def import_flows(rows, expected=201):
    r = client.post("/flows/import", json={"flows": rows})
    assert r.status_code == expected, r.text
    return r.json()


def find_position(resp, transfer_no):
    return next(p for p in resp["transfers"] if p["transfer_no"] == transfer_no)


# ------------------------------------------------------------------
print("== 1. 十月初始流水：T1 全额在途，T2 部分到账 ==")
batch1 = import_flows([
    flow("F001", 1, "T1", "2026-10-05", PAY, 1_000_000),       # 1万
    flow("F002", 1, "T2", "2026-10-10", PAY, 5_000_000),       # 5万
    flow("F003", 1, "T2", "2026-10-20", RECEIVE, 2_000_000),   # 到账2万
])
check("批次1 插入3条", batch1["inserted"] == 3, batch1)

r = client.get("/compute", params={"month_end": "2026-10-31"})
oct_compute = r.json()
check("10月在途总额 4000000",
      oct_compute["total_in_transit_cents"] == 4_000_000, oct_compute)
t1 = find_position(oct_compute, "T1")
check("T1 在途 1000000 / normal",
      t1["in_transit_cents"] == 1_000_000 and t1["status"] == "normal", t1)
check("T1 依据含 F001", t1["evidence_flow_nos"] == ["F001"], t1)
t2 = find_position(oct_compute, "T2")
check("T2 在途 3000000", t2["in_transit_cents"] == 3_000_000, t2)
check("10月无待核", oct_compute["pending_transfers"] == [], oct_compute)

print("== 2. 封存十月底稿（月结签字：仅含 F001-F003） ==")
r = client.post("/papers/seal", params={"month_end": "2026-10-15"})  # 非月末，自动归一
sealed = r.json()
paper_id = sealed["paper_id"]
check("封存日期归一为 2026-10-31", sealed["month_end"] == "2026-10-31", sealed)
check("封存快照批次为 batch1", sealed["batch_id"] == 1, sealed)
check("封存总额 4000000", sealed["total_in_transit_cents"] == 4_000_000, sealed)

r = client.post("/papers/seal", params={"month_end": "2026-10-31"})
check("同月重复封存 409", r.status_code == 409, r.text)

print("== 3. 月结后补传：缺付款、超额到账、重复付款三类待核 ==")
import_flows([
    flow("F101", 1, "T_MISS", "2026-10-25", RECEIVE, 500_000),        # 只有到账
    flow("F102", 1, "T_OVER", "2026-10-26", PAY, 300_000),
    flow("F103", 1, "T_OVER", "2026-10-27", RECEIVE, 400_000),        # 超100000
    flow("F104", 1, "T_DUP", "2026-10-28", PAY, 100_000),
    flow("F105", 1, "T_DUP", "2026-10-29", PAY, 100_000),             # 第二笔付款
])
r = client.get("/compute", params={"month_end": "2026-10-31"}).json()
pending_nos = {p["transfer_no"]: p["status"] for p in r["pending_transfers"]}
check("待核3笔且状态正确",
      pending_nos == {"T_MISS": "missing_payment",
                      "T_OVER": "over_received",
                      "T_DUP": "duplicate_payment"}, pending_nos)
check("超额到账在途为负 -100000",
      find_position(r, "T_OVER")["in_transit_cents"] == -100_000)

print("== 4. 同号同修订内容冲突：整批退回，数据不变 ==")
r = client.post("/flows/import", json={"flows": [
    flow("F002", 1, "T2", "2026-10-10", PAY, 9_999_999),  # 金额冲突
    flow("F200", 1, "T9", "2026-10-30", PAY, 100),        # 同批第二条也不能落库
]})
check("冲突返回 409", r.status_code == 409, r.text)
flows_now = client.get("/flows", params={"flow_no": "F200"}).json()
check("整批回滚：F200 不存在", flows_now == [], flows_now)
f002 = client.get("/flows", params={"flow_no": "F002"}).json()
check("F002 金额保持 5000000",
      len(f002) == 1 and f002[0]["amount_cents"] == 5_000_000, f002)

print("== 5. 幂等重报：同号同修订同内容不重复落库 ==")
idem = import_flows([flow("F001", 1, "T1", "2026-10-05", PAY, 1_000_000)])
check("重报标记 already_present",
      idem["already_present"] == 1 and idem["inserted"] == 0, idem)

print("== 6. 跨月补传：11月收到 T1 全额到账（10月底在途应转为0） ==")
import_flows([flow("F004", 1, "T1", "2026-11-03", RECEIVE, 1_000_000)])
r = client.get("/compute", params={"month_end": "2026-10-31"}).json()
check("11月到账不影响10月底：T1 仍在途 1000000",
      find_position(r, "T1")["in_transit_cents"] == 1_000_000)
r = client.get("/compute", params={"month_end": "2026-11-30"}).json()
check("11月底 T1 在途清零",
      find_position(r, "T1")["in_transit_cents"] == 0
      and find_position(r, "T1")["status"] == "normal")

print("== 7. 对照十月封存：补传后逐笔差异 ==")
r = client.get(f"/papers/{paper_id}/compare")
cmp_ = r.json()
check("对照识别出差异", cmp_["has_difference"] is True, cmp_)
changed = {c["flow_no"]: c["change_type"] for c in cmp_["changed_flows"]}
check("变动流水含新增待核 F101..F105（F004 因11月入账被截止排除）",
      all(f"F10{i}" in changed for i in range(1, 6)) and "F004" not in changed,
      changed)
# F004 入账日 11-03 晚于 10-31：封存与重算的10月依据都不应含它
recomputed_t1 = find_position(cmp_["recomputed"], "T1")
check("重算10月底 T1 依据仍只有 F001（F004 被月底截止排除）",
      recomputed_t1["evidence_flow_nos"] == ["F001"], recomputed_t1)
pos_changes = {c["transfer_no"]: c["change_type"]
               for c in cmp_["position_changes"]}
check("新增待核划款 T_MISS/T_OVER/T_DUP",
      pos_changes.get("T_MISS") == "added"
      and pos_changes.get("T_OVER") == "added"
      and pos_changes.get("T_DUP") == "added", pos_changes)
check("封存快照总额不变", cmp_["sealed"]["total_in_transit_cents"] == 4_000_000)
check("封存批次1 / 当前批次为最新批次",
      cmp_["sealed_batch_id"] == 1
      and cmp_["current_batch_id"] == client.get("/health").json()["latest_batch_id"],
      cmp_)

print("== 8. 封存底稿及其依据照常可查 ==")
detail = client.get(f"/papers/{paper_id}").json()
check("底稿元数据可查", detail["paper"]["paper_id"] == paper_id)
sealed_flows = {e["flow_no"]: e for e in detail["flow_evidence"]}
check("封存依据冻结为签字时点的3条 F001-F003（补传记录不入旧底稿）",
      set(sealed_flows) == {"F001", "F002", "F003"}, set(sealed_flows))
check("封存依据不含 F004（11月入账）", "F004" not in sealed_flows)
sealed_t2 = next(p for p in detail["positions"] if p["transfer_no"] == "T2")
check("封存 T2 结论保持在途3000000",
      sealed_t2["in_transit_cents"] == 3_000_000, sealed_t2)

print("== 9. 改归属：F003 修订2 把到账从 T2 改挂 T1，且入账日更正 ==")
import_flows([
    flow("F003", 2, "T1", "2026-10-21", RECEIVE, 2_000_000),  # 原 T2/10-20
])
r = client.get("/flows", params={"flow_no": "F003", "active_only": False}).json()
check("F003 两版都留存，仅修订2有效",
      {x["revision"]: x["is_active"] for x in r} == {1: False, 2: True}, r)
old = next(x for x in r if x["revision"] == 1)
health_batch = client.get("/health").json()["latest_batch_id"]
check("旧版 superseded_by_batch 已记录为最新批次",
      old["superseded_by_batch"] == health_batch, old)
r_active = client.get("/flows", params={"flow_no": "F003"}).json()
check("默认查询只返回有效最高修订", len(r_active) == 1 and r_active[0]["revision"] == 2)

r = client.get("/compute", params={"month_end": "2026-10-31"}).json()
t1 = find_position(r, "T1")
t2 = find_position(r, "T2")
check("改挂后 T1 在途 = 1000000-2000000-? （F004在11月不计）=-1000000",
      t1["in_transit_cents"] == -1_000_000, t1)
check("改挂后 T1 依据含 F001/F003",
      t1["evidence_flow_nos"] == ["F001", "F003"], t1)
check("T2 恢复全额在途 5000000", t2["in_transit_cents"] == 5_000_000, t2)
check("T1 因超额到账进入待核",
      t1["status"] == "over_received" and
      any(p["transfer_no"] == "T1" for p in r["pending_transfers"]))

r = client.get(f"/papers/{paper_id}/compare").json()
f003_change = next(c for c in r["changed_flows"] if c["flow_no"] == "F003")
check("对照识别 F003 修订升级",
      f003_change["change_type"] == "revision_changed"
      and f003_change["sealed_revision"] == 1
      and f003_change["current_revision"] == 2, f003_change)
check("F003 快照保留新旧划款号",
      f003_change["sealed_snapshot"]["transfer_no"] == "T2"
      and f003_change["current_snapshot"]["transfer_no"] == "T1")
t1c = next(c for c in r["position_changes"] if c["transfer_no"] == "T1")
check("T1 结论字段级差异含在途/状态/依据",
      set(t1c["field_diffs"]) >= {"in_transit_cents", "status",
                                  "evidence_flow_nos"}, t1c)
t2c = next(c for c in r["position_changes"] if c["transfer_no"] == "T2")
check("T2 在途差异 3000000 -> 5000000",
      t2c["field_diffs"]["in_transit_cents"] ==
      {"sealed": 3_000_000, "recomputed": 5_000_000}, t2c)

print("== 10. 纯函数单元：修订顺序无关、月底边界 ==")
f1 = compute.Flow("X", 1, "TA", date(2026, 10, 1), PAY, 100)
f2 = compute.Flow("X", 3, "TA", date(2026, 10, 2), PAY, 200)
f3 = compute.Flow("X", 2, "TA", date(2026, 10, 3), PAY, 300)
eff = {f.flow_no: f for f in compute.effective_flows([f1, f2, f3])}
check("乱序录入仍取最高修订3", eff["X"].revision == 3 and eff["X"].amount_cents == 200)
pos = compute.compute_positions([f2], date(2026, 10, 31))
check("入账日=月底当天计入", pos[0]["paid_cents"] == 200)
pos = compute.compute_positions([f2], date(2026, 10, 1))
check("入账日晚于截止日排除", pos == [], pos)

print("== 11. 十一月封存（每月一份，互不干扰） ==")
nov = client.post("/papers/seal", params={"month_end": "2026-11-30"}).json()
check("11月封存成功", nov["month_end"] == "2026-11-30" and nov["paper_id"] != paper_id)
papers = client.get("/papers").json()
check("底稿列表2份", len(papers) == 2, papers)
check("11月 T1 含 F004 后在途 = 1000000+(-2000000)-1000000 = -2000000",
      find_position(nov, "T1")["in_transit_cents"] == -2_000_000)

print("== 12. 输入校验 ==")
r = client.post("/flows/import", json={"flows": [
    flow("F9", 1, "T9", "2026-10-01", "sideways", 100)]})
check("非法方向 422", r.status_code == 422)
r = client.post("/flows/import", json={"flows": [
    flow("F9", 0, "T9", "2026-10-01", PAY, 100)]})
check("修订号<1 422", r.status_code == 422)
r = client.post("/flows/import", json={"flows": [
    flow("F9", 1, "T9", "2026-10-01", PAY, 0)]})
check("金额非正 422", r.status_code == 422)
r = client.get("/papers/9999")
check("不存在底稿 404", r.status_code == 404)

print("== 13. 边界：空库重算与空封存 ==")
# 当前库已有数据，换一个全新的临时库验证空态
import sqlite3  # noqa: E402
empty_db = Path(_TMP) / "empty.db"
empty_conn = sqlite3.connect(str(empty_db))
empty_conn.close()
from app import database as db2  # noqa: E402
db2.init_db(empty_db)
c2 = db2.connect(empty_db)
with db2.snapshot(c2):
    empty_positions = compute.compute_positions(
        compute.rows_to_flows(db2.active_flow_rows(c2)), date(2026, 10, 31))
check("空库重算为空列表、总额0", empty_positions == [])
c2.close()

# 用依赖覆盖把空库挂到 API 上，验证空封存 409
from app.main import get_conn  # noqa: E402
_empty_conn = db2.connect(empty_db)
app.dependency_overrides[get_conn] = lambda: _empty_conn
r = client.post("/papers/seal", params={"month_end": "2026-10-31"})
check("无入账记录时封存 409", r.status_code == 409, r.text)
r = client.get("/compute", params={"month_end": "2026-10-31"})
check("空库重算 as_of_batch_id 为 null",
      r.status_code == 200 and r.json()["as_of_batch_id"] is None
      and r.json()["transfers"] == [], r.text)
app.dependency_overrides.clear()
_empty_conn.close()

print("== 14. 乱序补报低修订号：不得出现两条有效版本 ==")
import_flows([flow("F300", 3, "T300", "2026-10-01", PAY, 300)])
import_flows([flow("F300", 1, "T300", "2026-10-01", PAY, 100)])  # 后补的低修订
rows_300 = client.get("/flows", params={"flow_no": "F300", "active_only": False}).json()
active_300 = [x for x in rows_300 if x["is_active"]]
check("F300 仅修订3有效", len(active_300) == 1 and active_300[0]["revision"] == 3, rows_300)
inactive_300 = next(x for x in rows_300 if x["revision"] == 1)
check("后补的修订1 直接标记失效", inactive_300["is_active"] is False)
r = client.get("/compute", params={"month_end": "2026-10-31"}).json()
t300 = find_position(r, "T300")
check("T300 按最高修订3取金额300", t300["paid_cents"] == 300, t300)

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
