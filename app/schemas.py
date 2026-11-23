"""请求/响应模型（金额一律以分计的正整数；日期 ISO 字符串 YYYY-MM-DD）。"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .config import DIRECTIONS, PAY, RECEIVE

Direction = Literal["pay", "receive"]


class FlowIn(BaseModel):
    """单条银行流水录入内容。"""
    flow_no: str = Field(..., min_length=1, max_length=64, description="流水号（业务主键，同号取最高修订）")
    revision: int = Field(..., ge=1, description="修订号，从 1 开始；同流水号取最大值为有效版本")
    transfer_no: str = Field(..., min_length=1, max_length=64, description="划款号，一次划款可对应多笔到账")
    post_date: date = Field(..., description="入账日 YYYY-MM-DD")
    direction: Direction = Field(..., description=f"收付方向：{PAY}=付款，{RECEIVE}=到账")
    amount_cents: int = Field(..., gt=0, description="金额（分），正整数")

    @field_validator("flow_no", "transfer_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空")
        return v


class ImportRequest(BaseModel):
    flows: list[FlowIn] = Field(..., min_length=1, max_length=10000)


class ImportedRow(BaseModel):
    flow_no: str
    revision: int
    transfer_no: str
    post_date: date
    direction: str
    amount_cents: int
    outcome: Literal["inserted", "superseded", "already_present"]


class ImportResponse(BaseModel):
    batch_id: int
    submitted_at: str
    inserted: int
    superseded: int
    already_present: int
    rows: list[ImportedRow]
    timing: "TimingImportStats" = Field(
        default_factory=lambda: TimingImportStats(),
        description="本批对到账时效明细的维护结果",
    )


class FlowRow(BaseModel):
    flow_no: str
    revision: int
    transfer_no: str
    post_date: date
    direction: str
    amount_cents: int
    batch_id: int
    is_active: bool
    superseded_by_batch: Optional[int] = None


class Position(BaseModel):
    """单个划款号在某月底的逐笔在途结论。"""
    transfer_no: str
    paid_cents: int
    received_cents: int
    in_transit_cents: int = Field(..., description="在途余额 = 付款 - 截至月底到账")
    status: Literal["normal", "missing_payment", "over_received", "duplicate_payment"]
    note: str
    evidence_flow_nos: list[str]


class ComputeResponse(BaseModel):
    month_end: date
    as_of_batch_id: Optional[int] = Field(None, description="重算所依据的最新提交批次，空库为 null")
    transfers: list[Position]
    total_in_transit_cents: int
    pending_transfers: list[Position]


class SealedPaperMeta(BaseModel):
    paper_id: int
    month_end: date
    sealed_at: str
    batch_id: int
    total_in_transit_cents: int
    transfer_count: int
    pending_count: int


class SealResponse(SealedPaperMeta):
    transfers: list[Position]


class FlowEvidence(BaseModel):
    flow_no: str
    revision: int
    transfer_no: str
    post_date: date
    direction: str
    amount_cents: int


class PositionChange(BaseModel):
    transfer_no: str
    change_type: Literal["added", "removed", "changed"]
    sealed: Optional[Position] = None
    recomputed: Optional[Position] = None
    field_diffs: dict[str, dict] = Field(
        default_factory=dict,
        description='字段名 -> {"sealed": 旧值, "recomputed": 新值}',
    )


class FlowChange(BaseModel):
    flow_no: str
    change_type: Literal["added", "removed", "revision_changed", "content_changed"]
    sealed_revision: Optional[int] = None
    current_revision: Optional[int] = None
    sealed_snapshot: Optional[FlowEvidence] = None
    current_snapshot: Optional[FlowEvidence] = None


class CompareResponse(BaseModel):
    month_end: date
    paper_id: int
    sealed_batch_id: int
    current_batch_id: int
    has_difference: bool
    changed_flows: list[FlowChange]
    position_changes: list[PositionChange]
    sealed: ComputeResponse
    recomputed: ComputeResponse


class Health(BaseModel):
    status: str
    db_path: str
    latest_batch_id: Optional[int] = None


# ---------------- 到账时效明细（按付款月复核、分页） ----------------

class ReceiptFlow(BaseModel):
    flow_no: str
    revision: int
    post_date: date
    amount_cents: int


class TimingDetail(BaseModel):
    """单个划款（恰好一笔有效付款）的到账时效明细。"""
    transfer_no: str
    payment_month: str = Field(..., description="付款月 YYYY-MM")
    payment_date: date
    payment_flow_no: str = Field(..., description="付款流水号")
    payment_revision: int = Field(..., description="付款流水修订号")
    paid_cents: int
    received_cents: int
    status: Literal["pending", "settled", "outstanding"]
    settled_date: Optional[date] = Field(None, description="结清日：到账合计等于付款额时的最后到账日")
    outstanding_cents: Optional[int] = Field(None, description="未到账金额；结清或待核时为 null/0")
    pending_reasons: list[Literal["over_received", "received_before_payment"]] = Field(
        default_factory=list,
        description="待核原因：超额到账 / 到账早于付款日",
    )
    receipt_flows: list[ReceiptFlow] = Field(
        default_factory=list, description="本划款当前有效到账流水（含流水号与修订号）"
    )


class TimingImportStats(BaseModel):
    recomputed: int = Field(0, description="本批重算的划款数")
    changed: int = Field(0, description="明细内容发生变化的划款数")
    removed: int = Field(0, description="明细被移除的划款数")


class TimingPage(BaseModel):
    batch_id: int = Field(..., description="本页固定读取的明细版本批次")
    as_of: int = Field(..., description="本轮固定在首页所见的批次，续页请原样回传")
    payment_date_from: Optional[date] = Field(None, description="付款日起（含）")
    payment_date_to: Optional[date] = Field(None, description="付款日止（含）")
    payment_month: Optional[str] = Field(None, description="付款月筛选 YYYY-MM")
    status: Optional[Literal["pending", "settled", "outstanding"]] = Field(
        None, description="结论筛选：待核 / 已结清 / 未足额"
    )
    items: list[TimingDetail]
    has_more: bool
    next_cursor: Optional[str] = Field(None, description="下一页游标；为空表示已到末页")
    total_count: int = Field(..., description="本轮固定版本下符合条件的总条数")


ImportResponse.model_rebuild()
