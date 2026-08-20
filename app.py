"""
VelosifyCredit credit restoration site + client portal.

Three surfaces in one Flask app:

  * the public landing page and order flow (Stripe Checkout, one-time fee)
  * the members area, where clients upload intake documents and follow
    their case
  * an admin area for reviewing those documents and moving cases along

Run locally:
    pip install -r requirements.txt
    python app.py            # http://127.0.0.1:5000

With no Stripe keys set the checkout step is simulated so the whole flow is
clickable offline. See config.py for every setting.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from markupsafe import Markup, escape

import config
import db as database
import mailer
from auth import (current_user, hash_password, is_admin, login_user,
                  logout_user, normalize_email, password_problem,
                  require_admin, require_login, valid_email, verify_password)
import security
from security import (audit, client_ip, clear_failures, consume_token,
                      csrf_ok, csrf_token, issue_token, record_failure,
                      retry_after_minutes, sweep_failures, throttled)
from db import (CASE_STAGE_KEYS, CASE_STAGE_LABELS, CASE_STAGES, DOC_STATUSES,
                DOCUMENT_TYPE_KEYS, DOCUMENT_TYPES, add_event,
                document_status_for, get_db, row_to_dict, utcnow)

try:
    import stripe
    HAS_STRIPE = True
except ImportError:  # pragma: no cover. the app still runs in dev mode
    stripe = None
    HAS_STRIPE = False

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.permanent_session_lifetime = timedelta(days=30)
# Flask rejects the request body before our handler runs, which gives a
# clean 413 instead of a half-written file on disk.
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = config.APP_BASE_URL.startswith("https://")

# Fingerprinted asset URLs. Every /static reference gains a ?v=<hash of the
# file>, so a deploy changes the URL and no cache anywhere can serve a stale
# stylesheet. Without this, an intermediary that sets its own browser TTL
# (Cloudflare defaults to 4 hours) will keep handing visitors the previous
# CSS long after the origin has been updated, and purging the edge cache
# does not recall what browsers already stored.
#
# Because the URL now changes whenever the bytes do, the files themselves can
# be cached hard and forever.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60 * 60 * 24 * 365

_ASSET_HASHES: dict[str, tuple[float, str]] = {}


def asset_fingerprint(filename: str) -> str:
    """Short content hash for a file under /static, memoised on mtime so the
    digest is computed once per file per deploy rather than per request."""
    path = Path(app.static_folder or "static") / filename
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "0"
    cached = _ASSET_HASHES.get(filename)
    if cached and cached[0] == mtime:
        return cached[1]
    digest = hashlib.md5(path.read_bytes()).hexdigest()[:10]
    _ASSET_HASHES[filename] = (mtime, digest)
    return digest


def absolute_url(path: str) -> str:
    """Build a fully-qualified URL for social metadata.

    Prefers APP_BASE_URL, but falls back to the live request host when that
    setting still points at localhost. Open Graph images must be absolute, so
    an unset APP_BASE_URL would otherwise publish link previews pointing at
    127.0.0.1 and every share would render blank.
    """
    base = config.APP_BASE_URL
    if not base or base.startswith(("http://127.0.0.1", "http://localhost")):
        base = request.url_root.rstrip("/") if request else base
    return f"{base}{path}"


@app.context_processor
def _social_meta() -> dict:
    return {"absolute_url": absolute_url}


@app.context_processor
def _fingerprinted_url_for() -> dict:
    def versioned(endpoint: str, **values):
        if endpoint == "static" and values.get("filename"):
            values["v"] = asset_fingerprint(values["filename"])
        return url_for(endpoint, **values)
    return {"url_for": versioned}


app.teardown_appcontext(database.close_db)
database.init_db()

if config.BILLING_ENABLED and HAS_STRIPE:
    stripe.api_key = config.STRIPE_SECRET_KEY


# ---------------------------------------------------------------------------
# Template globals
# ---------------------------------------------------------------------------

@app.context_processor
def inject_globals() -> dict:
    return {
        "cfg": config,
        "user": current_user(),
        "is_admin": is_admin(),
        "current_year": datetime.now(timezone.utc).year,
        "CASE_STAGES": CASE_STAGES,
        "CASE_STAGE_LABELS": CASE_STAGE_LABELS,
    }


@app.template_filter("money")
def money_filter(cents: int) -> str:
    return f"${cents / 100:,.2f}".replace(".00", "")


@app.template_filter("filesize")
def filesize_filter(num: int) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


@app.template_filter("datetime")
def datetime_filter(iso: str | None, fmt: str = "%b %-d, %Y at %-I:%M %p") -> str:
    if not iso:
        return "n/a"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.strftime(fmt)


@app.template_filter("date")
def date_filter(iso: str | None) -> str:
    return datetime_filter(iso, "%b %-d, %Y")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_ip() -> str:
    """Real client IP behind a proxy (Railway, Cloudflare). Takes the first
    hop in X-Forwarded-For, which is the client as far as we can tell."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.remote_addr or "")[:64]


def _paid_order(user_id: int) -> dict | None:
    return row_to_dict(get_db().execute(
        "SELECT * FROM orders WHERE user_id = ? AND status = 'paid' "
        "ORDER BY paid_at DESC, id DESC LIMIT 1",
        (user_id,),
    ).fetchone())


def _pending_order(user_id: int) -> dict | None:
    return row_to_dict(get_db().execute(
        "SELECT * FROM orders WHERE user_id = ? AND status = 'pending' "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (user_id,),
    ).fetchone())


def _safe_ext(filename: str) -> str | None:
    """Return the lowercased extension if we accept it, else None."""
    ext = Path(filename or "").suffix.lower()
    return ext if ext in config.ALLOWED_UPLOAD_EXTS else None


_PHONE_NON_DIGIT = re.compile(r"\D+")


def phone_problem(phone: str) -> str | None:
    """Reason a phone number is unusable, or None if it's fine.

    Deliberately relaxed about formatting, since people type numbers with
    dashes, dots, parentheses and a leading +1, and rejecting those reads as
    the form being broken. What it does insist on is enough digits to be a
    real number. The case team has to be able to reach the client, which is
    why this is required rather than optional.
    """
    digits = _PHONE_NON_DIGIT.sub("", phone or "")
    if not digits:
        return "Please enter a phone number so we can reach you about your case."
    # A US number written with its country code is still ten digits.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) < 10:
        return "That phone number looks too short. Please include the area code."
    if len(digits) > 15:
        return "That phone number doesn't look right."
    return None


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _display_name(filename: str) -> str:
    """Sanitised version of the client's original filename, kept only for
    display. It never touches the filesystem. See _store_upload."""
    base = Path(filename or "file").name
    cleaned = _UNSAFE_NAME.sub("_", base).strip("._-") or "file"
    return cleaned[:120]


def _store_upload(file_storage, user_id: int) -> tuple[str, int, str]:
    """Write an uploaded file to disk under a random name.

    Returns (stored_name, size_bytes, mime_type). The name on disk is
    unguessable and derived from nothing the client controls, so a hostile
    filename can't traverse out of the upload directory or collide with
    another client's file.
    """
    ext = _safe_ext(file_storage.filename)
    stored_name = f"{user_id}_{secrets.token_hex(16)}{ext}"
    dest = config.UPLOAD_DIR / stored_name
    file_storage.save(str(dest))
    size = dest.stat().st_size
    # Derived from the extension we already allow-listed, never from the
    # browser's Content-Type. That header is set by whoever is uploading, and
    # storing it meant a file called report.pdf could be handed back as
    # text/html and run as script on our own origin.
    mime = mime_for_ext(ext)
    return stored_name, size, mime


# The only Content-Type any stored document is ever served with. Keyed on the
# extension allow-list in config, so adding an extension there without adding
# it here degrades to a download rather than to a guess.
_EXT_MIME = {
    ".pdf":  "application/pdf",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".heic": "image/heic",
    ".webp": "image/webp",
    ".doc":  "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Types a browser may render in place. Anything else is sent as a download, so
# an unexpected type can never execute in our origin.
_INLINE_SAFE = {"application/pdf", "image/png", "image/jpeg", "image/webp"}


def mime_for_ext(ext: str) -> str:
    return _EXT_MIME.get((ext or "").lower(), "application/octet-stream")


def _cancellation_deadline(order: dict | None) -> str | None:
    """CROA gives the consumer three business days to cancel. Business days
    means skipping weekends, so we walk the calendar rather than adding a
    flat timedelta."""
    if not order or not order.get("paid_at"):
        return None
    try:
        dt = datetime.fromisoformat(order["paid_at"])
    except ValueError:
        return None
    remaining = config.CANCELLATION_DAYS
    while remaining > 0:
        dt += timedelta(days=1)
        if dt.weekday() < 5:
            remaining -= 1
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.get("/")
def landing():
    return render_template("landing.html")


@app.get("/favicon.ico")
def favicon():
    """Browsers request this path directly regardless of the <link> tags, so
    serve it rather than logging a 404 on every first visit."""
    return send_file(str(Path(app.static_folder) / "img" / "favicon.ico"),
                     mimetype="image/x-icon")


@app.get("/site.webmanifest")
def site_webmanifest():
    """Lets Android use the large icons when the site is added to a home
    screen. Served from a route so it picks up the brand name and colours
    from config instead of duplicating them in a static file."""
    return jsonify({
        "name": config.BRAND_NAME,
        "short_name": config.BRAND_NAME,
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f6f9fd",
        "theme_color": "#ffffff",
        "icons": [
            {"src": url_for("static", filename="img/favicon-192.png"),
             "sizes": "192x192", "type": "image/png"},
            {"src": url_for("static", filename="img/favicon-512.png"),
             "sizes": "512x512", "type": "image/png"},
        ],
    })


# ---------------------------------------------------------------------------
# Logging and error reporting
#
# Stdout is where the platform collects logs from, so that is where these go.
# Sentry is wired only if both the DSN and the package are present, so the
# app boots identically with neither.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)

if config.SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration

        sentry_sdk.init(
            dsn=config.SENTRY_DSN,
            integrations=[FlaskIntegration()],
            # This application handles credit reports and Social Security
            # proofs. Never let the reporter attach request bodies, headers or
            # anything that could carry one.
            send_default_pii=False,
            max_request_body_size="never",
            traces_sample_rate=0.0,
        )
        app.logger.info("sentry enabled")
    except ImportError:
        app.logger.warning("SENTRY_DSN is set but sentry-sdk is not installed")


@app.before_request
def _request_id():
    """One short id per request, logged with any error and shown on the error
    page, so a client can quote it and it can actually be found."""
    g.request_id = secrets.token_hex(6)


@app.before_request
def _csrf_gate():
    """Reject any unsafe request that does not carry this session's token.

    Runs before every view, so a new form cannot be added without protection
    by forgetting to decorate it: the default is to refuse.
    """
    if request.method in security.SAFE_METHODS:
        return None
    if request.endpoint in security.CSRF_EXEMPT_ENDPOINTS:
        return None
    if csrf_ok():
        return None
    app.logger.warning("csrf rejected: endpoint=%s ip=%s",
                       request.endpoint, client_ip())
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"error": "Session expired. Reload and try again."}), 400
    return render_template("error.html", code=400,
                           message="Your session expired while that form was "
                                   "open. Go back, reload the page and try "
                                   "again."), 400


@app.context_processor
def _csrf_field():
    """`{{ csrf_field() }}` in any form. A helper rather than a raw token so a
    template cannot half-implement it."""
    def field():
        return Markup(
            '<input type="hidden" name="csrf_token" value="%s">'
            % escape(csrf_token())
        )
    return {"csrf_field": field, "csrf_token": csrf_token}


@app.after_request
def _security_headers(resp):
    """Baseline headers on every response.

    The CSP is tight because it can be: there is not a single inline <script>
    in the templates, every script is a file under /static, and the only third
    party the pages touch is Google Fonts. Inline STYLE is allowed because a
    handful of templates use style attributes; inline script is not, which is
    the half that matters.
    """
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy",
                            "geolocation=(), microphone=(), camera=(), payment=()")
    resp.headers.setdefault("Content-Security-Policy", "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data:",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ]))
    # Only meaningful over TLS, and only correct once the site really is
    # HTTPS-only, which APP_BASE_URL tells us.
    if config.APP_BASE_URL.startswith("https://"):
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    return resp


@app.get("/robots.txt")
def robots():
    """Marketing pages are fair game. Anything behind a login, and anything
    that identifies a client, is not."""
    lines = [
        "User-agent: *",
        "Disallow: /portal",
        "Disallow: /admin",
        "Disallow: /files",
        "Disallow: /order/success",
        "Disallow: /forgot-password",
        "Disallow: /reset-password",
        "Allow: /",
        "",
        f"Sitemap: {absolute_url(url_for('sitemap'))}",
        "",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap():
    """Only pages a stranger can actually open, so search engines are never
    pointed at something that will bounce them to a login."""
    endpoints = [
        ("landing", "1.0"), ("order_page", "0.9"), ("legal_index", "0.4"),
        ("credit_file_rights", "0.5"), ("notice_of_cancellation", "0.5"),
        ("agreement_preview", "0.4"), ("terms", "0.3"), ("privacy", "0.3"),
        ("refunds", "0.3"), ("disclaimer", "0.3"), ("esign", "0.2"),
        ("cookies", "0.2"), ("accessibility", "0.3"), ("state_disclosures", "0.3"),
    ]
    urls = "".join(
        f"<url><loc>{escape(absolute_url(url_for(name)))}</loc>"
        f"<priority>{pri}</priority></url>"
        for name, pri in endpoints
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f"{urls}</urlset>")
    return Response(xml, mimetype="application/xml")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "billing": config.BILLING_ENABLED})


@app.get("/privacy")
def privacy():
    return render_template("legal/privacy.html")


@app.get("/terms")
def terms():
    return render_template("legal/terms.html")


@app.get("/agreement")
def agreement_preview():
    """The service agreement, readable before anyone pays for anything."""
    return render_template("legal/agreement.html", signed=None)


@app.get("/legal")
def legal_index():
    return render_template("legal/index.html")


# The two documents below are not marketing pages. The Credit Repair
# Organizations Act requires the disclosure to be handed over as a SEPARATE
# written document before any contract is signed (15 U.S.C. 1679c), and the
# cancellation notice to accompany the contract in duplicate (1679e). They
# have their own URLs so that both can be linked, printed and acknowledged
# independently of the agreement.

@app.get("/legal/credit-file-rights")
def credit_file_rights():
    return render_template("legal/credit_file_rights.html")


@app.get("/legal/notice-of-cancellation")
def notice_of_cancellation():
    return render_template("legal/notice_of_cancellation.html")


@app.get("/refunds")
def refunds():
    return render_template("legal/refunds.html")


@app.get("/disclaimer")
def disclaimer():
    return render_template("legal/disclaimer.html")


@app.get("/legal/electronic-communications")
def esign():
    return render_template("legal/esign.html")


@app.get("/cookies")
def cookies():
    return render_template("legal/cookies.html")


@app.get("/accessibility")
def accessibility():
    return render_template("legal/accessibility.html")


@app.get("/legal/state-disclosures")
def state_disclosures():
    return render_template("legal/state_disclosures.html")


# ---------------------------------------------------------------------------
# Order flow
# ---------------------------------------------------------------------------

@app.get("/order")
def order_page():
    user = current_user()
    # Someone who already paid has nothing to buy, so send them to the portal.
    if user and _paid_order(user["id"]):
        return redirect(url_for("portal"))
    if config.CHECKOUT_UNAVAILABLE:
        return render_template("order_unavailable.html"), 503
    return render_template("order.html", form={}, error=None)


@app.post("/order")
def order_submit():
    if config.CHECKOUT_UNAVAILABLE:
        return render_template("order_unavailable.html"), 503
    form = {
        "first_name": (request.form.get("first_name") or "").strip(),
        "last_name": (request.form.get("last_name") or "").strip(),
        "email": normalize_email(request.form.get("email")),
        "phone": (request.form.get("phone") or "").strip(),
        "signature": (request.form.get("signature") or "").strip(),
    }
    password = request.form.get("password") or ""
    accepted = request.form.get("accept_agreement") == "yes"
    # The Credit Repair Organizations Act requires the credit file rights
    # disclosure to be given as a separate document BEFORE the contract is
    # signed, and requires us to keep the consumer's signed acknowledgment
    # that they received it for two years (15 U.S.C. 1679c). This is that
    # acknowledgment, and it is recorded separately from agreement consent so
    # the audit trail shows two distinct acts, not one.
    disclosure_ack = request.form.get("accept_disclosure") == "yes"

    def fail(message: str):
        return render_template("order.html", form=form, error=message), 400

    if not form["first_name"] or not form["last_name"]:
        return fail("Please enter your first and last name.")
    if not valid_email(form["email"]):
        return fail("That email address doesn't look right.")
    problem = phone_problem(form["phone"])
    if problem:
        return fail(problem)
    problem = password_problem(password)
    if problem:
        return fail(problem)
    if not disclosure_ack:
        return fail("Please confirm you have read your credit file rights. "
                    "Federal law requires us to give you that disclosure "
                    "before you sign anything.")
    if not accepted:
        return fail("You'll need to accept the service agreement to continue.")
    if form["signature"].lower() != f"{form['first_name']} {form['last_name']}".lower():
        return fail("Type your full name exactly as entered above to sign the agreement.")

    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM users WHERE email = ?", (form["email"],)
    ).fetchone()

    if existing:
        # An account already exists. If it's paid, this is a returning
        # client who should just sign in. If it isn't, they abandoned
        # checkout earlier and we let them pick up where they left off, but
        # but only with the right password, so an email address alone
        # can't be used to take over a half-finished signup.
        if _paid_order(existing["id"]):
            return fail("You already have an account. Sign in to reach your portal.")
        if not verify_password(password, existing["password_hash"]):
            return fail("An account with that email already exists. "
                        "Sign in, or use the password you chose the first time.")
        user_id = existing["id"]
        conn.execute(
            "UPDATE users SET first_name = ?, last_name = ?, phone = ?, "
            "agreement_signed_at = ?, agreement_name = ?, agreement_ip = ?, "
            "disclosure_ack_at = ? "
            "WHERE id = ?",
            (form["first_name"], form["last_name"], form["phone"],
             utcnow(), form["signature"], _client_ip(), utcnow(), user_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, first_name, last_name, "
            "phone, role, case_stage, created_at, agreement_signed_at, "
            "agreement_name, agreement_ip, disclosure_ack_at) "
            "VALUES (?, ?, ?, ?, ?, 'client', 'intake', ?, ?, ?, ?, ?)",
            (form["email"], hash_password(password), form["first_name"],
             form["last_name"], form["phone"], utcnow(), utcnow(),
             form["signature"], _client_ip(), utcnow()),
        )
        user_id = int(cur.lastrowid)

    cur = conn.execute(
        "INSERT INTO orders (user_id, amount_cents, currency, status, created_at) "
        "VALUES (?, ?, ?, 'pending', ?)",
        (user_id, config.PRICE_CENTS, config.CURRENCY, utcnow()),
    )
    order_id = int(cur.lastrowid)
    conn.commit()

    # No Stripe configured, so simulate a successful payment and the rest of
    # the product is reachable in development.
    if config.DEV_FAKE_CHECKOUT:
        _mark_order_paid(order_id, payment_intent="dev_simulated")
        return redirect(url_for("order_success", dev="1", order_id=order_id))

    try:
        line_item = ({"price": config.STRIPE_PRICE_ID, "quantity": 1}
                     if config.STRIPE_PRICE_ID else
                     {"price_data": {
                         "currency": config.CURRENCY,
                         "unit_amount": config.PRICE_CENTS,
                         "product_data": {
                             "name": f"{config.BRAND_NAME} Credit Restoration Program",
                             "description": "Full-service credit restoration, one-time fee.",
                         },
                      },
                      "quantity": 1})
        checkout = stripe.checkout.Session.create(
            mode="payment",
            line_items=[line_item],
            customer_email=form["email"],
            client_reference_id=str(order_id),
            metadata={"order_id": str(order_id), "user_id": str(user_id)},
            success_url=f"{config.APP_BASE_URL}/order/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{config.APP_BASE_URL}/order/cancel?order_id={order_id}",
        )
    except Exception as exc:  # pragma: no cover. surfaces Stripe outages
        app.logger.exception("stripe checkout failed")
        return fail(f"We couldn't reach our payment processor ({exc.__class__.__name__}). "
                    "Please try again in a moment.")

    conn.execute("UPDATE orders SET stripe_session_id = ? WHERE id = ?",
                 (checkout.id, order_id))
    conn.commit()
    return redirect(checkout.url, code=303)


def _mark_order_paid(order_id: int, payment_intent: str = "") -> dict | None:
    """Flip an order to paid and kick off everything that follows.

    Idempotent, because Stripe will happily deliver the same webhook twice and the
    success page and the webhook race each other by design.
    """
    conn = get_db()
    order = row_to_dict(
        conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    )
    if not order:
        return None
    if order["status"] == "paid":
        return order

    conn.execute(
        "UPDATE orders SET status = 'paid', paid_at = ?, stripe_payment_intent = ? "
        "WHERE id = ?",
        (utcnow(), payment_intent or order.get("stripe_payment_intent") or "", order_id),
    )
    conn.execute("UPDATE users SET case_stage = 'documents' WHERE id = ?",
                 (order["user_id"],))
    add_event(
        conn, order["user_id"],
        title="Enrollment confirmed",
        body="Payment received. Your case is open. The next step is "
             "uploading your intake documents.",
        stage="documents",
    )
    conn.commit()

    user = row_to_dict(
        conn.execute("SELECT * FROM users WHERE id = ?", (order["user_id"],)).fetchone()
    )
    if user:
        mailer.send_welcome(user, money_filter(order["amount_cents"]))
        mailer.send_admin_new_client(user)
        # Sent once, on the way in. Not a gate: a paying client is never
        # locked out of their own file over an unread confirmation email.
        if not user.get("email_verified_at"):
            token = issue_token(conn, int(user["id"]), security.EMAIL_VERIFY)
            conn.commit()
            mailer.send_email_verification(
                user, absolute_url(url_for("verify_email", token=token)))
    return row_to_dict(
        conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    )


@app.get("/order/success")
def order_success():
    """Landing spot after Stripe. Verifies the session server-side rather
    than trusting the redirect, then signs the client in."""
    session_id = (request.args.get("session_id") or "").strip()
    conn = get_db()
    order = None

    if session_id and config.BILLING_ENABLED and HAS_STRIPE:
        try:
            cs = stripe.checkout.Session.retrieve(session_id)
        except Exception:  # pragma: no cover
            app.logger.exception("could not retrieve checkout session")
            cs = None
        if cs and cs.get("payment_status") == "paid":
            order_id = int((cs.get("metadata") or {}).get("order_id")
                           or cs.get("client_reference_id") or 0)
            if order_id:
                pi = cs.get("payment_intent")
                order = _mark_order_paid(order_id, payment_intent=str(pi or ""))
    elif request.args.get("dev") == "1" and config.DEV_FAKE_CHECKOUT:
        # Simulated checkout. The order id comes through the redirect. Never
        # infer it from "most recent", which silently signs you in as whoever
        # happened to pay last.
        try:
            dev_order_id = int(request.args.get("order_id") or 0)
        except ValueError:
            dev_order_id = 0
        if dev_order_id:
            order = row_to_dict(conn.execute(
                "SELECT * FROM orders WHERE id = ? AND status = 'paid'",
                (dev_order_id,),
            ).fetchone())

    if not order:
        return render_template("order_pending.html"), 202

    user = row_to_dict(
        conn.execute("SELECT * FROM users WHERE id = ?", (order["user_id"],)).fetchone()
    )
    if user:
        login_user(user["id"], user["email"], user["role"])
    return render_template("order_success.html", order=order)


@app.get("/order/cancel")
def order_cancel():
    return render_template("order_cancel.html")


@app.post("/webhook/stripe")
def stripe_webhook():
    """Source of truth for payment state. The success page is a convenience;
    this is what runs even when the client closes the tab mid-redirect."""
    if not (config.BILLING_ENABLED and HAS_STRIPE):
        return jsonify({"error": "Billing is not configured."}), 503

    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    if not config.STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Webhook secret not configured."}), 503
    try:
        event = stripe.Webhook.construct_event(
            payload, sig, config.STRIPE_WEBHOOK_SECRET
        )
    except Exception:
        # Bad signature, or a payload we can't parse. Never process it.
        return jsonify({"error": "Invalid signature."}), 400

    kind = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    if kind == "checkout.session.completed" and obj.get("payment_status") == "paid":
        order_id = int((obj.get("metadata") or {}).get("order_id")
                       or obj.get("client_reference_id") or 0)
        if order_id:
            _mark_order_paid(order_id, payment_intent=str(obj.get("payment_intent") or ""))

    elif kind == "charge.refunded":
        pi = str(obj.get("payment_intent") or "")
        if pi:
            conn = get_db()
            row = conn.execute(
                "SELECT * FROM orders WHERE stripe_payment_intent = ?", (pi,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE orders SET status = 'refunded', refunded_at = ? WHERE id = ?",
                    (utcnow(), row["id"]),
                )
                add_event(conn, row["user_id"], title="Refund issued",
                          body="Your payment was refunded in full.")
                conn.commit()

    return jsonify({"received": True})


# ---------------------------------------------------------------------------
# Sign in / out
# ---------------------------------------------------------------------------

@app.get("/login")
def login():
    if current_user():
        return redirect(url_for("portal"))
    return render_template("login.html", error=None, email="")


@app.post("/login")
def login_submit():
    email = normalize_email(request.form.get("email"))
    password = request.form.get("password") or ""
    conn = get_db()
    ip = client_ip()

    # Two counters, checked before the password is even looked at. The account
    # counter stops someone grinding one inbox; the address counter stops
    # someone spraying one password across many. Both are in the database, so
    # a container restart does not hand an attacker a fresh budget.
    for scope, key in (("login:email", email), ("login:ip", ip)):
        if throttled(conn, scope, key):
            sweep_failures(conn)
            conn.commit()
            audit("login.throttled", detail=f"{scope} {key}")
            conn.commit()
            wait = retry_after_minutes(scope)
            resp = render_template(
                "login.html", email=email,
                error=f"Too many sign-in attempts. Try again in {wait} minutes, "
                      f"or reset your password.",
            )
            return resp, 429

    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    if not row or not verify_password(password, row["password_hash"]):
        record_failure(conn, "login:email", email)
        record_failure(conn, "login:ip", ip)
        audit("login.failed", detail=email, conn=conn)
        sweep_failures(conn)
        conn.commit()
        # Same message either way, so we don't confirm which emails have accounts.
        return render_template(
            "login.html", email=email,
            error="That email and password don't match an account.",
        ), 401

    clear_failures(conn, "login:email", email)
    login_user(row["id"], row["email"], row["role"])
    # A fresh CSRF token on privilege change, so a token captured before
    # sign-in cannot be replayed against the session it becomes.
    session.pop("_csrf", None)
    audit("login.ok", actor=row_to_dict(row), conn=conn)
    conn.commit()

    nxt = request.form.get("next") or request.args.get("next") or ""
    # Only ever redirect within this site.
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("admin_clients") if row["role"] == "admin" else url_for("portal"))


# ---------------------------------------------------------------------------
# Password reset
#
# Before this existed there was no recovery path at all: a client who forgot
# their password could not reach the documents they had already uploaded, and
# no admin tool could help them.
#
# The responses are deliberately identical whether or not the address has an
# account. A reset form that says "no such user" is an account enumerator, and
# on a credit repair site the mere fact that an address has an account is
# sensitive.
# ---------------------------------------------------------------------------

@app.get("/forgot-password")
def forgot_password():
    if current_user():
        return redirect(url_for("portal"))
    return render_template("forgot_password.html", sent=False, error=None, email="")


@app.post("/forgot-password")
def forgot_password_submit():
    email = normalize_email(request.form.get("email"))
    conn = get_db()
    ip = client_ip()

    def sent():
        # Identical page whatever happened, including on throttle, so timing
        # and content both stay uninformative.
        return render_template("forgot_password.html", sent=True, error=None,
                               email=email)

    if not valid_email(email):
        return render_template("forgot_password.html", sent=False, email=email,
                               error="That email address doesn't look right."), 400

    if throttled(conn, "reset:email", email) or throttled(conn, "reset:ip", ip):
        audit("reset.throttled", detail=email, conn=conn)
        conn.commit()
        return sent()

    record_failure(conn, "reset:email", email)
    record_failure(conn, "reset:ip", ip)

    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        token = issue_token(conn, int(row["id"]), security.PASSWORD_RESET)
        link = absolute_url(url_for("reset_password", token=token))
        audit("reset.requested", target_user_id=int(row["id"]), conn=conn)
        conn.commit()
        mailer.send_password_reset(row_to_dict(row), link)
    else:
        conn.commit()
    return sent()


@app.get("/verify-email/<token>")
def verify_email(token: str):
    """Confirm the address is real and reachable.

    Deliberately not a gate. Someone who has paid must never be locked out of
    their own documents because a confirmation email went to spam; the point
    is to catch a typo early, and to know which addresses we can trust for
    case correspondence.
    """
    conn = get_db()
    user_id = consume_token(conn, token, security.EMAIL_VERIFY)
    if not user_id:
        flash("That confirmation link has expired or has already been used.", "error")
        return redirect(url_for("portal") if current_user() else url_for("login"))
    conn.execute("UPDATE users SET email_verified_at = ? WHERE id = ?",
                 (utcnow(), user_id))
    audit("email.verified", target_user_id=user_id, conn=conn)
    conn.commit()
    flash("Thanks, your email address is confirmed.", "success")
    return redirect(url_for("portal") if current_user() else url_for("login"))


@app.post("/portal/verify-email/resend")
@require_login
def resend_verification():
    user = current_user()
    conn = get_db()
    if not user.get("email_verified_at"):
        token = issue_token(conn, int(user["id"]), security.EMAIL_VERIFY)
        conn.commit()
        mailer.send_email_verification(
            user, absolute_url(url_for("verify_email", token=token)))
    flash("Confirmation email sent. Check your inbox.", "success")
    return redirect(url_for("portal"))


@app.get("/reset-password/<token>")
def reset_password(token: str):
    # Validity is not checked here, only on submit. Checking on GET would burn
    # the token when a mail client prefetches the link, which several do.
    return render_template("reset_password.html", token=token, error=None)


@app.post("/reset-password/<token>")
def reset_password_submit(token: str):
    password = request.form.get("password") or ""
    confirm = request.form.get("password_confirm") or ""
    conn = get_db()

    def fail(message: str, code: int = 400):
        return render_template("reset_password.html", token=token,
                               error=message), code

    if password != confirm:
        return fail("Those two passwords don't match.")
    problem = password_problem(password)
    if problem:
        return fail(problem)

    user_id = consume_token(conn, token, security.PASSWORD_RESET)
    if not user_id:
        return fail("That reset link has expired or has already been used. "
                    "Request a new one.", 410)

    conn.execute(
        "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
        (hash_password(password), utcnow(), user_id),
    )
    # Anyone holding an old session for this account loses it, which is the
    # point of a reset when the reason for it is that someone else got in.
    add_event(conn, user_id, title="Password changed",
              body="Your portal password was reset from the emailed link.",
              created_by="client")
    audit("reset.completed", target_user_id=user_id, conn=conn)
    clear_failures(conn, "login:email", "")
    conn.commit()

    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row:
        clear_failures(conn, "login:email", row["email"])
        conn.commit()
    session.clear()
    flash("Your password has been changed. Sign in with it now.", "success")
    return redirect(url_for("login"))


@app.get("/logout")
@app.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("landing"))


# ---------------------------------------------------------------------------
# Members area
# ---------------------------------------------------------------------------

@app.get("/portal")
@require_login
def portal():
    user = current_user()
    conn = get_db()
    order = _paid_order(user["id"])
    if not order:
        # Paid for nothing yet, so the portal has nothing to show.
        return render_template("portal/unpaid.html",
                               pending=_pending_order(user["id"]))

    checklist = document_status_for(conn, user["id"])
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM case_events WHERE user_id = ? ORDER BY created_at DESC, id DESC",
        (user["id"],),
    ).fetchall()]
    stage_index = (CASE_STAGE_KEYS.index(user["case_stage"])
                   if user["case_stage"] in CASE_STAGE_KEYS else 0)
    return render_template(
        "portal/dashboard.html",
        order=order,
        checklist=checklist,
        events=events,
        stage_index=stage_index,
        cancel_by=_cancellation_deadline(order),
    )


@app.get("/portal/documents")
@require_login
def portal_documents():
    user = current_user()
    if not _paid_order(user["id"]):
        return redirect(url_for("portal"))
    return render_template(
        "portal/documents.html",
        checklist=document_status_for(get_db(), user["id"]),
    )


@app.post("/portal/documents/upload")
@require_login
def portal_upload():
    user = current_user()
    if not _paid_order(user["id"]):
        return redirect(url_for("portal"))

    doc_type = (request.form.get("doc_type") or "").strip()
    if doc_type not in DOCUMENT_TYPE_KEYS:
        flash("Pick which document you're uploading.", "error")
        return redirect(url_for("portal_documents"))

    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("Choose at least one file to upload.", "error")
        return redirect(url_for("portal_documents"))

    conn = get_db()
    saved = 0
    for f in files:
        if not _safe_ext(f.filename):
            flash(f"{_display_name(f.filename)} isn't a file type we accept. "
                  f"Use PDF, JPG, PNG or Word.", "error")
            continue
        stored_name, size, mime = _store_upload(f, user["id"])
        if size == 0:
            (config.UPLOAD_DIR / stored_name).unlink(missing_ok=True)
            flash(f"{_display_name(f.filename)} appears to be empty.", "error")
            continue
        conn.execute(
            "INSERT INTO documents (user_id, doc_type, original_name, stored_name, "
            "mime_type, size_bytes, status, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'received', ?)",
            (user["id"], doc_type, _display_name(f.filename), stored_name,
             mime, size, utcnow()),
        )
        saved += 1

    if saved:
        label = next((d["label"] for d in DOCUMENT_TYPES if d["key"] == doc_type), doc_type)
        add_event(conn, user["id"],
                  title=f"Uploaded: {label}",
                  body=f"{saved} file{'s' if saved != 1 else ''} received.",
                  created_by="client")
        conn.commit()
        flash(f"{saved} file{'s' if saved != 1 else ''} uploaded.", "success")

        # Nudge the team the moment the required set is complete.
        status = document_status_for(conn, user["id"])
        if status["complete"] and user["case_stage"] == "documents":
            conn.execute("UPDATE users SET case_stage = 'analysis' WHERE id = ?",
                         (user["id"],))
            add_event(conn, user["id"],
                      title="All documents received",
                      body="Your file is complete and has moved to report analysis.",
                      stage="analysis")
            conn.commit()
            mailer.send_documents_complete(user)

    return redirect(url_for("portal_documents"))


@app.post("/portal/documents/<int:doc_id>/delete")
@require_login
def portal_delete_document(doc_id: int):
    """A client can withdraw a file they uploaded, as long as we haven't
    accepted it yet. Once it's accepted it's part of the case record."""
    user = current_user()
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, user["id"])
    ).fetchone()
    if not row:
        abort(404)
    if row["status"] == "accepted":
        flash("That document has already been accepted and can't be removed. "
              "Contact us if it needs to change.", "error")
        return redirect(url_for("portal_documents"))

    (config.UPLOAD_DIR / row["stored_name"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    # The row is gone, so the audit line is the only remaining evidence the
    # file ever existed. That is exactly why it is written.
    audit("document.deleted", actor=user, target_user_id=int(user["id"]),
          target_doc_id=doc_id, detail=row["original_name"], conn=conn)
    conn.commit()
    flash("Document removed.", "success")
    return redirect(url_for("portal_documents"))


@app.get("/files/<int:doc_id>")
@require_login
def download_document(doc_id: int):
    """The only way an uploaded file is ever served. Owner or admin only,
    nothing under the upload directory is reachable from /static."""
    user = current_user()
    row = get_db().execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        abort(404)
    if row["user_id"] != user["id"] and not is_admin():
        abort(404)  # 404 rather than 403, so the id can't be confirmed

    path = config.UPLOAD_DIR / row["stored_name"]
    if not path.exists():
        abort(404)

    # Recomputed here rather than read from the row, so documents stored
    # before the upload path stopped trusting the browser are served safely
    # too. Anything we would not render in place is forced to download.
    mime = mime_for_ext(os.path.splitext(row["stored_name"])[1])
    resp = send_file(
        str(path),
        mimetype=mime,
        as_attachment=(request.args.get("download") == "1"
                       or mime not in _INLINE_SAFE),
        download_name=row["original_name"],
        max_age=0,
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    # A document is somebody's ID or credit file. It must never be cached by
    # a proxy, and it must never be framed by another site.
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    # These files are government IDs, Social Security proofs and full credit
    # reports. Who opened whose, and when, has to have an answer.
    audit("document.viewed", actor=user, target_user_id=int(row["user_id"]),
          target_doc_id=doc_id, detail=row["original_name"])
    get_db().commit()
    return resp


@app.get("/portal/account")
@require_login
def portal_account():
    user = current_user()
    return render_template("portal/account.html", order=_paid_order(user["id"]))


@app.post("/portal/account")
@require_login
def portal_account_save():
    user = current_user()
    conn = get_db()
    first = (request.form.get("first_name") or "").strip()
    last = (request.form.get("last_name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    if not first or not last:
        flash("Name can't be blank.", "error")
        return redirect(url_for("portal_account"))
    problem = phone_problem(phone)
    if problem:
        flash(problem, "error")
        return redirect(url_for("portal_account"))
    conn.execute(
        "UPDATE users SET first_name = ?, last_name = ?, phone = ? WHERE id = ?",
        (first, last, phone, user["id"]),
    )
    conn.commit()
    flash("Details saved.", "success")
    return redirect(url_for("portal_account"))


@app.post("/portal/password")
@require_login
def portal_password():
    user = current_user()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    if not verify_password(current, user["password_hash"]):
        flash("Your current password isn't right.", "error")
        return redirect(url_for("portal_account"))
    problem = password_problem(new)
    if problem:
        flash(problem, "error")
        return redirect(url_for("portal_account"))
    conn = get_db()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (hash_password(new), user["id"]))
    conn.commit()
    flash("Password updated.", "success")
    return redirect(url_for("portal_account"))


@app.get("/portal/agreement")
@require_login
def portal_agreement():
    return render_template("legal/agreement.html", signed=current_user())


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.get("/admin")
@require_admin
def admin_home():
    return redirect(url_for("admin_clients"))


@app.get("/admin/clients")
@require_admin
def admin_clients():
    q = (request.args.get("q") or "").strip()
    sql = (
        "SELECT u.*, "
        "  (SELECT COUNT(*) FROM documents d WHERE d.user_id = u.id) AS doc_count, "
        "  (SELECT COUNT(*) FROM documents d WHERE d.user_id = u.id "
        "     AND d.status = 'received') AS unreviewed_count, "
        "  (SELECT status FROM orders o WHERE o.user_id = u.id "
        "     ORDER BY o.created_at DESC LIMIT 1) AS order_status "
        "FROM users u WHERE u.role = 'client' "
    )
    params: list = []
    if q:
        sql += "AND (u.email LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ?) "
        params += [f"%{q}%"] * 3
    sql += "ORDER BY u.created_at DESC"
    clients = [dict(r) for r in get_db().execute(sql, params).fetchall()]
    return render_template("admin/clients.html", clients=clients, q=q)


@app.get("/admin/clients/<int:user_id>")
@require_admin
def admin_client_detail(user_id: int):
    conn = get_db()
    client = row_to_dict(
        conn.execute("SELECT * FROM users WHERE id = ? AND role = 'client'",
                     (user_id,)).fetchone()
    )
    if not client:
        abort(404)
    orders = [dict(r) for r in conn.execute(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ).fetchall()]
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM case_events WHERE user_id = ? ORDER BY created_at DESC, id DESC",
        (user_id,),
    ).fetchall()]
    audit("admin.client_opened", actor=current_user(), target_user_id=user_id,
          conn=conn)
    conn.commit()
    return render_template(
        "admin/client_detail.html",
        client=client,
        orders=orders,
        events=events,
        checklist=document_status_for(conn, user_id),
    )


@app.get("/admin/audit")
@require_admin
def admin_audit():
    """The access trail, newest first.

    Read-only on purpose: there is no route that edits or deletes a row here,
    so an admin cannot quietly tidy up after themselves through the app.
    """
    conn = get_db()
    who = (request.args.get("client") or "").strip()
    sql = ("SELECT a.*, u.first_name, u.last_name, u.email AS target_email "
           "FROM audit_log a LEFT JOIN users u ON u.id = a.target_user_id ")
    params: list = []
    if who.isdigit():
        sql += "WHERE a.target_user_id = ? "
        params.append(int(who))
    sql += "ORDER BY a.created_at DESC, a.id DESC LIMIT 300"
    entries = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return render_template("admin/audit.html", entries=entries, who=who)


@app.post("/admin/clients/<int:user_id>/stage")
@require_admin
def admin_set_stage(user_id: int):
    stage = (request.form.get("stage") or "").strip()
    if stage not in CASE_STAGE_KEYS:
        abort(400)
    conn = get_db()
    conn.execute("UPDATE users SET case_stage = ? WHERE id = ?", (stage, user_id))
    add_event(conn, user_id,
              title=f"Case moved to {CASE_STAGE_LABELS[stage]}",
              stage=stage,
              created_by=current_user()["email"])
    audit("admin.stage_changed", actor=current_user(), target_user_id=user_id,
          detail=stage, conn=conn)
    conn.commit()
    flash("Stage updated.", "success")
    return redirect(url_for("admin_client_detail", user_id=user_id))


@app.post("/admin/clients/<int:user_id>/event")
@require_admin
def admin_add_event(user_id: int):
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not title:
        flash("An update needs a title.", "error")
        return redirect(url_for("admin_client_detail", user_id=user_id))
    conn = get_db()
    add_event(conn, user_id, title=title, body=body,
              created_by=current_user()["email"])
    audit("admin.event_posted", actor=current_user(), target_user_id=user_id,
          detail=title, conn=conn)
    conn.commit()
    flash("Update posted to the client's timeline.", "success")
    return redirect(url_for("admin_client_detail", user_id=user_id))


@app.post("/admin/documents/<int:doc_id>/status")
@require_admin
def admin_document_status(doc_id: int):
    status = (request.form.get("status") or "").strip()
    note = (request.form.get("review_note") or "").strip()
    if status not in DOC_STATUSES:
        abort(400)
    conn = get_db()
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        abort(404)
    conn.execute(
        "UPDATE documents SET status = ?, review_note = ?, reviewed_at = ? WHERE id = ?",
        (status, note, utcnow(), doc_id),
    )
    audit("admin.document_reviewed", actor=current_user(),
          target_user_id=int(row["user_id"]), target_doc_id=doc_id,
          detail=status, conn=conn)
    if status == "rejected":
        label = next((d["label"] for d in DOCUMENT_TYPES
                      if d["key"] == row["doc_type"]), row["doc_type"])
        add_event(conn, row["user_id"],
                  title=f"Action needed: {label}",
                  body=note or "We couldn't use the file you sent. "
                               "Please upload a replacement.",
                  created_by=current_user()["email"])
    conn.commit()
    flash("Document updated.", "success")
    return redirect(url_for("admin_client_detail", user_id=row["user_id"]))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404,
                           message="We couldn't find that page."), 404


@app.errorhandler(413)
def too_large(_e):
    return render_template(
        "error.html", code=413,
        message=f"That file is larger than the {config.MAX_UPLOAD_MB} MB limit. "
                "Try splitting it or reducing the scan quality."), 413


@app.errorhandler(500)
def server_error(_e):  # pragma: no cover
    # request_id is attached by the logging setup below, so the line the
    # operator finds in the logs and the line shown to the visitor can be
    # matched up without guessing.
    app.logger.exception("unhandled error rid=%s path=%s",
                         getattr(g, "request_id", "-"), request.path)
    return render_template("error.html", code=500,
                           message="Something went wrong on our end.",
                           request_id=getattr(g, "request_id", None)), 500


# ---------------------------------------------------------------------------
# First-boot admin bootstrap
# ---------------------------------------------------------------------------

def bootstrap_admin() -> None:
    """Create or promote the admin account named in the environment. Runs on
    every boot and is a no-op once the account exists with the right role."""
    email = normalize_email(config.ADMIN_BOOTSTRAP_EMAIL)
    if not email or not config.ADMIN_BOOTSTRAP_PASSWORD:
        return
    conn = database.get_db()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        if row["role"] != "admin":
            conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (row["id"],))
            conn.commit()
        return
    conn.execute(
        "INSERT INTO users (email, password_hash, first_name, last_name, role, "
        "case_stage, created_at) VALUES (?, ?, 'Admin', '', 'admin', 'intake', ?)",
        (email, hash_password(config.ADMIN_BOOTSTRAP_PASSWORD), utcnow()),
    )
    conn.commit()
    print(f"[bootstrap] admin account created for {email}", flush=True)


with app.app_context():
    bootstrap_admin()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=config.DEBUG)
