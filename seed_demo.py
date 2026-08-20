#!/usr/bin/env python3
"""
Create a demo client account for testing the portal end to end.

    python seed_demo.py                 # create it, print the credentials once
    python seed_demo.py --with-files    # also drop sample uploads to test with
    python seed_demo.py --remove        # delete the account and its files

The password is generated here, printed once to your terminal, and never
written anywhere else. Nothing chooses it for you and nothing stores it in
plaintext, so re-running is how you get a new one rather than looking the old
one up.

Two deliberate choices worth knowing about:

  * The account is created already paid, because document upload is gated on
    a paid order and the point is to test uploading without going through
    Stripe.
  * The email is under @example.com, which RFC 2606 reserves precisely so it
    can never route anywhere real. A typo cannot email a stranger.

Safe to run against production if you want to smoke-test a deploy, and
--remove takes it back out cleanly, including the uploaded files.
"""
from __future__ import annotations

import argparse
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from auth import hash_password  # noqa: E402

DEMO_EMAIL = "demo.client@example.com"
DEMO_FIRST = "Dana"
DEMO_LAST = "Demo"

# With --count you get several at once, each parked at a different point in
# the pipeline, so the admin list has something realistic to click through
# rather than three identical rows.
DEMO_PEOPLE = [
    ("Dana",  "Demo",   "documents"),
    ("Marcus", "Testcase", "analysis"),
    ("Priya", "Sample",  "disputes"),
    ("Ellis", "Trial",   "responses"),
    ("Robin", "Example", "complete"),
]


def demo_email(index: int) -> str:
    """demo.client@example.com for the first, then numbered."""
    return DEMO_EMAIL if index == 0 else f"demo.client{index + 1}@example.com"


def _now(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)
            ).replace(microsecond=0).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Sample uploads
#
# Deliberately not mock-ups of real documents. A convincing fake driving
# licence or Social Security card is a forgery whatever it was made for, and
# the portal does not care what is inside the file. These are plainly labelled
# placeholders that exist so there is something to drag onto the dropzone.
# ---------------------------------------------------------------------------

def _sample_pdf(title: str) -> bytes:
    """A single-page PDF, written by hand.

    Hand-rolled rather than pulled from a library because the whole file is
    120 lines of predictable syntax and this script should not add a
    dependency the application itself does not have.
    """
    lines = [
        ("SAMPLE DOCUMENT", 26, 700),
        (title, 16, 660),
        ("This file is a test fixture for the VelosifyCredit portal.", 11, 620),
        ("It contains no real personal information and is not a", 11, 602),
        ("record of anything. Delete it whenever you like.", 11, 584),
        (f"Generated {_now()[:10]}", 9, 545),
    ]
    text = "BT\n"
    for content, size, y in lines:
        escaped = content.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        text += f"/F1 {size} Tf 1 0 0 1 72 {y} Tm ({escaped}) Tj\n"
    text += "ET\n"
    stream = text.encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


SAMPLE_FILES = [
    ("credit_report", "sample-credit-report.pdf", "Credit report"),
    ("photo_id", "sample-photo-id.pdf", "Photo ID placeholder"),
    ("proof_of_address", "sample-proof-of-address.pdf", "Proof of address"),
    ("ssn_proof", "sample-ssn-proof.pdf", "Proof of Social Security number"),
]


def write_samples(out_dir: Path) -> list[Path]:
    """Write the placeholder files somewhere you can pick them up from."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for _, filename, title in SAMPLE_FILES:
        path = out_dir / filename
        path.write_bytes(_sample_pdf(title))
        written.append(path)
    return written


# ---------------------------------------------------------------------------

def remove(conn: sqlite3.Connection) -> None:
    """Take out every seeded account, however many were made.

    Matched on the reserved example.com pattern rather than on a list, so a
    demo account created by an older run of this script is still cleaned up.
    """
    rows = conn.execute(
        "SELECT id, email FROM users WHERE email LIKE 'demo.client%@example.com'"
    ).fetchall()
    if not rows:
        print("No demo accounts to remove.")
        return
    total_files = 0
    for row in rows:
        files = conn.execute("SELECT stored_name FROM documents WHERE user_id = ?",
                             (row["id"],)).fetchall()
        for f in files:
            (config.UPLOAD_DIR / f["stored_name"]).unlink(missing_ok=True)
        total_files += len(files)
        # Orders, documents and case events all cascade from the user row.
        conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
        print(f"  removed {row['email']}")
    conn.commit()
    print(f"Removed {len(rows)} demo account(s) and {total_files} uploaded file(s).")


def create(conn: sqlite3.Connection, with_files: bool, index: int = 0) -> str:
    password = secrets.token_urlsafe(12)
    first, last, stage = DEMO_PEOPLE[index % len(DEMO_PEOPLE)]
    email = demo_email(index)
    existing = conn.execute("SELECT id FROM users WHERE email = ?",
                            (email,)).fetchone()

    if existing:
        user_id = int(existing["id"])
        conn.execute(
            "UPDATE users SET password_hash = ?, password_changed_at = ?, "
            "role = 'client' WHERE id = ?",
            (hash_password(password), _now(), user_id),
        )
        print(f"Reset the password on {email} (id {user_id}).")
    else:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, first_name, last_name, "
            "phone, role, case_stage, created_at, agreement_signed_at, "
            "agreement_name, agreement_ip, disclosure_ack_at, "
            "email_verified_at, password_changed_at) "
            "VALUES (?, ?, ?, ?, '(555) 010-0100', 'client', ?, "
            "?, ?, ?, '127.0.0.1', ?, ?, ?)",
            (email, hash_password(password), first, last, stage,
             _now(-2), _now(-2), f"{first} {last}", _now(-2),
             _now(-2), _now()),
        )
        user_id = int(cur.lastrowid)

    # Clear any lockout on this address. Six failed sign-ins bar an account
    # for fifteen minutes, and mistyping a freshly generated password two or
    # three times is easy, so handing back a new password without lifting the
    # bar would look exactly like the new password not working either.
    conn.execute(
        "DELETE FROM auth_failures WHERE scope = 'login:email' AND key = ?",
        (email.lower(),),
    )

    # Upload is gated on a paid order, so the demo needs one. Marked with an
    # obviously fake Stripe id: nothing here ever touched Stripe, and a real
    # looking session id in the orders table would be misleading later.
    paid = conn.execute(
        "SELECT id FROM orders WHERE user_id = ? AND status = 'paid'", (user_id,)
    ).fetchone()
    if not paid:
        conn.execute(
            "INSERT INTO orders (user_id, amount_cents, currency, status, "
            "stripe_session_id, created_at, paid_at) "
            "VALUES (?, ?, ?, 'paid', 'cs_demo_seed_not_a_real_session', ?, ?)",
            (user_id, config.PRICE_CENTS, config.CURRENCY, _now(-2), _now(-2)),
        )
        conn.execute(
            "INSERT INTO case_events (user_id, title, body, stage, created_at, "
            "created_by) VALUES (?, 'Enrollment complete', "
            "'Demo account seeded for testing.', ?, ?, 'system')",
            (user_id, stage, _now(-2)),
        )

    if with_files:
        for doc_type, filename, title in SAMPLE_FILES:
            already = conn.execute(
                "SELECT id FROM documents WHERE user_id = ? AND doc_type = ?",
                (user_id, doc_type)).fetchone()
            if already:
                continue
            data = _sample_pdf(title)
            stored = f"{user_id}_{secrets.token_hex(16)}.pdf"
            (config.UPLOAD_DIR / stored).write_bytes(data)
            conn.execute(
                "INSERT INTO documents (user_id, doc_type, original_name, "
                "stored_name, mime_type, size_bytes, status, uploaded_at) "
                "VALUES (?, ?, ?, ?, 'application/pdf', ?, 'received', ?)",
                (user_id, doc_type, filename, stored, len(data), _now(-1)),
            )

    conn.commit()
    return password


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--remove", action="store_true",
                    help="delete the demo account and everything it uploaded")
    ap.add_argument("--with-files", action="store_true",
                    help="pre-attach sample uploads so the admin review flow "
                         "has something in it already")
    ap.add_argument("--count", type=int, default=1, metavar="N",
                    help="how many demo clients to create (default 1, max 5). "
                         "Each lands at a different case stage.")
    ap.add_argument("--samples-to", metavar="DIR",
                    help="also write the sample files here, to upload by hand")
    args = ap.parse_args()

    conn = _connect()
    try:
        if args.remove:
            remove(conn)
            return 0
        made = []
        for i in range(max(1, min(args.count, len(DEMO_PEOPLE)))):
            made.append((demo_email(i), create(conn, args.with_files, i)))
    finally:
        conn.close()

    if args.samples_to:
        for path in write_samples(Path(args.samples_to)):
            print(f"  sample file: {path}")

    print()
    print("=" * 62)
    print("  Demo client(s) ready. This is the only time the passwords are")
    print("  shown; run the script again to get new ones.")
    print()
    print(f"  Sign in at   {config.APP_BASE_URL}/login")
    print()
    for email, password in made:
        print(f"  {email:<32} {password}")
    print()
    print("  All paid, at assorted case stages, ready to upload.")
    print("  Remove them all with:  python seed_demo.py --remove")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
