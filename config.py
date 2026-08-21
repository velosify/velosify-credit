"""
VelosifyCredit configuration.

Every setting is env-driven so the same code runs locally (no keys needed)
and in production (Railway) without edits. Copy .env.example to .env and
fill in what you have; anything missing degrades gracefully rather than
crashing at import time.
"""
from __future__ import annotations

import json
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
# Whether a key was actually supplied. Without one we generate a random key
# per process, which works but silently signs everyone out on every deploy and
# breaks sessions outright across more than one worker. Worth being able to
# report on rather than leaving as a mystery.
SECRET_KEY_PROVIDED = bool(_env("SECRET_KEY"))
SECRET_KEY = _env("SECRET_KEY") or secrets.token_urlsafe(48)
DEBUG = _env("FLASK_DEBUG", "0") == "1"
APP_BASE_URL = _env("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")

# --- Brand ----------------------------------------------------------------
BRAND_NAME = _env("BRAND_NAME", "VelosifyCredit")
BRAND_DOMAIN = _env("BRAND_DOMAIN", "velosifycredit.com")
SUPPORT_EMAIL = _env("SUPPORT_EMAIL", "support@velosifycredit.com")
SUPPORT_PHONE = _env("SUPPORT_PHONE", "(602) 772-8020")
COMPANY_LEGAL_NAME = _env("COMPANY_LEGAL_NAME", "VelosifyCredit LLC")
# The Credit Repair Organizations Act requires the business address in the
# contract itself, so this defaults to the real one rather than to an empty
# string that would silently render a contract without it. Still overridable
# per environment.
COMPANY_ADDRESS = _env("COMPANY_ADDRESS", "901 Tower Dr, Troy, MI 48098")


def _tel(number: str) -> str:
    """A tel: href from whatever format the number is written in."""
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits}" if digits else ""


SUPPORT_PHONE_TEL = _tel(SUPPORT_PHONE)

# --- Storage --------------------------------------------------------------
#
# Everything a client gives us is in two places: rows in the database and
# files in the upload directory. Both must live on a mounted volume, NOT
# inside the application directory.
#
# A platform like Railway builds a fresh container image on every deploy and
# throws the old one away. Anything written inside the deployed source tree
# goes with it, silently: the app comes back up, creates an empty database,
# and every client, document and message is gone with no error anywhere. The
# only thing standing between that and a real client's file is DATA_DIR
# pointing at a volume.
#
# Set DATA_DIR to the volume's mount path (for example /data) and both the
# database and the uploads follow it. DB_PATH and UPLOAD_DIR still override
# individually for anyone who needs them apart.
DATA_DIR = Path(_env("DATA_DIR") or ROOT)
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(_env("DB_PATH") or (DATA_DIR / "velosify_credit.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

UPLOAD_DIR = Path(_env("UPLOAD_DIR") or (DB_PATH.parent / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Are we running on a platform that rebuilds the filesystem on deploy?
# Each of these is set by the platform itself, never by us.
HOSTED = bool(_env("RAILWAY_ENVIRONMENT") or _env("RAILWAY_PROJECT_ID")
              or _env("RENDER") or _env("FLY_APP_NAME") or _env("DYNO"))


def _inside_app_tree(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


def read_mount_points(source: Path = Path("/proc/mounts")) -> set[str]:
    """Every path the kernel currently has something mounted at.

    Empty on anything that is not Linux, which is the signal to fall back to
    the path heuristic rather than to guess.
    """
    try:
        return {line.split()[1] for line in
                source.read_text().splitlines() if len(line.split()) > 1}
    except (OSError, IndexError):
        return set()


def nearest_mount(path: Path, mounts: set[str]) -> str | None:
    """The mount point this path actually lives on, or None if unknown.

    Pure, so it can be tested against a made-up set of mounts instead of
    against whatever the machine running the tests happens to have.
    """
    if not mounts:
        return None
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if str(candidate) in mounts:
            return str(candidate)
    return None


MOUNTS = read_mount_points()
DB_MOUNT = nearest_mount(DB_PATH.parent, MOUNTS)

# Two ways to lose everything, and the second one is the one that caught us.
#
#   1. The data sits inside the deployed source tree, which the platform
#      rebuilds. Obvious once you look at the path.
#   2. The data sits at a perfectly sensible-looking path like /data, and no
#      volume is mounted there. The path looks right, the app works, and the
#      directory is part of the container image, so it is destroyed with it.
#      Nothing about DB_PATH tells you this. Only the mount table does.
#
# On a container the root filesystem is itself a mount, so "the nearest mount
# is /" means the data is on the image, not on a volume.
_ON_VOLUME = DB_MOUNT is not None and DB_MOUNT != "/"
STORAGE_EPHEMERAL = (
    _inside_app_tree(DB_PATH) or _inside_app_tree(UPLOAD_DIR) or not _ON_VOLUME
)
STORAGE_AT_RISK = HOSTED and STORAGE_EPHEMERAL

# Said in the app's own words wherever the warning is shown, because "it is
# not on a volume" and "it is inside the app directory" need different fixes.
if _inside_app_tree(DB_PATH) or _inside_app_tree(UPLOAD_DIR):
    STORAGE_REASON = ("the data is inside the application directory, which "
                      "this host rebuilds on every deploy")
elif not _ON_VOLUME:
    STORAGE_REASON = (f"nothing is mounted at {DB_PATH.parent}, so it is part "
                      f"of the container image and is destroyed with it — the "
                      f"path looks right but no volume is attached to it")
else:
    STORAGE_REASON = ""

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

# State registrations, published on /legal/state-disclosures. Empty until real
# registrations exist, and the page says so plainly rather than implying
# coverage we do not have. Set STATE_REGISTRATIONS to a JSON array of
# {"state", "registration", "bond"} objects as each one is granted, e.g.
#   [{"state": "Texas", "registration": "CSO #12345", "bond": "$10,000"}]
def _env_json_list(name: str) -> list:
    raw = _env(name, "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


STATE_REGISTRATIONS = _env_json_list("STATE_REGISTRATIONS")

# --- Observability --------------------------------------------------------
# Optional. With SENTRY_DSN set and sentry-sdk installed, unhandled errors are
# reported; without either, the app logs to stdout exactly as before. Nothing
# here is required to boot.
SENTRY_DSN = _env("SENTRY_DSN")
LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
