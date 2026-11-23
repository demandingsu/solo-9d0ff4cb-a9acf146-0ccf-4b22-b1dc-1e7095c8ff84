"""SQLite 持久化与事务封装。"""
from __future__ import annotations

import calendar
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

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
"""


class ConflictError(Exception):
    """同号同修订内容冲突等 409 场景。"""


class NotFoundError(Exception):
    """封存底稿不存在等 404 场景。"""


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
    finally:
        conn.close()


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

        return {
            "batch_id": batch_id,
            "inserted": inserted,
            "superseded": superseded,
            "already_present": already,
            "rows": outcomes,
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
