"""
VelosifyCredit affiliate program.

Anyone can sign up, confirm their email address and get a personal link. A
visitor who arrives through that link carries a cookie for REF_DAYS days; if
they ask for a call in that time, the lead is tagged with the affiliate. That
tag shows up in the lead alert email, on /admin/leads, and on the affiliate's
own dashboard.

What an affiliate sees about the people they sent is deliberately thin: first
name and last initial, the date, and where the call stands. Phone numbers,
email addresses and what someone wants removed from their credit report are
not theirs to see just because they shared a link.

Affiliates are rows in `users` with role 'affiliate', so signing in, password
reset and throttling are the same machinery clients and staff already use.
They are kept out of /portal and /admin by one gate here, rather than by every
route remembering to check.
"""
from __future__ import annotations

import re
import secrets

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   url_for)

import config
import mailer
import security
from auth import (current_user, hash_password, logout_user, normalize_email,
                  password_problem, require_admin, require_login, valid_email)
from db import get_db, row_to_dict, utcnow
from security import audit, client_ip, issue_token, record_failure, throttled

bp = Blueprint("affiliates", __name__)

ROLE = "affiliate"
COOKIE = "vc_ref"
# How long a click keeps counting. Long enough for someone to think it over
# and come back; short enough that a link shared once does not claim a person
# who found the site on their own months later.
REF_DAYS = 60

# Shown on the form and copied into the audit log when someone signs up, so
# what they agreed to is on record in their words-at-the-time.
TERMS_TEXT = (
    "I will tell people I may be paid for referring them. I will not promise "
    "anyone a specific score, a specific result, or that accurate information "
    "can be removed from their credit report, and I will not spam."
)

# Where a referral stands, in words that make sense to someone outside the
# business. 'closed' covers both signed and not interested, so it says neither.
STATUS_FOR_AFFILIATE = {
    "new": "Waiting for our call",
    "called": "Spoken to",
    "closed": "Closed",
}

SCHEMA = """
-- One row per new visitor through an affiliate link. "New" means they did not
-- already carry a referral cookie, so a person reloading the page, or
-- clicking the same link twice, is one visitor and not two.
CREATE TABLE IF NOT EXISTS affiliate_clicks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    affiliate_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aff_clicks ON affiliate_clicks(affiliate_id, created_at);
"""

_CODE_OK = re.compile(r"^[a-z0-9]{3,32}$")
_BLOCKED_PREFIXES = ("/portal", "/admin", "/files", "/letters")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_affiliate(user: dict | None) -> bool:
    return bool(user and user.get("role") == ROLE)


def _new_code(conn, first_name: str) -> str:
    """Something a person can read out loud: their first name and four digits.

    Readable on purpose, because these get typed into bios and said on
    videos. Uniqueness comes from the check, not from length.
    """
    stem = re.sub(r"[^a-z]", "", (first_name or "").lower())[:12] or "ref"
    for _ in range(60):
        code = f"{stem}{secrets.randbelow(9000) + 1000}"
        if not conn.execute("SELECT 1 FROM users WHERE affiliate_code = ?",
                            (code,)).fetchone():
            return code
    return secrets.token_hex(6)


def affiliate_by_code(conn, code: str | None) -> dict | None:
    """An affiliate whose link should count right now, or None.

    Unconfirmed and deactivated accounts both answer None, so their links
    quietly stop attributing rather than erroring at a visitor.
    """
    code = (code or "").strip().lower()
    if not _CODE_OK.match(code):
        return None
    return row_to_dict(conn.execute(
        "SELECT * FROM users WHERE affiliate_code = ? AND role = ? "
        "AND archived_at IS NULL AND email_verified_at IS NOT NULL",
        (code, ROLE),
    ).fetchone())


def referrer_for_request(conn) -> dict | None:
    """Who sent the person making this request, if anyone."""
    return affiliate_by_code(conn, request.cookies.get(COOKIE))


def _absolute(path: str) -> str:
    """Full URL for an email or a link to copy. Falls back to the live host
    while APP_BASE_URL still points at localhost."""
    base = config.APP_BASE_URL
    if not base or base.startswith(("http://127.0.0.1", "http://localhost")):
        base = request.url_root.rstrip("/")
    return f"{base}{path}"


def referral_link(code: str) -> str:
    return _absolute(f"/r/{code}")


def display_name(full_name: str) -> str:
    """'Maria Lopez' -> 'Maria L.' The most an affiliate is shown."""
    parts = (full_name or "").split()
    if not parts:
        return "Someone"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0].upper()}."


def _remember(code: str) -> None:
    """Note an arrival through an affiliate link.

    The first affiliate to send someone keeps the credit for REF_DAYS. A later
    link does not overwrite it, so one affiliate cannot claim another's
    referral by getting the same person to click again.
    """
    conn = get_db()
    if referrer_for_request(conn):
        return
    aff = affiliate_by_code(conn, code)
    if not aff:
        return
    me = current_user()
    if me and int(me["id"]) == int(aff["id"]):
        return  # an affiliate checking their own link is not a visitor
    g.set_ref = aff["affiliate_code"]
    conn.execute("INSERT INTO affiliate_clicks (affiliate_id, created_at) "
                 "VALUES (?, ?)", (aff["id"], utcnow()))
    conn.commit()


# ---------------------------------------------------------------------------
# Request hooks
# ---------------------------------------------------------------------------

@bp.before_app_request
def _keep_affiliates_out_of_client_and_staff_areas():
    if not request.path.startswith(_BLOCKED_PREFIXES):
        return None
    if is_affiliate(current_user()):
        return redirect(url_for("affiliates.dashboard"))
    return None


@bp.before_app_request
def _pick_up_ref_param():
    """`?ref=code` on any page works as well as /r/code, so an affiliate can
    point straight at the call form or a section of the landing page."""
    if request.method == "GET" and request.args.get("ref"):
        _remember(request.args["ref"])


@bp.after_app_request
def _set_ref_cookie(resp):
    code = g.get("set_ref")
    if code:
        resp.set_cookie(
            COOKIE, code, max_age=REF_DAYS * 86400, httponly=True,
            samesite="Lax", secure=config.APP_BASE_URL.startswith("https://"),
        )
    return resp


@bp.get("/r/<code>")
def short_link(code: str):
    _remember(code)
    return redirect(url_for("landing"))


# ---------------------------------------------------------------------------
# Sign up
# ---------------------------------------------------------------------------

def _phone_ok(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone or "")
    return len(digits) == 10 or (len(digits) == 11 and digits.startswith("1"))


@bp.get("/affiliates")
def signup():
    if is_affiliate(current_user()):
        return redirect(url_for("affiliates.dashboard"))
    return render_template("affiliates/signup.html", form={}, error=None,
                           terms=TERMS_TEXT, ref_days=REF_DAYS)


@bp.post("/affiliates")
def signup_submit():
    form = {
        "first_name": (request.form.get("first_name") or "").strip()[:60],
        "last_name": (request.form.get("last_name") or "").strip()[:60],
        "email": normalize_email(request.form.get("email")),
        "phone": (request.form.get("phone") or "").strip()[:40],
    }
    password = request.form.get("password") or ""

    def fail(message: str):
        return render_template("affiliates/signup.html", form=form,
                               error=message, terms=TERMS_TEXT,
                               ref_days=REF_DAYS), 400

    def sent():
        # The same page whether the address was new or already had an
        # account. Anything else would let this form answer "does this person
        # have an account with a credit repair company?", which is nobody's
        # business.
        return render_template("affiliates/check_email.html",
                               email=form["email"])

    if (request.form.get("company") or "").strip():  # honeypot
        return sent()

    conn = get_db()
    ip = client_ip()
    if throttled(conn, "affiliate:ip", ip):
        return fail("Too many sign-ups from this connection. Try again in an "
                    "hour, or email us and we'll set you up.")

    if not form["first_name"] or not form["last_name"]:
        return fail("Please enter your first and last name.")
    if not valid_email(form["email"]):
        return fail("That email address doesn't look right.")
    if not _phone_ok(form["phone"]):
        return fail("Please enter a 10-digit US phone number.")
    problem = password_problem(password)
    if problem:
        return fail(problem)
    if request.form.get("agree") != "yes":
        return fail("Please tick the box to agree to the affiliate rules.")

    record_failure(conn, "affiliate:ip", ip)
    existing = row_to_dict(conn.execute(
        "SELECT * FROM users WHERE email = ?", (form["email"],)).fetchone())

    if existing:
        if is_affiliate(existing) and not existing.get("email_verified_at"):
            # Signed up before and lost the email. Send a fresh link rather
            # than a dead end; the password they chose the first time stands.
            token = issue_token(conn, int(existing["id"]), security.EMAIL_VERIFY)
            conn.commit()
            mailer.send_affiliate_confirm(
                existing, _absolute(url_for("verify_email", token=token)))
        else:
            audit("affiliate.signup_existing", detail=form["email"], conn=conn)
            conn.commit()
            mailer.send_affiliate_existing(existing)
        return sent()

    now = utcnow()
    code = _new_code(conn, form["first_name"])
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, first_name, last_name, phone, "
        "role, case_stage, created_at, password_changed_at, affiliate_code) "
        "VALUES (?, ?, ?, ?, ?, ?, 'intake', ?, ?, ?)",
        (form["email"], hash_password(password), form["first_name"],
         form["last_name"], form["phone"], ROLE, now, now, code),
    )
    user_id = int(cur.lastrowid)
    audit("affiliate.signup", target_user_id=user_id,
          detail=f"code {code}; agreed: {TERMS_TEXT}", conn=conn)
    token = issue_token(conn, user_id, security.EMAIL_VERIFY)
    conn.commit()

    user = dict(form, id=user_id, affiliate_code=code)
    mailer.send_affiliate_confirm(user, _absolute(url_for("verify_email", token=token)))
    mailer.send_admin_new_affiliate(user, _absolute(url_for("affiliates.admin_list")))
    return sent()



# ---------------------------------------------------------------------------
# The affiliate's own dashboard
# ---------------------------------------------------------------------------

@bp.get("/affiliate")
@require_login
def dashboard():
    user = current_user()
    if not is_affiliate(user):
        return redirect(url_for("affiliates.admin_list") if user["role"] == "admin"
                        else url_for("portal"))
    if user.get("archived_at"):
        logout_user()
        flash("This affiliate account has been deactivated. Write to "
              f"{config.SUPPORT_EMAIL} if you think that's a mistake.", "error")
        return redirect(url_for("login"))

    conn = get_db()
    leads = [dict(r) for r in conn.execute(
        "SELECT full_name, status, created_at FROM leads WHERE affiliate_id = ? "
        "ORDER BY created_at DESC, id DESC", (user["id"],)).fetchall()]
    referrals = [{
        "name": display_name(l["full_name"]),
        "status": l["status"],
        "status_label": STATUS_FOR_AFFILIATE.get(l["status"], l["status"]),
        "created_at": l["created_at"],
    } for l in leads]
    clicks = conn.execute("SELECT COUNT(*) FROM affiliate_clicks "
                          "WHERE affiliate_id = ?", (user["id"],)).fetchone()[0]
    return render_template(
        "affiliates/dashboard.html",
        verified=bool(user.get("email_verified_at")),
        link=referral_link(user["affiliate_code"]),
        referrals=referrals,
        clicks=clicks,
        spoken_to=sum(1 for r in referrals if r["status"] in ("called", "closed")),
        terms=TERMS_TEXT,
        ref_days=REF_DAYS,
    )


@bp.post("/affiliate/confirm/resend")
@require_login
def resend_confirm():
    user = current_user()
    if is_affiliate(user) and not user.get("email_verified_at"):
        conn = get_db()
        token = issue_token(conn, int(user["id"]), security.EMAIL_VERIFY)
        conn.commit()
        mailer.send_affiliate_confirm(user, _absolute(url_for("verify_email", token=token)))
    flash("Confirmation email sent. Check your inbox, and your spam folder.", "success")
    return redirect(url_for("affiliates.dashboard"))


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@bp.get("/admin/affiliates")
@require_admin
def admin_list():
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT u.id, u.first_name, u.last_name, u.email, u.phone, "
        "       u.affiliate_code, u.created_at, u.email_verified_at, u.archived_at, "
        "  (SELECT COUNT(*) FROM affiliate_clicks c WHERE c.affiliate_id = u.id) AS clicks, "
        "  (SELECT COUNT(*) FROM leads l WHERE l.affiliate_id = u.id) AS referrals, "
        "  (SELECT COUNT(*) FROM leads l WHERE l.affiliate_id = u.id "
        "     AND l.status = 'new') AS waiting, "
        "  (SELECT MAX(l.created_at) FROM leads l WHERE l.affiliate_id = u.id) AS last_referral "
        "FROM users u WHERE u.role = ? "
        "ORDER BY u.archived_at IS NOT NULL, referrals DESC, u.created_at DESC",
        (ROLE,)).fetchall()]
    for r in rows:
        r["link"] = referral_link(r["affiliate_code"]) if r["affiliate_code"] else ""
    return render_template("admin/affiliates.html", affiliates=rows,
                           ref_days=REF_DAYS)


@bp.post("/admin/affiliates/<int:user_id>/status")
@require_admin
def admin_set_status(user_id: int):
    conn = get_db()
    row = row_to_dict(conn.execute("SELECT * FROM users WHERE id = ? AND role = ?",
                                   (user_id, ROLE)).fetchone())
    if not row:
        flash("That affiliate no longer exists.", "error")
        return redirect(url_for("affiliates.admin_list"))
    action = request.form.get("action")
    name = f"{row['first_name']} {row['last_name']}".strip() or row["email"]
    if action == "deactivate":
        conn.execute("UPDATE users SET archived_at = ?, archived_reason = ? "
                     "WHERE id = ?", (utcnow(), "affiliate deactivated", user_id))
        flash(f"{name} is deactivated. Their link no longer credits them and "
              f"they can't sign in. Past referrals stay on record.", "success")
    elif action == "reactivate":
        conn.execute("UPDATE users SET archived_at = NULL, archived_reason = '' "
                     "WHERE id = ?", (user_id,))
        flash(f"{name} is active again.", "success")
    elif action == "confirm":
        # For the affiliate who swears the email never came.
        conn.execute("UPDATE users SET email_verified_at = COALESCE(email_verified_at, ?) "
                     "WHERE id = ?", (utcnow(), user_id))
        flash(f"{name}'s email is marked confirmed and their link is live.", "success")
    else:
        return redirect(url_for("affiliates.admin_list"))
    audit(f"affiliate.{action}", actor=current_user(), target_user_id=user_id,
          detail=row["email"], conn=conn)
    conn.commit()
    return redirect(url_for("affiliates.admin_list"))
