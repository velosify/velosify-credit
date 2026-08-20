# VelosifyCredit

Credit restoration site and client portal. One Flask app serving three
surfaces:

| Surface | Path | What it is |
| --- | --- | --- |
| Marketing | `/` | Landing page, pricing, FAQ, legal pages |
| Order flow | `/order` | Account creation, signed service agreement, Stripe Checkout |
| Members area | `/portal` | Document upload, case progress, agreement, account |
| Admin | `/admin` | Client list, document review, case stage, timeline updates |

No build step, no frontend framework. Server-rendered Jinja templates, one
stylesheet, one small JS file. SQLite for storage.

---

## Run it locally

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000.

With no Stripe keys set, the app runs in **simulation mode**: checkout is
skipped and the order is marked paid, so you can click through the entire
flow offline. The order page says so on the button.

To get into the admin area, set these before starting:

```bash
export ADMIN_BOOTSTRAP_EMAIL=you@velosifycredit.com
export ADMIN_BOOTSTRAP_PASSWORD=something-long-and-private
python app.py
```

That account is created (or promoted to admin) on boot, every boot. Sign in
at `/login` and you'll land on `/admin/clients`.

---

## Configuration

Copy `.env.example` to `.env` and fill in what applies. Every value is read
from the environment. See `config.py` for the full list and defaults.

The ones that matter most:

- `SECRET_KEY`: **set this in production.** Without it a random key is
  generated at boot, which silently signs everyone out on every restart.
- `APP_BASE_URL`: your public URL. Used to build Stripe redirect URLs and
  links in emails, and to decide whether to set the `Secure` cookie flag.
- `DB_PATH` / `UPLOAD_DIR`: put both on a persistent volume in production.
- `PRICE_CENTS`: defaults to `99700` ($997).

### Stripe

1. Create a **one-time** payment. You can either let the app build the price
   inline (just set `PRICE_CENTS`) or create a Price object in
   the Stripe dashboard and set `STRIPE_PRICE_ID`.
2. Set `STRIPE_SECRET_KEY` and `STRIPE_PUBLISHABLE_KEY`.
3. Add a webhook endpoint pointing at `https://yourdomain.com/webhook/stripe`,
   subscribed to `checkout.session.completed` and `charge.refunded`. Put the
   signing secret in `STRIPE_WEBHOOK_SECRET`.

The webhook is the source of truth for payment state. The success page also
verifies the session server-side, so a client who closes the tab mid-redirect
still gets enrolled. Both paths are idempotent.

### Email

Set `RESEND_API_KEY` and `MAIL_FROM` to send real mail. Without a key, every
message is printed to the console instead of disappearing silently. Emails
sent: client welcome, admin new-client alert, admin documents-complete alert.

---

## Deploying to Railway

`Procfile` is ready to go. There is deliberately **no** `runtime.txt` or
`.python-version`: Railway's builder verifies GitHub artifact attestations
before installing a Python build, and older pinned patch versions (3.11.9 and
similar) have none, so the build dies during setup. Letting the builder pick
its own Python avoids that entirely. The app has no version-specific
dependencies, so anything 3.11 or newer runs it.

1. Push the repo, create the service.
2. Add a **volume** and mount it at `/data`.
3. Set `DB_PATH=/data/velosify_credit.db` and `UPLOAD_DIR=/data/uploads`.
   Without this, every redeploy wipes your clients and their documents.
4. Set the rest of the environment from `.env.example`.
5. Point your domain at the service and set `APP_BASE_URL` to match.

---

## How the data model works

Four tables, in `db.py`:

- **users**: clients and admins, distinguished by `role`. Carries the case
  stage and the agreement signature record (name, timestamp, IP).
- **orders**: one row per enrollment attempt. `pending` until payment
  confirms, then `paid`, or `refunded` if a refund webhook arrives.
- **documents**: one row per uploaded file, tagged with a `doc_type` from
  the intake checklist and a review `status`.
- **case_events**: the timeline. Written by the system (payment confirmed,
  documents received) and by admins (stage changes, manual updates). This is
  what the client sees on their dashboard.

The intake checklist and the case stages are both defined as lists at the top
of `db.py`. Add a document type or a pipeline stage there and it appears
everywhere automatically.

---

## Security notes

- Passwords are PBKDF2-HMAC-SHA256, 240k iterations, per-user salt.
- Uploaded files are stored **outside** the static directory under randomly
  generated names, and are served only through `/files/<id>`, which checks
  that the requester owns the file or is an admin. A miss returns 404 rather
  than 403, so ids can't be enumerated.
- Upload extensions are allowlisted and total request size is capped by
  `MAX_UPLOAD_MB` at the WSGI layer, before anything is written to disk.
- Session cookies are HttpOnly, SameSite=Lax, and Secure when
  `APP_BASE_URL` is https.
- Every unsafe request must carry this session's CSRF token. The check runs
  in `before_request`, so a new form is protected by default rather than by
  remembering to decorate it. The Stripe webhook is the one exemption; it
  carries its own signature, which is stronger.
- The Content-Type of an uploaded file is derived from its extension and
  never read from the browser, and anything we would not render in place is
  forced to download. Trusting the browser's header let a file named
  `report.pdf` be served back as `text/html` and run as script on this
  origin, in whatever session opened it.
- Every response carries a CSP with no `unsafe-inline` for scripts,
  `frame-ancestors 'none'`, nosniff, a referrer policy, and HSTS when
  `APP_BASE_URL` is https.
- Password reset tokens are single use, expire in an hour, supersede any
  earlier one, and only their SHA-256 is stored, so a leaked backup contains
  no working links. The reset page answers identically whether or not the
  address has an account.
- The Stripe webhook verifies the signature and refuses to run at all if
  `STRIPE_WEBHOOK_SECRET` isn't configured.
- Simulated checkout can't switch itself on in production. It requires an
  explicit `DEV_FAKE_CHECKOUT=1` (which prints a loud startup warning), or an
  unconfigured install on a real developer machine. Presence of any hosting
  platform marker (`RAILWAY_ENVIRONMENT`, `DYNO`, `FLY_APP_NAME` and friends)
  disqualifies the local heuristic on its own, so a deploy where nobody has
  set `APP_BASE_URL` yet fails closed instead of giving the program away.

---

## Backups

Everything a client hands over lives in two places: rows in SQLite and files
in the upload directory. A backup of one without the other restores nothing,
so `backup.py` takes both into a single archive and then opens it to check.

```bash
python backup.py                  # write into ./backups, keep the last 14
python backup.py --out /data/bk   # somewhere that survives a redeploy
python backup.py --verify FILE    # open an old archive and count what is in it
```

The database is copied with SQLite's online backup API, not with `cp`. Copying
a live SQLite file can capture a half-written transaction, and you find out
when you try to restore, which is the worst possible moment.

Run it on a schedule, and keep at least one copy somewhere that is not the
same disk as the app. A backup that dies with the volume is not a backup.

## Operations

**Audit log.** `/admin/audit` records who signed in, who opened which client's
documents, and every admin change. There is no route that edits or deletes an
entry, so an admin cannot tidy up after themselves through the app. Given
these files are government IDs and Social Security proofs, "who looked at
this" needs an answer.

**Error reporting.** Set `SENTRY_DSN` and install `sentry-sdk[flask]` to get
unhandled errors reported. It is configured with `send_default_pii=False` and
request bodies off, so a crash report can never carry a client's data. Without
the DSN, errors log to stdout and nothing else changes. Every request gets a
short id, printed with any error and shown on the error page, so a client can
quote it.

**Throttling.** Failed sign-ins and password reset requests are counted per
account and per IP address, in the database rather than in memory, because a
counter that resets when the container restarts is not a counter. Limits are
in `security.py`.

## Before you take a real payment

The legal templates in `templates/legal/` (the service agreement, terms and
privacy policy) are drafted to track what the federal **Credit Repair
Organizations Act** requires: a written contract, the separate "Consumer
Credit File Rights Under State and Federal Law" disclosure, and a
three-business-day cancellation right.

Two things to sort out with a lawyer licensed in your state:

1. **Advance payment.** CROA prohibits a credit repair organization from
   charging or receiving payment before the promised services are *fully
   performed*. Collecting the full $997 at signup is exactly the pattern the
   statute targets, and the FTC and state AGs have brought cases on it.
   Common ways businesses structure around it are billing after the work is
   done, or splitting the fee into per-milestone charges as each stage
   completes. The app is built so this is a change to the order flow, not a
   rewrite, since `orders` already supports multiple rows per client.

2. **State registration.** Many states require credit repair organizations to
   register and post a surety bond, and several impose their own disclosure
   language and cancellation windows on top of the federal ones.

Neither of these is legal advice, and I'm not a lawyer, but both are worth
resolving before launch rather than after.
