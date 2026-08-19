"""
VelosifyCredit — outbound email.

Uses Resend when RESEND_API_KEY is set, otherwise prints the message to the
console. Every send is best-effort: a mail failure must never break the
request that triggered it (a client who paid should land in the portal even
if the receipt email bounces).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import config


def send_email(to: str, subject: str, text: str) -> bool:
    to = (to or "").strip()
    if not to:
        return False

    if not config.RESEND_API_KEY:
        print(f"\n--- email (not sent: no RESEND_API_KEY) ---\n"
              f"To: {to}\nSubject: {subject}\n\n{text}\n---\n", flush=True)
        return False

    payload = json.dumps({
        "from": config.MAIL_FROM,
        "to": [to],
        "subject": subject,
        "text": text,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {config.RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[mail] send to {to} failed: {exc}", flush=True)
        return False


def send_welcome(user: dict, order_amount_label: str) -> None:
    first = user.get("first_name") or "there"
    send_email(
        to=user["email"],
        subject=f"Welcome to {config.BRAND_NAME} — next steps inside",
        text=(
            f"Hi {first},\n\n"
            f"Your {config.BRAND_NAME} enrollment is confirmed and your "
            f"payment of {order_amount_label} has been received.\n\n"
            "Here's what happens next:\n\n"
            "1. Sign in to your portal: "
            f"{config.APP_BASE_URL}/portal\n"
            "2. Upload your credit report, photo ID, proof of address and "
            "proof of Social Security number.\n"
            "3. Once those are in, we begin your report analysis — usually "
            "within one business day.\n\n"
            "Everything you upload is stored privately and is only visible "
            "to you and our case team.\n\n"
            f"Questions? Just reply to this email or write to {config.SUPPORT_EMAIL}.\n\n"
            f"— The {config.BRAND_NAME} team"
        ),
    )


def send_admin_new_client(user: dict) -> None:
    name = f"{user.get('first_name','')} {user.get('last_name','')}".strip() or user["email"]
    send_email(
        to=config.ADMIN_ALERT_EMAIL,
        subject=f"New {config.BRAND_NAME} client: {name}",
        text=(
            f"{name} just enrolled.\n\n"
            f"Email: {user['email']}\n"
            f"Phone: {user.get('phone') or '—'}\n\n"
            f"Open the admin panel: {config.APP_BASE_URL}/admin/clients/{user['id']}\n"
        ),
    )


def send_documents_complete(user: dict) -> None:
    name = f"{user.get('first_name','')} {user.get('last_name','')}".strip() or user["email"]
    send_email(
        to=config.ADMIN_ALERT_EMAIL,
        subject=f"{name} finished uploading documents",
        text=(
            f"{name} has uploaded every required document. The case is ready "
            f"for analysis.\n\n"
            f"{config.APP_BASE_URL}/admin/clients/{user['id']}\n"
        ),
    )
