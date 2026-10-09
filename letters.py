"""
Letters: dispute letters a client reads and e-signs inside the portal.

Staff paste the final letters for a client into a packet. The text is locked
by a SHA-256 hash the moment the packet exists. The client reads exactly that
text, consents to signing electronically, types their name and draws their
signature once. The system then stamps the signature and the signing date on
each letter as its own PDF and keeps an audit record (time, IP, browser, the
consent wording, the hash). Staff download the PDFs and mail them.

Everything rides on the app's existing machinery: session auth and roles from
auth.py, the global CSRF gate, the audit log and case timeline, and the same
upload volume as client documents, so nothing here needs its own setup.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, send_file, url_for)
from markupsafe import Markup, escape

import config
import mailer
from auth import current_user, is_admin, is_specialist, require_login, require_staff
from db import add_event, get_db, utcnow
from security import audit, client_ip

try:  # the business day, not UTC, decides what date goes on a letter
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(os.environ.get("LETTER_TIMEZONE", "America/Detroit"))
except Exception:  # pragma: no cover
    _TZ = None

bp = Blueprint("letters", __name__)

CONSENT_TEXT = (
    "I agree to sign these letters electronically. I understand my electronic "
    "signature has the same effect as a handwritten one, and that I can ask "
    "for paper copies to sign by hand instead."
)
APPROVE_TEXT = (
    "I have read every letter on this page. The facts stated in them are true "
    "to the best of my knowledge, and I authorize Velosify Credit to send "
    "them on my behalf."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS letter_packets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title        TEXT    NOT NULL,
    content_hash TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',   -- pending | signed | void
    created_at   TEXT    NOT NULL,
    created_by   INTEGER,
    signed_at    TEXT,
    voided_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_letter_packets_client ON letter_packets(client_id, status);

CREATE TABLE IF NOT EXISTS letter_items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id INTEGER NOT NULL REFERENCES letter_packets(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    title     TEXT    NOT NULL,
    body      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_letter_items_packet ON letter_items(packet_id, position);

CREATE TABLE IF NOT EXISTS letter_signatures (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id      INTEGER NOT NULL UNIQUE REFERENCES letter_packets(id) ON DELETE CASCADE,
    client_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    signer_name    TEXT    NOT NULL,
    signed_at      TEXT    NOT NULL,
    ip             TEXT    NOT NULL DEFAULT '',
    user_agent     TEXT    NOT NULL DEFAULT '',
    consent_text   TEXT    NOT NULL,
    approve_text   TEXT    NOT NULL,
    content_hash   TEXT    NOT NULL,
    signature_file TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS letter_files (
    id            TEXT PRIMARY KEY,
    packet_id     INTEGER NOT NULL REFERENCES letter_packets(id) ON DELETE CASCADE,
    client_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind          TEXT    NOT NULL,                    -- letter | record
    label         TEXT    NOT NULL,
    download_name TEXT    NOT NULL,
    stored_name   TEXT    NOT NULL
);
"""

# [DATE] is filled in at signing. Any other [bracket] means the letter is not
# finished, and the packet is refused rather than sent to a client half-done.
_OPEN_PLACEHOLDER = re.compile(r"\[(?!DATE\])[^\]\n]+\]")
_SIG_LINE = re.compile(r"^\s*_{8,}\s*$")
_NUM_ITEM = re.compile(r"^(\d+\.)\s+(.*)$")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _dir(sub: str) -> Path:
    path = Path(config.UPLOAD_DIR) / "letters" / sub
    path.mkdir(parents=True, exist_ok=True)
    return path


def _local(dt: datetime) -> datetime:
    return dt.astimezone(_TZ) if _TZ else dt


def _long_date(dt: datetime) -> str:
    dt = _local(dt)
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


def _hash(pairs) -> str:
    blob = json.dumps([[t, b] for t, b in pairs], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "letter"


def parse_letters(text: str):
    """Split pasted text into letters on a line of ===. A first line that
    starts with # becomes that letter's title."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for block in re.split(r"(?m)^\s*={3,}\s*$", text):
        block = block.strip("\n")
        if not block.strip():
            continue
        lines = block.split("\n")
        title = ""
        if lines[0].lstrip().startswith("#"):
            title = lines[0].lstrip("# ").strip()
            block = "\n".join(lines[1:]).strip("\n")
        out.append((title or f"Letter {len(out) + 1}", block))
    return out


def _client_row(user_id: int):
    row = get_db().execute(
        "SELECT * FROM users WHERE id = ? AND role = 'client'", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def _staff_can_see(client_id: int) -> bool:
    """The same rule as the rest of the app: an admin sees every file, a
    specialist only the clients assigned to them."""
    if is_admin():
        return True
    me = current_user()
    if not me or me["role"] != "specialist":
        return False
    row = get_db().execute("SELECT assigned_to FROM users WHERE id = ?",
                           (client_id,)).fetchone()
    return bool(row and row["assigned_to"] == int(me["id"]))


def _visible_client(user_id: int) -> dict:
    row = _client_row(user_id)
    if not row or not _staff_can_see(user_id):
        abort(404)
    return row


def _me_client() -> dict:
    me = current_user()
    if not me or me["role"] != "client":
        abort(404)
    return me


def pending_count(user_id: int) -> int:
    return get_db().execute(
        "SELECT COUNT(*) AS n FROM letter_packets WHERE client_id = ? AND status = 'pending'",
        (user_id,)).fetchone()["n"]


def purge_client(conn, user_id: int) -> int:
    """Remove a client's signed letters from disk and the database. Called by
    the delete-client route, so nothing signed is left on the volume after
    the person it belongs to has been deleted. Returns the file count."""
    n = 0
    base = Path(config.UPLOAD_DIR) / "letters"
    for sub, table, col in (("signed", "letter_files", "stored_name"),
                            ("signatures", "letter_signatures", "signature_file")):
        for r in conn.execute(f"SELECT {col} AS f FROM {table} WHERE client_id = ?",
                              (user_id,)).fetchall():
            try:
                (base / sub / r["f"]).unlink(missing_ok=True)
                n += 1
            except OSError:
                pass
    for t in ("letter_files", "letter_signatures"):
        conn.execute(f"DELETE FROM {t} WHERE client_id = ?", (user_id,))
    conn.execute("DELETE FROM letter_items WHERE packet_id IN "
                 "(SELECT id FROM letter_packets WHERE client_id = ?)", (user_id,))
    conn.execute("DELETE FROM letter_packets WHERE client_id = ?", (user_id,))
    return n


# --------------------------------------------------------------------------
# letter text -> blocks (one parser for the page and the PDF)
# --------------------------------------------------------------------------
def _blocks(body: str):
    buf = []
    for line in body.split("\n"):
        kind = None
        if _SIG_LINE.match(line):
            kind = "sig"
        elif line.startswith("- "):
            kind = "bullet"
        elif _NUM_ITEM.match(line):
            kind = "num"
        elif not line.strip():
            kind = "blank"
        if kind is None:
            buf.append(line)
            continue
        if buf:
            yield ("p", buf)
            buf = []
        if kind == "sig":
            yield ("sig",)
        elif kind == "bullet":
            yield ("li", "•", line[2:])
        elif kind == "num":
            m = _NUM_ITEM.match(line)
            yield ("li", m.group(1), m.group(2))
    if buf:
        yield ("p", buf)


def _inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", str(escape(text)))


@bp.app_template_filter("letter_html")
def letter_html(body: str, date_text: str = "[date of signing]") -> Markup:
    body = body.replace("[DATE]", date_text)
    out, has_sig = [], False
    for b in _blocks(body):
        if b[0] == "p":
            out.append("<p>" + "<br>".join(_inline(x) for x in b[1]) + "</p>")
        elif b[0] == "li":
            out.append(f'<p class="letter-li"><span>{escape(b[1])}</span>{_inline(b[2])}</p>')
        else:
            has_sig = True
            out.append('<p class="letter-sigline"><em>Your signature goes here</em></p>')
    if not has_sig:
        out.append('<p class="letter-sigline"><em>Your signature goes here</em></p>')
    return Markup("\n".join(out))


@bp.app_context_processor
def _nav_badge():
    """Number of letter packets waiting on the signed-in client. Feeds the
    badge on the Letters link; staff and anonymous visitors get zero."""
    try:
        user = current_user()
        if user and user["role"] == "client":
            return {"letters_pending": pending_count(int(user["id"]))}
    except Exception:  # pragma: no cover - a badge must never break a page
        pass
    return {"letters_pending": 0}


# --------------------------------------------------------------------------
# signature image + PDFs
# --------------------------------------------------------------------------
def decode_signature(data_url: str) -> bytes:
    """PNG bytes from the canvas, or ValueError with a message fit to show."""
    again = "The signature could not be read. Please draw it again."
    prefix = "data:image/png;base64,"
    if not data_url or not data_url.startswith(prefix):
        raise ValueError("Draw your signature in the box.")
    try:
        raw = base64.b64decode(data_url[len(prefix):], validate=True)
    except Exception:
        raise ValueError(again)
    if len(raw) > 600_000:
        raise ValueError("The signature image is too large. Please draw it again.")
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise ValueError(again)
    if img.format != "PNG" or img.width > 3000 or img.height > 1500:
        raise ValueError(again)
    if "A" in img.getbands():
        inked = sum(1 for a in img.getchannel("A").getdata() if a > 40)
    else:
        inked = sum(1 for v in img.convert("L").getdata() if v < 200)
    if inked < 150:
        raise ValueError("Your signature looks blank. Please sign in the box.")
    return raw


def _letter_pdf(title, body, sig_png, signer, signed_at, ref) -> bytes:
    from PIL import Image as PILImage
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer

    body = body.replace("[DATE]", _long_date(signed_at))
    base = ParagraphStyle("b", fontName="Times-Roman", fontSize=11, leading=14.5, spaceAfter=7)
    item = ParagraphStyle("i", parent=base, leftIndent=18, bulletIndent=4, spaceAfter=3)
    story, placed = [], False

    w, h = PILImage.open(io.BytesIO(sig_png)).size
    scale = min(2.6 * inch / w, 0.9 * inch / h)

    def signature():
        story.append(Spacer(1, 6))
        story.append(Image(io.BytesIO(sig_png), width=w * scale, height=h * scale,
                           hAlign="LEFT", mask="auto"))
        story.append(Paragraph("_" * 32, base))

    for b in _blocks(body):
        if b[0] == "p":
            story.append(Paragraph("<br/>".join(_inline(x) for x in b[1]), base))
        elif b[0] == "li":
            story.append(Paragraph(_inline(b[2]), item, bulletText=b[1]))
        else:
            signature()
            placed = True
    if not placed:
        signature()

    foot = f"Electronically signed by {signer} on {_long_date(signed_at)}  |  Ref {ref}"

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillGray(0.45)
        canvas.drawString(0.9 * inch, 0.5 * inch, foot)
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                      topMargin=0.8 * inch, bottomMargin=0.9 * inch, title=title,
                      author=signer).build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _record_pdf(packet, sig, titles) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    h = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=15, spaceAfter=10)
    s = ParagraphStyle("s", fontName="Helvetica", fontSize=9.5, leading=13, spaceAfter=4)
    k = ParagraphStyle("k", parent=s, fontName="Helvetica-Bold")
    rows = [
        ("Packet", f"#{packet['id']} - {packet['title']}"),
        ("Signer", sig["signer_name"]),
        ("Signed (UTC)", sig["signed_at"]),
        ("IP address", sig["ip"] or "unknown"),
        ("Browser", sig["user_agent"] or "unknown"),
        ("Content hash (SHA-256)", sig["content_hash"]),
        ("Letters signed", "; ".join(titles)),
    ]
    story = [Paragraph("Electronic signature record", h)]
    for a, b in rows:
        story += [Paragraph(a, k), Paragraph(str(escape(b)), s)]
    story += [Spacer(1, 8), Paragraph("Consent given", k),
              Paragraph(str(escape(sig["consent_text"])), s), Spacer(1, 4),
              Paragraph("Approval given", k), Paragraph(str(escape(sig["approve_text"])), s)]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=letter, leftMargin=inch, rightMargin=inch,
                      topMargin=inch, bottomMargin=inch,
                      title="Electronic signature record").build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------
# staff routes
# --------------------------------------------------------------------------
@bp.get("/admin/clients/<int:user_id>/letters")
@require_staff
def staff_list(user_id: int):
    client = _visible_client(user_id)
    packets = [dict(r) for r in get_db().execute(
        "SELECT * FROM letter_packets WHERE client_id = ? ORDER BY id DESC",
        (user_id,)).fetchall()]
    return render_template("admin/letters.html", client=client, packets=packets)


DEFAULT_PACKET_TITLE = "Credit report dispute letters"


@bp.route("/admin/clients/<int:user_id>/letters/new", methods=["GET", "POST"])
@require_staff
def staff_new(user_id: int):
    client = _visible_client(user_id)
    form = {"title": DEFAULT_PACKET_TITLE, "letters": ""}
    errors = []
    if request.method == "POST":
        form["title"] = (request.form.get("title") or "").strip() or DEFAULT_PACKET_TITLE
        form["letters"] = request.form.get("letters") or ""
        pairs = parse_letters(form["letters"])
        if not form["title"]:
            errors.append("Give the packet a title the client will recognize.")
        if not pairs:
            errors.append("Paste at least one letter. Separate letters with a line of ===.")
        for t, b in pairs:
            left = sorted(set(_OPEN_PLACEHOLDER.findall(b)))
            if left:
                errors.append(f"“{t}” still has unfinished placeholders: "
                              + ", ".join(left))
        if not errors:
            conn = get_db()
            me = current_user()
            cur = conn.execute(
                "INSERT INTO letter_packets (client_id, title, content_hash, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, form["title"], _hash(pairs), utcnow(), me["id"]))
            pid = cur.lastrowid
            for i, (t, b) in enumerate(pairs):
                conn.execute("INSERT INTO letter_items (packet_id, position, title, body) "
                             "VALUES (?, ?, ?, ?)", (pid, i, t, b))
            add_event(conn, user_id, title="Letters ready for your signature",
                      body=f"{form['title']}: {len(pairs)} letter(s) are waiting in "
                           "the Letters tab. Read them and sign to approve.",
                      created_by="staff")
            audit("letters.created", actor=me, target_user_id=user_id,
                  detail=f"packet {pid}: {form['title']} ({len(pairs)} letters)", conn=conn)
            conn.commit()
            try:
                mailer.send_email(
                    client["email"], "Letters ready for your signature",
                    f"Hi {client['first_name'] or 'there'},\n\n"
                    f"We've prepared letters on your behalf and they need your "
                    f"signature before we can send them. Sign in, open the Letters "
                    f"tab, read each one, and sign once:\n\n"
                    f"{config.APP_BASE_URL.rstrip('/')}{url_for('letters.portal_list')}\n\n"
                    f"If anything in them is wrong, don't sign. Message us and "
                    f"we'll fix it first.\n\nVelosify Credit")
            except Exception:  # an email failure must not undo the packet
                pass
            flash("Packet created and the client has been emailed.", "success")
            return redirect(url_for("letters.staff_view", user_id=user_id, packet_id=pid))
    return render_template("admin/letters_new.html", client=client, form=form, errors=errors)


@bp.route("/admin/clients/<int:user_id>/letters/<int:packet_id>", methods=["GET", "POST"])
@require_staff
def staff_view(user_id: int, packet_id: int):
    client = _visible_client(user_id)
    conn = get_db()
    row = conn.execute("SELECT * FROM letter_packets WHERE id = ? AND client_id = ?",
                       (packet_id, user_id)).fetchone()
    if not row:
        abort(404)
    packet = dict(row)
    if request.method == "POST":
        if request.form.get("action") == "void" and packet["status"] == "pending":
            conn.execute("UPDATE letter_packets SET status = 'void', voided_at = ? "
                         "WHERE id = ? AND status = 'pending'", (utcnow(), packet_id))
            audit("letters.voided", actor=current_user(), target_user_id=user_id,
                  detail=f"packet {packet_id}: {packet['title']}", conn=conn)
            conn.commit()
            flash("Packet voided. Create a new one to send revised letters.", "success")
        return redirect(url_for("letters.staff_view", user_id=user_id, packet_id=packet_id))
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM letter_items WHERE packet_id = ? ORDER BY position", (packet_id,)).fetchall()]
    sig = conn.execute("SELECT * FROM letter_signatures WHERE packet_id = ?", (packet_id,)).fetchone()
    files = [dict(r) for r in conn.execute(
        "SELECT * FROM letter_files WHERE packet_id = ? ORDER BY kind DESC, rowid", (packet_id,)).fetchall()]
    return render_template("admin/letters_view.html", client=client, packet=packet, items=items,
                           sig=dict(sig) if sig else None, files=files)


# --------------------------------------------------------------------------
# client routes
# --------------------------------------------------------------------------
@bp.get("/portal/letters")
@require_login
def portal_list():
    user = _me_client()
    packets = [dict(r) for r in get_db().execute(
        "SELECT * FROM letter_packets WHERE client_id = ? AND status != 'void' ORDER BY id DESC",
        (user["id"],)).fetchall()]
    return render_template("portal/letters.html", packets=packets)


@bp.route("/portal/letters/<int:packet_id>", methods=["GET", "POST"])
@require_login
def portal_sign(packet_id: int):
    user = _me_client()
    conn = get_db()
    row = conn.execute("SELECT * FROM letter_packets WHERE id = ? AND client_id = ? "
                       "AND status != 'void'", (packet_id, user["id"])).fetchone()
    if not row:
        abort(404)
    packet = dict(row)
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM letter_items WHERE packet_id = ? ORDER BY position", (packet_id,)).fetchall()]
    now = datetime.now().astimezone()
    ctx = dict(packet=packet, items=items, consent=CONSENT_TEXT, approve=APPROVE_TEXT,
               today=_long_date(now), error=None, name="", files=[])

    if packet["status"] == "signed":
        ctx["files"] = [dict(r) for r in conn.execute(
            "SELECT * FROM letter_files WHERE packet_id = ? AND kind = 'letter' ORDER BY rowid",
            (packet_id,)).fetchall()]
        return render_template("portal/letters_sign.html", **ctx)

    if request.method == "POST":
        ctx["name"] = (request.form.get("signer_name") or "").strip()
        pairs = [(i["title"], i["body"]) for i in items]
        try:
            if (request.form.get("content_hash") != packet["content_hash"]
                    or _hash(pairs) != packet["content_hash"]):
                raise ValueError("These letters changed after you opened the page. "
                                 "Reload to read them again.")
            if not request.form.get("consent") or not request.form.get("approve"):
                raise ValueError("Check both boxes to continue.")
            if len(ctx["name"]) < 3 or " " not in ctx["name"]:
                raise ValueError("Type your full legal name.")
            sig_png = decode_signature(request.form.get("signature", ""))
        except ValueError as e:
            ctx["error"] = str(e)
            return render_template("portal/letters_sign.html", **ctx), 400

        signed_iso = utcnow()
        sig_name = secrets.token_hex(16) + ".png"
        written = []
        try:
            claim = conn.execute("UPDATE letter_packets SET status = 'signed', signed_at = ? "
                                 "WHERE id = ? AND status = 'pending'", (signed_iso, packet_id))
            if claim.rowcount != 1:      # someone signed or voided it a moment ago
                conn.rollback()
                flash("These letters were already signed.", "error")
                return redirect(url_for("letters.portal_sign", packet_id=packet_id))
            sig_path = _dir("signatures") / sig_name
            sig_path.write_bytes(sig_png)
            written.append(sig_path)
            sig = dict(signer_name=ctx["name"], signed_at=signed_iso, ip=client_ip(),
                       user_agent=(request.headers.get("User-Agent") or "")[:300],
                       consent_text=CONSENT_TEXT, approve_text=APPROVE_TEXT,
                       content_hash=packet["content_hash"])
            conn.execute(
                "INSERT INTO letter_signatures (packet_id, client_id, signer_name, signed_at, ip, "
                "user_agent, consent_text, approve_text, content_hash, signature_file) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (packet_id, user["id"], sig["signer_name"], signed_iso, sig["ip"], sig["user_agent"],
                 CONSENT_TEXT, APPROVE_TEXT, sig["content_hash"], sig_name))
            outputs = [("letter", t, f"{i + 1:02d}-{_slug(t)}-signed.pdf",
                        _letter_pdf(t, b, sig_png, ctx["name"], now, f"{packet_id}-{i + 1}"))
                       for i, (t, b) in enumerate(pairs)]
            outputs.append(("record", "Signature record", f"signature-record-{packet_id}.pdf",
                            _record_pdf(packet, sig, [t for t, _ in pairs])))
            for kind, label, dl, data in outputs:
                stored = secrets.token_hex(16) + ".pdf"
                path = _dir("signed") / stored
                path.write_bytes(data)
                written.append(path)
                conn.execute("INSERT INTO letter_files (id, packet_id, client_id, kind, label, "
                             "download_name, stored_name) VALUES (?,?,?,?,?,?,?)",
                             (secrets.token_urlsafe(16), packet_id, user["id"], kind, label, dl, stored))
            add_event(conn, int(user["id"]), title="Letters signed",
                      body=f"You signed {packet['title']} ({len(pairs)} letter(s)). "
                           "We'll mail them and update you here.", created_by="client")
            audit("letters.signed", actor=user, target_user_id=int(user["id"]),
                  detail=f"packet {packet_id}: {packet['title']}; hash {packet['content_hash'][:12]}",
                  conn=conn)
            conn.commit()
        except Exception:
            conn.rollback()
            for p in written:
                try:
                    p.unlink()
                except OSError:
                    pass
            raise
        flash("Signed. We'll mail your letters and update your timeline.", "success")
        return redirect(url_for("letters.portal_sign", packet_id=packet_id))

    return render_template("portal/letters_sign.html", **ctx)


# --------------------------------------------------------------------------
# downloads: the client, an admin, or the assigned specialist. Anyone else,
# and any unknown id, gets the same 404, so an id can't be confirmed.
# --------------------------------------------------------------------------
@bp.get("/letters/files/<fid>")
@require_login
def download(fid: str):
    row = get_db().execute("SELECT * FROM letter_files WHERE id = ?", (fid,)).fetchone()
    if not row:
        abort(404)
    user = current_user()
    is_owner = user["role"] == "client" and int(user["id"]) == int(row["client_id"])
    if not is_owner and not _staff_can_see(int(row["client_id"])):
        abort(404)
    if row["kind"] == "record" and is_owner:      # the audit record is staff's copy
        abort(404)
    path = _dir("signed") / row["stored_name"]
    if not path.is_file():
        abort(404)
    audit("letters.file_opened", actor=user, target_user_id=int(row["client_id"]),
          detail=row["download_name"], conn=get_db())
    get_db().commit()
    resp = send_file(str(path), mimetype="application/pdf", as_attachment=True,
                     download_name=row["download_name"], max_age=0)
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp
