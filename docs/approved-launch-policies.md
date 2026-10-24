# Owner-approved launch policies — October 24, 2026

These decisions were explicitly approved by the Platform Owner. They supersede
the unapproved billing and retention choices in customer-billing-terms-draft.md.
Implementation remains local until merged, deployed and verified.

## Subscription billing

Basic costs USD399 per month or USD4,548.60 per year, per company. Annual billing
is 5% below twelve monthly payments. Subscriptions renew automatically.
Cancellation takes effect at the paid billing period's end. Monthly payments
are not automatically refunded except where required by law or an approved
exception. Annual refund requests receive manual review, without a guaranteed
refund. Enterprise pricing and scope require a separate agreement.

## File retrieval and retention

For 90 days after paid access ends, an existing active company administrator can
sign in and download that company's existing files at `/account/retrieve`.
Employee accounts and public/client sharing do not gain retrieval privileges.
Uploads, administrative changes and project edits are blocked. Explicit owner
security suspensions override retrieval access. Customers must keep their own
copies before the deadline. No indefinite retention is promised.

The application does not automatically delete objects after this window.
Any deletion must follow a separately reviewed process and owner authorization;
do not add a blanket 90-day R2 lifecycle rule, which would also delete files for
active customers. Billing, audit, backups and legal retention are separate from
the customer's download window.

## Deployment verification

- Confirm the five launch regression checks and existing tenant/billing tests pass.
- Confirm actual private R2 downloads work for the expired company administrator.
- Confirm another tenant, employee and public share cannot retrieve those files.
- Confirm expired subscriptions retain the original billing-period end timestamp.
- Confirm retrieval access stops at the deadline without deleting customer data.
- Configure Basic's approved monthly and annual Stripe Price IDs in owner settings.
- Publish these disclosures in checkout before turning on approval flags.
- Keep secrets in Render; customer files remain in private R2.
