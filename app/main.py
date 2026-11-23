"""FastAPI 路由：录入、重算、封存、对照。"""
from __future__ import annotations

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
        "- 封存与对照各自在一个只读快照内完成，只取同一次已提交数据"
    ),
    version="1.0.0",
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


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")
