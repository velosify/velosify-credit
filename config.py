"""
VelosifyCredit configuration.

Every setting is env-driven so the same code runs locally (no keys needed)
and in production (Railway) without edits. Copy .env.example to .env and
fill in what you have; anything missing degrades gracefully rather than
crashing at import time.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


# --- Core -----------------------------------------------------------------
SECRET_KEY = _env("SECRET_KEY") or secrets.token_urlsafe(48)
DEBUG = _env("FLASK_DEBUG", "0") == "1"
APP_BASE_URL = _env("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")

# --- Brand ----------------------------------------------------------------
BRAND_NAME = _env("BRAND_NAME", "VelosifyCredit")
BRAND_DOMAIN = _env("BRAND_DOMAIN", "velosifycredit.com")
SUPPORT_EMAIL = _env("SUPPORT_EMAIL", "support@velosifycredit.com")
SUPPORT_PHONE = _env("SUPPORT_PHONE", "")
COMPANY_LEGAL_NAME = _env("COMPANY_LEGAL_NAME", "VelosifyCredit LLC")
COMPANY_ADDRESS = _env("COMPANY_ADDRESS", "")

# --- Storage --------------------------------------------------------------
# DB and uploads sit under the same parent so a single mounted volume on
# Railway persists both across redeploys.
DB_PATH = Path(_env("DB_PATH") or (ROOT / "velosify_credit.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

UPLOAD_DIR = Path(_env("UPLOAD_DIR") or (DB_PATH.parent / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Uploads are never served from /static. They go through an auth-gated
# route. Keep the ceiling low enough that a bad actor can't fill the disk.
MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 25)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
ALLOWED_UPLOAD_EXTS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".heic", ".webp", ".doc", ".docx",
}

# --- Pricing --------------------------------------------------------------
# Stored in cents to avoid float math anywhere near money.
PRICE_CENTS = _env_int("PRICE_CENTS", 99700)
PRICE_LABEL = f"${PRICE_CENTS // 100:,}"
CURRENCY = _env("CURRENCY", "usd")

# --- Stripe ---------------------------------------------------------------
STRIPE_SECRET_KEY = _env("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = _env("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = _env("STRIPE_WEBHOOK_SECRET")
# Optional: use a pre-made Price in the Stripe dashboard instead of an
# inline price_data block. Handy if you want to change the amount without
# a redeploy.
STRIPE_PRICE_ID = _env("STRIPE_PRICE_ID")
BILLING_ENABLED = bool(STRIPE_SECRET_KEY)

# Simulation mode: the checkout step is skipped and the order is marked paid,
# so the whole product is clickable without Stripe keys.
#
# This is deliberately hard to switch on by accident. It requires either an
# explicit DEV_FAKE_CHECKOUT=1, or an unconfigured install that is clearly
# running on a developer's machine. Deploying with no Stripe keys must fail
# closed. An order flow that hands out enrollments for free is worse than one
# that is temporarily unavailable.
_FAKE_CHECKOUT_EXPLICIT = _env("DEV_FAKE_CHECKOUT", "0") == "1"

# Any of these means we're on a hosting platform, not someone's laptop. This
# is checked FIRST and independently of APP_BASE_URL, because APP_BASE_URL
# defaults to localhost, so a deploy where nobody set it yet would otherwise
# look local and quietly switch simulated checkout on, in public.
_ON_PLATFORM = any(os.environ.get(key) for key in (
    "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID",
    "RENDER", "FLY_APP_NAME", "DYNO", "HEROKU_APP_NAME", "K_SERVICE",
    "AWS_EXECUTION_ENV", "WEBSITE_INSTANCE_ID",
))
_RUNNING_LOCALLY = (
    not _ON_PLATFORM
    and APP_BASE_URL.startswith(("http://127.0.0.1", "http://localhost"))
)
DEV_FAKE_CHECKOUT = _FAKE_CHECKOUT_EXPLICIT or (not BILLING_ENABLED and _RUNNING_LOCALLY)

# True when the site is live but can't take money. The order page says so
# instead of pretending to sell something.
CHECKOUT_UNAVAILABLE = not BILLING_ENABLED and not DEV_FAKE_CHECKOUT

if DEV_FAKE_CHECKOUT and not _RUNNING_LOCALLY:
    print("\n*** WARNING: DEV_FAKE_CHECKOUT is on and APP_BASE_URL is not "
          "localhost. Anyone can enroll without paying. ***\n", flush=True)

# --- Email ----------------------------------------------------------------
# Resend is used when a key is present; otherwise emails are logged to the
# console so nothing silently disappears in development.
RESEND_API_KEY = _env("RESEND_API_KEY")
MAIL_FROM = _env("MAIL_FROM", f"{BRAND_NAME} <no-reply@{BRAND_DOMAIN}>")
ADMIN_ALERT_EMAIL = _env("ADMIN_ALERT_EMAIL", SUPPORT_EMAIL)

# --- Admin bootstrap ------------------------------------------------------
# On first boot, if this email is set, the account is created (or promoted)
# as an admin with ADMIN_BOOTSTRAP_PASSWORD.
ADMIN_BOOTSTRAP_EMAIL = _env("ADMIN_BOOTSTRAP_EMAIL")
ADMIN_BOOTSTRAP_PASSWORD = _env("ADMIN_BOOTSTRAP_PASSWORD")

# --- Compliance -----------------------------------------------------------
# The Credit Repair Organizations Act gives consumers 3 business days to
# cancel without penalty. Surfaced in the agreement and the portal.
CANCELLATION_DAYS = _env_int("CANCELLATION_DAYS", 3)
