"""SQLite 持久化与事务封装。"""
from __future__ import annotations

import calendar
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import compute
from .config import DB_PATH, DIRECTIONS

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flows (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_no               TEXT NOT NULL,
    revision              INTEGER NOT NULL CHECK (revision >= 1),
    transfer_no           TEXT NOT NULL,
    post_date             TEXT NOT NULL,
    direction             TEXT NOT NULL CHECK (direction IN ('pay', 'receive')),
    amount_cents          INTEGER NOT NULL CHECK (amount_cents > 0),
    batch_id              INTEGER NOT NULL REFERENCES import_batches(batch_id),
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    superseded_by_batch   INTEGER REFERENCES import_batches(batch_id),
    UNIQUE (flow_no, revision)
);
CREATE INDEX IF NOT EXISTS idx_flows_flow_no ON flows(flow_no);
CREATE INDEX IF NOT EXISTS idx_flows_active ON flows(is_active);
CREATE INDEX IF NOT EXISTS idx_flows_transfer ON flows(transfer_no);

CREATE TABLE IF NOT EXISTS working_papers (
    paper_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    month_end               TEXT NOT NULL UNIQUE,
    batch_id                INTEGER NOT NULL,
    total_in_transit_cents  INTEGER NOT NULL,
    transfer_count          INTEGER NOT NULL,
    pending_count           INTEGER NOT NULL,
    sealed_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    paper_id          INTEGER NOT NULL REFERENCES working_papers(paper_id),
    transfer_no       TEXT NOT NULL,
    paid_cents        INTEGER NOT NULL,
    received_cents    INTEGER NOT NULL,
    in_transit_cents  INTEGER NOT NULL,
    status            TEXT NOT NULL,
    note              TEXT NOT NULL,
    PRIMARY KEY (paper_id, transfer_no)
);

CREATE TABLE IF NOT EXISTS paper_position_evidence (
    paper_id     INTEGER NOT NULL REFERENCES working_papers(paper_id),
    transfer_no  TEXT NOT NULL,
    flow_no      TEXT NOT NULL,
    PRIMARY KEY (paper_id, transfer_no, flow_no)
);

CREATE TABLE IF NOT EXISTS paper_flow_evidence (
    paper_id       INTEGER NOT NULL REFERENCES working_papers(paper_id),
    flow_no        TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    transfer_no    TEXT NOT NULL,
    post_date      TEXT NOT NULL,
    direction      TEXT NOT NULL,
    amount_cents   INTEGER NOT NULL,
    PRIMARY KEY (paper_id, flow_no)
);

CREATE TABLE IF NOT EXISTS app_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- 到账时效明细：当前有效结论，录入批次与流水在同一事务内更新
CREATE TABLE IF NOT EXISTS timing_details (
    transfer_no        TEXT PRIMARY KEY,
    payment_month      TEXT NOT NULL,
    payment_date       TEXT NOT NULL,
    payment_flow_no    TEXT NOT NULL,
    payment_revision   INTEGER NOT NULL,
    paid_cents         INTEGER NOT NULL,
    received_cents     INTEGER NOT NULL,
    status             TEXT NOT NULL,
    settled_date       TEXT,
    outstanding_cents  INTEGER,
    pending_reasons    TEXT NOT NULL,
    receipt_flows      TEXT NOT NULL,
    updated_batch_id   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timing_details_month
    ON timing_details(payment_month, payment_date, transfer_no);

-- 明细逐批次版本：分页固定到首页所见的批次，重试/重启后仍按该版本续读
CREATE TABLE IF NOT EXISTS timing_detail_history (
    batch_id          INTEGER NOT NULL,
    transfer_no       TEXT NOT NULL,
    is_present        INTEGER NOT NULL CHECK (is_present IN (0, 1)),
    payment_month     TEXT,
    payment_date      TEXT,
    payment_flow_no   TEXT,
    payment_revision  INTEGER,
    paid_cents        INTEGER,
    received_cents    INTEGER,
    status            TEXT,
    settled_date      TEXT,
    outstanding_cents INTEGER,
    pending_reasons   TEXT,
    receipt_flows     TEXT,
    PRIMARY KEY (batch_id, transfer_no)
);
CREATE INDEX IF NOT EXISTS idx_timing_history_transfer
    ON timing_detail_history(transfer_no, batch_id);
CREATE INDEX IF NOT EXISTS idx_timing_history_batch
    ON timing_detail_history(batch_id);
"""


class ConflictError(Exception):
    """同号同修订内容冲突等 409 场景。"""


class NotFoundError(Exception):
    """封存底稿不存在等 404 场景。"""


TIMING_SCHEMA_VERSION = "1"


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path), timeout=30, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        ensure_timing_details(conn)
    finally:
        conn.close()


# ---------------- 到账时效明细维护 ----------------

def _detail_row(detail: dict[str, Any], batch_id: int) -> tuple[Any, ...]:
    return (
        detail["transfer_no"],
        detail["payment_month"],
        detail["payment_date"],
        detail["payment_flow_no"],
        detail["payment_revision"],
        detail["paid_cents"],
        detail["received_cents"],
        detail["status"],
        detail["settled_date"],
        detail["outstanding_cents"],
        json.dumps(detail["pending_reasons"], ensure_ascii=False),
        json.dumps(detail["receipt_flows"], ensure_ascii=False),
        batch_id,
    )


_DETAIL_INSERT_SQL = (
    "INSERT INTO timing_details(transfer_no, payment_month, payment_date, "
    "payment_flow_no, payment_revision, paid_cents, received_cents, status, "
    "settled_date, outstanding_cents, pending_reasons, receipt_flows, "
    "updated_batch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_HISTORY_INSERT_SQL = (
    "INSERT INTO timing_detail_history(batch_id, transfer_no, is_present, "
    "payment_month, payment_date, payment_flow_no, payment_revision, paid_cents, "
    "received_cents, status, settled_date, outstanding_cents, pending_reasons, "
    "receipt_flows) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _history_values(batch_id: int, detail: dict[str, Any] | None,
                    transfer_no: str) -> tuple[Any, ...]:
    """批次版本行：detail 为 None 表示该划款在本批次后明细消失。"""
    if detail is None:
        return (batch_id, transfer_no, 0, None, None, None, None, None, None,
                None, None, None, None, None)
    d = _detail_row(detail, batch_id)
    return (batch_id, d[0], 1, *d[1:12])


def _detail_signature(detail: dict[str, Any]) -> tuple[Any, ...]:
    return (
        detail["payment_month"], detail["payment_date"], detail["payment_flow_no"],
        detail["payment_revision"], detail["paid_cents"], detail["received_cents"],
        detail["status"], detail["settled_date"], detail["outstanding_cents"],
        detail["pending_reasons"], detail["receipt_flows"],
    )


def _row_signature(row: sqlite3.Row) -> tuple[Any, ...]:
    return (
        row["payment_month"], row["payment_date"], row["payment_flow_no"],
        row["payment_revision"], row["paid_cents"], row["received_cents"],
        row["status"], row["settled_date"], row["outstanding_cents"],
        json.loads(row["pending_reasons"]), json.loads(row["receipt_flows"]),
    )


def _all_transfer_flows(conn: sqlite3.Connection,
                        transfer_nos: Iterable[str]) -> dict[str, list[compute.Flow]]:
    """读取指定划款的当前全部有效流水（不按月份截止）。"""
    nos = sorted(set(transfer_nos))
    if not nos:
        return {}
    out: dict[str, list[compute.Flow]] = {}
    # 分批查询，避免 SQLite 变量上限
    for i in range(0, len(nos), 500):
        chunk = nos[i:i + 500]
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT flow_no, revision, transfer_no, post_date, direction, "
            f"amount_cents FROM flows WHERE is_active = 1 AND transfer_no IN ({marks})",
            chunk,
        ).fetchall()
        for flow in compute.rows_to_flows(rows):
            out.setdefault(flow.transfer_no, []).append(flow)
    return out


def refresh_timing_details(
    conn: sqlite3.Connection,
    transfer_nos: Iterable[str],
    batch_id: int,
) -> dict[str, Any]:
    """按给定划款重算并写入明细，同时记录本批次的明细版本。

    调用方须已在写事务中。只处理传入的划款：未变化的明细内容不会产生
    新历史行，也不会改写 updated_batch_id 以外的内容。
    """
    nos = sorted(set(transfer_nos))
    stats = {"recomputed": 0, "changed": 0, "removed": 0}
    if not nos:
        return stats

    flows_by_transfer = _all_transfer_flows(conn, nos)
    existing = {}
    for i in range(0, len(nos), 500):
        chunk = nos[i:i + 500]
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT * FROM timing_details WHERE transfer_no IN ({marks})", chunk
        ).fetchall():
            existing[row["transfer_no"]] = row

    history_rows: list[tuple[Any, ...]] = []
    for transfer_no in nos:
        new_detail = compute.timing_detail(
            transfer_no, flows_by_transfer.get(transfer_no, [])
        )
        old = existing.get(transfer_no)
        stats["recomputed"] += 1
        if new_detail is None:
            if old is not None:
                conn.execute(
                    "DELETE FROM timing_details WHERE transfer_no = ?", (transfer_no,)
                )
                history_rows.append(_history_values(batch_id, None, transfer_no))
                stats["removed"] += 1
            continue

        if old is not None and _row_signature(old) == _detail_signature(new_detail):
            # 内容未变：只推进版本指针，不追加历史
            if old["updated_batch_id"] != batch_id:
                conn.execute(
                    "UPDATE timing_details SET updated_batch_id = ? "
                    "WHERE transfer_no = ?",
                    (batch_id, transfer_no),
                )
            continue

        conn.execute("DELETE FROM timing_details WHERE transfer_no = ?", (transfer_no,))
        conn.execute(_DETAIL_INSERT_SQL, _detail_row(new_detail, batch_id))
        history_rows.append(_history_values(batch_id, new_detail, transfer_no))
        stats["changed"] += 1

    if history_rows:
        conn.executemany(_HISTORY_INSERT_SQL, history_rows)
    return stats


def transfers_touched_by_batch(conn: sqlite3.Connection,
                               batch_id: int) -> set[str]:
    """某批次真正改变“有效流水”的划款号。

    - 本批次新插入且生效的流水所属划款；
    - 本批次顶替掉的旧有效流水所属划款（改归属时即原划款）。
    乱序补报的低修订、重复重报都不会改变有效流水，不纳入重算。
    """
    rows = conn.execute(
        "SELECT DISTINCT transfer_no FROM flows "
        "WHERE batch_id = ? AND is_active = 1", (batch_id,)
    ).fetchall()
    touched = {r["transfer_no"] for r in rows}
    old_rows = conn.execute(
        "SELECT DISTINCT transfer_no FROM flows "
        "WHERE superseded_by_batch = ? AND is_active = 0", (batch_id,)
    ).fetchall()
    touched |= {r["transfer_no"] for r in old_rows}
    return touched


def ensure_timing_details(conn: sqlite3.Connection) -> None:
    """旧库升级：明细表为空时按当前有效流水一次性补齐。

    补齐内容与流水版本同时落在最新批次下，使后续增量与分页版本可延续。
    幂等：已记录版本标记时直接返回。
    """
    with transaction(conn):
        version = conn.execute(
            "SELECT value FROM app_meta WHERE key = 'timing_schema_version'"
        ).fetchone()
        if version is not None and version["value"] == TIMING_SCHEMA_VERSION:
            return
        count = conn.execute("SELECT COUNT(*) AS c FROM timing_details").fetchone()["c"]
        batch_id = latest_batch_id(conn) or 0
        if count == 0:
            flows = compute.rows_to_flows(conn.execute(
                "SELECT flow_no, revision, transfer_no, post_date, direction, "
                "amount_cents FROM flows WHERE is_active = 1"
            ).fetchall())
            details = compute.compute_timing_details(flows)
            conn.executemany(
                _DETAIL_INSERT_SQL, [_detail_row(d, batch_id) for d in details]
            )
            # 补齐同时写入基线版本，保证此后任一既有批次都能据此重建明细
            conn.executemany(
                _HISTORY_INSERT_SQL,
                [_history_values(batch_id, d, d["transfer_no"]) for d in details],
            )
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES ('timing_schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (TIMING_SCHEMA_VERSION,),
        )


# ---------------- 分页读取明细 ----------------

def timing_batch_bounds(conn: sqlite3.Connection) -> int | None:
    """首页固定批次：当前最新已提交批次。空库返回 None。"""
    return latest_batch_id(conn)


def timing_page(
    conn: sqlite3.Connection,
    batch_id: int,
    after_payment_date: str | None,
    after_transfer_no: str | None,
    limit: int,
    payment_date_from: str | None = None,
    payment_date_to: str | None = None,
    payment_month: str | None = None,
    status: str | None = None,
) -> tuple[list[dict[str, Any]], bool, int]:
    """按付款日、划款号读取某批次版本的明细页，返回（行, has_more, 总数）。

    版本语义：每个划款取“batch_id 当时最新的一条历史行”；没有历史行的划款
    （例如该批次未触及且明细在上次补齐后才创建）回落读取当前明细，但要求
    其 updated_batch_id <= batch_id。
    """
    filter_params: list[Any] = []
    filter_sql = ""
    if payment_date_from is not None:
        filter_sql += " AND payment_date >= ?"
        filter_params.append(payment_date_from)
    if payment_date_to is not None:
        filter_sql += " AND payment_date <= ?"
        filter_params.append(payment_date_to)
    if payment_month is not None:
        filter_sql += " AND payment_month = ?"
        filter_params.append(payment_month)
    if status is not None:
        filter_sql += " AND status = ?"
        filter_params.append(status)

    cursor_sql = ""
    cursor_params: list[Any] = []
    if after_payment_date is not None and after_transfer_no is not None:
        cursor_sql = (
            " AND (payment_date > ? OR (payment_date = ? AND transfer_no > ?))"
        )
        cursor_params = [after_payment_date, after_payment_date, after_transfer_no]

    base = f"""
        WITH versioned AS (
            SELECT h.transfer_no, h.is_present, h.payment_month, h.payment_date,
                   h.payment_flow_no, h.payment_revision, h.paid_cents,
                   h.received_cents, h.status, h.settled_date,
                   h.outstanding_cents, h.pending_reasons, h.receipt_flows
            FROM timing_detail_history h
            JOIN (
                SELECT transfer_no, MAX(batch_id) AS mb
                FROM timing_detail_history WHERE batch_id <= ?
                GROUP BY transfer_no
            ) latest
              ON h.transfer_no = latest.transfer_no AND h.batch_id = latest.mb
            UNION ALL
            SELECT t.transfer_no, 1, t.payment_month, t.payment_date,
                   t.payment_flow_no, t.payment_revision, t.paid_cents,
                   t.received_cents, t.status, t.settled_date,
                   t.outstanding_cents, t.pending_reasons, t.receipt_flows
            FROM timing_details t
            WHERE t.updated_batch_id <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM timing_detail_history h
                  WHERE h.transfer_no = t.transfer_no
              )
        )
        SELECT * FROM versioned
        WHERE is_present = 1 AND payment_date IS NOT NULL
        {filter_sql}
    """
    fixed = [batch_id, batch_id] + filter_params
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM ({base})", fixed
    ).fetchone()["c"]
    rows = conn.execute(
        f"{base} {cursor_sql} ORDER BY payment_date, transfer_no LIMIT ?",
        fixed + cursor_params + [limit + 1],
    ).fetchall()
    has_more = len(rows) > limit
    out = [{
        "transfer_no": r["transfer_no"],
        "payment_month": r["payment_month"],
        "payment_date": r["payment_date"],
        "payment_flow_no": r["payment_flow_no"],
        "payment_revision": r["payment_revision"],
        "paid_cents": r["paid_cents"],
        "received_cents": r["received_cents"],
        "status": r["status"],
        "settled_date": r["settled_date"],
        "outstanding_cents": r["outstanding_cents"],
        "pending_reasons": json.loads(r["pending_reasons"]),
        "receipt_flows": json.loads(r["receipt_flows"]),
    } for r in rows[:limit]]
    return out, has_more, total


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务；已在事务中时退化为 SAVEPOINT，保证内层可独立回滚。"""
    if conn.in_transaction:
        sp = f"sp_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {sp}")
        try:
            yield conn
            conn.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
            raise
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """只读一致性快照：事务内所有读取对应同一次已提交的数据。"""
    if conn.in_transaction:  # 复用外层事务，不另开
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def month_end_of(d: date) -> date:
    last_day = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, last_day)


# ---------------- 录入 ----------------

def import_flows(conn: sqlite3.Connection, payload: list[dict[str, Any]]) -> dict[str, Any]:
    """整批在一个事务内录入；任一冲突则全部退回。"""
    # 批内自查：同 (flow_no, revision) 重复且内容不一致 -> 冲突
    seen: dict[tuple[str, int], dict[str, Any]] = {}
    for row in payload:
        key = (row["flow_no"], row["revision"])
        norm = {
            "transfer_no": row["transfer_no"],
            "post_date": row["post_date"].isoformat(),
            "direction": row["direction"],
            "amount_cents": row["amount_cents"],
        }
        if key in seen and seen[key] != norm:
            raise ConflictError(
                f"批内流水号 {key[0]} 修订 {key[1]} 出现内容冲突，整批退回"
            )
        seen.setdefault(key, norm)

    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO import_batches(submitted_at) VALUES (?)",
            (datetime.utcnow().isoformat(timespec="seconds") + "Z",),
        )
        batch_id = cur.lastrowid
        inserted = superseded = already = 0
        outcomes: list[dict[str, Any]] = []

        for (flow_no, revision), norm in seen.items():
            existing = conn.execute(
                "SELECT transfer_no, post_date, direction, amount_cents, batch_id "
                "FROM flows WHERE flow_no = ? AND revision = ?",
                (flow_no, revision),
            ).fetchone()

            if existing is not None:
                old = {
                    "transfer_no": existing["transfer_no"],
                    "post_date": existing["post_date"],
                    "direction": existing["direction"],
                    "amount_cents": existing["amount_cents"],
                }
                if old != norm:
                    raise ConflictError(
                        f"流水号 {flow_no} 修订 {revision} 已存在且内容不一致，"
                        f"已存={old} 本次={norm}，整批退回"
                    )
                already += 1
                outcomes.append({
                    "flow_no": flow_no, "revision": revision, **norm,
                    "outcome": "already_present",
                })
                continue

            conn.execute(
                "INSERT INTO flows(flow_no, revision, transfer_no, post_date, "
                "direction, amount_cents, batch_id, is_active) "
                "VALUES (?,?,?,?,?,?,?,1)",
                (flow_no, revision, norm["transfer_no"], norm["post_date"],
                 norm["direction"], norm["amount_cents"], batch_id),
            )
            # 正常路径：新修订高于现存有效版本，旧有效版本置失效
            replaced = conn.execute(
                "UPDATE flows SET is_active = 0, superseded_by_batch = ? "
                "WHERE flow_no = ? AND revision < ? AND is_active = 1",
                (batch_id, flow_no, revision),
            ).rowcount
            if replaced:
                superseded += replaced
            else:
                # 乱序补报：存在更高修订号，新插入的低修订本身即为失效版本
                higher = conn.execute(
                    "SELECT batch_id FROM flows WHERE flow_no = ? AND revision > ? "
                    "ORDER BY revision DESC LIMIT 1",
                    (flow_no, revision),
                ).fetchone()
                if higher is not None:
                    conn.execute(
                        "UPDATE flows SET is_active = 0, superseded_by_batch = ? "
                        "WHERE flow_no = ? AND revision = ?",
                        (higher["batch_id"], flow_no, revision),
                    )
            inserted += 1
            outcomes.append({
                "flow_no": flow_no, "revision": revision, **norm,
                "outcome": "superseded" if replaced else "inserted",
            })

        # 只重算本批次有效流水发生变化的划款；明细与流水同一事务提交
        timing = refresh_timing_details(
            conn, transfers_touched_by_batch(conn, batch_id), batch_id
        )

        return {
            "batch_id": batch_id,
            "inserted": inserted,
            "superseded": superseded,
            "already_present": already,
            "rows": outcomes,
            "timing": timing,
        }


# ---------------- 读取 ----------------

def latest_batch_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(batch_id) AS m FROM import_batches").fetchone()
    return row["m"]


def all_flow_rows(conn: sqlite3.Connection, active_only: bool,
                  flow_no: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM flows WHERE 1=1"
    params: list[Any] = []
    if active_only:
        sql += " AND is_active = 1"
    if flow_no:
        sql += " AND flow_no = ?"
        params.append(flow_no)
    sql += " ORDER BY flow_no, revision"
    return conn.execute(sql, params).fetchall()


def active_flow_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM flows WHERE is_active = 1 ORDER BY flow_no"
    ).fetchall()


# ---------------- 封存 ----------------

def seal_paper(conn: sqlite3.Connection, month_end: date,
               positions: list[dict[str, Any]],
               evidence: list[dict[str, Any]],
               total: int) -> dict[str, Any]:
    me = month_end.isoformat()
    with transaction(conn):
        dup = conn.execute(
            "SELECT paper_id FROM working_papers WHERE month_end = ?", (me,)
        ).fetchone()
        if dup is not None:
            raise ConflictError(f"{me} 月底稿已封存（paper_id={dup['paper_id']}），每月仅一份")
        batch_id = latest_batch_id(conn)
        sealed_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        cur = conn.execute(
            "INSERT INTO working_papers(month_end, batch_id, total_in_transit_cents, "
            "transfer_count, pending_count, sealed_at) VALUES (?,?,?,?,?,?)",
            (me, batch_id, total, len(positions),
             sum(1 for p in positions if p["status"] != "normal"), sealed_at),
        )
        paper_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO paper_positions(paper_id, transfer_no, paid_cents, "
            "received_cents, in_transit_cents, status, note) "
            "VALUES (?,?,?,?,?,?,?)",
            [(paper_id, p["transfer_no"], p["paid_cents"], p["received_cents"],
              p["in_transit_cents"], p["status"], p["note"]) for p in positions],
        )
        conn.executemany(
            "INSERT INTO paper_position_evidence(paper_id, transfer_no, flow_no) "
            "VALUES (?,?,?)",
            [(paper_id, p["transfer_no"], no)
             for p in positions for no in p["evidence_flow_nos"]],
        )
        conn.executemany(
            "INSERT INTO paper_flow_evidence(paper_id, flow_no, revision, transfer_no, "
            "post_date, direction, amount_cents) VALUES (?,?,?,?,?,?,?)",
            [(paper_id, e["flow_no"], e["revision"], e["transfer_no"],
              e["post_date"].isoformat() if isinstance(e["post_date"], date) else e["post_date"],
              e["direction"], e["amount_cents"]) for e in evidence],
        )
        return {
            "paper_id": paper_id,
            "month_end": month_end,
            "batch_id": batch_id,
            "total_in_transit_cents": total,
            "transfer_count": len(positions),
            "pending_count": sum(1 for p in positions if p["status"] != "normal"),
            "sealed_at": sealed_at,
            "positions": positions,
        }


def list_papers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM working_papers ORDER BY month_end"
    ).fetchall()


def get_paper(conn: sqlite3.Connection, paper_id: int) -> dict[str, Any]:
    meta = conn.execute(
        "SELECT * FROM working_papers WHERE paper_id = ?", (paper_id,)
    ).fetchone()
    if meta is None:
        raise NotFoundError(f"paper_id={paper_id} 不存在")
    positions = conn.execute(
        "SELECT * FROM paper_positions WHERE paper_id = ? ORDER BY transfer_no",
        (paper_id,),
    ).fetchall()
    out_positions = []
    for p in positions:
        ev = conn.execute(
            "SELECT flow_no FROM paper_position_evidence WHERE paper_id = ? "
            "AND transfer_no = ? ORDER BY flow_no",
            (paper_id, p["transfer_no"]),
        ).fetchall()
        out_positions.append({
            "transfer_no": p["transfer_no"],
            "paid_cents": p["paid_cents"],
            "received_cents": p["received_cents"],
            "in_transit_cents": p["in_transit_cents"],
            "status": p["status"],
            "note": p["note"],
            "evidence_flow_nos": [r["flow_no"] for r in ev],
        })
    evidence = conn.execute(
        "SELECT * FROM paper_flow_evidence WHERE paper_id = ? ORDER BY flow_no",
        (paper_id,),
    ).fetchall()
    return {"meta": meta, "positions": out_positions,
            "evidence": [dict(e) for e in evidence]}


def find_paper_by_month(conn: sqlite3.Connection, month_end: date) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM working_papers WHERE month_end = ?",
        (month_end.isoformat(),),
    ).fetchone()
