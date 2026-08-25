# Kesari Ananta — Meta Ads Decision Center

Static dashboard (`index.html`) showing live Meta Ads data for the Kesari Ananta ad account: Active Campaigns (last 5 / 10 days) and All Campaigns (lifetime), each with click-to-drill Campaign → Ad Set → Ad.

## How it stays up to date

`.github/workflows/refresh.yml` runs every 4 hours (and can be run manually from the Actions tab). It calls `scripts/refresh_data.py`, which pulls fresh data directly from the Meta Graph API and rewrites the data payload embedded in `index.html`, then commits and pushes if anything changed. Netlify is connected to this repo and redeploys automatically on every push — no manual steps after initial setup.

## One-time setup required

In this repo's **Settings → Secrets and variables → Actions**, add:

- `META_ACCESS_TOKEN` (required) — a Meta access token with `ads_read` on the ad account. A System User token from Business Manager is recommended since it doesn't expire; a regular user token expires in ~60 days and would need periodic renewal.
- `META_AD_ACCOUNT_ID` (optional) — defaults to `act_1273398820495430` if not set.

Until `META_ACCESS_TOKEN` is set, the scheduled workflow will fail (visible in the Actions tab) rather than publish fabricated data — this is intentional.

## Hosting

Deployed on Netlify via "Import an existing project" pointed at this repo, publish directory `.` (root), no build command needed — it's a static file.
