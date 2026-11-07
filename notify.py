"""
Notifications beyond email: phone push, text messages, and the automatic
reminders that use them.

Push
    Clients (and staff) who add the portal to their phone's home screen can
    turn on notifications. The keys that sign them (VAPID) are generated on
    first use and kept in the database, so there is nothing to configure.

Text messages
    Sent through Twilio when TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and
    TWILIO_FROM_NUMBER are set, and only to clients who switched texts on
    themselves. The consent wording they agreed to is written to the audit log.

Reminders
    A background loop, every 15 minutes, nudges clients who have:
      * a letter packet sent 24 hours ago and never opened
      * a letter packet opened 48 hours ago and still unsigned
      * required documents still missing 2 days, then 7 days, after enrolling
    Each reminder is claimed in reminder_log before it is sent, so two server
    processes running the same loop never send it twice, and a client never
    gets the same nudge again.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, jsonify, request, flash, redirect, url_for

import config
import mailer
from auth import current_user, require_login, require_admin
from db import get_db, utcnow
from security import audit

log = logging.getLogger("notify")
bp = Blueprint("notify", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint   TEXT    NOT NULL UNIQUE,
    p256dh     TEXT    NOT NULL,
    auth       TEXT    NOT NULL,
    user_agent TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id);

-- One row per reminder ever sent. The unique key is the claim.
CREATE TABLE IF NOT EXISTS reminder_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind     TEXT    NOT NULL,
    ref_id   INTEGER NOT NULL DEFAULT 0,
    sent_at  TEXT    NOT NULL,
    channels TEXT    NOT NULL DEFAULT '',
    UNIQUE (user_id, kind, ref_id)
);
"""

SMS_CONSENT_TEXT = (
    "Text me reminders and case updates from Velosify Credit at this number. "
    "Up to 4 messages a month. Message and data rates may apply. Reply STOP "
    "to opt out at any time. Consent is not a condition of service."
)


def _base() -> str:
    return config.APP_BASE_URL.rstrip("/")


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------

def _setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def vapid_keys(conn=None) -> tuple[str, str] | None:
    """(public key for the browser, private key PEM). Made once and stored.

    INSERT OR IGNORE then re-read, so two processes racing on the very first
    request settle on the same pair."""
    conn = conn or get_db()
    pub, priv = _setting(conn, "vapid_public"), _setting(conn, "vapid_private")
    if pub and priv:
        return pub, priv
    try:
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid01
    except ImportError:
        log.warning("pywebpush is not installed; push notifications are off")
        return None
    v = Vapid01()
    v.generate_keys()
    raw = v.public_key.public_bytes(serialization.Encoding.X962,
                                    serialization.PublicFormat.UncompressedPoint)
    new_pub = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    new_priv = v.private_pem().decode()
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('vapid_public', ?)", (new_pub,))
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('vapid_private', ?)", (new_priv,))
    conn.commit()
    return _setting(conn, "vapid_public"), _setting(conn, "vapid_private")


def push_available() -> bool:
    try:
        import pywebpush  # noqa: F401
        return True
    except ImportError:
        return False


def push_to_user(user_id: int, title: str, body: str, url: str = "/portal", conn=None) -> int:
    """Send to every device this person turned notifications on for.
    Best-effort: returns how many went, never raises."""
    if not push_available():
        return 0
    conn = conn or get_db()
    subs = conn.execute("SELECT * FROM push_subscriptions WHERE user_id = ?", (user_id,)).fetchall()
    if not subs:
        return 0
    keys = vapid_keys(conn)
    if not keys:
        return 0
    from py_vapid import Vapid01
    from pywebpush import WebPushException, webpush
    signer = Vapid01.from_pem(keys[1].encode())
    payload = json.dumps({"title": title, "body": body, "url": url})
    sent = 0
    for s in subs:
        try:
            webpush(
                subscription_info={"endpoint": s["endpoint"],
                                   "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                data=payload,
                vapid_private_key=signer,
                vapid_claims={"sub": f"mailto:{config.SUPPORT_EMAIL}"},
                ttl=86400,
                timeout=10,
            )
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):      # the device unsubscribed or was reset
                conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (s["id"],))
                conn.commit()
            else:
                log.warning("push failed for user %s: %s", user_id, e)
        except Exception as e:  # pragma: no cover - a push must never break a request
            log.warning("push failed for user %s: %s", user_id, e)
    return sent


def push_to_case_team(client: dict, title: str, body: str, url: str, conn=None) -> None:
    """The specialist on the file, or every administrator if nobody is."""
    conn = conn or get_db()
    if client.get("assigned_to"):
        ids = [client["assigned_to"]]
    else:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM users WHERE role = 'admin' AND archived_at IS NULL")]
    for uid in ids:
        push_to_user(int(uid), title, body, url, conn=conn)


# ---------------------------------------------------------------------------
# Text messages
# ---------------------------------------------------------------------------

def e164(phone: str) -> str | None:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def send_sms(phone: str, text: str) -> bool:
    if not config.SMS_ENABLED:
        return False
    to = e164(phone)
    if not to:
        return False
    data = urllib.parse.urlencode({"To": to, "From": config.TWILIO_FROM_NUMBER,
                                   "Body": text}).encode()
    auth = base64.b64encode(f"{config.TWILIO_ACCOUNT_SID}:{config.TWILIO_AUTH_TOKEN}".encode()).decode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json",
        data=data, headers={"Authorization": f"Basic {auth}",
                            "User-Agent": "VelosifyCredit/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        log.warning("sms to %s failed: %s %s", to[-4:], e.code, e.read()[:300])
    except Exception as e:
        log.warning("sms to %s failed: %s", to[-4:], e)
    return False


def text_client(client: dict, text: str) -> bool:
    """Only to a client who switched texts on, and only with STOP wording the
    first time is not enough: carrier rules want it on every automated text."""
    if not client.get("sms_opt_in_at") or not client.get("phone"):
        return False
    return send_sms(client["phone"], f"Velosify Credit: {text} Reply STOP to opt out.")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

SERVICE_WORKER = r"""
/* Velosify Credit service worker: shows push notifications and opens the
   right page when one is tapped. It deliberately caches nothing, so a client
   never sees a stale copy of their file. */
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener("push", function (event) {
  var d = {};
  try { d = event.data ? event.data.json() : {}; } catch (err) { d = { body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(d.title || "Velosify Credit", {
    body: d.body || "",
    icon: "/static/img/favicon-192.png",
    badge: "/static/img/favicon-192.png",
    data: { url: d.url || "/portal" }
  }));
});
self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/portal";
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if ("focus" in list[i]) { list[i].navigate(url); return list[i].focus(); }
    }
    return self.clients.openWindow(url);
  }));
});
"""


@bp.get("/sw.js")
def service_worker():
    """Served from the root so it may control every page of the portal."""
    resp = Response(SERVICE_WORKER, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@bp.get("/push/key")
@require_login
def push_key():
    keys = vapid_keys() if push_available() else None
    if not keys:
        return jsonify({"error": "Notifications aren't available right now."}), 503
    return jsonify({"key": keys[0]})


@bp.post("/push/subscribe")
@require_login
def push_subscribe():
    user = current_user()
    data = request.get_json(silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        return jsonify({"error": "That subscription isn't valid."}), 400
    conn = get_db()
    # The same device signing in as someone else takes the subscription over.
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    conn.execute("INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (user["id"], endpoint, keys["p256dh"], keys["auth"],
                  (request.headers.get("User-Agent") or "")[:300], utcnow()))
    audit("push.enabled", actor=user, target_user_id=int(user["id"]), conn=conn)
    conn.commit()
    push_to_user(int(user["id"]), "Notifications are on",
                 "We'll let you know here the moment something on your case needs you.",
                 "/portal", conn=conn)
    return jsonify({"ok": True})


@bp.post("/push/unsubscribe")
@require_login
def push_unsubscribe():
    user = current_user()
    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute("DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
                 (user["id"], (data.get("endpoint") or "").strip()))
    conn.commit()
    return jsonify({"ok": True})


@bp.post("/portal/texts")
@require_login
def portal_texts():
    """Turn reminder texts on or off. On is only ever the client's own act,
    with the consent wording they saw written to the audit log."""
    user = current_user()
    if user["role"] != "client":
        return redirect(url_for("portal"))
    conn = get_db()
    back = request.form.get("next") or url_for("portal_account")
    if not back.startswith("/portal"):
        back = url_for("portal_account")
    if request.form.get("enable") == "1":
        if not request.form.get("consent"):
            flash("Tick the box to agree to receive texts.", "error")
            return redirect(back)
        if not e164(user.get("phone") or ""):
            flash("Add a US mobile number to your account first.", "error")
            return redirect(url_for("portal_account"))
        conn.execute("UPDATE users SET sms_opt_in_at = ? WHERE id = ?", (utcnow(), user["id"]))
        audit("sms.opt_in", actor=user, target_user_id=int(user["id"]),
              detail=f"{(user.get('phone') or '')[-4:]}; agreed: {SMS_CONSENT_TEXT}", conn=conn)
        flash("Text reminders are on.", "success")
    else:
        conn.execute("UPDATE users SET sms_opt_in_at = NULL WHERE id = ?", (user["id"],))
        audit("sms.opt_out", actor=user, target_user_id=int(user["id"]), conn=conn)
        flash("Text reminders are off.", "success")
    conn.commit()
    return redirect(back)


@bp.post("/admin/reminders/run")
@require_admin
def run_now():
    """For the follow-ups page: send whatever is due right now, ignoring the
    quiet hours, so an administrator can see the reminders working."""
    n = run_due_reminders(respect_hours=False)
    flash(f"{n} reminder{'s' if n != 1 else ''} sent." if n else
          "Nothing is due. Every client is either up to date or has already been reminded.",
          "success")
    return redirect(request.referrer or url_for("portal"))


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def _claim(conn, user_id: int, kind: str, ref_id: int = 0) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO reminder_log (user_id, kind, ref_id, sent_at) "
                       "VALUES (?, ?, ?, ?)", (user_id, kind, ref_id, utcnow()))
    conn.commit()
    return cur.rowcount == 1


def _deliver(conn, client: dict, kind: str, ref_id: int, subject: str,
             email_text: str, short: str, path: str) -> None:
    url = _base() + path
    used = []
    if mailer.send_email(client["email"], subject,
                         f"Hi {client.get('first_name') or 'there'},\n\n{email_text}\n\n{url}\n\nVelosify Credit"):
        used.append("email")
    if text_client(client, f"{short} {url}"):
        used.append("sms")
    if push_to_user(int(client["id"]), subject, short, path, conn=conn):
        used.append("push")
    conn.execute("UPDATE reminder_log SET channels = ? WHERE user_id = ? AND kind = ? AND ref_id = ?",
                 (",".join(used), client["id"], kind, ref_id))
    audit("reminder.sent", target_user_id=int(client["id"]),
          detail=f"{kind} {ref_id or ''} via {','.join(used) or 'nothing'}".strip(), conn=conn)
    conn.commit()


def _in_hours() -> bool:
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Detroit"))
    except Exception:
        now = datetime.now()
    start, end = config.REMINDER_HOURS
    return start <= now.hour < end


def run_due_reminders(respect_hours: bool = True) -> int:
    """Send every reminder that is due and has not been sent. Needs an app
    context. Returns how many went out."""
    if respect_hours and not _in_hours():
        return 0
    from db import document_status_for  # late: db imports nothing from here
    conn = get_db()
    sent = 0
    active = "u.role = 'client' AND u.archived_at IS NULL"

    # Letters sent a day ago and never opened.
    for r in conn.execute(
            f"SELECT p.id AS pid, p.title, u.* FROM letter_packets p JOIN users u ON u.id = p.client_id "
            f"WHERE {active} AND p.status = 'pending' AND p.first_opened_at IS NULL "
            f"AND p.created_at <= ?", (_ago(hours=24),)).fetchall():
        c = dict(r)
        if _claim(conn, c["id"], "packet_unopened", c["pid"]):
            _deliver(conn, c, "packet_unopened", c["pid"], "Your letters are waiting for you",
                     "Your dispute letters are ready and waiting for your signature. We can't "
                     "mail them until you sign. It takes about two minutes:",
                     "Your dispute letters are ready to sign. It takes 2 minutes:",
                     f"/portal/letters/{c['pid']}")
            sent += 1

    # Opened two days ago, still not signed.
    for r in conn.execute(
            f"SELECT p.id AS pid, p.title, u.* FROM letter_packets p JOIN users u ON u.id = p.client_id "
            f"WHERE {active} AND p.status = 'pending' AND p.first_opened_at IS NOT NULL "
            f"AND p.first_opened_at <= ?", (_ago(hours=48),)).fetchall():
        c = dict(r)
        if _claim(conn, c["id"], "packet_unsigned", c["pid"]):
            _deliver(conn, c, "packet_unsigned", c["pid"], "One step left: sign your letters",
                     "You opened your dispute letters but haven't signed them yet. If something "
                     "in them looks wrong, message us and we'll fix it. Otherwise, sign here:",
                     "You're one signature away from us mailing your dispute letters:",
                     f"/portal/letters/{c['pid']}")
            sent += 1

    # Documents still missing after 2 days, and again after 7.
    for r in conn.execute(
            f"SELECT u.*, MIN(o.paid_at) AS paid_at FROM users u JOIN orders o ON o.user_id = u.id "
            f"WHERE {active} AND o.status = 'paid' AND u.case_stage IN ('intake', 'documents') "
            f"GROUP BY u.id HAVING MIN(o.paid_at) <= ?", (_ago(days=2),)).fetchall():
        c = dict(r)
        status = document_status_for(conn, c["id"])
        if status["complete"]:
            continue
        late = c["paid_at"] <= _ago(days=7)
        kind = "docs_7d" if late else "docs_2d"
        if late:
            _claim(conn, c["id"], "docs_2d")  # never send the 2-day one after the 7-day
        if _claim(conn, c["id"], kind):
            left = status["required_total"] - status["required_done"]
            nxt = status["next"]["short"] if status["next"] else "your documents"
            _deliver(conn, c, kind, 0,
                     "We still need a few documents" if left > 1 else "Just one document to go",
                     f"We can't start disputing until we have all your documents. {left} still "
                     f"to go, starting with: {nxt}. You can snap a photo with your phone:",
                     f"{left} document{'s' if left != 1 else ''} left before we can start. Next: {nxt}.",
                     "/portal/documents")
            sent += 1
    return sent


_started = False


def start_reminder_loop(app, every_seconds: int = 900) -> None:
    """One daemon thread per process. Safe with several processes because each
    reminder is claimed in the database before it is sent."""
    global _started
    if _started or not config.REMINDERS_ENABLED:
        return
    _started = True

    def loop():
        time.sleep(60)  # let the process finish booting first
        while True:
            try:
                with app.app_context():
                    n = run_due_reminders()
                    if n:
                        log.info("sent %s reminder(s)", n)
            except Exception:  # pragma: no cover
                log.exception("reminder run failed")
            time.sleep(every_seconds)

    threading.Thread(target=loop, name="reminders", daemon=True).start()
