"""SQLite persistence layer.

Design points
-------------
* Every accepted row keeps its full revision history. A higher revision of the
  same ``record_no`` inserts a new row and marks the previous current row via
  ``superseded_by``; only rows with ``superseded_by IS NULL`` are "current".
* All mutating endpoints run inside ``BEGIN IMMEDIATE`` transactions. A batch
  that contains any invalid row is rejected before any write, so accepted data
  can never be partially modified.
* The database runs in WAL mode. Read-only report requests open an explicit
  read transaction; SQLite then serves a single committed snapshot for the
  whole request even if corrections / cutoff edits commit concurrently.
* A monotonically increasing ``snapshot_version`` in the ``meta`` table is
  bumped inside every write transaction and echoed back by queries so the UI
  can show which committed state a result was computed from.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_batches (
    id INTEGER PRIMARY KEY,
    batch_ref TEXT NOT NULL UNIQUE,
    filename TEXT,
    row_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accepted_records (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER REFERENCES upload_batches(id),
    record_no TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    patient_id TEXT NOT NULL,
    sample_date TEXT NOT NULL,
    organism TEXT NOT NULL,
    drug TEXT NOT NULL,
    mic_raw TEXT NOT NULL,
    superseded_by INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(record_no, revision_no)
);

CREATE INDEX IF NOT EXISTS idx_ar_current
    ON accepted_records(record_no) WHERE superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_ar_group
    ON accepted_records(patient_id, organism, drug, sample_date, record_no);

CREATE TABLE IF NOT EXISTS cutoffs (
    id INTEGER PRIMARY KEY,
    organism TEXT NOT NULL,
    drug TEXT NOT NULL,
    s_cutoff REAL NOT NULL,
    r_cutoff REAL NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organism, drug)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

REQUIRED_COLUMNS = ["记录号", "修订号", "匿名患者号", "UTC采样日", "菌种", "药物", "MIC"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('snapshot_version', '0')"
        )
        conn.commit()
    finally:
        conn.close()


def get_conn(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI runs sync endpoints in a worker
    # thread. Safety is provided by BEGIN IMMEDIATE for writers and
    # snapshot-scoped read transactions; no connection is shared concurrently
    # across requests (each request gets its own via the dependency).
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """Serialize writers; commit/rollback handled centrally."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def read_snapshot(conn: sqlite3.Connection):
    """Pin one committed snapshot for the duration of a read request."""
    conn.execute("BEGIN")
    try:
        yield
        conn.rollback()  # read-only txn; rollback simply releases it
    except Exception:
        conn.rollback()
        raise


def bump_snapshot_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='snapshot_version'"
    ).fetchone()
    version = int(row["value"]) + 1
    conn.execute(
        "UPDATE meta SET value=? WHERE key='snapshot_version'", (str(version),)
    )
    return version


def current_snapshot_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='snapshot_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


# ---------------------------------------------------------------------------
# Batch upload
# ---------------------------------------------------------------------------

def insert_batch(conn: sqlite3.Connection, filename: str, rows: list[dict]) -> dict:
    """Insert a fully validated batch and apply revision corrections.

    ``rows`` are dicts produced by :mod:`app.services` validation, containing
    parsed ``revision_no`` (int), ``sample_date`` (date) and the original
    string fields. Runs inside the caller's write transaction.
    """
    batch_ref = uuid.uuid4().hex
    now = utc_now_iso()
    cur = conn.execute(
        "INSERT INTO upload_batches(batch_ref, filename, row_count, created_at) "
        "VALUES (?, ?, ?, ?)",
        (batch_ref, filename, len(rows), now),
    )
    batch_id = cur.lastrowid

    inserted = 0
    corrections = 0
    for r in rows:
        existing = conn.execute(
            "SELECT id, revision_no FROM accepted_records WHERE record_no=? "
            "ORDER BY revision_no DESC LIMIT 1",
            (r["record_no"],),
        ).fetchone()
        if existing is not None and r["revision_no"] <= existing["revision_no"]:
            # Defense in depth: services layer validates this before opening
            # the transaction, but never let a stale revision land here.
            raise ValueError(
                f"记录号 {r['record_no']} 的修订号 {r['revision_no']} 不高于已接纳的 "
                f"最高修订号 {existing['revision_no']}，整批回滚"
            )
        cur = conn.execute(
            "INSERT INTO accepted_records(batch_id, record_no, revision_no, "
            "patient_id, sample_date, organism, drug, mic_raw, superseded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                batch_id, r["record_no"], r["revision_no"], r["patient_id"],
                r["sample_date"].isoformat(), r["organism"], r["drug"],
                r["mic_raw"], now,
            ),
        )
        new_id = cur.lastrowid
        if existing is not None:
            conn.execute(
                "UPDATE accepted_records SET superseded_by=? WHERE id=?",
                (new_id, existing["id"]),
            )
            corrections += 1
        inserted += 1

    version = bump_snapshot_version(conn)
    return {
        "batch_ref": batch_ref,
        "inserted": inserted,
        "corrections": corrections,
        "snapshot_version": version,
    }


# ---------------------------------------------------------------------------
# Cutoffs
# ---------------------------------------------------------------------------

def upsert_cutoff(conn: sqlite3.Connection, organism: str, drug: str,
                  s_cutoff: float, r_cutoff: float) -> dict:
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO cutoffs(organism, drug, s_cutoff, r_cutoff, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(organism, drug) DO UPDATE SET "
        "s_cutoff=excluded.s_cutoff, r_cutoff=excluded.r_cutoff, "
        "updated_at=excluded.updated_at",
        (organism, drug, float(s_cutoff), float(r_cutoff), now),
    )
    version = bump_snapshot_version(conn)
    return {
        "organism": organism, "drug": drug,
        "s_cutoff": float(s_cutoff), "r_cutoff": float(r_cutoff),
        "snapshot_version": version,
    }


def list_cutoffs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT organism, drug, s_cutoff, r_cutoff, updated_at "
        "FROM cutoffs ORDER BY organism, drug"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def fetch_current_records(conn: sqlite3.Connection,
                          organism: Optional[str] = None,
                          drug: Optional[str] = None) -> list[sqlite3.Row]:
    sql = ("SELECT id, record_no, revision_no, patient_id, sample_date, "
           "organism, drug, mic_raw, created_at "
           "FROM accepted_records WHERE superseded_by IS NULL")
    params: list = []
    if organism:
        sql += " AND organism=?"
        params.append(organism)
    if drug:
        sql += " AND drug=?"
        params.append(drug)
    sql += " ORDER BY patient_id, organism, drug, sample_date, record_no, revision_no"
    return conn.execute(sql, params).fetchall()


def fetch_record_history(conn: sqlite3.Connection, record_no: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ar.id, ar.record_no, ar.revision_no, ar.patient_id, ar.sample_date, "
        "ar.organism, ar.drug, ar.mic_raw, ar.superseded_by, ar.created_at, "
        "ub.batch_ref "
        "FROM accepted_records ar LEFT JOIN upload_batches ub ON ar.batch_id=ub.id "
        "WHERE ar.record_no=? ORDER BY ar.revision_no",
        (record_no,),
    ).fetchall()


def fetch_all_current_records_for_dedup(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Full current history — dedup windows must span beyond query ranges."""
    return conn.execute(
        "SELECT record_no, revision_no, patient_id, sample_date, organism, "
        "drug, mic_raw FROM accepted_records WHERE superseded_by IS NULL "
        "ORDER BY patient_id, organism, drug, sample_date, record_no"
    ).fetchall()
