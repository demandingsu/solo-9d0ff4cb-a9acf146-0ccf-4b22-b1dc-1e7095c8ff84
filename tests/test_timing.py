"""到账时效明细：分页、版本固定、跨月更正、改归属、旧库补齐。

运行：python -I tests/test_timing.py（需 PYTHONPATH 指向依赖目录）
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

for p in ("/tmp/pylibs", str(Path(__file__).resolve().parents[1])):
    if p not in sys.path:
        sys.path.insert(0, p)

_TMP = tempfile.mkdtemp(prefix="transit_timing_")
os.environ["TRANSIT_DB_PATH"] = str(Path(_TMP) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import database as db  # noqa: E402
from app.config import PAY, RECEIVE  # noqa: E402

client = TestClient(app)
client.__enter__()
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def flow(no, rev, tr, d, direction, amt):
    return {"flow_no": no, "revision": rev, "transfer_no": tr,
            "post_date": d, "direction": direction, "amount_cents": amt}


def imp(rows, expected=201):
    r = client.post("/flows/import", json={"flows": rows})
    assert r.status_code == expected, r.text
    return r.json()


def page(**params):
    r = client.get("/timing-details", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def by_no(resp, no):
    return next(x for x in resp["items"] if x["transfer_no"] == no)


# ------------------------------------------------------------------
print("== 1. 只有一笔付款才进入明细：缺付款/重复付款排除 ==")
b1 = imp([
    flow("A01", 1, "T1", "2026-01-05", PAY, 1_000_000),
    flow("A02", 1, "T1", "2026-01-20", RECEIVE, 1_000_000),
    flow("A03", 1, "T_NOPAY", "2026-01-06", RECEIVE, 100),   # 缺付款
    flow("A04", 1, "T_DUP", "2026-01-07", PAY, 100),         # 重复付款
    flow("A05", 1, "T_DUP", "2026-01-08", PAY, 100),
])
check("批次1 重算3笔触及划款，仅1笔形成明细",
      b1["timing"]["recomputed"] == 3 and b1["timing"]["changed"] == 1,
      b1["timing"])
p = page()
check("明细仅 T1", [x["transfer_no"] for x in p["items"]] == ["T1"], p)
t1 = by_no(p, "T1")
check("T1 结清，结清日为最后到账日",
      t1["status"] == "settled" and t1["settled_date"] == "2026-01-20"
      and t1["outstanding_cents"] == 0, t1)
check("附付款/到账流水号与修订号",
      t1["payment_flow_no"] == "A01" and t1["payment_revision"] == 1
      and t1["receipt_flows"] == [{"flow_no": "A02", "revision": 1,
                                   "post_date": "2026-01-20",
                                   "amount_cents": 1_000_000}], t1)
check("付款月为 2026-01", t1["payment_month"] == "2026-01", t1)

print("== 2. 未足额：列未到账金额 ==")
imp([flow("A06", 1, "T2", "2026-01-10", PAY, 500_000),
     flow("A07", 1, "T2", "2026-01-15", RECEIVE, 200_000)])
t2 = by_no(page(), "T2")
check("T2 未足额 300000，无结清日",
      t2["status"] == "outstanding" and t2["outstanding_cents"] == 300_000
      and t2["settled_date"] is None, t2)

print("== 3. 待核：超额到账 / 到账早于付款日 ==")
imp([flow("A08", 1, "T3", "2026-02-01", PAY, 100_000),
     flow("A09", 1, "T3", "2026-02-02", RECEIVE, 120_000)])
imp([flow("A10", 1, "T4", "2026-02-10", PAY, 100_000),
     flow("A11", 1, "T4", "2026-02-09", RECEIVE, 100_000)])
t3 = by_no(page(), "T3")
t4 = by_no(page(), "T4")
check("超额到账标待核", t3["status"] == "pending"
      and t3["pending_reasons"] == ["over_received"]
      and t3["settled_date"] is None, t3)
check("到账早于付款日标待核", t4["status"] == "pending"
      and t4["pending_reasons"] == ["received_before_payment"], t4)
check("状态筛选 pending 得2笔",
      {x["transfer_no"] for x in page(status="pending")["items"]} == {"T3", "T4"})

print("== 4. 分页：按付款日、划款号，游标续读 ==")
imp([flow("B01", 1, "T10", "2026-03-01", PAY, 10),
     flow("B02", 1, "T11", "2026-03-01", PAY, 10),
     flow("B03", 1, "T12", "2026-03-01", PAY, 10),
     flow("B04", 1, "T13", "2026-03-02", PAY, 10)])
first = page(page_size=2)
seen = [x["transfer_no"] for x in first["items"]]
check("首页2条且有下一页", len(seen) == 2 and first["has_more"] is True, first)
check("总数8", first["total_count"] == 8, first)
all_seen = list(seen)
cur = first["next_cursor"]
pages = 1
while cur:
    # 只回传 cursor，不带 as_of 也应续读同一轮
    nxt = page(page_size=2, cursor=cur)
    pages += 1
    all_seen += [x["transfer_no"] for x in nxt["items"]]
    check(f"续页 {pages} 仅凭游标即固定同批次 {first['as_of']}",
          nxt["batch_id"] == first["as_of"], nxt)
    cur = nxt["next_cursor"]
check("翻完无重无漏，共8条", len(all_seen) == 8 and len(set(all_seen)) == 8, all_seen)
check("按付款日排序，同日按划款号",
      all_seen[:6] == ["T1", "T2", "T3", "T4", "T10", "T11"], all_seen[:7])

print("== 5. 轮次固定：翻页中途有新录入，旧游标仍读原批次 ==")
mid = page(page_size=3)
old_cursor = mid["next_cursor"]
old_as_of = mid["as_of"]
imp([flow("B05", 1, "T1", "2026-03-05", RECEIVE, 1)])  # 改变 T1 明细
later = page(page_size=3, as_of=old_as_of, cursor=old_cursor)
check("旧游标续读原批次，条目数与首页轮次一致",
      later["total_count"] == mid["total_count"] == 8, later)
t1_old = by_no(page(as_of=old_as_of), "T1")
t1_new = by_no(page(), "T1")
check("旧批次 T1 结论不随后续录入变化",
      t1_old["received_cents"] == 1_000_000 != t1_new["received_cents"], (t1_old, t1_new))
check("as_of 缺失时取最新批次", page()["as_of"] == client.get(
    "/health").json()["latest_batch_id"])
r = client.get("/timing-details", params={"as_of": 9999})
check("越界 as_of 409", r.status_code == 409, r.text)
r = client.get("/timing-details", params={"as_of": old_as_of + 1, "cursor": old_cursor})
check("as_of 与游标批次不一致 409", r.status_code == 409, r.text)
r = client.get("/timing-details", params={"cursor": "not-base64"})
check("非法 cursor 400", r.status_code == 400, r.text)

# 伪造游标里的筛选值：不得 500，统一 400
import base64 as _b64  # noqa: E402
import json as _json  # noqa: E402


def forged(filters):
    return _b64.urlsafe_b64encode(_json.dumps({
        "as_of": old_as_of, "payment_date": "2026-01-01",
        "transfer_no": "T1", "filters": filters}).encode()).decode()


for bad in ({"status": "bogus"}, {"payment_month": "xx"},
            {"payment_date_from": "not-a-date"}):
    r = client.get("/timing-details", params={"cursor": forged(bad)})
    check(f"伪造游标筛选 {bad} 返回400", r.status_code == 400, r.text)

# 筛选口径固定：首页按付款月筛选，续页沿用；显式改筛选报 409
f1 = page(payment_month="2026-02", page_size=1)
check("2月筛选后总数2（T3/T4）", f1["total_count"] == 2, f1)
f2 = page(cursor=f1["next_cursor"], page_size=1)
check("仅凭游标延续付款月筛选", f2["payment_month"] == "2026-02"
      and f2["total_count"] == 2, f2)
r = client.get("/timing-details", params={"cursor": f1["next_cursor"], "payment_month": "2026-01"})
check("续页擅自改筛选 409", r.status_code == 409, r.text)

# 日期范围筛选（按付款日）
dr = page(payment_date_from="2026-01-06", payment_date_to="2026-02-01")
check("付款日范围筛选生效且有序",
      [x["transfer_no"] for x in dr["items"]] == ["T2", "T3"], dr)
r = client.get("/timing-details", params={"page_size": 0})
check("page_size 越界 422", r.status_code == 422, r.text)
r = client.get("/timing-details", params={"status": "bogus"})
check("非法 status 422", r.status_code == 422, r.text)
r = client.get("/timing-details", params={"payment_month": "2026-1"})
check("非法 payment_month 422", r.status_code == 422, r.text)

print("== 6. 跨月更正：改付款月，两个月明细同步 ==")
imp([flow("C01", 1, "M1", "2026-04-05", PAY, 1_000),
     flow("C02", 1, "M1", "2026-04-15", RECEIVE, 1_000)])
check("4月有 M1", {x["transfer_no"] for x in page(payment_month="2026-04")["items"]} == {"M1"})
check("5月无 M1", "M1" not in {x["transfer_no"] for x in page(payment_month="2026-05")["items"]})
br = imp([flow("C01", 2, "M1", "2026-05-05", PAY, 1_000)])
check("改付款月重算本划款", br["timing"]["recomputed"] == 1
      and br["timing"]["changed"] == 1, br["timing"])
check("原4月明细消失", "M1" not in {x["transfer_no"] for x in page(payment_month="2026-04")["items"]})
m1 = by_no(page(payment_month="2026-05"), "M1")
check("新5月明细出现并因到账早于付款日待核",
      m1["payment_revision"] == 2 and m1["status"] == "pending"
      and m1["pending_reasons"] == ["received_before_payment"], m1)
old_apr = page(as_of=br["batch_id"] - 1, payment_month="2026-04")
check("历史批次仍能看到更正前的4月明细",
      "M1" in {x["transfer_no"] for x in old_apr["items"]}, old_apr)

print("== 6b. 跨月更正后：旧游标仍沿原批次续读 ==")
# 两个不同月份的划款，按付款日排序；首页取 1 条，游标指向第 1 条之后
imp([flow("C10", 1, "M2", "2026-04-20", PAY, 500),
     flow("C11", 1, "M2", "2026-04-25", RECEIVE, 500)])
head = page(page_size=1)
old_cursor_x = head["next_cursor"]
old_as_of_x = head["as_of"]
old_total_x = head["total_count"]
# 跨月更正：把 M1 的付款月从 5 月改回 4 月，再改成 6 月
imp([flow("C01", 3, "M1", "2026-04-01", PAY, 1_000)])
imp([flow("C01", 4, "M1", "2026-06-01", PAY, 1_000)])
cont = page(cursor=old_cursor_x)  # 不带 as_of，仅凭游标
check("旧游标仍固定在更正前批次", cont["as_of"] == old_as_of_x, cont)
check("旧游标续读总数不变（与首页同一轮口径）",
      cont["total_count"] == old_total_x, (cont["total_count"], old_total_x))
# 旧批次中 M1 仍在 5 月、结论为原样；最新批次中 M1 已到 6 月
m1_old = by_no(page(as_of=old_as_of_x, payment_month="2026-05"), "M1")
m1_new = by_no(page(payment_month="2026-06"), "M1")
check("旧批次 M1 仍在5月且付款为修订2",
      m1_old["payment_month"] == "2026-05" and m1_old["payment_revision"] == 2, m1_old)
check("最新批次 M1 已到6月且付款为修订4",
      m1_new["payment_month"] == "2026-06" and m1_new["payment_revision"] == 4, m1_new)
check("旧批次续读与最新批次互不影响",
      m1_old["payment_month"] != m1_new["payment_month"])

print("== 7. 改归属：原划款与新划款都重算 ==")
imp([flow("D01", 1, "OLD", "2026-06-01", PAY, 2_000),
     flow("D02", 1, "OLD", "2026-06-10", RECEIVE, 2_000),
     flow("D03", 1, "NEW", "2026-06-02", RECEIVE, 3_000)])
check("OLD 结清", by_no(page(), "OLD")["status"] == "settled")
check("NEW 尚无付款，无明细", "NEW" not in {x["transfer_no"] for x in page()["items"]})
br = imp([flow("D01", 2, "NEW", "2026-06-01", PAY, 2_000)])
check("改归属重算2笔（原+新）", br["timing"]["recomputed"] == 2, br["timing"])
newd = by_no(page(), "NEW")
check("新划款 NEW 出现且因超额到账待核", newd["status"] == "pending"
      and newd["pending_reasons"] == ["over_received"], newd)
check("NEW 到账依据为该划款原到账流水",
      {r["flow_no"]: r["revision"] for r in newd["receipt_flows"]}
      == {"D03": 1}, newd)
check("原划款 OLD 明细移除", "OLD" not in {x["transfer_no"] for x in page()["items"]})
check("历史批次仍可查 OLD 更正前结果",
      by_no(page(as_of=br["batch_id"] - 1), "OLD")["status"] == "settled")

print("== 8. 只重算有效流水有变化的划款 ==")
br = imp([flow("E01", 1, "UNTOUCHED", "2026-07-01", PAY, 100)])
check("新划款重算1", br["timing"]["recomputed"] == 1, br["timing"])
br = imp([flow("E01", 1, "UNTOUCHED", "2026-07-01", PAY, 100)])  # 幂等重报
check("幂等重报重算0", br["timing"]["recomputed"] == 0, br["timing"])
br = imp([flow("E01", 2, "UNTOUCHED", "2026-07-01", PAY, 100)])  # 内容一致的新修订
check("新修订重算1；明细修订号随之更新，结论不变",
      br["timing"]["recomputed"] == 1 and br["timing"]["changed"] == 1, br["timing"])
e1 = by_no(page(), "UNTOUCHED")
check("明细引用付款修订2且仍未足额",
      e1["payment_revision"] == 2 and e1["outstanding_cents"] == 100, e1)
imp([flow("E02", 3, "E_LOW", "2026-07-01", PAY, 1)])
br = imp([flow("E02", 1, "E_OLD", "2026-07-01", PAY, 9)])  # 乱序低修订，不生效
check("乱序低修订不改变有效流水，重算0", br["timing"]["recomputed"] == 0, br["timing"])

print("== 9. 写入失败整批回滚，明细沿用原结果 ==")
before = page(as_of=client.get("/health").json()["latest_batch_id"])
r = client.post("/flows/import", json={"flows": [
    flow("A01", 1, "T1", "2026-01-05", PAY, 9_999),   # 冲突
    flow("Z99", 1, "Z", "2026-08-01", PAY, 1),        # 不应落库
]})
check("冲突 409", r.status_code == 409, r.text)
after = page()
check("失败批次不改变明细", after["items"] == before["items"], after)
check("失败批次流水未落库", client.get("/flows", params={"flow_no": "Z99"}).json() == [])

print("== 10. 旧库升级：无明细表时按当前有效流水补齐 ==")
old_db = Path(_TMP) / "old.db"
db.init_db(old_db)
c2 = db.connect(old_db)


def raw_import(conn, rows):
    return db.import_flows(conn, [
        {"flow_no": n, "revision": r, "transfer_no": t,
         "post_date": date.fromisoformat(d), "direction": di,
         "amount_cents": a} for n, r, t, d, di, a in rows])


raw_import(c2, [("K01", 1, "KT", "2026-01-01", PAY, 5_000),
                ("K02", 1, "KT", "2026-01-09", RECEIVE, 5_000)])
c2.executescript(
    "DROP TABLE timing_details; DROP TABLE timing_detail_history; "
    "DROP TABLE app_meta;")
c2.close()
db.init_db(old_db)  # 模拟旧库启动升级
c3 = db.connect(old_db)
rows = c3.execute("SELECT * FROM timing_details").fetchall()
check("升级补齐1条明细", len(rows) == 1 and rows[0]["transfer_no"] == "KT", rows)
hist = c3.execute(
    "SELECT * FROM timing_detail_history WHERE batch_id = 1").fetchall()
check("补齐同时写入基线版本", len(hist) == 1 and hist[0]["is_present"] == 1, hist)
db.init_db(old_db)
check("重复升级幂等", c3.execute(
    "SELECT COUNT(*) c FROM timing_details").fetchone()["c"] == 1)
br = raw_import(c3, [("K03", 1, "KT", "2026-02-01", RECEIVE, 1)])
check("升级后录入仍增量重算", br["batch_id"] == 2
      and br["timing"]["recomputed"] == 1, br["timing"])
p1 = db.timing_page(c3, 1, None, None, 50)[0]
p2 = db.timing_page(c3, 2, None, None, 50)[0]
check("升级基线批次与后续批次各自读到正确版本",
      p1[0]["received_cents"] == 5_000 and p2[0]["received_cents"] == 5_001,
      (p1, p2))
c3.close()

print("== 11. 空库分页 ==")
empty_db = Path(_TMP) / "empty.db"
db.init_db(empty_db)
ec = db.connect(empty_db)
from app.main import get_conn  # noqa: E402
app.dependency_overrides[get_conn] = lambda: ec
r = client.get("/timing-details")
check("空库分页 200 空列表",
      r.status_code == 200 and r.json()["items"] == []
      and r.json()["total_count"] == 0, r.text)
r = client.get("/timing-details", params={"as_of": 1})
check("空库指定 as_of 409", r.status_code == 409, r.text)
app.dependency_overrides.clear()
ec.close()

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
