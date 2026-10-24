# Hosted launch runbook

This is the remaining handoff from the local preview to a hosted service. The
local product is implemented, but this checklist is not evidence that a hosted
deployment or a live integration has been completed. Do not put credentials in
Git, this document, or chat.

## 1. Choose the hosting and domain layout

- Choose a managed Python host for the Flask platform, with a managed PostgreSQL
  database and a stable HTTPS hostname (for example, `app.example.com`).
- Choose a public HTTPS hostname for the marketing site (for example,
  `www.example.com`). The marketing site is a separate Cloudflare Worker and
  needs its own D1 database for enterprise/contact inquiries.
- Set the platform's `PUBLIC_APP_URL` and `MARKETING_SITE_URL` to the final HTTPS
  origins. Set the landing build's `PLATFORM_LOGIN_URL`,
  `PLATFORM_SIGNUP_URL`, and `PLATFORM_ENTERPRISE_URL` to the platform routes.
- Keep the marketing site and platform on separate hostnames, or explicitly
  route only marketing paths to the Worker. Do not send platform login, account,
  project, or media paths to the marketing Worker.

## 2. Provision private platform services

In the hosting provider's secret manager, configure the values in
`.env.production.example`: a random `SECRET_KEY` (at least 32 characters),
`ADMIN_USERNAME` and initial `ADMIN_PASSWORD`, managed PostgreSQL `DATABASE_URL`,
HTTPS `PUBLIC_APP_URL`, `TRUSTED_PROXY_COUNT`, SMTP settings, and the private
enterprise inquiry recipient. Keep the existing recipient private; the public
site must not render it.

Create a private Cloudflare R2 bucket and a scoped access key. Set
`STORAGE_BACKEND=r2`, `R2_BUCKET_NAME`, `R2_ACCOUNT_ID` or `R2_ENDPOINT_URL`,
`R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY`. Configure bucket CORS for the
platform's HTTPS origin and browser `PUT` requests. Do not make the bucket
public. Configure managed database backups and an independent backup/copy policy
for media objects before accepting customer uploads.

## 3. Configure Stripe in test mode first

Use a Stripe test account/key and a test webhook endpoint before enabling live
billing. The webhook URL is:

```text
https://<platform-host>/api/billing/stripe-webhook
```

Subscribe the endpoint to `checkout.session.completed`,
`customer.subscription.created`, `customer.subscription.updated`,
`customer.subscription.deleted`, `invoice.payment_failed`,
`invoice.payment_succeeded`, `invoice.paid`, `refund.created`, and
`refund.updated`. Store the endpoint signing secret and API key as protected
environment variables.

Create a recurring monthly Stripe Price for Basic at $399/month. After the owner
account is initialized, enter its `price_…` identifier in Platform Owner → Basic
plan settings; the application intentionally does not invent or hard-code a
Stripe Price ID. Do not configure an annual Basic amount or Price until approved.
Preserve the existing Enterprise configuration and customer agreements.

Run the local verification checklist with Stripe test mode and a private test R2
bucket first. Before live billing, the Platform Owner must approve the customer
billing/refund terms and data-retention policy, set
`BILLING_TERMS_APPROVED=1` and `DATA_RETENTION_POLICY_APPROVED=1`, switch to live
Stripe credentials, create the live monthly Price, and verify the live webhook.
Production startup rejects test Stripe keys.

## 4. Deploy in order

1. Provision the platform host, managed database, private R2 bucket, SMTP, and
   Stripe test endpoint. Set protected environment variables.
2. Run the platform release command from `Procfile` to apply additive database
   migrations; then start the WSGI app with the configured production server.
3. Sign in as the Platform Owner and configure Basic's monthly Stripe Price ID.
4. Build the marketing site with final platform HTTPS URLs. Provision its D1
   database, apply the checked-in migrations, and deploy the Worker using the
   reviewed `wrangler.jsonc` configuration.
5. Verify registration, email verification, checkout, signed webhook delivery,
   cancellation, owner refund review, private upload/playback, viewer links,
   inquiry persistence, and mobile layouts on staging.
6. Review backups and restoration, monitoring, error alerts, customer-facing
   policies, domain/TLS, and data-retention decisions. Only then request explicit
   approval before changing the Stripe environment to live or publishing the
   production deployment.

## Still needed from the owner

- Hosting provider and the two public hostnames (platform and marketing site).
- Stripe account access/configuration for test mode, then live mode after review;
  never send secret keys in chat.
- Cloudflare account setup for private R2 plus the marketing Worker's D1 database;
  keep API tokens in the host/Cloudflare secret manager.
- SMTP provider credentials and sender domain verification.
- Final approval of billing/refund terms, retention/export period, and backup
  recovery objectives.
- Explicit approval when a live billing switch or production deployment is ready.
