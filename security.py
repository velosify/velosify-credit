"""
VelosifyCredit security primitives.

Four concerns that all need the database and would otherwise be scattered
through app.py: single-use tokens, failure throttling, CSRF, and the audit
log. Kept together because they are the parts that have to be right, and
having them in one file makes them reviewable in one sitting.

Everything here is stdlib. No new dependency is worth adding for any of it.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import request, session

from db import get_db, utcnow

# ---------------------------------------------------------------------------
# Time helpers
#
# Everything in the database is an ISO-8601 UTC string, so comparisons are
# lexicographic and no parsing is needed on the hot path.
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _ago(**kwargs) -> str:
    return _stamp(_now() - timedelta(**kwargs))


def _ahead(**kwargs) -> str:
    return _stamp(_now() + timedelta(**kwargs))


# ---------------------------------------------------------------------------
# Client identity
# ---------------------------------------------------------------------------

def client_ip() -> str:
    """The caller's address, trusting exactly one proxy hop.

    Railway and Cloudflare both sit in front of this, so remote_addr is the
    edge, not the visitor. We take the FIRST entry of X-Forwarded-For, which
    is the one the edge appended, and cap the length so a hostile header
    cannot bloat a row.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.remote_addr or "")[:64]


# ---------------------------------------------------------------------------
# Single-use tokens
#
# The plaintext token is returned to the caller once, to be emailed, and is
# never stored. Only its SHA-256 lands in the database, so a leaked backup
# does not contain working reset links.
# ---------------------------------------------------------------------------

PASSWORD_RESET = "password_reset"
EMAIL_VERIFY = "email_verify"
ADMIN_INVITE = "admin_invite"
CLIENT_ACTIVATE = "client_activate"

# The invite window is long enough to survive a weekend and short enough that
# a forgotten invite in an old mailbox is not a standing back door.
_TOKEN_TTL_HOURS = {PASSWORD_RESET: 1, EMAIL_VERIFY: 168, ADMIN_INVITE: 72,
                    # A client has to read a contract before signing it, so
                    # this one is measured in days rather than hours.
                    CLIENT_ACTIVATE: 336}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(conn: sqlite3.Connection, user_id: int, kind: str) -> str:
    """Mint a token, invalidating any earlier unused one of the same kind.

    Superseding matters: without it, every reset a user requested today would
    stay live for an hour, so one intercepted email out of five would still
    work. There is only ever one valid reset link per account.
    """
    conn.execute(
        "UPDATE auth_tokens SET used_at = ? "
        "WHERE user_id = ? AND kind = ? AND used_at IS NULL",
        (utcnow(), user_id, kind),
    )
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_tokens (user_id, kind, token_hash, created_at, "
        "expires_at, request_ip) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, kind, _hash_token(token), utcnow(),
         _ahead(hours=_TOKEN_TTL_HOURS.get(kind, 1)), client_ip()),
    )
    return token


def peek_token(conn: sqlite3.Connection, token: str, kind: str) -> int | None:
    """Who a token belongs to, without spending it.

    Needed when a form has to be validated against the account before the
    token is burned: checking a typed signature against the right name, for
    instance. A failed validation must leave the link usable.
    """
    if not token:
        return None
    row = conn.execute(
        "SELECT user_id, expires_at, used_at FROM auth_tokens "
        "WHERE token_hash = ? AND kind = ?",
        (_hash_token(token), kind),
    ).fetchone()
    if not row or row["used_at"] or row["expires_at"] < utcnow():
        return None
    return int(row["user_id"])


def consume_token(conn: sqlite3.Connection, token: str, kind: str) -> int | None:
    """Validate and burn a token. Returns the user id, or None.

    The update is conditional on used_at still being null, and we check how
    many rows it changed, so two requests racing on the same link cannot both
    win.
    """
    if not token:
        return None
    row = conn.execute(
        "SELECT id, user_id, expires_at, used_at FROM auth_tokens "
        "WHERE token_hash = ? AND kind = ?",
        (_hash_token(token), kind),
    ).fetchone()
    if not row or row["used_at"] or row["expires_at"] < utcnow():
        return None
    changed = conn.execute(
        "UPDATE auth_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
        (utcnow(), row["id"]),
    ).rowcount
    return int(row["user_id"]) if changed == 1 else None


# ---------------------------------------------------------------------------
# Throttling
#
# Counting lives in the database rather than in a process-local dict because
# this deploys to a platform that restarts containers freely, and a
# brute-force counter that resets on deploy is not a counter.
#
# Two scopes are checked for every attempt: the account being targeted, and
# the address doing the targeting. The first stops someone grinding one
# account; the second stops someone spraying one password across many.
# ---------------------------------------------------------------------------

LIMITS = {
    # scope:            (max failures, window minutes)
    "login:email":      (6, 15),
    "login:ip":         (20, 15),
    "reset:email":      (4, 60),
    "reset:ip":         (12, 60),
}


def _count(conn: sqlite3.Connection, scope: str, key: str, minutes: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM auth_failures "
        "WHERE scope = ? AND key = ? AND created_at >= ?",
        (scope, key.lower()[:190], _ago(minutes=minutes)),
    ).fetchone()
    return int(row["n"])


def throttled(conn: sqlite3.Connection, scope: str, key: str) -> bool:
    limit, minutes = LIMITS[scope]
    return _count(conn, scope, key, minutes) >= limit


def record_failure(conn: sqlite3.Connection, scope: str, key: str) -> None:
    conn.execute(
        "INSERT INTO auth_failures (scope, key, created_at) VALUES (?, ?, ?)",
        (scope, key.lower()[:190], utcnow()),
    )


def clear_failures(conn: sqlite3.Connection, scope: str, key: str) -> None:
    """Called on success, so a legitimate sign-in resets the account counter."""
    conn.execute("DELETE FROM auth_failures WHERE scope = ? AND key = ?",
                 (scope, key.lower()[:190]))


def sweep_failures(conn: sqlite3.Connection) -> None:
    """Drop rows older than the longest window. Cheap, and keeps the table from
    growing without bound on a site that is being scanned."""
    conn.execute("DELETE FROM auth_failures WHERE created_at < ?", (_ago(hours=24),))


def retry_after_minutes(scope: str) -> int:
    return LIMITS[scope][1]


# ---------------------------------------------------------------------------
# CSRF
#
# SameSite=Lax already blocks the cross-site POST that most CSRF depends on,
# but it is one cookie attribute between an attacker and a client's document
# store, and browsers have shipped bugs in it before. A token is a second,
# independent barrier that does not rely on the browser getting anything
# right.
# ---------------------------------------------------------------------------

_CSRF_KEY = "_csrf"

# The webhook is the one POST that legitimately has no session and no token.
# Stripe signs its payloads, and that signature is verified in the handler, so
# it is authenticated by a stronger mechanism than this one.
CSRF_EXEMPT_ENDPOINTS = {"stripe_webhook"}

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def csrf_token() -> str:
    token = session.get(_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_CSRF_KEY] = token
    return token


def csrf_ok() -> bool:
    expected = session.get(_CSRF_KEY)
    if not expected:
        return False
    supplied = (request.form.get("csrf_token")
                or request.headers.get("X-CSRF-Token")
                or "")
    return hmac.compare_digest(str(expected), str(supplied))


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def audit(action: str, *, actor: dict | None = None, target_user_id: int | None = None,
          target_doc_id: int | None = None, detail: str = "",
          conn: sqlite3.Connection | None = None) -> None:
    """Append one line to the audit trail.

    Deliberately forgiving: auditing must never be the reason a request fails,
    so a broken insert is swallowed. It is also never the only record of
    anything a client can see, which lives in case_events instead.
    """
    try:
        conn = conn or get_db()
        conn.execute(
            "INSERT INTO audit_log (created_at, actor_user_id, actor_email, "
            "actor_role, action, target_user_id, target_doc_id, detail, ip, "
            "user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (utcnow(),
             (actor or {}).get("id"),
             ((actor or {}).get("email") or "")[:190],
             ((actor or {}).get("role") or "")[:32],
             action[:64],
             target_user_id,
             target_doc_id,
             detail[:500],
             client_ip(),
             (request.headers.get("User-Agent") or "")[:250]),
        )
    except Exception:  # pragma: no cover - auditing must not break a request
        pass
