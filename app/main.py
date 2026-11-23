"""FastAPI 路由：录入、重算、封存、对照、到账时效明细分页。"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import date
from typing import Any

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse

from . import database as db
from . import compute
from .config import DB_PATH
from .schemas import (
    CompareResponse,
    ComputeResponse,
    FlowChange,
    FlowRow,
    Health,
    ImportRequest,
    ImportResponse,
    Position,
    PositionChange,
    SealResponse,
    SealedPaperMeta,
    TimingDetail,
    TimingImportStats,
    TimingPage,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db(DB_PATH)
    yield


app = FastAPI(
    title="基金会在途款核对系统",
    description=(
        "录入银行流水（金额以分计），按月底重算在途余额、封存底稿、"
        "补传/更正后逐笔对照封存与重算结论。\n\n"
        "- 同流水号取最高修订号；同号同修订内容冲突整批退回\n"
        "- 两次核算均只取入账日不晚于月底的记录\n"
        "- 缺付款/超额到账/重复付款的划款单列为待核\n"
        "- 封存与对照各自在一个只读快照内完成，只取同一次已提交数据\n"
        "- `/timing-details` 按付款月分页复核到账时效，明细分批次维护，"
        "游标固定轮次、可跨重启续读"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def get_conn() -> Any:
    conn = db.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@app.get("/health", response_model=Health, tags=["系统"])
def health(conn=Depends(get_conn)) -> Health:
    return Health(status="ok", db_path=str(DB_PATH),
                  latest_batch_id=db.latest_batch_id(conn))


# ---------------- 流水录入/查询 ----------------

@app.post("/flows/import", response_model=ImportResponse, tags=["流水"],
          status_code=201)
def import_flows(req: ImportRequest, conn=Depends(get_conn)) -> ImportResponse:
    payload = [f.model_dump() for f in req.flows]
    try:
        result = db.import_flows(conn, payload)
    except db.ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    submitted = conn.execute(
        "SELECT submitted_at FROM import_batches WHERE batch_id = ?",
        (result["batch_id"],),
    ).fetchone()["submitted_at"]
    return ImportResponse(
        batch_id=result["batch_id"],
        submitted_at=submitted,
        inserted=result["inserted"],
        superseded=result["superseded"],
        already_present=result["already_present"],
        rows=result["rows"],
        timing=TimingImportStats(**result["timing"]),
    )


@app.get("/flows", response_model=list[FlowRow], tags=["流水"])
def list_flows(
    active_only: bool = Query(True, description="只看当前有效（最高修订）流水"),
    flow_no: str | None = Query(None, description="按流水号过滤"),
    conn=Depends(get_conn),
) -> list[FlowRow]:
    rows = db.all_flow_rows(conn, active_only=active_only, flow_no=flow_no)
    return [
        FlowRow(
            flow_no=r["flow_no"], revision=r["revision"],
            transfer_no=r["transfer_no"], post_date=date.fromisoformat(r["post_date"]),
            direction=r["direction"], amount_cents=r["amount_cents"],
            batch_id=r["batch_id"], is_active=bool(r["is_active"]),
            superseded_by_batch=r["superseded_by_batch"],
        )
        for r in rows
    ]


# ---------------- 重算 ----------------

def _compute_response(conn, month_end: date) -> ComputeResponse:
    flows = compute.rows_to_flows(db.active_flow_rows(conn))
    positions = compute.compute_positions(flows, month_end)
    total, pending = compute.summarize(positions)
    return ComputeResponse(
        month_end=month_end,
        as_of_batch_id=db.latest_batch_id(conn),
        transfers=[Position(**p) for p in positions],
        total_in_transit_cents=total,
        pending_transfers=[Position(**p) for p in pending],
    )


def _sealed_compute_response(paper: dict[str, Any]) -> ComputeResponse:
    positions = paper["positions"]
    total, pending = compute.summarize(positions)
    return ComputeResponse(
        month_end=date.fromisoformat(paper["meta"]["month_end"]),
        as_of_batch_id=paper["meta"]["batch_id"],
        transfers=[Position(**p) for p in positions],
        total_in_transit_cents=total,
        pending_transfers=[Position(**p) for p in pending],
    )


@app.get("/compute", response_model=ComputeResponse, tags=["核算"])
def recompute(
    month_end: date = Query(..., description="月底日期，自动归一为该月最后一天"),
    conn=Depends(get_conn),
) -> ComputeResponse:
    with db.snapshot(conn):
        return _compute_response(conn, db.month_end_of(month_end))


# ---------------- 封存 ----------------

@app.post("/papers/seal", response_model=SealResponse, tags=["底稿"],
          status_code=201)
def seal(
    month_end: date = Query(..., description="月底日期，自动归一为该月最后一天；每月仅可封存一份"),
    conn=Depends(get_conn),
) -> SealResponse:
    me = db.month_end_of(month_end)
    with db.snapshot(conn):
        flows = compute.rows_to_flows(db.active_flow_rows(conn))
        positions = compute.compute_positions(flows, me)
        if not positions:
            raise HTTPException(
                status_code=409,
                detail=f"{me.isoformat()} 及之前没有任何入账记录，无可封存内容",
            )
        total, _ = compute.summarize(positions)
        in_scope = [
            f.to_evidence() for f in compute.effective_flows(flows)
            if f.post_date <= me
        ]
        try:
            result = db.seal_paper(conn, me, positions, in_scope, total)
        except db.ConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
    return SealResponse(
        paper_id=result["paper_id"], month_end=me,
        sealed_at=result["sealed_at"], batch_id=result["batch_id"],
        total_in_transit_cents=total,
        transfer_count=result["transfer_count"],
        pending_count=result["pending_count"],
        transfers=positions,
    )


@app.get("/papers", response_model=list[SealedPaperMeta], tags=["底稿"])
def papers(conn=Depends(get_conn)) -> list[SealedPaperMeta]:
    with db.snapshot(conn):
        rows = db.list_papers(conn)
        return [
            SealedPaperMeta(
                paper_id=r["paper_id"],
                month_end=date.fromisoformat(r["month_end"]),
                sealed_at=r["sealed_at"], batch_id=r["batch_id"],
                total_in_transit_cents=r["total_in_transit_cents"],
                transfer_count=r["transfer_count"],
                pending_count=r["pending_count"],
            )
            for r in rows
        ]


@app.get("/papers/{paper_id}", tags=["底稿"])
def paper_detail(paper_id: int, conn=Depends(get_conn)) -> dict[str, Any]:
    with db.snapshot(conn):
        try:
            paper = db.get_paper(conn, paper_id)
        except db.NotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
    meta = dict(paper["meta"])
    return {
        "paper": meta,
        "positions": paper["positions"],
        "flow_evidence": paper["evidence"],
    }


# ---------------- 对照 ----------------

@app.get("/papers/{paper_id}/compare", response_model=CompareResponse, tags=["对照"])
def compare(paper_id: int, conn=Depends(get_conn)) -> CompareResponse:
    # 整个对照在一个只读快照中完成：封存依据与当前重算取自同一次已提交数据
    with db.snapshot(conn):
        try:
            paper = db.get_paper(conn, paper_id)
        except db.NotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

        meta = paper["meta"]
        me = date.fromisoformat(meta["month_end"])
        sealed_resp = _sealed_compute_response(paper)

        flows = compute.rows_to_flows(db.active_flow_rows(conn))
        current_positions = compute.compute_positions(flows, me)
        total, _ = compute.summarize(current_positions)
        current_evidence = [
            f.to_evidence() for f in compute.effective_flows(flows)
            if f.post_date <= me
        ]
        current_resp = ComputeResponse(
            month_end=me,
            as_of_batch_id=db.latest_batch_id(conn),
            transfers=[Position(**p) for p in current_positions],
            total_in_transit_cents=total,
            pending_transfers=[
                Position(**p) for p in current_positions
                if p["status"] in compute.PENDING_STATUSES
            ],
        )

        flow_changes = compute.diff_flows(paper["evidence"], current_evidence)
        pos_changes = compute.diff_positions(paper["positions"], current_positions)

        return CompareResponse(
            month_end=me,
            paper_id=paper_id,
            sealed_batch_id=meta["batch_id"],
            current_batch_id=db.latest_batch_id(conn),
            has_difference=bool(flow_changes or pos_changes),
            changed_flows=[FlowChange(**c) for c in flow_changes],
            position_changes=[PositionChange(**c) for c in pos_changes],
            sealed=sealed_resp,
            recomputed=current_resp,
        )


@app.get(
    "/timing-details",
    response_model=TimingPage,
    tags=["到账时效"],
    summary="按付款月分页复核到账时效明细",
    description=(
        "按付款日、划款号排序列出“恰好一笔有效付款”的划款，附付款/到账流水号与修订号。\n\n"
        "- 到账合计 > 付款额，或到账早于付款日：`pending` 待核；\n"
        "- 到账合计 = 付款额：`settled`，结清日取最后到账日；\n"
        "- 到账合计 < 付款额：`outstanding`，给出未到账金额。\n\n"
        "直接读取已维护的明细。首页返回 `as_of` 与 `next_cursor`；续页只需回传 `cursor`，"
        "游标已固定本轮的批次与筛选口径，重试或服务重启后仍沿原结果续读。"
    ),
)
def timing_details(
    as_of: int | None = Query(
        None, description="固定批次：首页返回的 as_of，续页原样回传；省略则取当前最新批次"
    ),
    cursor: str | None = Query(
        None, description="上一页返回的 next_cursor；与 as_of 配套使用"
    ),
    page_size: int = Query(50, ge=1, le=500, description="每页条数"),
    payment_date_from: date | None = Query(None, description="付款日起（含）"),
    payment_date_to: date | None = Query(None, description="付款日止（含）"),
    payment_month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$",
                                      description="付款月 YYYY-MM"),
    status: str | None = Query(None, pattern=r"^(pending|settled|outstanding)$",
                               description="结论：pending 待核 / settled 已结清 / outstanding 未足额"),
    conn=Depends(get_conn),
) -> TimingPage:
    """按付款日、划款号分页列出到账时效明细。

    直接读取已维护的明细；同一轮固定在首页所见的批次（as_of），
    重试或重启后仍按该批次续读原结果，不受后续录入影响。
    """
    with db.snapshot(conn):
        latest = db.timing_batch_bounds(conn)

        # 先解析游标：游标自带首页固定的批次与筛选口径，续页只要回传 cursor 即可
        after_date = after_transfer = None
        cursor_as_of: int | None = None
        cursor_filters: dict[str, Any] = {}
        if cursor is not None:
            try:
                decoded = json.loads(
                    base64.urlsafe_b64decode(cursor.encode()).decode()
                )
                after_date = decoded["payment_date"]
                after_transfer = decoded["transfer_no"]
                cursor_as_of = int(decoded["as_of"])
                cursor_filters = decoded.get("filters") or {}
                date.fromisoformat(after_date)  # 校验日期格式，非法会抛 ValueError
                if not after_transfer or cursor_as_of < 1:
                    raise ValueError("bad cursor")
                if not isinstance(cursor_filters, dict):
                    raise ValueError("bad filters")
            except (binascii.Error, UnicodeDecodeError, KeyError,
                    TypeError, ValueError, json.JSONDecodeError):
                raise HTTPException(status_code=400, detail="cursor 无效")

        if as_of is not None and cursor_as_of is not None and as_of != cursor_as_of:
            raise HTTPException(
                status_code=409,
                detail=f"as_of={as_of} 与游标所属批次 {cursor_as_of} 不一致",
            )
        requested_as_of = as_of if as_of is not None else cursor_as_of

        # 显式传入的筛选必须与首页一致；未传则沿用游标里的口径
        explicit = {
            "payment_date_from": payment_date_from.isoformat() if payment_date_from else None,
            "payment_date_to": payment_date_to.isoformat() if payment_date_to else None,
            "payment_month": payment_month,
            "status": status,
        }
        if cursor is not None:
            for key, value in explicit.items():
                expected = cursor_filters.get(key)
                if value is not None and value != expected:
                    raise HTTPException(
                        status_code=409,
                        detail=f"筛选参数 {key}={value} 与游标所属轮次 {expected} 不一致",
                    )
                if value is None:
                    explicit[key] = expected
            # 游标来自客户端，回填的筛选值需重新校验，防止伪造游标引发 500
            if explicit["status"] not in (None, "pending", "settled", "outstanding"):
                raise HTTPException(status_code=400, detail="cursor 中的 status 无效")
            if explicit["payment_month"] is not None and not re.fullmatch(
                r"\d{4}-\d{2}", str(explicit["payment_month"])
            ):
                raise HTTPException(status_code=400, detail="cursor 中的 payment_month 无效")
            for key in ("payment_date_from", "payment_date_to"):
                if explicit[key] is not None:
                    try:
                        date.fromisoformat(str(explicit[key]))
                    except (TypeError, ValueError):
                        raise HTTPException(
                            status_code=400, detail=f"cursor 中的 {key} 无效"
                        )
        payment_date_from_s = explicit["payment_date_from"]
        payment_date_to_s = explicit["payment_date_to"]
        payment_month_s = explicit["payment_month"]
        status_s = explicit["status"]

        if latest is None:
            if requested_as_of is not None:
                raise HTTPException(status_code=409, detail="库中还没有任何录入批次")
            return TimingPage(
                batch_id=0, as_of=0,
                payment_date_from=payment_date_from,
                payment_date_to=payment_date_to,
                payment_month=payment_month, status=status,
                items=[], has_more=False, next_cursor=None, total_count=0,
            )
        if requested_as_of is not None and (
            requested_as_of < 1 or requested_as_of > latest
        ):
            raise HTTPException(
                status_code=409,
                detail=f"as_of={requested_as_of} 不是有效批次；当前可用范围为 1..{latest}",
            )
        fixed = requested_as_of if requested_as_of is not None else latest

        rows, has_more, total = db.timing_page(
            conn, fixed, after_date, after_transfer, page_size,
            payment_date_from=payment_date_from_s,
            payment_date_to=payment_date_to_s,
            payment_month=payment_month_s, status=status_s,
        )

    next_cursor = None
    if has_more and rows:
        next_cursor = base64.urlsafe_b64encode(json.dumps({
            "as_of": fixed,
            "payment_date": rows[-1]["payment_date"],
            "transfer_no": rows[-1]["transfer_no"],
            "filters": explicit,
        }, ensure_ascii=False).encode()).decode()

    return TimingPage(
        batch_id=fixed, as_of=fixed,
        payment_date_from=explicit["payment_date_from"],
        payment_date_to=explicit["payment_date_to"],
        payment_month=explicit["payment_month"], status=explicit["status"],
        items=[TimingDetail(**r) for r in rows],
        has_more=has_more, next_cursor=next_cursor, total_count=total,
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")
