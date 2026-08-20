"""
VelosifyCredit database layer.

SQLite via the stdlib. Schema is created idempotently at import time and
each additive change goes in as its own guarded migration, so deploying a
new version over an existing database is always safe.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from flask import g

import config

# ---------------------------------------------------------------------------
# The intake checklist. Order here is the order the client sees.
#
# `required` drives the "you're ready" gate on the dashboard. Optional docs
# still show up, they just don't block the case from moving forward.
# ---------------------------------------------------------------------------
DOCUMENT_TYPES = [
    {
        "key": "credit_report",
        "label": "Credit report",
        "required": True,
        "help": "All three bureaus if you have them: Experian, Equifax and "
                "TransUnion. A PDF export from annualcreditreport.com is ideal.",
    },
    {
        "key": "photo_id",
        "label": "Government-issued photo ID",
        "required": True,
        "help": "Driver's license, state ID or passport. Make sure all four "
                "corners are visible and the text is readable.",
    },
    {
        "key": "proof_of_address",
        "label": "Proof of address",
        "required": True,
        "help": "A utility bill, bank statement or lease dated within the "
                "last 60 days showing your name and current address.",
    },
    {
        "key": "ssn_proof",
        "label": "Proof of Social Security number",
        "required": True,
        "help": "Social Security card, W-2 or SSA letter. Bureaus require "
                "this to process a dispute on your behalf.",
    },
    {
        "key": "ftc_report",
        "label": "FTC identity theft report",
        "required": False,
        "help": "Only if fraudulent accounts are on your report. File at "
                "identitytheft.gov and upload the PDF.",
    },
    {
        "key": "other",
        "label": "Anything else",
        "required": False,
        "help": "Bureau letters you've received, collection notices, court "
                "documents, or anything else you think we should see.",
    },
]

DOCUMENT_TYPE_KEYS = {d["key"] for d in DOCUMENT_TYPES}
REQUIRED_DOCUMENT_KEYS = [d["key"] for d in DOCUMENT_TYPES if d["required"]]

# Case pipeline. `key` is stored on the user row; the list drives the
# progress bar in the portal.
CASE_STAGES = [
    ("intake", "Intake"),
    ("documents", "Collecting documents"),
    ("analysis", "Report analysis"),
    ("disputes", "Disputes filed"),
    ("responses", "Bureau responses"),
    ("complete", "Complete"),
]
CASE_STAGE_KEYS = [k for k, _ in CASE_STAGES]
CASE_STAGE_LABELS = dict(CASE_STAGES)

DOC_STATUSES = ("received", "accepted", "rejected")


def utcnow() -> str:
    """ISO-8601 UTC timestamp. Stored as text, because SQLite has no date type and
    ISO strings sort correctly, which is all we need."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    """Per-request connection, closed by close_db on teardown."""
    conn = getattr(g, "_db", None)
    if conn is None:
        conn = sqlite3.connect(str(config.DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL keeps reads from blocking on the write that happens during a
        # document upload. Worth it even on a single-process deploy.
        conn.execute("PRAGMA journal_mode = WAL")
        g._db = conn
    return conn


def close_db(_exc=None) -> None:
    conn = getattr(g, "_db", None)
    if conn is not None:
        conn.close()
        g._db = None


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    email               TEXT    NOT NULL UNIQUE,
    password_hash       TEXT    NOT NULL,
    first_name          TEXT    NOT NULL DEFAULT '',
    last_name           TEXT    NOT NULL DEFAULT '',
    phone               TEXT    NOT NULL DEFAULT '',
    role                TEXT    NOT NULL DEFAULT 'client',
    case_stage          TEXT    NOT NULL DEFAULT 'intake',
    created_at          TEXT    NOT NULL,
    last_login_at       TEXT,
    email_verified_at   TEXT,
    password_changed_at TEXT,
    -- Agreement acceptance is recorded at order time and never edited.
    -- These three columns together are the audit trail.
    agreement_signed_at TEXT,
    agreement_name      TEXT,
    agreement_ip        TEXT,
    -- Separate from the agreement: the Credit Repair Organizations Act wants
    -- a signed acknowledgment that the credit file rights disclosure was
    -- received BEFORE the contract was signed, kept for two years.
    disclosure_ack_at   TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount_cents         INTEGER NOT NULL,
    currency             TEXT    NOT NULL DEFAULT 'usd',
    status               TEXT    NOT NULL DEFAULT 'pending',
    stripe_session_id    TEXT,
    stripe_payment_intent TEXT,
    created_at           TEXT    NOT NULL,
    paid_at              TEXT,
    refunded_at          TEXT,
    -- 'stripe' for a real checkout, 'admin' for an enrollment created by
    -- hand. Kept because an order marked paid with no Stripe session behind
    -- it is a question somebody will eventually have to answer.
    source               TEXT    NOT NULL DEFAULT 'stripe',
    note                 TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(stripe_session_id);

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    doc_type      TEXT    NOT NULL,
    original_name TEXT    NOT NULL,
    stored_name   TEXT    NOT NULL,
    mime_type     TEXT    NOT NULL DEFAULT '',
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'received',
    review_note   TEXT    NOT NULL DEFAULT '',
    uploaded_at   TEXT    NOT NULL,
    reviewed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, doc_type);

CREATE TABLE IF NOT EXISTS case_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT    NOT NULL,
    body       TEXT    NOT NULL DEFAULT '',
    stage      TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    created_by TEXT    NOT NULL DEFAULT 'system'
);
CREATE INDEX IF NOT EXISTS idx_events_user ON case_events(user_id, created_at);

-- Single-use, expiring tokens for password reset and email confirmation.
-- Only the SHA-256 of the token is stored: a stolen database backup must not
-- hand anyone a working reset link.
CREATE TABLE IF NOT EXISTS auth_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,          -- 'password_reset' | 'email_verify'
    token_hash TEXT    NOT NULL UNIQUE,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL,
    used_at    TEXT,
    request_ip TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON auth_tokens(user_id, kind);

-- Failure counting for throttling. Rows are written for failures only, and
-- swept once they age out, so this stays small. It lives in the database
-- rather than in memory because a process restart must not reset a
-- brute-force counter to zero.
CREATE TABLE IF NOT EXISTS auth_failures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT    NOT NULL,          -- 'login:email' | 'login:ip' | 'reset:email' | 'reset:ip'
    key        TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failures ON auth_failures(scope, key, created_at);

-- Who did what, to whose file. Documents here are government IDs, Social
-- Security proofs and full credit reports, so "which admin opened this and
-- when" is a question that has to have an answer.
CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT    NOT NULL,
    actor_user_id  INTEGER,              -- null for anonymous or system
    actor_email    TEXT    NOT NULL DEFAULT '',
    actor_role     TEXT    NOT NULL DEFAULT '',
    action         TEXT    NOT NULL,
    target_user_id INTEGER,
    target_doc_id  INTEGER,
    detail         TEXT    NOT NULL DEFAULT '',
    ip             TEXT    NOT NULL DEFAULT '',
    user_agent     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_user_id, created_at);
"""


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add a column to a table that already exists, so each one is applied here as
# its own guarded ALTER. Adding to this list is the only thing a new additive
# column needs; the guard makes it a no-op on a database that already has it.
_ADDED_COLUMNS = [
    ("users", "disclosure_ack_at", "TEXT"),
    ("users", "email_verified_at", "TEXT"),
    ("users", "password_changed_at", "TEXT"),
    ("orders", "source", "TEXT NOT NULL DEFAULT 'stripe'"),
    ("orders", "note", "TEXT NOT NULL DEFAULT ''"),
]


def _apply_added_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue  # table isn't there yet; the schema will create it whole
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    """Create the schema, then apply any additive migrations.

    Safe to call on every boot, and safe to run against a database created by
    an older version of this file.
    """
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        conn.executescript(SCHEMA)
        _apply_added_columns(conn)
        conn.commit()
    finally:
        conn.close()


def add_event(conn: sqlite3.Connection, user_id: int, title: str,
              body: str = "", stage: str = "", created_by: str = "system") -> None:
    """Append to a client's timeline. Callers commit, so an event and
    the state change it describes land in the same transaction."""
    conn.execute(
        "INSERT INTO case_events (user_id, title, body, stage, created_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, title, body, stage, utcnow(), created_by),
    )


def document_status_for(conn: sqlite3.Connection, user_id: int) -> dict:
    """Build the checklist view model: for each document type, the files the
    client has uploaded and whether that slot counts as satisfied.

    A slot is satisfied by any upload that hasn't been rejected. We don't
    make the client wait on review to see progress, but a rejection reopens
    the slot immediately.
    """
    rows = conn.execute(
        "SELECT * FROM documents WHERE user_id = ? ORDER BY uploaded_at DESC",
        (user_id,),
    ).fetchall()

    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["doc_type"], []).append(dict(r))

    items = []
    for spec in DOCUMENT_TYPES:
        files = by_type.get(spec["key"], [])
        satisfied = any(f["status"] != "rejected" for f in files)
        items.append({**spec, "files": files, "satisfied": satisfied})

    required_done = sum(
        1 for i in items if i["required"] and i["satisfied"]
    )
    required_total = len(REQUIRED_DOCUMENT_KEYS)
    return {
        "items": items,
        "required_done": required_done,
        "required_total": required_total,
        "complete": required_done >= required_total,
        "percent": int(round(100 * required_done / max(1, required_total))),
    }
