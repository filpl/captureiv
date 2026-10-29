# Capture IV landing page

The current Capture IV marketing website, kept in its own folder so it can be developed separately from the Flask platform. This contribution adds no changes to the platform's Python code, accounts, database, or uploads.

Includes the homepage, Platform, Solutions, Clients, Pricing, About Us and Contact pages; responsive navigation; the Capture IV wordmark; Basic and Enterprise package information; team-size selection; and a demo-request handoff to the contact form. Basic is listed at $399/month. Annual billing is intentionally unavailable until its amount is approved and configured in the platform.

## Run and check

Use Node.js 20 or newer. The build and runtime have no external package dependencies.

```sh
cd landing-page
npm run build
npm test
npm run preview
```

Open the URL printed by preview. The local preview saves contact inquiries to a private, ignored `.local/contact-inquiries.json` file with owner-only permissions. Set `CAPTUREIV_CONTACT_STORE` to choose another local path. It does not send email. Production builds continue to use the configured D1 binding.

## Connect your platform

The checked-in `site.config.json` points Sign in, Client Portal, Get started, and enterprise requests to the local Flask app on port 5009. Before a production build, set `platformLoginUrl`, `platformSignupUrl`, and `platformEnterpriseUrl` to the platform's HTTPS endpoints. The `PLATFORM_LOGIN_URL`, `PLATFORM_SIGNUP_URL`, and `PLATFORM_ENTERPRISE_URL` build environment variables override their matching JSON settings.

```json
{"platformLoginUrl": "https://platform.example.com/login"}
```

If both apps share a domain and `/login` is reserved for the platform, use `/login`. The legacy `/portal` path redirects to the configured login, with no intermediate page.

This is navigation only: do not copy authentication/session logic into this marketing site. The platform remains responsible for account permissions and project access. On the platform's login and account pages, set the Capture IV logo and Back to home link to your marketing homepage URL. Configure the return URL on the platform separately; this folder does not change the platform application.

## Deployment and routes

The build produces a standalone Cloudflare Worker ESM file at `dist/server/index.js`. It serves `/`, `/platform`, `/solutions`, `/clients`, `/pricing`, `/about`, `/contact`, assets, and `POST /api/contact`. Deploy the marketing site on its own hostname, then link it to the platform. If hosting on the same hostname, route marketing paths to this Worker and reserve `/login`, platform/account/project routes and their assets for the platform. Do not route every platform request into this marketing Worker.

The source assumes it is served at the root of its marketing hostname. A `/landing-page/` URL prefix needs route and asset-path adaptation; the GitHub folder name is source organization, not a required URL prefix.

For Cloudflare, copy `wrangler.example.jsonc` to `wrangler.jsonc`, set your D1 database ID, and use your approved Wrangler installation. Apply the SQL migration before deployment:

```sh
npx wrangler d1 migrations apply construct-iv-inquiries --remote --config wrangler.jsonc
npx wrangler deploy --config wrangler.jsonc
```

These commands are setup instructions, not deployments performed by this contribution. Choose the intended domain/access policy before publishing. No deployment credentials or database contents are included here. See the repository's `docs/hosted-launch-runbook.md` for the complete platform and marketing-site handoff.

## Contact inquiries

`worker/contact.js` uses a D1 binding named `DB` and the `contact_inquiries` table from `drizzle/` in production. The local preview supplies a file-backed adapter for the same validated insert, rate-limit and success-response behavior. Both paths validate fields, restrict browser origins, reject honeypot input, limit body size and submissions, and confirm saving only after persistence succeeds. The local inquiry file is not served by the website and there is no public inquiry-listing endpoint.

The hosted site's existing saved inquiries stay in its existing private database. This folder includes schema/migrations only, not those records. If moving the form to another host, plan database migration separately. A non-Cloudflare host needs its own secure `/api/contact` persistence adapter.

No email is sent yet. Creating support@captureiv.com alone will not activate notifications: connect an email provider, keep its credentials server-side, and add tested delivery/retry behavior when ready. Some capture/scanning workflows are proposed offerings; hardware and third-party integrations are not implemented here.

## Files

- `web/`: editable page content, styles, browser behavior and imagery.
- `site.config.json`: destination for platform login links.
- `scripts/build.mjs`: embeds assets, versions CSS/JS URLs and builds the Worker.
- `scripts/preview.mjs`: local preview with private file-backed inquiry storage.
- `worker/contact.js`, `db/schema.ts`, `drizzle/`: contact persistence handler and schema.
- `scripts/test-site.mjs`: route/link and contact-handler checks.
- `wrangler.example.jsonc`: optional deployment configuration template.

The photography is illustrative. Source: https://unsplash.com/photos/NMrUtSA7094. Manrope and DM Sans are loaded from Google Fonts. The logo is styled text in a rounded rectangle.
