# CLAUDE.md — Google BudgetBuddy

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
    sheets.py             — Google Sheets: Meta section + Google Ads section sync/write-back
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
   - Syncs budgets from sheet first (Google Ads section)
   - Fetches MTD spend + clicks + conversions via GAQL
   - Calculates expected spend for days elapsed
   - Computes `pace_ratio = actual / expected`
   - Recommends daily budget adjustment
   - Optionally pushes new daily budget to Google Ads
   - Writes a `PacingData` row per campaign + a `PacingRun` audit record
   - Writes MTD spend back to sheet col D
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
```

On fresh deployments these are created automatically by `db.create_all()`.

---

## Change log

### 2026-05-19 — Fix "Set daily to $0" + math mismatch with Google Sheet
**What:** Fixed two related bugs causing every account to show "Set daily to $0" and recommended budget numbers that didn't match the Google Sheet.
**Bug 1 — Inactive campaign overwrites segment budget:** `seg_budget_map` (pacing.py) and `segBudgets`/`getSegments` (Home.jsx, AccountDashboard.jsx) used last-value-wins when iterating campaigns in a segment. An inactive campaign with `monthly_budget=0` (excluded from the sheet sync) appearing after an active campaign silently reset the segment budget to 0, causing `max(0, budget - spend) = 0`. Fixed by using `Math.max` so only the highest (correct) budget in a segment is used.
**Bug 2 — Sheet budget sync excluded inactive campaigns:** `sync_google_ads_budgets_for_account` only queried `is_active=True` campaigns. Paused/ended campaigns that still showed MTD spend never got `monthly_budget` or `budget_label` set, perpetuating the overwrite issue. Fixed by querying all campaigns for the account.
**Bug 3 — Recommended daily formula didn't match sheet:** Backend `_compute_recommendation` and frontend `computePace` divided remaining budget by `days_remaining` (~12), but the Google Sheet uses `days_in_month` (31): `=(Budget - Spend) / total_days`. Changed both to divide by `days_in_month` so numbers are consistent with the sheet.
**Changes:**
- `backend/routes/pacing.py`: `seg_budget_map` assignment now uses max (both `run_pacing` and `run_pacing_for_account`). `_compute_recommendation` now divides by `days_in_month`. Updated docstring + module comment.
- `backend/routes/sheets.py`: `sync_google_ads_budgets_for_account` now queries all campaigns (not just `is_active=True`).
- `frontend/src/pages/Home.jsx`: `segBudgets` and `getSegments` now use `Math.max`. `computePace.dailyRec` now divides by `daysInMonth`.
- `frontend/src/pages/AccountDashboard.jsx`: Same `computePace` and `getSegments` fixes applied.

### 2026-05-18 — Dashboard shows only campaigns that spent this month
**What:** Old campaigns kept appearing on both the Home dashboard and per-account view. Google Ads leaves campaigns set to ENABLED for years after they stop running, so the old "is_active OR spent-this-month" filter still let zombies through.
**Why:** `is_active` is unreliable — it just mirrors the Google Ads status flag. The only trustworthy signal that a campaign actually ran is spend > 0 this calendar month. Trade-off accepted: brand-new campaigns with $0 spend won't show until their first spend (usually same day).
**Changes:**
- `backend/database.py`: Added `Campaign.has_spend_this_month()` and `Campaign.is_visible()` helpers. `is_visible()` returns `has_spend_this_month()` only — the `is_active` OR branch was removed because ENABLED status alone wasn't a useful signal. `Account.to_dict()` now uses `is_visible()` for both the summary calcs (`campaign_count`, `total_monthly_budget`, `pacing_status`) and the `campaigns` array.
- `backend/routes/campaigns.py`: Removed the local `_has_spend_this_month` duplicate; `get_campaigns()` now calls `c.is_visible()`. Dropped the now-unused top-level `from datetime import datetime`.

### 2026-05-19 — run-all timeout fix (background thread + Gunicorn bump)
**What:** Fixed Gunicorn worker timeout killing the "Run All" pacing job on larger MCCs.
**Why:** With 15+ accounts, sequential Google Ads API calls exceed the old 120s worker limit.
**Changes:**
- `backend/Procfile`: Bumped `--timeout 120` → `--timeout 300` as a safety net.
- `backend/routes/pacing.py`: `run-all` now mirrors the MCC sync pattern — acquires `_pacing_all_lock`, spawns `_run_pacing_all_job()` in a background thread, returns 202 immediately. Returns 409 if already running. The background job calls `run_pacing_for_account()` per account (sheet sync + spend fetch + PacingData write).

### 2026-05-18 — Campaign liveness filter + paused-but-spending inclusion
**What:** Fixed two related campaign filtering bugs.
**Bug 1 — Zombie ENABLED campaigns:** Campaigns with status=ENABLED but a past `campaign.end_date` were being treated as live and included in pacing. Fix: `list_campaigns()` now fetches `campaign.end_date`; a new `_is_campaign_live()` helper in `accounts.py` sets `is_active = True` only for ENABLED campaigns with no end date or a future end date.
**Bug 2 — PAUSED campaigns silently excluded:** PAUSED campaigns always got `is_active = False`, so spend they racked up earlier in the month was invisible. Fix: pacing runs now fetch MTD spend for ALL campaigns (live + inactive), then include inactive ones where `spend > 0` this month.
**Changes:**
- `backend/google_ads_client.py`: Added `campaign.end_date` to `list_campaigns()` SELECT; returned as `end_date` in each campaign dict.
- `backend/routes/accounts.py`: Added `_is_campaign_live(lc)` helper. All sync paths (`_sync_all_campaigns_for_account`, MCC sync loop, `import_campaigns`) use it instead of hardcoding `is_active=True`.
- `backend/routes/pacing.py`: `run_pacing`, `run_all_pacing`, `run_pacing_for_account` all split into `live_campaigns` + `inactive_campaigns`, fetch spend for all, then build `active_campaigns = live_campaigns + spending_inactive`.

### 2026-05-18 — Segment budget fix: full budget stored per campaign + segment-level pacing
**What:** Fixed two related bugs that caused incorrect budget figures and wrong pacing calculations for multi-campaign segments.
**Bug 1 — Budget division:** `sync_google_ads_budgets_for_account` was dividing the sheet's segment budget by the number of matched campaigns (`budget / len(matched)`) before storing it on each campaign. A $1000 IndyCar budget with 3 campaigns showed as $333.33 on the dashboard.
**Bug 2 — Per-campaign pacing against full segment budget:** Even with the correct budget, pacing each campaign individually against the full segment budget made every campaign appear severely underpaced (each campaign's $200 spend vs a $1000 budget = 20% pace, when the segment as a whole is at 60%).
**Fix:**
- `backend/routes/sheets.py` → `sync_google_ads_budgets_for_account`: Removed the `/len(matched)` division. Each campaign in a segment now carries the **full** segment budget.
- `backend/routes/pacing.py` → `run_pacing` and `run_pacing_for_account`: Added a pre-loop segment aggregation pass (`seg_spend_map`, `seg_budget_map`, `seg_count_map`). Pacing is now computed at the segment level (segment total spend vs segment budget → `pace_ratio`, `recommended_daily`). The segment-level recommended daily budget is then split equally across campaigns in the segment.

### 2026-05-19 — Home dashboard spend fix + MCC sync now runs pacing
**What:** Fixed two bugs causing the home dashboard to show 0/0 spent for all accounts.
**Bug 1 — Missing campaigns in API response:** `Account.to_dict()` never included the `campaigns` array, so the home page's `/api/campaigns/all` response had `account.campaigns = undefined`. The frontend silently fell back to `[]` for every account → 0 spend, 0 budget. Fix: added `'campaigns': [c.to_dict() for c in self.campaigns]` to `Account.to_dict()` in `database.py`.
**Bug 2 — MCC sync didn't run pacing:** The home "Sync" button (`POST /api/accounts/sync-from-mcc`) synced campaigns and sheet budgets but never fetched MTD spend from Google Ads. Fix: moved `_run_pacing_for_account` from `app.py` into `routes/pacing.py` as `run_pacing_for_account(account, refresh_token_str, triggered_by)`, then called it at the end of `_run_mcc_sync_job` after sheet sync. The scheduler in `app.py` was also updated to import from `routes.pacing`.
**Changes:**
- `backend/database.py`: Added `'campaigns'` key to `Account.to_dict()`.
- `backend/routes/pacing.py`: Added `run_pacing_for_account()` at bottom of file.
- `backend/app.py`: Deleted inline `_run_pacing_for_account()`. Scheduler now calls `from routes.pacing import run_pacing_for_account`.
- `backend/routes/accounts.py`: Added pacing step at end of `_run_mcc_sync_job`. Final log includes paced count.

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
