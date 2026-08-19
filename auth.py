"""
VelosifyCredit — authentication.

Session-cookie auth with PBKDF2 password hashing from the stdlib (no
dependency on werkzeug's helpers, so the hashing scheme is explicit and
easy to audit).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from functools import wraps

from flask import g, jsonify, redirect, request, session, url_for

from db import get_db, row_to_dict, utcnow

# PBKDF2-HMAC-SHA256. 240k iterations is comfortably above the current
# OWASP floor and still well under 100ms on the kind of box this deploys to.
_PBKDF2_ITERATIONS = 240_000
_HASH_PREFIX = "pbkdf2_sha256"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS
    )
    return f"{_HASH_PREFIX}${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify. Returns False on any malformed hash rather than
    raising, so a corrupt row can't 500 the login page."""
    try:
        scheme, iterations, salt, digest = stored.split("$", 3)
        if scheme != _HASH_PREFIX:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    """Return a human-readable reason the password is unacceptable, or None.

    Deliberately minimal: length is the rule that actually correlates with
    strength, and composition rules mostly push people toward Passw0rd!.
    """
    if len(password) < 10:
        return "Password must be at least 10 characters."
    if password.lower() in {"password12", "1234567890", "velosify12"}:
        return "That password is too easy to guess."
    return None


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email or ""))


# --- Session helpers ------------------------------------------------------

def login_user(user_id: int, email: str, role: str) -> None:
    session.clear()
    session["user_id"] = user_id
    session["email"] = email
    session["role"] = role
    session.permanent = True
    get_db().execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow(), user_id)
    )
    get_db().commit()


def logout_user() -> None:
    session.clear()


def current_user() -> dict | None:
    """The logged-in user row, cached for the duration of the request.

    Re-reads from the database rather than trusting the cookie for anything
    beyond the id, so a role change or deletion takes effect immediately.
    """
    if "user" in g.__dict__:
        return g.user
    uid = session.get("user_id")
    user = None
    if uid:
        user = row_to_dict(
            get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        )
        if user is None:
            session.clear()
    g.user = user
    return user


def is_admin() -> bool:
    user = current_user()
    return bool(user and user["role"] == "admin")


def _unauthorized():
    if request.accept_mimetypes.best == "application/json" or request.path.startswith("/api/"):
        return jsonify({"error": "Sign in to continue."}), 401
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return _unauthorized()
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return _unauthorized()
        if not is_admin():
            return jsonify({"error": "Not authorized."}), 403
        return fn(*args, **kwargs)
    return wrapper
