"""
Private sign-up link.

One secret URL (/join/<code>) that an administrator texts or emails to a new
client. The client fills in their details, chooses a password, reads the
credit file rights disclosure and signs the agreement, all on one page. Their
account is created with a pending order and stays on a "we're reviewing your
enrollment" screen until an administrator approves it, which is when payment
has been arranged.

The link is not linked from anywhere on the site. Administrators can switch it
off or replace it with a new one (which kills the old) from Sign-up link in
the admin area. A wrong or retired code gets the ordinary 404 page, so the URL
cannot be guessed at.
"""
from __future__ import annotations

import re
import secrets

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)

import config
import mailer
import notify
from auth import (current_user, hash_password, login_user, normalize_email,
                  password_problem, require_admin, valid_email)
from db import add_event, get_db, row_to_dict, utcnow
from security import audit, client_ip, record_failure, throttled

bp = Blueprint("enroll", __name__)

THROTTLE = "join:ip"
US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
             "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR").split()
PAY_METHODS = ["Zelle", "Cash App", "Card by phone", "Invoice", "Cash or check", "Other"]


# ---------------------------------------------------------------------------
# The link
# ---------------------------------------------------------------------------

def _get(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def link_state(conn=None) -> dict:
    """The current code and whether it works. Made on first look."""
    conn = conn or get_db()
    code = _get(conn, "enroll_code")
    if not code:
        _set(conn, "enroll_code", secrets.token_urlsafe(9))
        _set(conn, "enroll_enabled", "1")
        conn.commit()
        code = _get(conn, "enroll_code")
    enabled = _get(conn, "enroll_enabled") != "0"
    base = config.APP_BASE_URL
    if not base or base.startswith(("http://127.0.0.1", "http://localhost")):
        base = request.url_root.rstrip("/")
    return {"code": code, "enabled": enabled, "url": f"{base}/join/{code}"}


def _check_code(code: str) -> None:
    state = link_state()
    if not state["enabled"] or not secrets.compare_digest(code, state["code"]):
        abort(404)


# ---------------------------------------------------------------------------
# The client's side
# ---------------------------------------------------------------------------

def _fee() -> str:
    return f"${config.PRICE_CENTS / 100:,.2f}".replace(".00", "")


def _page(code: str, form: dict, error: str | None = None, status: int = 200):
    return render_template("join.html", code=code, form=form, error=error,
                           states=US_STATES, agreement_amount=_fee()), status


@bp.get("/join/<code>")
def join(code: str):
    _check_code(code)
    me = current_user()
    if me and me["role"] == "client":
        return redirect(url_for("portal"))
    return _page(code, {})


@bp.post("/join/<code>")
def join_submit(code: str):
    _check_code(code)
    conn = get_db()
    ip = client_ip()
    f = {k: (request.form.get(k) or "").strip() for k in
         ("first_name", "last_name", "email", "phone", "street", "city", "state", "zip", "signature")}
    f["email"] = normalize_email(f["email"])
    f["state"] = f["state"].upper()

    def fail(msg: str, status: int = 400):
        return _page(code, f, msg, status)

    if throttled(conn, THROTTLE, ip):
        return fail("Too many sign-ups from this connection. Please call us and we'll finish it with you.", 429)

    if not f["first_name"] or not f["last_name"]:
        return fail("Enter your first and last name as they appear on your ID.")
    if not valid_email(f["email"]):
        return fail("That email address doesn't look right.")
    digits = re.sub(r"\D", "", f["phone"])
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return fail("Enter a 10-digit phone number so we can reach you.")
    if not f["street"] or not f["city"]:
        return fail("Enter your current mailing address. Your dispute letters are sent from it.")
    if f["state"] not in US_STATES:
        return fail("Pick your state.")
    if not re.fullmatch(r"\d{5}", f["zip"]):
        return fail("Enter your 5-digit ZIP code.")
    password = request.form.get("password") or ""
    if password != (request.form.get("password_confirm") or ""):
        return fail("Those two passwords don't match.")
    problem = password_problem(password)
    if problem:
        return fail(problem)
    if request.form.get("accept_disclosure") != "yes":
        return fail("Please confirm you have read your credit file rights. Federal law requires "
                    "that disclosure before you sign.")
    if request.form.get("accept_agreement") != "yes":
        return fail("You'll need to accept the service agreement to continue.")
    expected = f"{f['first_name']} {f['last_name']}"
    if f["signature"].lower() != expected.lower():
        return fail(f"To sign, type your full name exactly as “{expected}”.")

    # Counted per attempt that got this far, so a well-formed flood is still
    # stopped, not only failures.
    record_failure(conn, THROTTLE, ip)

    existing = conn.execute("SELECT id FROM users WHERE email = ?", (f["email"],)).fetchone()
    if existing:
        conn.commit()
        return fail("There's already an account with that email. Sign in instead, or call us "
                    "and we'll sort it out.")

    phone = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, first_name, last_name, phone, role, case_stage, "
        "created_at, password_changed_at, agreement_signed_at, agreement_name, agreement_ip, "
        "disclosure_ack_at, street, city, state, zip) "
        "VALUES (?, ?, ?, ?, ?, 'client', 'intake', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f["email"], hash_password(password), f["first_name"], f["last_name"], phone, now, now,
         now, f["signature"], ip, now, f["street"][:120], f["city"][:60], f["state"], f["zip"]))
    user_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO orders (user_id, amount_cents, currency, status, created_at, source, note) "
        "VALUES (?, ?, ?, 'pending', ?, 'signup_link', 'Signed up through the private link')",
        (user_id, config.PRICE_CENTS, config.CURRENCY, now))
    add_event(conn, user_id, title="Agreement signed",
              body="You signed up and signed your agreement online. We'll confirm payment with you "
                   "and open your file.", stage="intake", created_by="client")
    audit("client.signed_up", target_user_id=user_id,
          detail=f"{f['email']} via private link; signed as {f['signature']}", conn=conn)
    conn.commit()

    client = row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
    admin_url = f"{config.APP_BASE_URL.rstrip('/')}{url_for('admin_client_detail', user_id=user_id)}"
    mailer.send_email(
        config.ADMIN_ALERT_EMAIL, f"New sign-up to approve: {expected}",
        f"{expected} just signed up through your private link and signed the agreement.\n\n"
        f"Email: {f['email']}\nPhone: {phone}\n"
        f"Address: {f['street']}, {f['city']}, {f['state']} {f['zip']}\n\n"
        f"Their portal stays locked until you approve them. Once payment is arranged, approve here:\n"
        f"{admin_url}\n")
    mailer.send_email(
        f["email"], f"Welcome to {config.BRAND_NAME}",
        f"Hi {f['first_name']},\n\nThanks for signing up. Your agreement is signed and on file.\n\n"
        f"Next, we'll contact you at {phone} to confirm payment. As soon as that's done your portal "
        f"opens and you can upload your documents.\n\n"
        f"You can cancel within {config.CANCELLATION_DAYS} business days for a full refund.\n\n"
        f"{config.BRAND_NAME}\n{config.SUPPORT_PHONE}")
    for row in conn.execute("SELECT id FROM users WHERE role = 'admin' AND archived_at IS NULL").fetchall():
        notify.push_to_user(int(row["id"]), "New sign-up to approve", f"{expected} just enrolled.",
                            url_for("admin_client_detail", user_id=user_id), conn=conn)

    login_user(user_id, f["email"], "client")
    return redirect(url_for("portal"))


# ---------------------------------------------------------------------------
# The administrator's side
# ---------------------------------------------------------------------------

def awaiting_approval(conn=None) -> list[dict]:
    conn = conn or get_db()
    return [dict(r) for r in conn.execute(
        "SELECT u.id, u.first_name, u.last_name, u.email, u.phone, o.id AS order_id, "
        "o.amount_cents, o.created_at AS signed_up_at, o.source FROM orders o "
        "JOIN users u ON u.id = o.user_id WHERE o.status = 'pending' AND u.role = 'client' "
        "AND u.archived_at IS NULL AND NOT EXISTS (SELECT 1 FROM orders p WHERE p.user_id = u.id "
        "AND p.status = 'paid') ORDER BY o.created_at").fetchall()]


@bp.route("/admin/signup-link", methods=["GET", "POST"])
@require_admin
def admin_link():
    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        me = current_user()
        if action == "regenerate":
            _set(conn, "enroll_code", secrets.token_urlsafe(9))
            _set(conn, "enroll_enabled", "1")
            audit("signup_link.replaced", actor=me, conn=conn)
            flash("New link made. The old one no longer works.", "success")
        elif action in ("on", "off"):
            _set(conn, "enroll_enabled", "1" if action == "on" else "0")
            audit(f"signup_link.{action}", actor=me, conn=conn)
            flash("Sign-up link is on." if action == "on" else
                  "Sign-up link is off. Anyone opening it sees a not-found page.", "success")
        conn.commit()
        return redirect(url_for("enroll.admin_link"))
    return render_template("admin/signup_link.html", link=link_state(conn),
                           waiting=awaiting_approval(conn), methods=PAY_METHODS)


@bp.post("/admin/orders/<int:order_id>/approve")
@require_admin
def approve(order_id: int):
    """Payment is arranged: mark the order paid and open the client's portal."""
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ? AND status = 'pending'", (order_id,)).fetchone()
    if not order:
        abort(404)
    client = row_to_dict(conn.execute("SELECT * FROM users WHERE id = ? AND role = 'client'",
                                      (order["user_id"],)).fetchone())
    if not client:
        abort(404)
    method = (request.form.get("method") or "").strip()[:40]
    try:
        cents = int(round(float(request.form.get("amount") or order["amount_cents"] / 100) * 100))
    except ValueError:
        cents = order["amount_cents"]
    me = current_user()
    note = f"Approved by {me['email']}" + (f" — paid by {method}" if method else "")
    conn.execute("UPDATE orders SET status = 'paid', paid_at = ?, amount_cents = ?, note = ? WHERE id = ?",
                 (utcnow(), max(0, cents), note, order_id))
    conn.execute("UPDATE users SET case_stage = 'documents' WHERE id = ? AND case_stage = 'intake'",
                 (client["id"],))
    add_event(conn, client["id"], title="You're approved. Welcome!",
              body="Your portal is open. Start by uploading your documents.",
              stage="documents", created_by=me["email"])
    audit("client.approved", actor=me, target_user_id=client["id"],
          detail=f"order {order_id}; ${cents / 100:,.2f}; {method or 'method not given'}", conn=conn)
    conn.commit()
    portal_url = f"{config.APP_BASE_URL.rstrip('/')}/portal"
    mailer.send_email(
        client["email"], "Your portal is open",
        f"Hi {client['first_name'] or 'there'},\n\nYou're all set. Your {config.BRAND_NAME} portal is open.\n\n"
        f"Sign in and start with your documents. You can snap photos with your phone:\n{portal_url}\n\n"
        f"{config.BRAND_NAME}")
    notify.push_to_user(client["id"], "Your portal is open", "Sign in and upload your documents to get started.",
                        "/portal", conn=conn)
    notify.text_client(client, f"You're approved! Your portal is open: {portal_url}")
    flash(f"{client['first_name']} {client['last_name']} is approved and has been emailed.", "success")
    back = request.form.get("next") or url_for("admin_client_detail", user_id=client["id"])
    return redirect(back if back.startswith("/admin") else url_for("admin_client_detail", user_id=client["id"]))
