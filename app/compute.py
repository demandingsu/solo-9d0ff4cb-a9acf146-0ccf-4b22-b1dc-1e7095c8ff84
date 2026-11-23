"""在途款核算纯函数。

规则：
- 每个流水号取最高修订号为有效流水；
- 核算某月底时，只使用入账日不晚于该月底的有效流水；
- 按划款号汇总：一次划款一次付出，可分笔到账；
  在途余额 = 付款合计 - 截至月底到账合计；
- 缺付款（只有到账没有付款）、超额到账（到账 > 付款）、
  重复付款（同一划款号出现多笔付款）的划款单列为待核。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from .config import PAY, RECEIVE

NORMAL = "normal"
MISSING_PAYMENT = "missing_payment"
OVER_RECEIVED = "over_received"
DUPLICATE_PAYMENT = "duplicate_payment"

PENDING_STATUSES = {MISSING_PAYMENT, OVER_RECEIVED, DUPLICATE_PAYMENT}


@dataclass(frozen=True)
class Flow:
    flow_no: str
    revision: int
    transfer_no: str
    post_date: date
    direction: str
    amount_cents: int

    def to_evidence(self) -> dict[str, Any]:
        return {
            "flow_no": self.flow_no,
            "revision": self.revision,
            "transfer_no": self.transfer_no,
            "post_date": self.post_date,
            "direction": self.direction,
            "amount_cents": self.amount_cents,
        }


def _as_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(v)


def rows_to_flows(rows: Iterable[Any]) -> list[Flow]:
    """把 sqlite 行/字典转成 Flow（日期字段自动转 date）。"""
    flows: list[Flow] = []
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        flows.append(
            Flow(
                flow_no=d["flow_no"],
                revision=int(d["revision"]),
                transfer_no=d["transfer_no"],
                post_date=_as_date(d["post_date"]),
                direction=d["direction"],
                amount_cents=int(d["amount_cents"]),
            )
        )
    return flows


def effective_flows(flows: Iterable[Flow]) -> list[Flow]:
    """同流水号只保留最高修订号的那条。"""
    latest: dict[str, Flow] = {}
    for f in flows:
        cur = latest.get(f.flow_no)
        if cur is None or f.revision > cur.revision:
            latest[f.flow_no] = f
    return list(latest.values())


def _position(transfer_no: str, items: list[Flow]) -> dict[str, Any]:
    pays = [f for f in items if f.direction == PAY]
    recvs = [f for f in items if f.direction == RECEIVE]
    paid = sum(f.amount_cents for f in pays)
    received = sum(f.amount_cents for f in recvs)
    in_transit = paid - received

    if not pays:
        status = MISSING_PAYMENT
        note = "待核：该划款号只有到账记录、缺少付款记录"
    elif len(pays) > 1:
        status = DUPLICATE_PAYMENT
        note = f"待核：同一划款号出现 {len(pays)} 笔付款，一次划款应只付一次"
    elif received > paid:
        status = OVER_RECEIVED
        note = f"待核：到账 {received} 分超过付款 {paid} 分"
    else:
        status = NORMAL
        note = "正常：付款与到账匹配" if in_transit == 0 else "正常：仍有在途余额"

    return {
        "transfer_no": transfer_no,
        "paid_cents": paid,
        "received_cents": received,
        "in_transit_cents": in_transit,
        "status": status,
        "note": note,
        "evidence_flow_nos": sorted(f.flow_no for f in items),
    }


def compute_positions(flows: Iterable[Flow], month_end: date) -> list[dict[str, Any]]:
    """按当前有效流水重算 month_end 月底的逐划款在途结论。"""
    month_end = _as_date(month_end)
    active = effective_flows(flows)
    in_scope = [f for f in active if f.post_date <= month_end]

    grouped: dict[str, list[Flow]] = {}
    for f in in_scope:
        grouped.setdefault(f.transfer_no, []).append(f)

    positions = [_position(t, items) for t, items in sorted(grouped.items())]
    return positions


def summarize(positions: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    """返回（在途总额，待核划款列表）。"""
    total = sum(p["in_transit_cents"] for p in positions)
    pending = [p for p in positions if p["status"] in PENDING_STATUSES]
    return total, pending


def diff_positions(
    sealed: list[dict[str, Any]],
    recomputed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """逐划款对照封存与重算结论。"""
    sealed_map = {p["transfer_no"]: p for p in sealed}
    rec_map = {p["transfer_no"]: p for p in recomputed}
    changes: list[dict[str, Any]] = []

    for t in sorted(set(sealed_map) | set(rec_map)):
        old, new = sealed_map.get(t), rec_map.get(t)
        if old is None:
            changes.append({"transfer_no": t, "change_type": "added",
                            "sealed": None, "recomputed": new, "field_diffs": {}})
            continue
        if new is None:
            changes.append({"transfer_no": t, "change_type": "removed",
                            "sealed": old, "recomputed": None, "field_diffs": {}})
            continue
        field_diffs: dict[str, dict[str, Any]] = {}
        for key in ("paid_cents", "received_cents", "in_transit_cents",
                    "status", "note", "evidence_flow_nos"):
            if old[key] != new[key]:
                field_diffs[key] = {"sealed": old[key], "recomputed": new[key]}
        if field_diffs:
            changes.append({"transfer_no": t, "change_type": "changed",
                            "sealed": old, "recomputed": new,
                            "field_diffs": field_diffs})
    return changes


def diff_flows(
    sealed_evidence: list[dict[str, Any]],
    current_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """对照两次核算实际使用的流水依据。"""
    def _norm(e: dict[str, Any]) -> dict[str, Any]:
        e = dict(e)
        e["post_date"] = _as_date(e["post_date"]).isoformat()
        return e

    old_map = {e["flow_no"]: _norm(e) for e in sealed_evidence}
    new_map = {e["flow_no"]: _norm(e) for e in current_evidence}
    content_keys = ("transfer_no", "post_date", "direction", "amount_cents")
    changes: list[dict[str, Any]] = []

    for no in sorted(set(old_map) | set(new_map)):
        old, new = old_map.get(no), new_map.get(no)
        if old is None:
            changes.append({"flow_no": no, "change_type": "added",
                            "sealed_revision": None, "current_revision": new["revision"],
                            "sealed_snapshot": None, "current_snapshot": new})
            continue
        if new is None:
            changes.append({"flow_no": no, "change_type": "removed",
                            "sealed_revision": old["revision"], "current_revision": None,
                            "sealed_snapshot": old, "current_snapshot": None})
            continue
        if old["revision"] != new["revision"]:
            change_type = "revision_changed"
        elif any(old[k] != new[k] for k in content_keys):
            change_type = "content_changed"
        else:
            continue
        changes.append({"flow_no": no, "change_type": change_type,
                        "sealed_revision": old["revision"],
                        "current_revision": new["revision"],
                        "sealed_snapshot": old, "current_snapshot": new})
    return changes
