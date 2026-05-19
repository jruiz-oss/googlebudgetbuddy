# AGENTS.md — Google BudgetBuddy

This file is the living source of truth for the project. Update it after every major change: add an entry to the Change Log and revise any stale sections. Delete entries older than ~6 months that are no longer relevant.

---

## What this is

**BudgetBuddy** is an internal Google Ads budget pacing tool for Commit Agency. It replaces a Google Ads MCC Script + Supabase pipeline that previously ran the same job.

It connects to Google Ads via OAuth, pulls MTD spend per campaign, compares it to a monthly budget sourced from a linked Google Sheet, and tells you whether each campaign is over-pacing, under-pacing, or on-track. It can optionally push budget adjustments back to Google Ads and auto-pause campaigns that blow past a spend threshold.

The original script (`scripts/google_ads_budget_pacer.js`) is kept for reference only — do not deploy it while BudgetBuddy is active.

---

## Stack

| Layer    | Tech                                      |
|----------|-------------------------------------------|
| Frontend | React (Vite), React Router, Axios, Lucide |
| Backend  | Python / Flask, SQLAlchemy, PostgreSQL    |
| Hosting  | Railway (backend + DB) + Vercel (frontend)|
| Google   | Google Ads REST API v23, Google Sheets API (service account via gspread)|

---

## Key files

```
backend/
  app.py                  — Flask app factory, blueprints, CORS, APScheduler
  database.py             — SQLAlchemy models (User, Account, Campaign, PacingData, …)
  google_ads_client.py    — All Google Ads API calls (OAuth, GAQL queries, budget mutations)
  routes/
    accounts.py           — CRUD for accounts + MCC browser + campaign sync
    campaigns.py          — Campaign CRUD + pacing data endpoint
    pacing.py             — Core pacing run logic (MTD spend vs target, budget recommendations)
    sheets.py             — Google Sheets: Google Ads section preview + sync/write-back (legacy Meta helpers still present)
    oauth.py              — Google OAuth2 flow (authorize, callback, disconnect)
    auth.py               — Session-based user auth (register, login, logout)
    settings.py           — Per-account settings (auto-pause threshold, sheet ID, etc.)
    leads.py              — Lead form submission pull + CSV export
    history.py            — Pacing run history

scripts/
  google_ads_budget_pacer.js  — REFERENCE ONLY. Original MCC script (Supabase pipeline).

frontend/src/
  App.jsx                 — Router, auth context, toast provider
  pages/
    Home.jsx              — Dashboard: account cards + inline rename + MCC import modal
    AccountDashboard.jsx  — Per-account view: campaigns, pacing charts, manual run
    CampaignDetail.jsx    — Single campaign detail + spend chart
    Settings.jsx          — Per-account settings (Google OAuth, Sheets, auto-pause, leads)
    History.jsx           — Pacing run history table
    Leads.jsx             — Lead form submissions table + CSV export
    Login.jsx / Register.jsx
  components/
    Sidebar.jsx, SpendChart.jsx, Skeleton.jsx, Toast.jsx, EmptyState.jsx
```

---

## Environment variables (backend)

| Variable                    | Description                                      |
|-----------------------------|--------------------------------------------------|
| `DATABASE_URL`              | PostgreSQL connection string (Neon or Railway)   |
| `SECRET_KEY`                | Flask session secret                             |
| `GOOGLE_ADS_CLIENT_ID`      | OAuth2 client ID from Google Cloud Console       |
| `GOOGLE_ADS_CLIENT_SECRET`  | OAuth2 client secret                             |
| `GOOGLE_ADS_DEVELOPER_TOKEN`| Google Ads developer token                       |
| `GOOGLE_ADS_MCC_ID`         | Default MCC customer ID (no dashes)              |
| `GOOGLE_CREDENTIALS_JSON`   | Service account JSON for Google Sheets (gspread) |
| `BACKEND_URL`               | Public backend URL (used for OAuth redirect URI) |
| `FRONTEND_URL`              | Public frontend URL (used for CORS + redirects)  |
| `SMTP_*`                    | Optional — for daily digest emails               |
| `CRON_SECRET`               | Optional — protects POST /api/cron/run-all-accounts |
| `WEBHOOK_API_KEY`           | Must match `WEBHOOK_API_KEY` in the MCC script — authenticates inbound webhook calls |

---

## Data flow

1. User connects Google account via OAuth → refresh token stored in `google_oauth_tokens`.
2. User imports accounts from MCC or adds manually → saved to `accounts`.
3. User links a Google Sheet per account → `account_settings.google_sheet_id`.
4. **Google Ads section sync** (`POST /api/sheets/<id>/sync-google-ads`):
   - Reads `[Account Name, Campaign Filter, Budget, MTD Spend]` rows
   - Tags campaigns with `budget_label` + `campaign_filter` for composite segmentation
   - Sets `monthly_budget` on each campaign (distributed by segment)
5. Pacing run (manual or scheduled via APScheduler at 06:00 UTC):
   - Syncs budgets from sheet first using the Google Ads section flow
   - Fetches MTD spend + clicks + conversions via GAQL
   - Calculates expected spend for days elapsed
   - Computes `pace_ratio = actual / expected`
   - Recommends daily budget adjustment
   - Optionally pushes new daily budget to Google Ads
   - Writes a `PacingData` row per campaign + a `PacingRun` audit record
   - Writes MTD spend back to sheet col D during both manual and scheduled runs
6. Dashboard reads latest `PacingData` per campaign to render status pills and spend bars.

---

## Business rules ported from the MCC script

These rules were in `google_ads_budget_pacer.js` and are now enforced in the Flask backend:

### A. Composite segmentation (`budget_label` / `campaign_filter`)
An account can have multiple rows in the Google Ads sheet section — one per budget segment. Each row has a Campaign Filter keyword (e.g. "IndyCar"). Campaigns whose name *contains* that keyword belong to that segment and share its budget. An **empty-filter row** is the "Primary" segment: it automatically claims all campaigns *not* matched by any named filter for that account.

`Campaign.budget_label` stores the segment name. `Campaign.campaign_filter` stores the keyword.

### B. Grant account bypass
Accounts whose `account_name` contains the string `"grant"` (case-insensitive) are **exempt from auto-pause**. Their spend is still tracked and displayed, but the pause threshold check is skipped. This prevents auto-pausing Google Grant campaigns that are allowed to exceed normal caps.

Implemented in: `routes/pacing.py` → `run_pacing()` and `app.py` → `_run_pacing_for_account()`.

### C. Phantom channel type filtering
`LOCAL_SERVICES`, `SMART`, `HOTEL`, and `LOCAL` channel type campaigns are excluded from all spend/budget calculations. These use non-standard budget structures and would inflate totals with phantom budgets.

Implemented in: `google_ads_client.py` → `list_campaigns()` and `get_campaign_mtd_spend()`.

### D. Dead campaign protection
Historical MTD spend from ended/paused campaigns is **included** in budget tracking (a campaign that spent money earlier in the month still counts). However, `PacingData.current_daily_budget` is set to `0` for non-ENABLED campaigns so the dashboard's "active daily budget" metric stays accurate.

### E. Composite unique_id (for reference)
The original script sent data to Supabase using `account_id + "_" + budget_label` as a composite primary key to prevent segment rows from overwriting each other. BudgetBuddy's DB uses separate `Campaign` rows per campaign, so this is naturally handled — no composite key needed.

---

## Google Ads sheet section format

The monthly tab (e.g. `May 2026`) in the linked Google Sheet should have a **"Google Ads"** section header in column A. Rows under it:

| Col A          | Col B             | Col C          | Col D         |
|----------------|-------------------|----------------|---------------|
| Account Name   | Campaign Filter   | Monthly Budget | MTD Spend     |
| Phoenix Raceway| IndyCar           | 1000           | ← written back|
| Phoenix Raceway| Brand             | 500            | ← written back|
| Phoenix Raceway| (blank = Primary) | 2000           | ← written back|

If no "Google Ads" header exists, BudgetBuddy reads the entire sheet as Google Ads rows (backward-compatible with the original script format).

---

## DB migration notes

The following columns were added in May 2026. They do NOT exist in older DB deployments and require manual `ALTER TABLE` statements:

```sql
-- Campaign segment tracking
ALTER TABLE campaigns ADD COLUMN budget_label VARCHAR(100);
ALTER TABLE campaigns ADD COLUMN campaign_filter VARCHAR(100);

-- Performance metrics on pacing snapshots
ALTER TABLE pacing_data ADD COLUMN clicks INTEGER;
ALTER TABLE pacing_data ADD COLUMN conversions FLOAT;
ALTER TABLE pacing_data ADD COLUMN cpc FLOAT;

-- Future duplicate guard after cleaning existing duplicate rows
-- 1) Pick one canonical row per (account_id, google_campaign_id)
-- 2) Move any history you want to keep, or leave old rows inactive for reference
-- 3) Add the uniqueness guard:
ALTER TABLE campaigns ADD CONSTRAINT uq_campaign_account_google_id UNIQUE (account_id, google_campaign_id);
```

On fresh deployments these are created automatically by `db.create_all()`.

---

## Change log

### 2026-05-19 — Canonical MTD spend source of truth
**What:** Made the current Google Ads API pull the source of truth for each pacing run and blocked stale/duplicate DB rows from re-entering MTD totals.
**Why:** Logs for Goodwill AZ - Retail Grant showed the live API aggregate was correct (`$6,021.08`), but duplicate campaign rows and stale `PacingData` could still leak into dashboard totals or sheet writeback.
**Changes:**
- `backend/database.py`: Added canonical campaign helpers so dashboards and live calculations use one row per `google_campaign_id`, restricted to the latest pacing date. Fresh DBs now include a uniqueness guard on `(account_id, google_campaign_id)`.
- `backend/routes/pacing.py`: Manual, scheduler, run-all, and MCC-triggered pacing now fetch spend for canonical campaigns only, replace same-day `PacingData` before writing fresh snapshots, and pass current-run segment totals directly to sheet writeback.
- `backend/routes/sheets.py`: Google Ads budget sync/writeback now match canonical campaigns only. Manual writeback no longer falls back to older pacing rows; it requires today's pacing data unless pacing passes current-run totals.
- `backend/routes/accounts.py`: Campaign import/MCC sync now update one canonical campaign row and mark older duplicates inactive instead of allowing more duplicate active rows.
- `backend/routes/campaigns.py`, `AccountDashboard.jsx`, `Notifications.jsx`: Dashboard-facing reads use latest-run/deduped campaign data; immediate post-run UI merges now include the pacing date.
- `backend/tests/test_spend_accuracy.py`: Added regression tests for duplicate campaign canonicalization, stale prior/same-day pacing rows, and current-run sheet writeback totals.

### 2026-05-18 — Google Ads sheet flow wired into live pacing
**What:** Made the live pacing flow use the Google Ads sheet section consistently for preview, budget sync, and spend writeback.
**Why:** The app had the new Google Ads section helpers, but manual runs, scheduler syncs, and the Settings preview were still partially pointed at older sheet logic, which made budgets and current spend look stale or missing.
**Changes:**
- `backend/routes/sheets.py`: Added Google Ads preview helper, kept `/api/sheets/<id>/preview` but made it return Google Ads section matches, and added `sync_sheet_budgets_for_account()` / `write_sheet_spend_for_account()` as the primary live entry points.
- `backend/routes/sheets.py`: Added INFO-level logging around Google Ads row discovery, row-to-campaign matching, skips, and spend writeback so Railway logs can explain why a specific account did or did not sync.
- `backend/routes/pacing.py`: `POST /api/pacing/<id>/run` now syncs budgets through the Google Ads flow and writes spend back to the sheet after a successful run.
- `backend/routes/pacing.py` and `backend/app.py`: Added INFO-level pacing start logs that include account name, customer ID, MCC ID, and whether a sheet ID is configured, plus more explicit spend-fetch failure context for Google Ads permission issues.
- `backend/routes/pacing.py` and `backend/app.py`: Pacing now falls back to the global `GOOGLE_ADS_MCC_ID` env var when an account record is missing its own `mcc_customer_id`, which helps older imported accounts query Google Ads through the default manager account.
- `backend/app.py`: Scheduler now uses the same Google Ads-first sheet sync and spend writeback path as manual pacing, and only one Gunicorn worker should start APScheduler in production via a Postgres advisory lock.
- `backend/routes/settings.py`: Added `POST /api/settings/apply-sheet-to-all` so one Google Sheet ID can be applied across every account in the workspace.
- `frontend/src/pages/Settings.jsx`: Fixed sheet preview rendering to read the backend response shape correctly.
- `frontend/src/pages/AccountDashboard.jsx`: Dashboard totals now refresh immediately after a pacing run, sheet writeback warnings/successes are surfaced in the UI, and accounts without a saved Sheet ID show an explicit warning before/after pacing runs.
- `frontend/src/pages/Home.jsx`: Added a shared-sheet control on the dashboard so the team can paste one sheet ID/URL and apply it to all accounts at once.

### 2026-05-18 — Script replacement + composite segmentation
**What:** Replaced the Google Ads MCC Script (Supabase pipeline) with BudgetBuddy handling everything natively. Ported all business rules from the script into the Flask backend.
**Why:** Consolidate to one system; add a proper UI; make pacing data accessible beyond the spreadsheet.
**Changes:**
- `backend/google_ads_client.py`: Added phantom channel type filtering (`LOCAL_SERVICES`, `SMART`, `HOTEL`, `LOCAL` excluded). `get_campaign_mtd_spend` now returns `{cid: {spend, clicks, conversions}}` instead of `{cid: spend_float}`.
- `backend/database.py`: Added `budget_label`, `campaign_filter` to `Campaign`. Added `clicks`, `conversions`, `cpc` to `PacingData`.
- `backend/routes/pacing.py`: Grant account bypass (skips auto-pause for accounts with "grant" in name). Updated to unpack new spend dict shape. Stores clicks/conversions/CPC on `PacingData`.
- `backend/routes/sheets.py`: Added `_get_google_ads_section()`, `sync_google_ads_budgets_for_account()`, `write_google_ads_spend_for_account()` and endpoints `POST /api/sheets/<id>/sync-google-ads` and `POST /api/sheets/<id>/write-google-ads-spend`.
- `backend/app.py`: Updated scheduler's `_run_pacing_for_account` for new spend dict shape + Grant bypass.
- `scripts/google_ads_budget_pacer.js`: Original script stored for reference (do not deploy).

### 2026-05-18 — MCC account name fix + inline rename
**What:** Fixed accounts imported from MCC showing only numeric IDs instead of real names.
**Why:** `customer_client.descriptive_name` is empty for some account types (test accounts, recently-created accounts). The old code silently fell back to `"Account <id>"`.
**Changes:**
- `backend/google_ads_client.py`: Added `_fetch_customer_name()` — makes a secondary per-account GAQL query (`SELECT customer.descriptive_name FROM customer`) when `descriptiveName` is missing. Added `_fmt_customer_id()` as a final XXX-XXX-XXXX fallback. Updated `list_mcc_child_accounts` to use both.
- `frontend/src/pages/Home.jsx`: `ImportMccModal` now renders each account name as an editable input so users can fix names before clicking Import. Home page account cards now have an inline pencil-icon rename flow (click → edit field → ✓ / ✗), saved via `PUT /api/accounts/:id`.

---

## Imported Claude Cowork project instructions

This is a budget pacing tool for Google Ads
