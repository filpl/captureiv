# Construct IV — Construction Visual Intelligence

A Flask portal for securely browsing construction drone footage and project documents. It includes admin-managed accounts, protected media access, flight metadata, tags, audit logging, and a small authenticated JSON API.

## Run locally

Requires Python 3.10 or newer.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="replace-with-a-long-random-secret"
export ADMIN_USERNAME="admin"
export ADMIN_PASSWORD="replace-with-a-strong-password"
python app.py
```

Open `http://127.0.0.1:5000`. On first run, the app creates `instance/drone_portal.db` and an administrator account from the environment variables. `ADMIN_PASSWORD` is required when creating the initial administrator; the app will stop with a clear error if it is missing.

## Configuration

- `SECRET_KEY`: Flask session signing key; set a stable secret in production.
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`: initial administrator account. Existing accounts are not overwritten.
- `PORT`: HTTP port (default `5000`).
- `MAX_CONTENT_LENGTH`: maximum request/upload size in bytes (default 8 GiB).
- `SESSION_COOKIE_SECURE=1`: enable secure cookies when HTTPS is configured.

Uploads are stored under `static/uploads/` and SQLite data under `instance/`. Back up both directories. The app sets `SESSION_COOKIE_SECURE` to false by default for local HTTP development; turn it on behind HTTPS.

## Backup and recovery planning

Keep database backups and media-object backups separate. For a local SQLite deployment, use SQLite's online backup API while the app is running, then copy `static/uploads/` to a separate backup location; a raw copy of an active SQLite file may miss write-ahead-log data. For production PostgreSQL, configure the selected database provider's encrypted backups and point-in-time recovery before accepting customer data. For R2, configure an independent backup target or versioned copy process; R2 availability alone is not a backup. No production retention period or deletion policy is configured here. Before launch, document the selected providers, backup frequency, access controls, and recovery objectives, then perform a restore into an isolated environment and verify database records and referenced objects together.

## Features

- Date, project, direction, tag, and text filtering for flights.
- Protected video streaming and document downloads with SHA-256 duplicate detection.
- Admin account management, flight uploads, and document uploads.
- Authenticated endpoints at `/api/health`, `/api/flights`, `/api/tags`, `/api/projects`, `/api/stats`, and admin-only `/api/audit`.

For a production deployment, run behind a production WSGI server and HTTPS reverse proxy, use managed PostgreSQL and private R2 object storage, and configure secrets and upload size limits for the host.

The Flask app now exposes `wsgi:app` for Gunicorn and an explicit `flask --app app init-db` command for applying the additive compatibility migration and initializing configured defaults. Applied schema revisions are recorded in `schema_migration`; the first SaaS foundation revision backfills legacy records without deleting them. The included `Procfile` runs initialization as a release step and starts Gunicorn with two workers and four threads. `APP_ENV=production` requires a non-SQLite database URL, a canonical HTTPS `PUBLIC_APP_URL`, a `SECRET_KEY` of at least 32 characters, private R2 credentials, SMTP, and live Stripe credentials plus its webhook secret; secure session cookies are forced on. Set `TRUSTED_PROXY_COUNT` to the number of trusted reverse proxies (default 1); production trusts forwarded client IP and HTTPS headers only from that configured proxy chain. Verification, invitation, and billing return links use the canonical origin. Startup fails until these requirements are met. No live services are activated by the example files.

Use `.env.example` for local development and `.env.testing.example` for disposable test data. `.env.production.example` is a secret-free checklist for an eventual deployment; fill it only in the hosting provider's protected environment or secret manager. `.env.*` runtime files are git-ignored, while the three `.example` templates remain tracked. The application accepts `sk_test_` Stripe keys outside production and `sk_live_` keys only when `APP_ENV=production`.

## Read-only project sharing

Sign in as an administrator and open **Admin → Project viewer access**. Create a project viewer by name, or generate access for a project that already has uploaded footage/documents. The admin page displays a share URL with a random opaque token and a separate four-letter code. Visitors open the URL and enter the code; knowing either credential alone is insufficient. Regenerating access rotates both credentials, and expiration or revocation disables the URL.

The project viewer is read-only. Visitors can browse and stream only the project attached to that code, download media/documents, and copy the project link/code from **Project Access**. Upload a site map in Admin to use it as the viewer overview; map pins are joined in placement order to draw the critical path.

## Multi-flight uploads and site maps

Company administrators and employees assigned to a project can open **Upload media** from the flight archive and submit multiple videos for that project. Employees can upload only to projects assigned to them; map setup and other workspace administration remain company-admin-only. Set the project and date once, then assign a direction and title to each selected file. Under **Site overview map & direction pins**, upload a JPG, PNG, or WEBP plan/aerial image, then click the map to place and label compass pins. The viewer displays that map and pins for the project; if no map is uploaded, it keeps the schematic overview.

The **Photos** tab on the same upload and mapping page uses that project site image as its coordinate reference. Authorized project members can place yellow photo pins and attach up to 20 JPG, PNG, or WebP images per upload (25 MB per image, 100 MB per request). Images use the workspace storage quota and remain private behind the project viewer’s access code; selecting a pin on the viewer map opens its image gallery. Removing a pin also removes its attached images.

## SaaS account and billing foundation

The standalone marketing site lives in `landing-page/` and links into the Flask app for Basic registration and enterprise requests. Basic is fixed at $399/month with one active project, two authenticated accounts, and 1 TB of total occupied storage. Authorized public viewers are unlimited. Annual Basic billing stays unavailable until the Platform Owner approves an amount and configures its matching Stripe Price ID. Enterprise remains custom priced and scoped under its existing agreements.

New registrations remain pending until email verification and a signed Stripe test webhook confirms billing. With no SMTP configured in local development, the app shows a local verification link. The owner account is the existing `admin` account unless `ADMIN_USERNAME` is set. Company data queries are scoped by active company membership; uploads are associated with that company and checked against the configured storage allowance. Existing data is assigned to a retained “Legacy workspace” during the additive migration.

The account recovery flow is `/forgot-password`; pending company admins can request a fresh verification link at `/resend-verification`. Resends rotate one-time tokens, are rate-limited, and do not reveal whether an email is registered. Billing admins are linked to Stripe’s hosted billing portal after a Stripe customer is attached. Checkout displays the recurring price and cancellation/refund disclosures before sending a customer to Stripe. Stripe is optional locally; test keys are accepted in development/testing and live keys only in production. Repeated checkout requests use a subscription-scoped Stripe idempotency key, and active subscriptions cannot reopen signup checkout. Renewal cancellation is scheduled for Stripe's current period end; annual refund reviews are separate and never automatically cancel a subscription.

Failed subscription payments retain access for `BILLING_GRACE_DAYS` (default 7, configurable from 0 to 30). The grace window starts at the first failed-payment event and repeated failures or unrelated subscription updates do not extend it. After the window, paid access is paused while project records and uploaded files remain preserved. A confirmed paid invoice restores access unless the Platform Owner has explicitly suspended the company.

Company administrators invite employees by work email. Each invitation is a one-time, 48-hour setup link; employees set their own password, and pending invitations count toward the company seat limit. In local development without SMTP, the administrator receives a private on-screen setup link. Schema revisions are tracked additively, including viewer-link expiry/revocation and a composite rate-limit lookup index.

The Platform Owner can adjust project, employee, and storage limits for an individual company from the Companies panel. Blank values inherit the subscription plan; storage overrides are versioned and do not alter existing files. Reducing an allowance blocks future uploads above the new cap without deleting data.

Backup and restore steps for the local SQLite/file store and a future PostgreSQL/R2 deployment are documented in [docs/backup-recovery.md](docs/backup-recovery.md). This runbook does not configure a backup schedule, retention policy, or provider service.

The hosted-service handoff, required account setup, Stripe webhook events, R2 configuration, landing Worker/D1 deployment sequence, and owner approvals are listed in [docs/hosted-launch-runbook.md](docs/hosted-launch-runbook.md). It is a preparation checklist; no provider has been connected and no deployment has been performed.

The local customer, enterprise, employee, viewer, upload, plan-entitlement, billing, and external-service verification steps are in [docs/local-verification.md](docs/local-verification.md). Stripe and real R2 smoke tests remain gated on owner-configured test accounts and credentials. Production startup additionally requires explicit owner approval flags for customer billing/refund terms and the data-retention policy; the current terms document is an internal draft.

## Public marketing site

The standalone marketing site is in `landing-page/`; it does not replace the authenticated Flask application or its project-code entry at `/`. It can be built and previewed independently with the bundled Node runtime or Node.js 20+:

```sh
cd landing-page
node scripts/build.mjs
PORT=5173 node scripts/preview.mjs
```

The local landing-page buttons point to the Flask app at `http://127.0.0.1:5009`. Set `site.config.json` to the final HTTPS app URLs before a production build. The local preview stores contact inquiries locally; the hosted Worker requires its configured D1 database. No external service is activated by this local setup.

### Local SaaS preview

Run the platform on port 5009 and the landing page preview on port 5173. The preview landing page is available at `/pricing`; Basic signup, sign in, and Enterprise inquiry connect to the local platform. Stripe checkout remains unavailable until the owner supplies test-mode Stripe configuration through environment variables and configures the matching monthly Basic Price ID. Never place credentials in the landing site or source control.

Useful environment variables include `DATABASE_URL`, `APP_ENV`, `MARKETING_SITE_URL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `MAIL_FROM`, `ENTERPRISE_INQUIRY_EMAIL`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `BILLING_TERMS_APPROVED`, `DATA_RETENTION_POLICY_APPROVED`, and `R2_*`. Enterprise requests are saved for Platform Owner review and optionally sent to the privately configured `ENTERPRISE_INQUIRY_EMAIL`; the recipient is not displayed on the public site. Email delivery requires SMTP configuration. Keep `.env` local. Local storage remains the default (`STORAGE_BACKEND=local`). Once approved and configured, `STORAGE_BACKEND=r2` stores new uploads in a private R2 bucket; existing local files remain where they are. R2 setup requires a bucket, account ID or endpoint URL, and private access credentials. Authorized playback uses short-lived signed URLs, allowing the browser to request byte ranges directly from object storage. The admin batch upload page, site-capture batch uploader, and route video uploader send resumable multipart parts directly from the browser to R2. The server owns each upload session, reserves the expected bytes against the tenant quota, and verifies multipart parts and final object size before creating the flight record. Configure the bucket CORS policy to allow the application’s HTTPS origin, `PUT`, and required request headers (typically `*`), and configure an R2 lifecycle rule to abort incomplete multipart uploads after one day. This direct upload path is enabled only when `STORAGE_BACKEND=r2`; the local default still uses the existing server upload flow.

Both local uploads and R2 multipart sessions reserve their expected bytes against the company allowance before data transfer. Reservations expire after 24 hours if an upload process stops unexpectedly, and are released when the file metadata is committed or the upload fails.
