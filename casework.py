"""
Casework: the parts of a file that show a client results, and the staff tools
that keep files moving.

  * Credit scores   staff record each bureau's score at intake and after each
                    round; the client's home screen charts them.
  * Dispute items   one row per negative item per bureau, with its status, so a
                    client sees "3 of 7 removed" instead of a black box.
  * Follow-ups      what is stuck and on whom: documents in but no letters,
                    letters unsigned, signed letters not mailed, bureaus past
                    their 30 days.
  * Saved replies   one-tap answers in the staff message box.
  * Bulk actions    assign, move stage or message several clients at once.
  * Client referral a "refer a friend" link, reusing the affiliate tracking.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)

import mailer
import notify
from auth import current_user, is_admin, is_specialist, require_admin, require_staff
from db import (CASE_STAGE_KEYS, CASE_STAGE_LABELS, REQUIRED_DOCUMENT_KEYS,
                add_event, get_db, post_message, row_to_dict, utcnow)
from letters import _visible_client
from security import audit

bp = Blueprint("casework", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_scores (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bureau     TEXT    NOT NULL,              -- experian | equifax | transunion
    score      INTEGER NOT NULL,
    as_of      TEXT    NOT NULL,              -- YYYY-MM-DD, the report's date
    created_at TEXT    NOT NULL,
    created_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_scores_user ON credit_scores(user_id, as_of);

CREATE TABLE IF NOT EXISTS dispute_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    creditor    TEXT    NOT NULL,
    account_ref TEXT    NOT NULL DEFAULT '',  -- last 4 only, never the full number
    item_type   TEXT    NOT NULL DEFAULT 'other',
    bureau      TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'disputed',
    round       INTEGER NOT NULL DEFAULT 1,
    note        TEXT    NOT NULL DEFAULT '',  -- shown to the client
    created_at  TEXT    NOT NULL,
    status_at   TEXT    NOT NULL,             -- when the status last changed
    resolved_at TEXT,
    created_by  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_disputes_user ON dispute_items(user_id, status);

CREATE TABLE IF NOT EXISTS saved_replies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    body       TEXT    NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL,
    created_by INTEGER
);
"""

BUREAUS = [("experian", "Experian"), ("equifax", "Equifax"), ("transunion", "TransUnion")]
BUREAU_LABELS = dict(BUREAUS)
BUREAU_COLORS = {"experian": "#1364df", "equifax": "#b45309", "transunion": "#0f766e"}

ITEM_TYPES = [
    ("collection", "Collection"),
    ("late_payment", "Late payment"),
    ("charge_off", "Charge-off"),
    ("inquiry", "Hard inquiry"),
    ("public_record", "Public record"),
    ("repossession", "Repossession"),
    ("personal_info", "Personal information"),
    ("other", "Other"),
]
ITEM_TYPE_LABELS = dict(ITEM_TYPES)

# key, staff label, client label, badge, resolved?
DISPUTE_STATUSES = [
    ("disputed", "Disputed", "Disputed, waiting on the bureau", "badge-accent", False),
    ("in_review", "Response received", "Bureau responded, we're reviewing", "badge-warn", False),
    ("deleted", "Deleted", "Removed from your report", "badge-success", True),
    ("updated", "Updated / corrected", "Corrected on your report", "badge-success", True),
    ("verified", "Verified (stayed)", "Bureau kept it; we'll challenge it again", "", True),
]
# One or two words for the badge on a phone; the full line goes underneath.
CHIP = {"disputed": "Disputed", "in_review": "Responded", "deleted": "Removed ✓",
        "updated": "Corrected ✓", "verified": "Next round"}
STATUS = {k: {"key": k, "staff": s, "client": c, "badge": b, "resolved": r, "chip": CHIP[k]}
          for k, s, c, b, r in DISPUTE_STATUSES}

DEFAULT_REPLIES = [
    ("When will I see results?",
     "Hi {first_name}, great question. The bureaus have 30 days to investigate each dispute once "
     "they receive it, so most clients see their first changes 30 to 45 days after we mail the "
     "letters. You'll see every result on your Home screen as it comes in."),
    ("Why do you need my SSN proof?",
     "Hi {first_name}, the bureaus won't process a dispute sent on your behalf unless it includes "
     "proof of your Social Security number and identity. It's stored privately and only your case "
     "team can see it."),
    ("Documents look good",
     "Hi {first_name}, thanks! Everything you sent looks good. We're reviewing your credit report "
     "now and will have your dispute letters ready for you to sign soon."),
    ("Need a clearer copy",
     "Hi {first_name}, we couldn't read one of your documents clearly. Could you retake the photo "
     "in good light with all four corners showing? Tap Documents in your portal to upload it."),
    ("Letters mailed",
     "Hi {first_name}, your signed dispute letters are in the mail by certified mail. You can follow "
     "the tracking on your Letters page. The bureaus then have 30 days to respond."),
    ("Bureau response received",
     "Hi {first_name}, we received a response from the bureau and are reviewing it now. We'll "
     "update the items on your Home screen and let you know the next step."),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(days: int) -> str:
    return (_now() - timedelta(days=days)).isoformat(timespec="seconds")


def seed_replies(conn) -> None:
    """Give a new install a starting set. Never touches a table that has rows,
    so an administrator deleting these does not see them come back."""
    if conn.execute("SELECT 1 FROM saved_replies LIMIT 1").fetchone():
        return
    for i, (t, b) in enumerate(DEFAULT_REPLIES):
        conn.execute("INSERT INTO saved_replies (title, body, position, created_at) VALUES (?, ?, ?, ?)",
                     (t, b, i, utcnow()))


def purge_client(conn, user_id: int) -> None:
    for table in ("credit_scores", "dispute_items"):
        conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM reminder_log WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------

def score_band(score: int) -> tuple[str, str]:
    if score >= 800:
        return "Exceptional", "good"
    if score >= 740:
        return "Very good", "good"
    if score >= 670:
        return "Good", "good"
    if score >= 580:
        return "Fair", "fair"
    return "Poor", "poor"


def score_summary(user_id: int, conn=None) -> dict | None:
    """Latest score per bureau, the change since the first one, and a chart."""
    conn = conn or get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM credit_scores WHERE user_id = ? ORDER BY as_of, id", (user_id,)).fetchall()]
    if not rows:
        return None
    by = {k: [r for r in rows if r["bureau"] == k] for k, _ in BUREAUS}
    cards = []
    for k, label in BUREAUS:
        rs = by[k]
        if not rs:
            cards.append({"key": k, "label": label, "score": None})
            continue
        first, last = rs[0], rs[-1]
        band, tone = score_band(last["score"])
        cards.append({"key": k, "label": label, "score": last["score"], "as_of": last["as_of"],
                      "change": last["score"] - first["score"], "since": first["as_of"],
                      "band": band, "tone": tone, "color": BUREAU_COLORS[k]})

    dates = sorted({r["as_of"] for r in rows})
    chart = None
    if len(dates) >= 2:
        W, H, PX, PY = 600, 200, 36, 18
        vals = [r["score"] for r in rows]
        lo = max(300, (min(vals) - 25) // 25 * 25)
        hi = min(850, (max(vals) + 35) // 25 * 25)
        if hi - lo < 50:
            hi = lo + 50
        def x(d):
            return PX + (W - 2 * PX) * dates.index(d) / (len(dates) - 1)
        def y(v):
            return PY + (H - 2 * PY) * (1 - (v - lo) / (hi - lo))
        series = []
        for k, label in BUREAUS:
            pts = [(round(x(r["as_of"]), 1), round(y(r["score"]), 1), r["score"]) for r in by[k]]
            if pts:
                series.append({"key": k, "label": label, "color": BUREAU_COLORS[k],
                               "points": " ".join(f"{a},{b}" for a, b, _ in pts), "dots": pts})
        grid = [{"y": round(y(v), 1), "v": v} for v in range(lo, hi + 1, max(25, (hi - lo) // 4 // 25 * 25))]
        labels = [{"x": round(x(d), 1), "text": _short_date(d)} for d in dates]
        chart = {"w": W, "h": H, "series": series, "grid": grid, "labels": labels}
    return {"cards": cards, "chart": chart, "rows": rows[::-1],
            "total_change": sum(c["change"] for c in cards if c.get("score") is not None)}


def _short_date(iso_day: str) -> str:
    try:
        d = datetime.strptime(iso_day, "%Y-%m-%d")
        return d.strftime("%b %-d") if d.year == _now().year else d.strftime("%b %Y")
    except ValueError:
        return iso_day


@bp.post("/admin/clients/<int:user_id>/scores")
@require_staff
def add_scores(user_id: int):
    client = _visible_client(user_id)
    as_of = (request.form.get("as_of") or "").strip()
    back = redirect(url_for("admin_client_detail", user_id=user_id) + "#scores")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
        flash("Pick the date of the report the scores came from.", "error")
        return back
    got = []
    for k, label in BUREAUS:
        raw = (request.form.get(k) or "").strip()
        if not raw:
            continue
        if not raw.isdigit() or not 300 <= int(raw) <= 850:
            flash(f"{label}: a score is a number from 300 to 850.", "error")
            return back
        got.append((k, label, int(raw)))
    if not got:
        flash("Enter at least one score.", "error")
        return back
    conn = get_db()
    me = current_user()
    first = not conn.execute("SELECT 1 FROM credit_scores WHERE user_id = ?", (user_id,)).fetchone()
    for k, _, v in got:
        conn.execute("INSERT INTO credit_scores (user_id, bureau, score, as_of, created_at, created_by) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (user_id, k, v, as_of, utcnow(), me["id"]))
    text = ", ".join(f"{label} {v}" for _, label, v in got)
    add_event(conn, user_id, title="Your starting scores" if first else "New credit scores",
              body=f"{text} (report dated {_short_date(as_of)}).", created_by=me["email"])
    audit("scores.added", actor=me, target_user_id=user_id, detail=f"{as_of}: {text}", conn=conn)
    conn.commit()
    notify.push_to_user(user_id, "Your credit scores were updated", text, "/portal", conn=conn)
    flash("Scores saved. The client sees them on their Home screen.", "success")
    return back


@bp.post("/admin/scores/<int:score_id>/delete")
@require_staff
def delete_score(score_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM credit_scores WHERE id = ?", (score_id,)).fetchone()
    if not row:
        abort(404)
    _visible_client(row["user_id"])
    conn.execute("DELETE FROM credit_scores WHERE id = ?", (score_id,))
    audit("scores.deleted", actor=current_user(), target_user_id=row["user_id"],
          detail=f"{row['bureau']} {row['score']} {row['as_of']}", conn=conn)
    conn.commit()
    flash("Score removed.", "success")
    return redirect(url_for("admin_client_detail", user_id=row["user_id"]) + "#scores")


# ---------------------------------------------------------------------------
# Dispute items
# ---------------------------------------------------------------------------

def dispute_summary(user_id: int, conn=None) -> dict | None:
    conn = conn or get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM dispute_items WHERE user_id = ? ORDER BY "
        "CASE status WHEN 'in_review' THEN 0 WHEN 'disputed' THEN 1 WHEN 'verified' THEN 2 ELSE 3 END, "
        "creditor, bureau", (user_id,)).fetchall()]
    if not rows:
        return None
    for r in rows:
        r["st"] = STATUS.get(r["status"], STATUS["disputed"])
        r["bureau_label"] = BUREAU_LABELS.get(r["bureau"], r["bureau"])
        r["type_label"] = ITEM_TYPE_LABELS.get(r["item_type"], "Other")
    removed = sum(1 for r in rows if r["status"] in ("deleted", "updated"))
    open_ = sum(1 for r in rows if r["status"] in ("disputed", "in_review"))
    return {"items": rows, "total": len(rows), "removed": removed, "open": open_,
            "percent": int(round(100 * removed / len(rows)))}


@bp.post("/admin/clients/<int:user_id>/disputes")
@require_staff
def add_disputes(user_id: int):
    _visible_client(user_id)
    back = redirect(url_for("admin_client_detail", user_id=user_id) + "#disputes")
    creditor = (request.form.get("creditor") or "").strip()[:80]
    ref = re.sub(r"\D", "", request.form.get("account_ref") or "")[-4:]
    item_type = request.form.get("item_type") or "other"
    bureaus = [b for b in request.form.getlist("bureaus") if b in BUREAU_LABELS]
    note = (request.form.get("note") or "").strip()[:300]
    try:
        rnd = max(1, min(20, int(request.form.get("round") or 1)))
    except ValueError:
        rnd = 1
    if not creditor:
        flash("Name the creditor or collector.", "error")
        return back
    if not bureaus:
        flash("Tick at least one bureau it was disputed with.", "error")
        return back
    if item_type not in ITEM_TYPE_LABELS:
        item_type = "other"
    conn = get_db()
    me = current_user()
    now = utcnow()
    for b in bureaus:
        conn.execute("INSERT INTO dispute_items (user_id, creditor, account_ref, item_type, bureau, status, "
                     "round, note, created_at, status_at, created_by) VALUES (?, ?, ?, ?, ?, 'disputed', ?, ?, ?, ?, ?)",
                     (user_id, creditor, ref, item_type, b, rnd, note, now, now, me["id"]))
    names = ", ".join(BUREAU_LABELS[b] for b in bureaus)
    add_event(conn, user_id, title=f"Disputed: {creditor}",
              body=f"{ITEM_TYPE_LABELS[item_type]} disputed with {names} (round {rnd}).",
              created_by=me["email"])
    audit("disputes.added", actor=me, target_user_id=user_id, detail=f"{creditor} {ref}: {names}", conn=conn)
    conn.commit()
    flash(f"Added {creditor} for {names}.", "success")
    return back


@bp.post("/admin/disputes/<int:item_id>/status")
@require_staff
def set_dispute_status(item_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM dispute_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        abort(404)
    client = _visible_client(row["user_id"])
    status = request.form.get("status") or ""
    if status not in STATUS:
        abort(400)
    note = (request.form.get("note") or "").strip()[:300]
    back = redirect(url_for("admin_client_detail", user_id=row["user_id"]) + "#disputes")
    if status == row["status"] and note == row["note"]:
        return back
    now = utcnow()
    resolved = now if STATUS[status]["resolved"] else None
    conn.execute("UPDATE dispute_items SET status = ?, note = ?, status_at = CASE WHEN status = ? "
                 "THEN status_at ELSE ? END, resolved_at = ? WHERE id = ?",
                 (status, note, status, now, resolved, item_id))
    me = current_user()
    if status != row["status"]:
        bureau = BUREAU_LABELS.get(row["bureau"], row["bureau"])
        titles = {
            "deleted": f"Removed from your {bureau} report: {row['creditor']}",
            "updated": f"Corrected on your {bureau} report: {row['creditor']}",
            "verified": f"{bureau} kept {row['creditor']}. We'll challenge it again.",
            "in_review": f"{bureau} responded about {row['creditor']}",
            "disputed": f"Disputed again with {bureau}: {row['creditor']}",
        }
        add_event(conn, row["user_id"], title=titles[status], body=note, created_by=me["email"])
        if status == "disputed":
            conn.execute("UPDATE dispute_items SET round = round + 1 WHERE id = ? AND ? IN ('verified')",
                         (item_id, row["status"]))
    audit("disputes.status", actor=me, target_user_id=row["user_id"],
          detail=f"item {item_id} {row['status']} -> {status}", conn=conn)
    conn.commit()
    if status in ("deleted", "updated"):
        notify.push_to_user(row["user_id"], "Good news on your credit report",
                            f"{row['creditor']} was {'removed from' if status == 'deleted' else 'corrected on'} "
                            f"your {BUREAU_LABELS.get(row['bureau'])} report.", "/portal", conn=conn)
        notify.text_client(client, f"Good news: {row['creditor']} was "
                                   f"{'removed from' if status == 'deleted' else 'corrected on'} your "
                                   f"{BUREAU_LABELS.get(row['bureau'])} report.")
    elif status != row["status"]:
        notify.push_to_user(row["user_id"], "Update on your disputes",
                            f"{row['creditor']} ({BUREAU_LABELS.get(row['bureau'])}): "
                            f"{STATUS[status]['client']}", "/portal", conn=conn)
    flash("Item updated.", "success")
    return back


@bp.post("/admin/disputes/<int:item_id>/delete")
@require_staff
def delete_dispute(item_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM dispute_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        abort(404)
    _visible_client(row["user_id"])
    conn.execute("DELETE FROM dispute_items WHERE id = ?", (item_id,))
    audit("disputes.removed", actor=current_user(), target_user_id=row["user_id"],
          detail=f"{row['creditor']} {row['bureau']}", conn=conn)
    conn.commit()
    flash("Item removed from the tracker.", "success")
    return redirect(url_for("admin_client_detail", user_id=row["user_id"]) + "#disputes")


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------

def followups(conn=None, specialist_id: int | None = None) -> dict:
    """Files that are waiting on the team, by reason. Each list holds client
    rows with a `why` line. A specialist sees only their own files."""
    conn = conn or get_db()
    scope = "u.role = 'client' AND u.archived_at IS NULL"
    params: tuple = ()
    if specialist_id:
        scope += " AND u.assigned_to = ?"
        params = (specialist_id,)
    req = ",".join("?" * len(REQUIRED_DOCUMENT_KEYS))

    ready = [dict(r) for r in conn.execute(
        f"SELECT u.id, u.first_name, u.last_name, u.email FROM users u WHERE {scope} "
        f"AND (SELECT COUNT(DISTINCT d.doc_type) FROM documents d WHERE d.user_id = u.id "
        f"     AND d.status != 'rejected' AND d.doc_type IN ({req})) = ? "
        f"AND NOT EXISTS (SELECT 1 FROM letter_packets p WHERE p.client_id = u.id AND p.status != 'void') "
        f"AND u.case_stage NOT IN ('complete') ORDER BY u.id",
        params + tuple(REQUIRED_DOCUMENT_KEYS) + (len(REQUIRED_DOCUMENT_KEYS),)).fetchall()]
    for r in ready:
        r["why"] = "All documents in, no letters drafted yet"
        r["href"] = url_for("letters.staff_new", user_id=r["id"])
        r["cta"] = "Draft letters"

    unsigned = [dict(r) for r in conn.execute(
        f"SELECT u.id, u.first_name, u.last_name, u.email, p.id AS pid, p.title, p.created_at, "
        f"p.first_opened_at, p.open_count FROM letter_packets p JOIN users u ON u.id = p.client_id "
        f"WHERE {scope} AND p.status = 'pending' AND p.created_at <= ? ORDER BY p.created_at",
        params + (_ago(3),)).fetchall()]
    for r in unsigned:
        r["why"] = (f"“{r['title']}” unsigned for 3+ days, "
                    + (f"opened {r['open_count']}×" if r["first_opened_at"] else "never opened"))
        r["href"] = url_for("letters.staff_view", user_id=r["id"], packet_id=r["pid"])
        r["cta"] = "View packet"

    to_mail = [dict(r) for r in conn.execute(
        f"SELECT u.id, u.first_name, u.last_name, u.email, p.id AS pid, p.title, p.signed_at, "
        f"(SELECT COUNT(*) FROM letter_items i WHERE i.packet_id = p.id AND i.mailed_at IS NULL) AS left_n "
        f"FROM letter_packets p JOIN users u ON u.id = p.client_id WHERE {scope} AND p.status = 'signed' "
        f"AND EXISTS (SELECT 1 FROM letter_items i WHERE i.packet_id = p.id AND i.mailed_at IS NULL) "
        f"ORDER BY p.signed_at", params).fetchall()]
    for r in to_mail:
        r["why"] = f"Signed, {r['left_n']} letter{'s' if r['left_n'] != 1 else ''} not marked mailed"
        r["href"] = url_for("letters.staff_view", user_id=r["id"], packet_id=r["pid"]) + "#mailing"
        r["cta"] = "Add tracking"

    overdue = [dict(r) for r in conn.execute(
        f"SELECT u.id, u.first_name, u.last_name, u.email, COUNT(*) AS n, MIN(i.status_at) AS since "
        f"FROM dispute_items i JOIN users u ON u.id = i.user_id WHERE {scope} AND i.status = 'disputed' "
        f"AND i.status_at <= ? GROUP BY u.id ORDER BY since", params + (_ago(35),)).fetchall()]
    for r in overdue:
        r["why"] = f"{r['n']} disputed item{'s' if r['n'] != 1 else ''} past the bureau's 30 days"
        r["href"] = url_for("admin_client_detail", user_id=r["id"]) + "#disputes"
        r["cta"] = "Update items"

    groups = [
        ("ready", "Ready for letters", "Documents complete but nothing drafted.", ready),
        ("unsigned", "Letters not signed", "Sent three or more days ago.", unsigned),
        ("to_mail", "Signed, not mailed", "Print, mail certified, and add the tracking number.", to_mail),
        ("overdue", "Bureau responses overdue", "Disputed more than 35 days ago with no result logged.", overdue),
    ]
    return {"groups": groups, "total": sum(len(g[3]) for g in groups),
            "counts": {k: len(v) for k, _, _, v in groups}}


@bp.get("/admin/follow-ups")
@require_staff
def followups_page():
    me = current_user()
    data = followups(specialist_id=int(me["id"]) if is_specialist() else None)
    return render_template("admin/followups.html", data=data)


# ---------------------------------------------------------------------------
# Saved replies
# ---------------------------------------------------------------------------

def saved_replies(conn=None) -> list[dict]:
    conn = conn or get_db()
    return [dict(r) for r in conn.execute("SELECT * FROM saved_replies ORDER BY position, id").fetchall()]


def fill_reply(body: str, client: dict) -> str:
    return body.replace("{first_name}", (client.get("first_name") or "there").strip())


@bp.route("/admin/replies", methods=["GET", "POST"])
@require_admin
def replies_page():
    conn = get_db()
    if request.method == "POST":
        rid = request.form.get("id")
        title = (request.form.get("title") or "").strip()[:80]
        body = (request.form.get("body") or "").strip()[:2000]
        if not title or not body:
            flash("A saved reply needs a name and the message.", "error")
        elif rid and rid.isdigit():
            conn.execute("UPDATE saved_replies SET title = ?, body = ? WHERE id = ?", (title, body, int(rid)))
            conn.commit()
            flash("Saved.", "success")
        else:
            pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM saved_replies").fetchone()[0]
            conn.execute("INSERT INTO saved_replies (title, body, position, created_at, created_by) "
                         "VALUES (?, ?, ?, ?, ?)", (title, body, pos, utcnow(), current_user()["id"]))
            conn.commit()
            flash("Reply added. It's in the message box on every client file.", "success")
        return redirect(url_for("casework.replies_page"))
    return render_template("admin/replies.html", replies=saved_replies(conn))


@bp.post("/admin/replies/<int:reply_id>/delete")
@require_admin
def delete_reply(reply_id: int):
    conn = get_db()
    conn.execute("DELETE FROM saved_replies WHERE id = ?", (reply_id,))
    conn.commit()
    flash("Reply deleted.", "success")
    return redirect(url_for("casework.replies_page"))


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------

def _staff_name(user: dict) -> str:
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    if first and last:
        return f"{first} {last[0]}."
    return first or (user.get("email") or "").split("@")[0]


@bp.post("/admin/clients/bulk")
@require_admin
def bulk():
    ids = sorted({int(i) for i in request.form.getlist("ids") if i.isdigit()})
    action = request.form.get("action") or ""
    back = redirect(request.referrer or url_for("admin_clients"))
    if not ids:
        flash("Tick at least one client first.", "error")
        return back
    conn = get_db()
    me = current_user()
    clients = [dict(r) for r in conn.execute(
        f"SELECT * FROM users WHERE role = 'client' AND id IN ({','.join('?' * len(ids))})", ids).fetchall()]

    if action == "assign":
        raw = request.form.get("specialist_id") or ""
        person = None
        if raw not in ("", "none"):
            person = row_to_dict(conn.execute("SELECT * FROM users WHERE id = ? AND role = 'specialist'",
                                              (int(raw) if raw.isdigit() else 0,)).fetchone())
            if not person:
                flash("Pick a credit specialist.", "error")
                return back
        name = (f"{person['first_name']} {person['last_name']}".strip() or person["email"]) if person else ""
        for c in clients:
            if person:
                if c.get("assigned_to") == person["id"]:
                    continue
                conn.execute("UPDATE users SET assigned_to = ?, assigned_at = ? WHERE id = ?",
                             (person["id"], utcnow(), c["id"]))
                add_event(conn, c["id"], title="Your case has a specialist",
                          body=f"{name} is now working on your file. You can message them from your "
                               f"portal any time.", created_by=me["email"])
            else:
                conn.execute("UPDATE users SET assigned_to = NULL, assigned_at = NULL WHERE id = ?", (c["id"],))
            audit("client.assigned" if person else "client.unassigned", actor=me, target_user_id=c["id"],
                  detail=f"bulk; {person['email'] if person else ''}", conn=conn)
        conn.commit()
        if person:
            for c in clients:
                mailer.send_case_assigned(person, c)
        flash(f"{len(clients)} client{'s' if len(clients) != 1 else ''} "
              f"{'assigned to ' + name if person else 'unassigned'}.", "success")

    elif action == "stage":
        stage = request.form.get("stage") or ""
        if stage not in CASE_STAGE_KEYS:
            flash("Pick a stage.", "error")
            return back
        for c in clients:
            if c["case_stage"] == stage:
                continue
            conn.execute("UPDATE users SET case_stage = ? WHERE id = ?", (stage, c["id"]))
            add_event(conn, c["id"], title=f"Case moved to {CASE_STAGE_LABELS[stage]}", stage=stage,
                      created_by=me["email"])
            audit("admin.stage_changed", actor=me, target_user_id=c["id"], detail=f"bulk; {stage}", conn=conn)
        conn.commit()
        flash(f"Moved {len(clients)} client{'s' if len(clients) != 1 else ''} to "
              f"{CASE_STAGE_LABELS[stage]}.", "success")

    elif action == "message":
        body = (request.form.get("body") or "").strip()[:4000]
        if not body:
            flash("Write the message first.", "error")
            return back
        for c in clients:
            text = fill_reply(body, c)
            post_message(conn, c["id"], text, sender_id=int(me["id"]), sender_role=me["role"],
                         sender_name=_staff_name(me))
            audit("message.sent_to_client", actor=me, target_user_id=c["id"],
                  detail=f"bulk; {len(text)} chars", conn=conn)
        conn.commit()
        for c in clients:
            mailer.send_message_to_client(c, _staff_name(me))
            notify.push_to_user(c["id"], f"New message from {_staff_name(me)}",
                                fill_reply(body, c)[:120], "/portal/messages", conn=conn)
        flash(f"Message sent to {len(clients)} client{'s' if len(clients) != 1 else ''}.", "success")
    else:
        flash("Pick what to do with the selected clients.", "error")
    return back


# ---------------------------------------------------------------------------
# Client referrals
# ---------------------------------------------------------------------------

REFERRAL_AFTER_DAYS = 30


def referral_for(client: dict, paid_at: str | None, disputes: dict | None, conn=None) -> dict | None:
    """The refer-a-friend card, once the client has reason to recommend us:
    thirty days in, or their first item removed. Gives them a link code the
    first time it is shown."""
    import affiliates
    conn = conn or get_db()
    eligible = bool(disputes and disputes["removed"]) or bool(paid_at and paid_at <= _ago(REFERRAL_AFTER_DAYS))
    if not eligible:
        return None
    code = client.get("affiliate_code")
    if not code:
        code = affiliates._new_code(conn, client.get("first_name") or "friend")
        conn.execute("UPDATE users SET affiliate_code = ? WHERE id = ? AND affiliate_code IS NULL",
                     (code, client["id"]))
        conn.commit()
        code = conn.execute("SELECT affiliate_code FROM users WHERE id = ?", (client["id"],)).fetchone()[0]
    referred = conn.execute("SELECT COUNT(*) FROM leads WHERE affiliate_id = ?", (client["id"],)).fetchone()[0]
    return {"link": affiliates.referral_link(code), "referred": referred}
