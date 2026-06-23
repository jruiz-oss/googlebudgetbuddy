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
   - Computes sheet-style `pace_ratio = actual_spend / monthly_budget`
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
ALTER TABLE campaigns ADD COLUMN current_daily_budget FLOAT;

-- Performance metrics on pacing snapshots
ALTER TABLE pacing_data ADD COLUMN clicks INTEGER;
ALTER TABLE pacing_data ADD COLUMN conversions FLOAT;
ALTER TABLE pacing_data ADD COLUMN cpc FLOAT;

-- Zombie campaign filter (May 2026)
ALTER TABLE campaigns ADD COLUMN google_end_date DATE;

-- Future duplicate guard after cleaning existing duplicate rows
-- 1) Pick one canonical row per (account_id, google_campaign_id)
-- 2) Move any history you want to keep, or leave old rows inactive for reference
-- 3) Add the uniqueness guard:
ALTER TABLE campaigns ADD CONSTRAINT uq_campaign_account_google_id UNIQUE (account_id, google_campaign_id);
```

On fresh deployments these are created automatically by `db.create_all()`. On Postgres deployments, app startup also runs lightweight `ADD COLUMN IF NOT EXISTS` checks for the additive columns above so a missed manual migration does not break account loading. The SQL remains useful for manual repair or one-off DB consoles.

---

## Change log

### 2026-06-23 — Account lockdown ("must stay OFF") kill-switch
**What:** New per-account `lockdown_enabled` flag for accounts that are supposed to spend $0. When locked, the hourly job pauses **every** campaign the moment any MTD spend > $0 is detected. Locked accounts also always show all their campaigns on the dashboard (regardless of spend) so the user can confirm everything is off.
**Behavior:**
- Trigger = any MTD spend > $0 (one penny). When triggered, pauses every currently-ENABLED campaign in the account and writes a `PauseEvent(triggered_by='LOCKDOWN')`.
- **Overrides** the Grant bypass (rule B) AND `auto_pause_enabled` — a locked account is enforced even if auto-pause is off. The lockdown branch runs before both guards in the hourly job.
- Enforced by the existing hourly auto-pause job (`:30` past each hour) — max ~1hr exposure. The daily/2-hourly pacing run is unchanged (no pause there).
**Changes:**
- `backend/database.py`: `AccountSettings.lockdown_enabled` (Boolean, default False) + in `to_dict()`. `visible_latest_campaigns()` gained a `show_all` param that bypasses the spend gate; `Account.to_dict()` passes `show_all=settings.lockdown_enabled`.
- `backend/routes/settings.py`: PUT accepts `lockdown_enabled`.
- `backend/app.py`: `_hourly_auto_pause_job` lockdown branch (before grant/auto_pause guards); imports `canonical_campaigns`. Startup migration `ALTER TABLE account_settings ADD COLUMN IF NOT EXISTS lockdown_enabled BOOLEAN DEFAULT FALSE`.
- `frontend/src/pages/Settings.jsx`: "Lockdown — Must Stay OFF" toggle card.
- `frontend/src/pages/Home.jsx`: 🔒 LOCKED badge on locked account headers.
**Watch:** Lockdown sums spend across ALL canonical campaigns' metrics (incl. paused-but-spent), not just the active set, so a campaign that spent earlier in the month and was already paused still triggers the event.

### 2026-06-09 — "Copy for Sheets" button on Leads page
**What:** One-click copy of pulled leads as TSV to the clipboard — pasting into Google Sheets splits into columns. Same dynamic columns as the Excel export (base + extra standard fields + custom questions). Frontend-only (`frontend/src/pages/Leads.jsx`, `copyForSheets()`); tabs/newlines inside answers are flattened to spaces so they can't break rows.

### 2026-06-09 — Lead export is now a real Excel .xlsx (was CSV)
**What:** Exported CSVs opened in the user's code editor (file association) and read as raw text. Export now produces a real `.xlsx` so it always opens in Excel/Numbers.
**Changes:**
- `backend/requirements.txt`: Added `openpyxl==3.1.5`.
- `backend/routes/leads.py`: `/export` builds an openpyxl Workbook (bold header, auto column widths, same dynamic answer columns) and returns it with the xlsx mimetype. Removed `csv` import.
- `frontend/src/pages/Leads.jsx`: Download filename `.xlsx`; button renamed "Export Excel".
**Deploy note:** Railway must reinstall deps (`openpyxl`) — happens automatically on deploy from requirements.txt.

### 2026-06-09 — Surface all submitted lead form answers (table + CSV)
**What:** Lead answers beyond name/email/phone/city (extra standard fields like postal code/company, plus custom question answers) were captured but never shown or exported.
**Changes:**
- `backend/routes/leads.py`: CSV export now appends one column per extra standard field (union across leads, Title Cased) and one column per custom question text, after the base columns.
- `frontend/src/pages/Leads.jsx`: Added an "Answers" column listing each extra field/custom Q&A per lead; `extraAnswers()` helper merges `fields` (minus base four) + `custom_fields`.
**Note:** Columns are dynamic — exports from different accounts/forms will have different headers.

### 2026-06-09 — Fix CSV export navigating to home instead of downloading
**What:** "Export CSV" on the Leads page redirected to the home dashboard instead of saving a file.
**Root cause:** `exportCsv()` used `window.open('/api/leads/<id>/export?…')` with a *relative* URL. Axios calls carry `baseURL = VITE_API_URL` (Railway backend), but `window.open` resolved against the Vercel frontend origin, where no `/api` routes exist — the SPA catch-all rendered Home.
**Fix:** `frontend/src/pages/Leads.jsx` — export now fetches via axios (`responseType: 'blob'`, session cookie + backend baseURL included) and triggers the download with a temporary object-URL anchor. Errors surface as a toast.
**Watch:** Any other `window.open`/`<a href>` pointing at `/api/...` has the same bug — always go through axios or prefix with the API base.

### 2026-06-09 — Lead pull returning 0: diagnostics + custom-field capture
**What:** After the 403 fix, lead pulls returned HTTP 200 with `count: 0` for every date range on an account confirmed to use native lead form assets. A silent zero is ambiguous, so `/pull` now self-diagnoses.
**Changes:**
- `backend/google_ads_client.py`: Added `diagnose_lead_form_setup()` — two probes: (1) count of `LEAD_FORM` assets on the customer, (2) unfiltered `lead_form_submission_data` query (LIMIT 50) with sample `submission_date_time` values. Distinguishes "no lead form assets visible / wrong customer ID" vs "assets exist, zero submissions in API (60-day retention or visibility)" vs "submissions exist but the date filter drops them" (sample timestamps expose the format mismatch). Also: main pull query now selects `custom_lead_form_submission_fields` and returns them as `custom_fields` per lead — custom questions were previously dropped.
- `backend/routes/leads.py`: `/pull` runs the diagnostic when 0 leads come back, logs it (`Lead pull diagnostics for account …`), and returns `diagnostic` (human-readable) + `diagnostic_data` (raw) in the response. Best-effort — diagnostics never fail the pull.
- `frontend/src/pages/Leads.jsx`: Shows the diagnostic in a callout under "No leads found".
**Known constraint:** The Google Ads API retains lead form submissions for 60 days (UI download: only 30 days, per https://support.google.com/google-ads/answer/12080108). Leads older than 60 days are unrecoverable via API.
**Next step:** Re-run a pull and read the "Why" callout to pick the real fix.

### 2026-06-09 — Fix lead pull 403 USER_PERMISSION_DENIED (missing login-customer-id fallback)
**What:** Pulling leads failed with `403 PERMISSION_DENIED / USER_PERMISSION_DENIED` ("User doesn't have permission to access customer…") on accounts where pacing worked fine.
**Root cause:** `routes/pacing.py` has `_effective_mcc_customer_id(account)`, which falls back to the `GOOGLE_ADS_MCC_ID` env var when `account.mcc_customer_id` is null. Every pacing call uses it. But `routes/leads.py`, `routes/reports.py`, and the `pause_campaigns` call in `app.py`'s hourly auto-pause passed `account.mcc_customer_id` **directly** with no fallback. For any account with a null `mcc_customer_id`, no `login-customer-id` (manager) header was sent, so Google treated it as direct client access and denied it. Pacing succeeded on the same account only because of the env fallback.
**Fix:**
- `backend/routes/leads.py`: Added a local `_effective_mcc_customer_id()` (mirrors pacing) + `import os`. Both `/pull` and `/export` now use it.
- `backend/routes/reports.py`: Added the same helper; both Google Ads pulls in `/generate` (`get_campaign_spend_for_period`, `get_top_search_terms`) now use it.
- `backend/app.py`: Hourly auto-pause now imports `_effective_mcc_customer_id` from `routes.pacing` and uses it for the `pause_campaigns` call.
**Watch:** Any new Google Ads call should route its MCC id through `_effective_mcc_customer_id(account)`, never `account.mcc_customer_id` raw.

### 2026-06-03 — Monthly AI summaries
**What:** Added a "Monthly Summary" button to each account dashboard that opens a modal where you type notes about what happened (strategy shifts, copy changes, etc.), then click Generate. Claude pulls top search terms from Google Ads for the month, combines them with pacing data and your notes, and writes a 2–4 paragraph narrative — not a metrics readout. Output is editable and saved per account per month. Month/year picker for historical access.
**New files:**
- `backend/routes/reports.py` — CRUD for monthly reports + `/generate` endpoint (Anthropic API call)
- `backend/google_ads_client.py` — added `get_top_search_terms()` via `search_term_view` GAQL
- `database.py` — `MonthlyReport` (account_id, year, month, notes, generated_summary) + `UserSettings` (anthropic_api_key per user)
**Settings:** Anthropic API key stored per-user in `UserSettings`. Add via Settings → AI Summaries.
**Env:** `ANTHROPIC_API_KEY` can be set server-side as fallback. Per-user key takes precedence.
**Migration:** `app.py` creates `user_settings` and `monthly_reports` tables via `CREATE TABLE IF NOT EXISTS` on boot.



### 2026-06-01 — Fix $0 current daily budget for ENABLED campaigns with stale is_active flag
**What:** Campaigns that are ENABLED in Google Ads but marked `is_active=False` in the DB showed $0 for "Current Daily" on the dashboard, while their real Google Ads daily budgets were being ignored.
**Root cause:** `_current_daily_for_run()` in `routes/pacing.py` checked `if not campaign.is_active: return 0.0` *before* checking the API value. So any campaign with a stale `is_active=False` (e.g. was paused in a prior sync but re-enabled since) would write `current_daily_budget=0` to its `PacingData` row even though the Google Ads API returned a valid budget. The dashboard then displayed $0 and the share % showed 0.0%.
**Also fixed:** `update_campaign_budget()` in `google_ads_client.py` now floors the new daily budget to a minimum of $0.01 (10,000 micros) before sending to Google Ads, preventing `MONEY_AMOUNT_LESS_THAN_CURRENCY_MINIMUM_CPC` 400 errors when a recommended budget rounds to $0.
**Fix:**
- `backend/routes/pacing.py`: `_current_daily_for_run()` — moved `is_active` guard after the API lookup. API value is now authoritative regardless of DB `is_active` state; `is_active=False` only returns 0 when there is genuinely no API data.
- `backend/google_ads_client.py`: `update_campaign_budget()` — clamps `new_daily_usd` to `max(new_daily_usd, 0.01)` before converting to micros.

### 2026-06-01 — Fix campaign sync failing on all accounts: `campaign.end_date` renamed in API v23
**What:** MCC campaign sync was failing for **every** account ("MCC sync campaigns: 0 added, 0 updated across 28 accounts"). Live/new campaigns never made it into the DB, so accounts with recently-launched campaigns (e.g. Skytop Lodge) showed stale/missing campaigns while accounts whose campaigns were already cached looked fine.
**Root cause:** As of Google Ads API **v23**, `campaign.end_date` was renamed to `campaign.end_date_time` (and `campaign.start_date` → `campaign.start_date_time`). The two GAQL queries in `google_ads_client.py` still selected `campaign.end_date`, so every `list_campaigns()` / status query returned `400 INVALID_ARGUMENT` / `queryError: UNRECOGNIZED_FIELD` ("Unrecognized field in the query: 'campaign.end_date'."). `accounts.py` caught this as "Campaign fetch failed for account N" and skipped the account. This was a silent regression introduced when `campaign.end_date` was added to the SELECT (2026-05-18), surfacing once the deployment hit v23.
**Fix:**
- `backend/google_ads_client.py`: Both queries (`list_campaigns()` ~line 248 and the status query in `get_campaign_mtd_spend()` ~line 336) now select `campaign.end_date_time`. Added `_date_part()` helper that normalizes the new datetime value (`'YYYY-MM-DD HH:MM:SS'` / ISO) back to a plain `'YYYY-MM-DD'` string, so downstream consumers (`accounts.py._parse_google_end_date` / `_is_campaign_live`, `database.py`) are unchanged. Response keys are now read as `endDateTime` instead of `endDate`. Also corrected the stale module docstring (v18 → v23).
**Note:** Removing the field instead of fixing it was rejected — `end_date` powers the zombie/ended-campaign filter (`_is_campaign_live`, `_is_zombie_campaign`, `visible_latest_campaigns`); dropping it would re-admit long-ended ENABLED campaigns as "live."
**Watch:** Other resources may have the same `_date` → `_date_time` rename if more fields are added later.

### 2026-06-01 — Fix month rollover: dashboard showed last month's campaigns/spend + "day 2 of 30" on the 1st
**What:** At the start of a new month, before the first pacing run of that month, the dashboard kept showing campaigns that only spent *last* month and counted their prior-month spend as current MTD. Separately, the "day X of Y" label read "day 2 of 30" on the 1st.
**Root cause (visibility/MTD):** `visible_latest_campaigns`, `latest_pacing_date`, `campaign_mtd_spend_total`, and `segment_spend_summaries` keyed off the *globally* latest `PacingData` date regardless of calendar month. On e.g. June 1 (no June run yet) that latest date was May 31, so every campaign with May 31 spend looked like it was "spending now" and its May spend was summed as June MTD. `Campaign.has_spend_this_month()` already gated on `date >= month_start`, but the dashboard path used the unscoped functions instead — an inconsistency.
**Root cause (day label):** `daysIn = max(getDate()-1, 1)` is floored to 1 for divide-by-zero safety (spend is through EOD yesterday). The label rendered `daysIn + 1`, so on the 1st it showed `1 + 1 = 2`.
**Fix:**
- `backend/database.py`: Added `current_month_start()`. `latest_pacing_date`, `_campaign_latest_pacing`, `campaign_mtd_spend_total`, `segment_spend_summaries`, and `visible_latest_campaigns` now take an optional `month_start` (defaulting to the current UTC month) and ignore pacing rows before it. `visible_latest_campaigns`'s `has_ever_been_paced` is now scoped to the current month. `Account.to_dict()` computes `month_start` once and threads it through. Result: at a new month's start, MTD = 0 and only truly-new live campaigns show until the first run writes in-month data; it then self-corrects.
- `frontend/src/pages/Home.jsx`, `AccountDashboard.jsx`: `getDaysInfo()` now also returns `dayOfMonth` (`today.getDate()`); the "day X of Y" labels use `dayOfMonth` instead of `daysIn + 1`. `daysIn` (floored) is unchanged for pacing math.
- `backend/tests/test_spend_accuracy.py`: Existing May fixtures now pass an explicit `month_start=date(2026,5,1)` (deterministic regardless of run date). Added `test_month_rollover_excludes_prior_month_spend` and `test_month_rollover_includes_current_month_spend`.
**Note:** Pre-existing unrelated test `test_pace_ratio_matches_sheet_budget_used_percent` fails (asserts the old `/days_in_month` recommendation while `_compute_recommendation` divides by `days_remaining`). Not touched here.

### 2026-05-31 — Zero-budget accounts pause on any spend at the 100% threshold
**What:** When an account has **no budget** (segment total `$0`/blank) but is spending, it now gets paused — provided its `auto_pause_threshold` is set to **100%**. Previously both the hourly pause job and the daily warning check skipped any account with `total_budget <= 0` outright (couldn't divide by a zero budget), so a campaign with no budget row could spend freely regardless of the auto-pause setting.
**Behavior:**
- At a **100%** threshold, any spend > $0 against a $0/missing budget is treated as over a $0 cap → pause every active campaign + write an `AUTO` `PauseEvent` (hourly job), and surface an `auto_pause_warning` (daily run).
- At **any lower threshold (50–99%)**, a zero/missing budget is still skipped — a percentage of zero is undefined.
- Grant accounts remain exempt (business rule B); `auto_pause_enabled = False` still skips entirely.
**Caveat:** A $0 budget is indistinguishable from a budget sheet that hasn't synced or failed to sync. Gating this to the strictest 100% threshold limits blast radius, and both the pause and the warning log a "confirm the budget sheet synced" note. If false pauses become a problem, the next step is distinguishing "explicit 0" from "missing row" at sheet-sync time.
**Changes:**
- `backend/app.py`: `_hourly_auto_pause_job` — replaced the `if total_budget <= 0: continue` guard with an `else` branch that pauses when `auto_pause_threshold >= 100 and total_spend > 0`.
- `backend/routes/pacing.py`: `run_pacing` auto-pause check — added the matching `elif` so the dashboard warning is consistent with the hourly pause.

### 2026-05-31 — Hourly auto-pause for over-budget accounts
**What:** Added a second APScheduler job that runs **every hour at :30** and actually pauses campaigns for any account at/over its auto-pause threshold. Previously the only scheduled check was the 06:00 UTC daily pacing run, and "auto-pause" was warning-only (the daily run set an `auto_pause_warning` flag but never paused — the sole real pause path was the manual `/pause` button). A campaign could blow past its cap mid-day and keep spending until the next morning.
**Behavior:**
- For each account, fetches CURRENT MTD spend via the shared `_execute_pacing_run()` core (one Google Ads call, no sheet sync/writeback), then compares segment-level total spend vs total budget using the same math as the daily threshold check.
- If `spend_pct >= auto_pause_threshold`, pauses every active campaign via `pause_campaigns()` and writes a `PauseEvent(triggered_by='AUTO')`.
- **Skips:** accounts with `auto_pause_enabled = False`, Grant accounts (business rule B), accounts with no valid OAuth token, and accounts with no budget.
- Once paused, an account has no active campaigns left, so subsequent hourly passes are no-ops — no duplicate `PauseEvent` spam.
- Alerts are **log + dashboard/DB only** (no email); over-budget pauses appear in pause history.
**Changes:**
- `backend/app.py`: Added `_hourly_auto_pause_job(app)`. Registered as a cron job (`minute=30`) in the scheduler block, gated by the existing production check plus a new `DISABLE_HOURLY_AUTOPAUSE` env flag. Runs only in the worker holding the APScheduler advisory lock.
**Env:** `DISABLE_HOURLY_AUTOPAUSE` — set to `'true'` to disable just the hourly job (daily run still fires).

### 2026-05-29 — Fix Sync & Pace "Done!" reported while still running (cross-worker state)
**What:** Fixed the home "Sync & Pace All" button showing "complete" prematurely, then returning "already in progress" (409) on the next click.
**Root cause:** Gunicorn runs `--workers 2`. Run-all progress was tracked with a module-level `threading.Lock` (`_pacing_all_lock`) and an in-memory dict (`_pacing_all_progress`), which only exist inside ONE worker process. The background job ran in worker A, but `GET /run-all/status` polls were load-balanced to worker B — where the lock was free — so the frontend saw `running:false` and fired the success toast while worker A was still pacing. A follow-up click that landed on worker A then hit the still-held lock and got a 409.
**Fix:** Persist run-all state in Postgres so all workers agree.
- `backend/database.py`: Added `JobState` model (`job_state` table) — `job_key`, `is_running`, `completed`, `total`, `started_at`, `heartbeat`, `finished_at`. `is_stale()` treats a job whose heartbeat is older than `STALE_AFTER_SECONDS` (300, matching gunicorn `--timeout`) as not-running so a crashed worker can't lock the UI forever. Auto-created by `db.create_all()` on boot (new table, not a column).
- `backend/routes/pacing.py`: Removed `_pacing_all_progress`. Added `_claim_run_all_job()` (atomic claim via `SELECT … FOR UPDATE`, with SQLite fallback; reclaims stale runs), `_update_run_all_progress()` (writes progress + heartbeat per account), and `_finish_run_all_job()` (always clears the flag in the worker's `finally`). `run_all_pacing` now claims via DB instead of the in-memory lock; `run_all_status` reads the shared `JobState` row. `_pacing_all_lock` is kept only as a same-process guard around the claim.
**No frontend change needed** — `Home.jsx` polling was already correct; it was being fed a per-process lie.

### 2026-05-20 — Fix run-all pacing: stale session objects causing MTD spend duplication
**What:** Fixed `_run_pacing_all_job` operating on expired SQLAlchemy objects for accounts 2+ in the loop, which caused segment spend maps and `_is_zombie_campaign()` to produce incorrect results (doubled MTD spend, wrong recommendations).
**Root cause:** `db.session.commit()` inside `run_pacing_for_account` triggers `expire_on_commit=True`, expiring every ORM object in the session. The next account in the loop used the now-expired Account object from the initial bulk load. Accessing `account.campaigns` lazy-loaded Campaign rows *without* `pacing_data` pre-loaded, so `canonical_campaigns()` and `_is_zombie_campaign()` fired N+1 lazy queries against a session that still had in-memory references to rows deleted by `_delete_today_pacing_data(..., synchronize_session=False)`. This left orphaned objects in the identity map and caused stale data to feed the segment aggregation maps. Additionally, if `sync_sheet_budgets_for_account` threw an exception, the reload was bypassed entirely.
**Fix:** Changed `_run_pacing_all_job` to fetch only account IDs up front, then reload each account fresh with `selectinload(Account.campaigns).selectinload(Campaign.pacing_data)` at the *start* of every loop iteration — before sheet sync, not only inside the `if settings.google_sheet_id` block. Also always reloads after sheet sync attempt (success or failure) so `monthly_budget` values are current.
**Changes:**
- `backend/routes/pacing.py`: `_run_pacing_all_job` now queries only IDs, then loads each account fresh per iteration with full selectinload. Removed the old conditional reload that only fired on sheet sync success.

### 2026-05-19 — Yesterday trend badge on pace %
**What:** Added a "↑ Improving · was X%" / "↓ Worsening · was X%" trend indicator wherever pace % is shown — on Home page account cards and the AccountDashboard header + Pace stat.
**Why:** Knowing the current pace % in isolation doesn't tell you if things are getting better or worse. The trend badge shows direction by comparing today's % DIFF against yesterday's.
**How it works:** Backend `Campaign.to_dict()` now includes `prev_pacing` — the most recent pacing row on a prior date. Frontend uses `prev_pacing.actual_spend` aggregated across deduped campaigns to recompute yesterday's `deltaPct` (same `computePace()` formula, with `daysIn - 1`). "Improving" = today's |deltaPct| is >0.5pp smaller than yesterday's; "Worsening" = >0.5pp larger; "Stable" = within that threshold.
**Changes:**
- `backend/database.py`: `Campaign.to_dict()` includes `prev_pacing` (previous distinct-date pacing row).
- `frontend/src/pages/Home.jsx`: Added `accountPrevPacing()`, `trendDirection()`, `TrendBadge` component. `AccountCard` renders `TrendBadge` below the pace pill.
- `frontend/src/pages/AccountDashboard.jsx`: Added `trendDirection()`, `TrendBadge`. `prevDeltaPct` computed from campaigns' `prev_pacing`. Trend shown in page header (inline) and Pace stat grid cell.
- `frontend/src/index.css`: Added `.trend-badge` + `.improving` / `.worsening` / `.stable` styles.

### 2026-05-19 — Exclude zombie campaigns (ended before this month, $0 spend)
**What:** Campaigns that ended in a prior month and have no MTD spend this month are now excluded from both dashboard views and pacing runs.
**Why:** Google Ads frequently leaves campaigns ENABLED years after their end_date passes. Relying on `is_active` alone wasn't sufficient because campaigns synced before the liveness-check fix still had `is_active=True`. These zombies inflated segment counts, distorted budget ratios, and cluttered the dashboard.
**Rule:** A campaign is excluded when `google_end_date < start_of_this_month AND no_spend_this_month`. A campaign that ended mid-month but did spend is still included.
**Changes:**
- `backend/database.py`: Added `Campaign.google_end_date` column (nullable Date). Updated `visible_latest_campaigns()` to skip zombies. Added `google_end_date` to `Campaign.to_dict()`.
- `backend/routes/accounts.py`: Added `_parse_google_end_date()` helper. `_upsert_campaign_from_live()` now stores `google_end_date` on every sync.
- `backend/routes/pacing.py`: Added `_is_zombie_campaign()` helper. `_campaigns_for_pacing()` now excludes zombies from `live_campaigns` so they don't inflate segment counts or budget-ratio math.
- `backend/app.py`: Added `ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS google_end_date DATE` to startup migration.

### 2026-05-19 — Fix Pace % formula and days-elapsed off-by-one
**What:** Changed displayed Pace % to match the sheet's % DIFF column (I), and fixed `daysIn` to use the prior day since spend is through EOD of the prior day.
**Why:** The app showed `spend / budget` (sheet column G "% Through Month") instead of the sheet's more actionable `% DIFF = (projected_monthly − budget) / budget`. Additionally, `daysIn` used `today.getDate()` (e.g. 19) while Google Ads spend only reflects through EOD of day 18, making ideal-spend projections 1 day off.
**Sheet formulas mapped:**
- G (% Through Month): `=D/C` (spend/budget) — NOT what we display
- H (On Track): `=D * days_in_month / days_elapsed` — projected full-month spend
- I (% DIFF): `=(H−C)/C` = `(spend * daysInMonth / daysIn − budget) / budget` — ✅ now displayed as Pace %
- E (Daily Rec): `=(C−D) / days_in_month` — already correct, unchanged
**Changes:**
- `frontend/src/pages/AccountDashboard.jsx`: `getDaysInfo()` now uses `Math.max(today.getDate()-1, 1)`. Pace pill and stat grid "Pace" now display `fmtPct(pace.deltaPct)` (% DIFF with +/− sign). Segment table pace column same. Label updated to "vs ideal pace (% DIFF)".
- `frontend/src/pages/Home.jsx`: Same `daysIn` fix. Account card pace pill uses `deltaPct`. Portfolio pace uses `portfolioDelta` (% DIFF at portfolio level) instead of `totalSpend/totalMonthly`.
- `frontend/src/pages/Notifications.jsx`: Same `daysIn` fix.

### 2026-05-19 — Fix segmented account rollups
**What:** Made segmented account dashboards and sheet writeback treat each sheet segment as one rollup that can contain multiple campaigns.
**Why:** Segmented accounts could show MTD mismatches or appear to split one segment into campaign-level subsegments when campaign filters overlapped or the account dashboard rebuilt segment totals from campaign rows.
**Changes:**
- `backend/routes/sheets.py`: Added one-time campaign assignment per Google Ads sheet row, using the most-specific matching filter first, so overlapping filters cannot double-claim a campaign or double-count spend.
- `backend/routes/pacing.py`: Manual/scheduled pacing now serializes one segment summary per `budget_label`, including rolled-up MTD spend, budget, current daily total, and campaign count.
- `frontend/src/pages/AccountDashboard.jsx`: Account-specific dashboards now prefer backend segment summaries and backend account MTD/budget totals instead of recomputing segmented totals from campaign rows.
- `backend/tests/test_spend_accuracy.py`: Added regression tests for multi-campaign segment rollups and overlapping sheet filters.

### 2026-05-19 — Match sheet pace percentage math
**What:** Changed app-facing Pace % to match the Google Sheet's budget-utilization percentage.
**Why:** MTD spend, monthly budget, and recommended daily matched the sheet, but Pace % differed because BudgetBuddy was showing variance vs ideal MTD spend (`actual / expected - 1`) instead of the sheet's `actual / monthly budget`.
**Changes:**
- `backend/routes/pacing.py` and `backend/routes/webhook.py`: `pace_ratio` now stores `actual_spend / monthly_budget`; expected MTD remains available for charts/projection.
- `frontend/src/pages/Home.jsx` and `frontend/src/pages/AccountDashboard.jsx`: Pace pills/cards/segment rows now display plain budget-used percentage while keeping projection math for warning color/status.
- `frontend/src/pages/Notifications.jsx`: Aligned the notification daily recommendation helper with the sheet formula that divides by total days in month.
- `backend/tests/test_spend_accuracy.py`: Added regression coverage that Pace % equals budget used while recommended daily remains sheet-aligned.

### 2026-05-19 — Harden MTD totals against remaining duplicate rows
**What:** Added normalized Google campaign ID deduping to backend account totals and frontend fallback aggregation.
**Why:** Some production accounts still showed exactly 2x MTD spend, which indicates one remaining path was summing duplicate DB campaign rows/pacing snapshots.
**Changes:**
- `backend/database.py`: Added `campaign_identity_key()`, deduped account-level `mtd_spend`, latest pacing date, and segment summaries using normalized numeric Google campaign IDs.
- `frontend/src/pages/Home.jsx`, `AccountDashboard.jsx`, `Notifications.jsx`: Fallback spend/current-daily/apply calculations now dedupe by normalized campaign ID, and Home prefers backend-provided MTD/segment totals when available.
- `backend/tests/test_spend_accuracy.py`: Added regression coverage for duplicate campaign IDs with different formatting being counted once.

### 2026-05-19 — Add startup checks for additive DB columns
**What:** Added a lightweight Postgres migration pass during backend startup for additive campaign/pacing columns.
**Why:** Production account loading failed after `Campaign.current_daily_budget` was deployed before the DB column existed; `/api/campaigns/all` crashed with `UndefinedColumn`.
**Changes:**
- `backend/app.py`: After `db.create_all()`, startup now runs `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for `campaigns.budget_label`, `campaigns.campaign_filter`, `campaigns.current_daily_budget`, and the pacing performance columns.
- `CLAUDE.md` / `AGENTS.md`: Documented that these additive columns are now auto-checked on Postgres while keeping manual SQL notes for emergency repair.

### 2026-05-19 — Preserve campaign budget ratios on apply
**What:** Applying recommended daily budgets now preserves each campaign's current share of the account/segment daily budget instead of splitting evenly.
**Why:** If a segment's campaigns are currently weighted 30% / 70%, the new recommended total should keep that weighting rather than forcing a 50% / 50% split.
**Changes:**
- `backend/database.py`: Added `Campaign.current_daily_budget` and dashboard visibility now includes all live campaigns, even if they have $0 MTD spend, while still deduping by `google_campaign_id`.
- `backend/google_ads_client.py`: MTD spend fetch now also returns campaign budget amount/resource metadata when Google Ads includes it.
- `backend/routes/accounts.py`: Campaign import/MCC sync stores the live Google Ads daily budget on each campaign.
- `backend/routes/pacing.py`: Pacing includes all live canonical campaigns for recommendation allocation, updates stored current daily budgets when available, and allocates `recommended_daily_budget` by current daily-budget ratio with an equal-split fallback only when all current budgets are zero.
- `frontend/src/pages/AccountDashboard.jsx`: Account dashboard now shows live campaigns with segment, status, current daily budget, budget share, and MTD spend. Segment/account apply fallbacks also preserve current daily-budget ratios.
- `frontend/src/pages/Home.jsx`: Home-page apply fallback preserves current daily-budget ratios instead of equal-splitting.

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

### 2026-05-19 — Fix 2x MTD spend and $0 recommended daily (duplicate DB rows)
**What:** Fixed MTD spend showing exactly 2x the real number, and recommended daily showing $0, caused by duplicate `google_campaign_id` rows in the DB (43 rows for ~21 unique campaigns).
**Root causes:**
- **Frontend spend double-counting:** `AccountDashboard.jsx`, `Home.jsx`, and `Notifications.jsx` all summed `c.latest_pacing?.actual_spend` over every DB campaign row. Since pacing.py writes the same per-Google-campaign spend to both duplicate rows, the frontend was counting each campaign's spend twice → 2x total. With 2x spend > monthly budget, `max(0, monthly - doubled_spend) = 0` → "Set daily to $0".
- **Backend `seg_count_map` inflated:** `pacing.py` deduped `seg_spend_map` by `google_campaign_id` (existing fix) but still incremented `seg_count_map` for every DB row. This caused `rec = seg_rec / 43` instead of `/ ~21`, further shrinking per-campaign recommendations.
**Fixes:**
- `frontend/src/pages/AccountDashboard.jsx`: `getSegments()` and the top-level `spend` reduction both deduplicate by `google_campaign_id` using a `Set` before summing spend.
- `frontend/src/pages/Home.jsx`: Same dedup applied to `getSegments()` and `accountPacing()`'s spend reduction.
- `frontend/src/pages/Notifications.jsx`: Same dedup applied to spend reduction.
- `backend/routes/pacing.py`: `seg_count_map` increment moved inside the `_counted_gids` guard in both `run_pacing` and `run_pacing_for_account`, so segment count reflects unique campaigns rather than DB rows.

### 2026-05-19 — Fix spend double-counting + stale recommendation display
**What:** Fixed three bugs causing the app to show $12,042 MTD spend (should be $6,021) and $4,281 recommended daily (should be $128) for Goodwill AZ - Retail Grant and potentially other accounts.
**Bug 1 — Spend double-counted via duplicate google_campaign_id:** The `seg_spend_map` loop in `run_pacing` and `run_pacing_for_account` ran once per DB campaign row. If two DB rows share the same `google_campaign_id` (e.g. an active + a re-imported duplicate), the API returns one spend value for that ID but both rows claimed it, doubling the segment spend. Fixed by tracking a `_counted_gids` set and only adding spend to `seg_spend_map` the first time each `google_campaign_id` is seen.
**Bug 2 — Stale `recFromBackend` overriding live formula:** `AccountDashboard.jsx` used `recFromBackend > 0 ? recFromBackend : pace.dailyRec`. Any positive DB recommendation (even from a run with wrong data) silently overrode the fresh formula. Fixed by always using `pace.dailyRec` (`max(0, monthly - spend) / daysInMonth`) which matches the Google Sheet formula exactly.
**Bug 3 — Sheet write-back excluded inactive campaigns:** `write_google_ads_spend_for_account` queried only `is_active=True` campaigns, but pacing includes inactive campaigns with MTD spend. This caused the sheet to show a lower spend than the app. Fixed by querying all campaigns (matching budget sync behavior).
**Changes:**
- `backend/routes/pacing.py`: Added `_counted_gids` set in both `run_pacing` and `run_pacing_for_account` to deduplicate `seg_spend_map` by `google_campaign_id`.
- `frontend/src/pages/AccountDashboard.jsx`: Removed `recFromBackend` logic; `displayRec` now always equals `pace.dailyRec`.
- `backend/routes/sheets.py`: `write_google_ads_spend_for_account` now queries all campaigns (not `is_active=True` only).

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
