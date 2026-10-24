# Local verification checklist

Use a local database and test accounts only. Do not use live Stripe keys, copy
production media into the test environment, or expose the local preview to the
public internet. Create customer test data in a disposable local database.

## Start the local product

In one terminal, start the Flask app with local-only credentials on the configured
local database. On a first run, supply `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and
`SECRET_KEY` through the shell environment. Existing databases keep their current
users and data.

```sh
PORT=5009 .venv/bin/python app.py
```

In another terminal, build and start the standalone marketing preview:

```sh
cd landing-page
node scripts/build.mjs
PORT=5173 node scripts/preview.mjs
```

Open `http://127.0.0.1:5173/` and `http://127.0.0.1:5009/`.

Use `.env.testing.example` only with disposable test records. The production
template is not runnable until a managed database, canonical HTTPS app URL,
strong secret, SMTP, private R2 bucket credentials, live Stripe configuration,
trusted reverse-proxy count, and explicit billing/refund and data-retention policy
approvals have been provisioned.
Production mode rejects test Stripe keys; development and testing reject live
keys.

## Local checks

- [ ] The landing-page Pricing, Sign in, Get started, and Enterprise request links
  open the local Flask app. Refresh and directly open `/pricing`, `/platform`,
  `/clients`, and `/enterprise/request` in the marketing preview.
- [ ] Submit an Enterprise request with test data. Confirm the received page does
  not display the internal recipient address and the request appears in `/owner`.
- [ ] Sign in as Platform Owner. Confirm Basic remains fixed at $399/month, one
  active project, two authenticated accounts, and 1 TB; annual billing remains
  unavailable until both its approved amount and Stripe Price ID are configured.
  Confirm Enterprise pricing and limits retain their custom configuration.
- [ ] Submit a Basic registration. Confirm it remains pending and cannot open the
  customer dashboard without email verification and a verified billing webhook.
- [ ] With SMTP unset, use the local verification link shown by the app. Test an
  invalid, expired, and successfully used token; verify successful verification
  does not activate paid access by itself.
- [ ] Request a replacement verification link. Confirm previous unused tokens
  stop working, the response does not reveal account existence in production,
  and attempts are rate-limited.
- [ ] Mock or use Stripe test mode to open checkout twice for one pending signup.
  Confirm both requests use the same idempotency key and a paid subscription
  cannot start another signup checkout.
- [ ] Before checkout, confirm recurring amount and cancellation/refund disclosures
  appear and that payment cannot proceed without explicit checkbox acceptance.
- [ ] Mock Stripe cancellation and reversal. Confirm cancellation is scheduled for
  the actual Stripe period end, access and files remain through that date, and
  reversal works before expiry. Confirm monthly cancellation issues no refund.
- [ ] Submit an annual refund review request and confirm it does not cancel the
  subscription or issue a refund. As Platform Owner, test full, partial, denied,
  and under-review decisions; approved refunds must use a stable Stripe idempotency
  key and must not delete workspace data.
- [ ] As a company administrator, invite an employee. Confirm a pending invitation
  consumes a seat, the one-time setup link establishes a password, and the employee
  can read and upload only to assigned projects.
- [ ] As an administrator, create a project viewer. Confirm a random share URL and
  four-letter code are both required. Verify Flight Scans loads by default, denied
  tabs redirect, document access requires its explicit permission, and expiration
  or revocation blocks the link.
- [ ] Upload a small test video and document locally. Confirm the files are private,
  tenant storage use updates, range playback works, and an upload over quota is
  rejected. Do not alter or relocate existing local videos.
- [ ] In Upload media → Photos, place a pin on the selected project image and
  attach multiple test pictures. Open the viewer map, select the yellow pin, and
  confirm the gallery opens. Confirm a different project’s viewer code cannot
  fetch those image URLs and that image bytes count against the same quota.
- [ ] Disable a plan feature and retry the corresponding page, API, upload, and
  direct file URL. Each request must be denied without deleting existing data.
- [ ] Rehearse a database and file-store restore using the isolated procedure in
  [backup-recovery.md](backup-recovery.md).

Automated regression suite:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Marketing build and tests (run from `landing-page/` using Node.js 20+):

```sh
node scripts/build.mjs
npm test
```

## External-service checks that require owner setup

The app intentionally does not activate production services. Full Stripe checkout,
portal, renewal, cancellation, and webhook checks require a Stripe account configured
in test mode, test Price IDs, and a test webhook secret supplied through environment
variables. R2 multipart and signed-read behavior is covered with fake storage in the
automated suite; a real bucket smoke test requires an approved private test bucket,
credentials, and CORS configuration. Do not enter credentials into this document,
the landing site, or source control. Production providers, retention, backup policy,
and deployment require separate owner decisions and are outside local verification.
