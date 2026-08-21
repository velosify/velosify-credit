"""
VelosifyCredit outbound email.

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


ENABLED = bool(config.RESEND_API_KEY)

# Set by the most recent failed send, so the admin screens can say what went
# wrong instead of "it didn't work". Resend puts the useful part in the body
# of a 4xx (unverified domain, bad key), which is exactly the detail that
# never used to be logged.
last_error: str = "" if ENABLED else "RESEND_API_KEY is not set"


# Set by the application at import time so a send can be recorded without
# mailer needing to know about the database or the request. Left as None in
# tests and scripts, where nothing is watching.
on_result = None


def send_email(to: str, subject: str, text: str) -> bool:
    global last_error
    to = (to or "").strip()
    if not to:
        last_error = "no recipient"
        return _report(to, subject, False)

    if not config.RESEND_API_KEY:
        last_error = "RESEND_API_KEY is not set"
        print(f"\n--- email (not sent: no RESEND_API_KEY) ---\n"
              f"To: {to}\nSubject: {subject}\n\n{text}\n---\n", flush=True)
        return _report(to, subject, False)

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
            "Accept": "application/json",
            # Not decoration. Resend's API sits behind Cloudflare, which bans
            # clients by request signature, and urllib's default
            # "Python-urllib/3.x" is on that list. Without this header every
            # send came back 403 with Cloudflare error 1010 and never reached
            # Resend at all, which looks exactly like a bad key or an
            # unverified domain and is neither.
            "User-Agent": f"{config.BRAND_NAME}/1.0 (+{config.APP_BASE_URL})",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                last_error = ""
                return _report(to, subject, True)
            last_error = f"Resend returned {resp.status}"
    except urllib.error.HTTPError as exc:
        # The body is where Resend explains itself: an unverified sending
        # domain and a revoked key both arrive as a bare 4xx otherwise.
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        last_error = f"Resend {exc.code}: {detail or exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        last_error = f"could not reach Resend: {exc}"
    print(f"[mail] send to {to} failed: {last_error}", flush=True)
    return _report(to, subject, False)


def _report(to: str, subject: str, ok: bool) -> bool:
    """Hand the outcome to whoever is listening, then return it unchanged.

    Wrapped in a try because a mail send must never fail because recording it
    failed; that would be the tail wagging the dog.
    """
    if on_result:
        try:
            on_result(to, subject, ok, last_error)
        except Exception:
            pass
    return ok


def send_welcome(user: dict, order_amount_label: str) -> bool:
    first = user.get("first_name") or "there"
    return send_email(
        to=user["email"],
        subject=f"Welcome to {config.BRAND_NAME}, next steps inside",
        text=(
            f"Hi {first},\n\n"
            f"Your {config.BRAND_NAME} enrollment is confirmed and your "
            f"payment of {order_amount_label} has been received.\n\n"
            "Here's what happens next:\n\n"
            "1. Sign in to your portal: "
            f"{config.APP_BASE_URL}/portal\n"
            "2. Upload your credit report, photo ID, proof of address and "
            "proof of Social Security number.\n"
            "3. Once those are in, we begin your report analysis, usually "
            "within one business day.\n\n"
            "Everything you upload is stored privately and is only visible "
            "to you and our case team.\n\n"
            f"Questions? Just reply to this email or write to {config.SUPPORT_EMAIL}.\n\n"
            f"The {config.BRAND_NAME} team"
        ),
    )


def send_password_reset(user: dict, link: str) -> bool:
    """The reset link, and nothing else.

    No account details, no name beyond the first, and an explicit line telling
    someone who did not ask for it that they need do nothing. Reset mail is
    the message most likely to land in the wrong inbox, so it carries as
    little as it can.
    """
    first = user.get("first_name") or "there"
    return send_email(
        to=user["email"],
        subject=f"Reset your {config.BRAND_NAME} password",
        text=(
            f"Hi {first},\n\n"
            "Someone asked to reset the password on this account. If that was "
            "you, use the link below. It works once and expires in one hour.\n\n"
            f"{link}\n\n"
            "If it wasn't you, you don't need to do anything: your password "
            "has not changed and this link will expire on its own. If you get "
            f"several of these, tell us at {config.SUPPORT_EMAIL}.\n\n"
            "We will never ask you for your password, and we will never ask "
            "you to send it by email.\n\n"
            f"The {config.BRAND_NAME} team"
        ),
    )


def send_email_verification(user: dict, link: str) -> bool:
    first = user.get("first_name") or "there"
    return send_email(
        to=user["email"],
        subject=f"Confirm your email for {config.BRAND_NAME}",
        text=(
            f"Hi {first},\n\n"
            "Please confirm this is the right address for your case updates. "
            "Everything about your file is sent here, so a typo means you "
            "would miss it.\n\n"
            f"{link}\n\n"
            "If you did not enroll with us, ignore this message and nothing "
            "further will be sent.\n\n"
            f"The {config.BRAND_NAME} team"
        ),
    )


def send_admin_invite(user: dict, link: str, invited_by: str,
                      role: str = "admin") -> bool:
    """Invite to a staff account. Carries a link, never a password.

    Nobody should ever be sent a password they did not choose, least of all
    for an account that can open a client's Social Security proof. The
    recipient sets their own, and it is known only to them.
    """
    specialist = role == "specialist"
    what = "a credit specialist account" if specialist else "an administrator account"
    scope = (
        "This account can read the full file of every client assigned to "
        "you, including their government ID, Social Security proof and "
        "credit reports."
        if specialist else
        "This account can read every client's file, including government "
        "IDs, Social Security proofs and full credit reports."
    )
    return send_email(
        to=user["email"],
        subject=f"Your {config.BRAND_NAME} staff account",
        text=(
            f"{invited_by} has set up {what} for you on "
            f"{config.BRAND_NAME}.\n\n"
            "Set your password here. The link works once and expires in 72 "
            "hours:\n\n"
            f"{link}\n\n"
            f"{scope} Every document you open is logged against your name. "
            "Please use a long, unique password and a password manager.\n\n"
            "If you were not expecting this, do not use the link, and tell "
            f"{config.SUPPORT_EMAIL} straight away.\n\n"
            f"The {config.BRAND_NAME} team"
        ),
    )


def send_client_activation(user: dict, link: str) -> bool:
    """For a client enrolled by an administrator rather than through checkout.

    Says plainly that no payment is being asked for, because an unexpected
    email about a credit account is exactly the shape of a scam, and a client
    who suspects one is right to.
    """
    first = user.get("first_name") or "there"
    return send_email(
        to=user["email"],
        subject=f"Your {config.BRAND_NAME} account is ready",
        text=(
            f"Hi {first},\n\n"
            f"We have set up your {config.BRAND_NAME} account. Use the link "
            "below to choose a password, read your credit file rights and "
            "sign the service agreement. It takes about five minutes.\n\n"
            f"{link}\n\n"
            "You will not be asked to pay anything on that page. If you were "
            "not expecting this email, ignore it and nothing further will "
            f"happen, or tell us at {config.SUPPORT_EMAIL}.\n\n"
            f"The {config.BRAND_NAME} team"
        ),
    )


# ---------------------------------------------------------------------------
# Message notifications
#
# None of these carry the message itself. A thread about someone's credit file
# names accounts, balances and sometimes identity theft, and email is the one
# channel in this system we do not control the far end of. The notification
# says there is something to read and where to read it; the content stays
# behind the sign-in.
# ---------------------------------------------------------------------------

def send_message_to_client(client: dict, staff_name: str) -> bool:
    first = client.get("first_name") or "there"
    return send_email(
        to=client["email"],
        subject=f"New message about your {config.BRAND_NAME} case",
        text=(
            f"Hi {first},\n\n"
            f"{staff_name} has replied on your case. Read it and answer here:\n\n"
            f"{config.APP_BASE_URL}/portal/messages\n\n"
            "We keep case messages in the portal rather than in email, "
            "because your file has account numbers in it.\n\n"
            f"The {config.BRAND_NAME} team"
        ),
    )


def send_message_to_staff(staff: dict, client: dict) -> bool:
    name = f"{client.get('first_name','')} {client.get('last_name','')}".strip()
    return send_email(
        to=staff["email"],
        subject=f"{name or client['email']} sent you a message",
        text=(
            f"{name or client['email']} has written on their case.\n\n"
            f"{config.APP_BASE_URL}/admin/clients/{client['id']}#thread\n"
        ),
    )


def send_message_unassigned(client: dict) -> bool:
    """Nobody is on this file yet, so it goes to the office."""
    name = f"{client.get('first_name','')} {client.get('last_name','')}".strip()
    return send_email(
        to=config.ADMIN_ALERT_EMAIL,
        subject=f"Unassigned client message: {name or client['email']}",
        text=(
            f"{name or client['email']} has written a message and has no "
            f"specialist assigned, so nobody has been notified but you.\n\n"
            f"{config.APP_BASE_URL}/admin/clients/{client['id']}#thread\n"
        ),
    )


def send_case_assigned(staff: dict, client: dict) -> bool:
    name = f"{client.get('first_name','')} {client.get('last_name','')}".strip()
    return send_email(
        to=staff["email"],
        subject=f"You have been assigned {name or client['email']}",
        text=(
            f"{name or client['email']} is now on your list.\n\n"
            f"{config.APP_BASE_URL}/admin/clients/{client['id']}\n\n"
            "Their documents include a government ID and a Social Security "
            "proof. Every one you open is logged against your name.\n"
        ),
    )


def send_admin_new_client(user: dict) -> bool:
    name = f"{user.get('first_name','')} {user.get('last_name','')}".strip() or user["email"]
    return send_email(
        to=config.ADMIN_ALERT_EMAIL,
        subject=f"New {config.BRAND_NAME} client: {name}",
        text=(
            f"{name} just enrolled.\n\n"
            f"Email: {user['email']}\n"
            f"Phone: {user.get('phone') or 'n/a'}\n\n"
            f"Open the admin panel: {config.APP_BASE_URL}/admin/clients/{user['id']}\n"
        ),
    )


def send_documents_complete(user: dict) -> bool:
    name = f"{user.get('first_name','')} {user.get('last_name','')}".strip() or user["email"]
    return send_email(
        to=config.ADMIN_ALERT_EMAIL,
        subject=f"{name} finished uploading documents",
        text=(
            f"{name} has uploaded every required document. The case is ready "
            f"for analysis.\n\n"
            f"{config.APP_BASE_URL}/admin/clients/{user['id']}\n"
        ),
    )
