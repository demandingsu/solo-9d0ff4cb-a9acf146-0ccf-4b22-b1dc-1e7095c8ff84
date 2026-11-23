"""HTTP 冒烟脚本：仅用标准库，对真实 uvicorn 进程打请求。"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8077")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def f(no, rev, tr, d, direction, amt):
    return {"flow_no": no, "revision": rev, "transfer_no": tr,
            "post_date": d, "direction": direction, "amount_cents": amt}


ok = True

# 1. 月结前流水
s, r = call("POST", "/flows/import", {"flows": [
    f("B01", 1, "P1", "2026-09-02", "pay", 8_000_000),
    f("B02", 1, "P1", "2026-09-20", "receive", 3_000_000),
]})
assert s == 201 and r["inserted"] == 2, r
print("1. 导入批次", r["batch_id"], "插入", r["inserted"])

# 2. 9月底重算并封存
s, r = call("GET", "/compute?month_end=2026-09-30")
p1 = next(x for x in r["transfers"] if x["transfer_no"] == "P1")
assert p1["in_transit_cents"] == 5_000_000, p1
print("2. 9月底 P1 在途", p1["in_transit_cents"])

s, paper = call("POST", "/papers/seal?month_end=2026-09-30")
assert s == 201, paper
pid = paper["paper_id"]
print("3. 封存底稿 paper_id =", pid, "总额", paper["total_in_transit_cents"])

# 3. 月结后补传（9月入账的遗漏到账）+ 更正修订
s, r = call("POST", "/flows/import", {"flows": [
    f("B03", 1, "P1", "2026-09-25", "receive", 2_000_000),
    f("B02", 2, "P1", "2026-09-21", "receive", 3_000_000),  # 入账日更正
]})
assert s == 201 and r["superseded"] == 1, r
print("4. 补传 B03、B02 修订2（旧修订被置为失效", r["superseded"], "条）")

# 4. 同号同修订冲突 -> 409 整批退回
s, r = call("POST", "/flows/import", {"flows": [
    f("B01", 1, "P9", "2026-09-02", "pay", 1),
    f("B99", 1, "P9", "2026-09-02", "pay", 1),
]})
assert s == 409, r
s, check = call("GET", "/flows?flow_no=B99")
assert check == [], check
print("5. 冲突整批退回 409，B99 未落库：", r["detail"][:48], "...")

# 5. 对照
s, cmp_ = call("GET", f"/papers/{pid}/compare")
assert s == 201 or s == 200, cmp_
types = {c["flow_no"]: c["change_type"] for c in cmp_["changed_flows"]}
assert types == {"B02": "revision_changed", "B03": "added"}, types
p1_new = next(x for x in cmp_["recomputed"]["transfers"]
              if x["transfer_no"] == "P1")
p1_old = next(x for x in cmp_["sealed"]["transfers"]
              if x["transfer_no"] == "P1")
assert p1_old["in_transit_cents"] == 5_000_000
assert p1_new["in_transit_cents"] == 3_000_000, (p1_old, p1_new)
print("6. 对照：变动流水", types)
print("   P1 在途 封存", p1_old["in_transit_cents"],
      "-> 重算", p1_new["in_transit_cents"])
print("   封存依据批次", cmp_["sealed_batch_id"],
      "当前批次", cmp_["current_batch_id"],
      "has_difference =", cmp_["has_difference"])

# 6. 旧底稿依据不变
s, detail = call("GET", f"/papers/{pid}")
ev = {e["flow_no"]: e["revision"] for e in detail["flow_evidence"]}
assert ev == {"B01": 1, "B02": 1}, ev
print("7. 旧底稿依据仍冻结为", ev)

# 7. 待核：缺付款
s, r = call("POST", "/flows/import", {"flows": [
    f("B10", 1, "P_NOPAY", "2026-09-28", "receive", 999),
]})
s, r = call("GET", "/compute?month_end=2026-09-30")
pend = {p["transfer_no"]: p["status"] for p in r["pending_transfers"]}
assert pend["P_NOPAY"] == "missing_payment", pend
print("8. 待核划款：", pend)

# 8. 校验错误
s, r = call("POST", "/flows/import",
            {"flows": [f("B11", 1, "P", "2026-09-28", "receive", -5)]})
assert s == 422
print("9. 金额为负返回 422 校验错误")

print("\nHTTP 冒烟全部通过")
