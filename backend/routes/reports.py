"""
Monthly AI summary routes.

GET  /api/reports/<account_id>/<year>/<month>      — get (or create) a report
PUT  /api/reports/<account_id>/<year>/<month>      — save notes / edited summary
POST /api/reports/<account_id>/<year>/<month>/generate — call Claude to generate

Claude is given:
  • account name, month/year
  • active campaign names + segment labels
  • MTD spend, monthly budget, pacing status
  • top search terms pulled from Google Ads for the month
  • the user's free-form notes

The model is told to write like a thoughtful analyst, not a metrics reporter.
"""

import calendar
import logging
import os
from datetime import date, datetime

import requests
from flask import Blueprint, jsonify, request, session

from database import (
    Account, Campaign, GoogleOAuthToken, MonthlyReport, UserSettings,
    current_month_start, db, visible_latest_campaigns, latest_pacing_date,
    campaign_mtd_spend_total, segment_spend_summaries,
)
from routes.auth import login_required

logger = logging.getLogger(__name__)

reports_bp = Blueprint('reports', __name__, url_prefix='/api/reports')

ANTHROPIC_API_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_MODEL   = 'claude-sonnet-4-6'


def _effective_mcc_customer_id(account):
    """Use the account-specific MCC when present, otherwise fall back to env.

    Mirrors routes.pacing._effective_mcc_customer_id so report Google Ads pulls
    send the same login-customer-id header pacing uses. Without it, accounts
    with a null mcc_customer_id hit USER_PERMISSION_DENIED on client queries.
    """
    return (account.mcc_customer_id or os.environ.get('GOOGLE_ADS_MCC_ID', '')).replace('-', '').strip() or None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_report(account_id: int, year: int, month: int) -> MonthlyReport:
    r = MonthlyReport.query.filter_by(
        account_id=account_id, year=year, month=month
    ).first()
    if not r:
        r = MonthlyReport(account_id=account_id, year=year, month=month)
        db.session.add(r)
        db.session.commit()
    return r


def _month_date_range(year: int, month: int):
    """Return (date_start, date_end) for the given month."""
    start = date(year, month, 1)
    end   = date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def _build_context(account: Account, year: int, month: int,
                   search_terms: list,
                   live_spend: dict = None) -> str:
    """Build the data block sent to Claude.

    live_spend: {google_campaign_id_str: {spend, clicks, conversions}} fetched
    directly from the Google Ads API for the full month. When provided, this
    is used instead of pacing_data rows (which only reflect pacing-run snapshots
    and will be incomplete for past months). Falls back to DB data if None.
    """
    month_name = calendar.month_name[month]
    today = date.today()

    month_start = date(year, month, 1)
    campaigns = account.campaigns or []

    # For segment budget totals we still use the DB (source of truth for budgets).
    # We use all campaigns for the account, not just "visible" ones, because
    # visibility is current-month logic and we may be summarising a past month.
    from database import canonical_campaigns, segment_spend_summaries as _seg_summaries
    all_campaigns = canonical_campaigns(campaigns)

    # Budget: pull from DB segment structure
    from database import segment_budget_total
    total_budget = segment_budget_total(all_campaigns)

    # Days info
    last_day = calendar.monthrange(year, month)[1]
    if year == today.year and month == today.month:
        days_elapsed = max(today.day - 1, 1)
    else:
        days_elapsed = last_day

    if live_spend:
        # ── Live path: use Google Ads API data for this month ──────────────
        total_spend = sum(v['spend'] for v in live_spend.values())

        # Per-segment spend using live data + DB budget labels
        seg_spend = {}
        seg_budget = {}
        for c in all_campaigns:
            label = (c.budget_label or 'Primary').strip()
            key = str(c.google_campaign_id or '')
            api = live_spend.get(key, {})
            seg_spend[label]  = seg_spend.get(label, 0.0) + api.get('spend', 0.0)
            seg_budget[label] = max(seg_budget.get(label, 0.0), c.monthly_budget or 0.0)

        seg_lines = []
        for label in sorted(seg_spend):
            bgt = seg_budget.get(label, 0)
            spd = seg_spend[label]
            pct = (spd / bgt * 100) if bgt > 0 else 0
            seg_lines.append(f"  • {label}: ${spd:,.0f} of ${bgt:,.0f} ({pct:.0f}% used)")

        # Per-campaign lines sorted by live spend
        camp_data = []
        for c in all_campaigns:
            key = str(c.google_campaign_id or '')
            api = live_spend.get(key, {})
            camp_data.append((c, api.get('spend', 0.0), api.get('clicks', 0), api.get('conversions', 0.0)))
        camp_data.sort(key=lambda x: x[1], reverse=True)

        camp_lines = []
        for c, spend, clicks, convs in camp_data[:15]:
            if spend == 0 and clicks == 0:
                continue  # skip zero-activity campaigns from the narrative
            status = (c.google_status or '').upper()
            status_label = 'Live' if status == 'ENABLED' else ('Paused' if status == 'PAUSED' else status or 'Unknown')
            line = f"  • {c.campaign_name} [{status_label}] — ${spend:,.0f}"
            if c.budget_label:
                line += f" (segment: {c.budget_label})"
            if clicks:
                line += f", {clicks:,} clicks"
            if convs and convs > 0:
                line += f", {convs:.0f} conversions"
            camp_lines.append(line)

    else:
        # ── Fallback path: use pacing_data from DB ─────────────────────────
        visible = visible_latest_campaigns(campaigns, month_start=month_start)
        latest_date = latest_pacing_date(visible, month_start=month_start)
        total_spend = campaign_mtd_spend_total(visible, latest_date, month_start=month_start)
        segments    = _seg_summaries(visible, month_start=month_start)

        seg_lines = [
            f"  • {s['name']}: ${s['spend']:,.0f} of ${s['monthly']:,.0f} ({s['pace_pct']:.0f}% used)"
            for s in segments
        ]

        _min_date = date(2000, 1, 1)
        def _latest_spend_db(c):
            rows = sorted(c.pacing_data or [], key=lambda p: (p.date or _min_date, p.id or 0))
            return rows[-1].actual_spend if rows else 0

        sorted_campaigns = sorted(visible, key=_latest_spend_db, reverse=True)
        camp_lines = []
        seen = set()
        for c in sorted_campaigns[:15]:
            key = c.google_campaign_id
            if key in seen:
                continue
            seen.add(key)
            lp = sorted(c.pacing_data or [], key=lambda p: (p.date or _min_date, p.id or 0))
            latest = lp[-1] if lp else None
            spend = latest.actual_spend if latest else 0
            clicks = latest.clicks if latest else None
            convs  = latest.conversions if latest else None
            status = (c.google_status or '').upper()
            status_label = 'Live' if status == 'ENABLED' else ('Paused' if status == 'PAUSED' else status or 'Unknown')
            line = f"  • {c.campaign_name} [{status_label}] — ${spend:,.0f} MTD"
            if c.budget_label:
                line += f" (segment: {c.budget_label})"
            if clicks is not None:
                line += f", {clicks:,} clicks"
            if convs is not None and convs > 0:
                line += f", {convs:.0f} conversions"
            camp_lines.append(line)

    # Compute pct_used and delta_pct for context block (used in both live and DB paths)
    pct_used = (total_spend / total_budget * 100) if total_budget else 0
    # delta_pct: % DIFF vs ideal pace (positive = ahead, negative = behind)
    projected = (total_spend * last_day / days_elapsed) if days_elapsed > 0 else 0
    delta_pct = ((projected - total_budget) / total_budget * 100) if total_budget else 0

    # Build search terms block
    st_lines = []
    for st in search_terms[:20]:
        line = f"  • \"{st['query']}\" — {st['clicks']:,} clicks"
        if st['conversions'] > 0:
            line += f", {st['conversions']:.0f} conv"
        if st['ctr'] > 0:
            line += f", {st['ctr']:.1f}% CTR"
        st_lines.append(line)

    ctx = f"""ACCOUNT: {account.account_name}
MONTH: {month_name} {year}
REPORT DATE: {today.isoformat()}

=== BUDGET & PACING ===
Monthly budget: ${total_budget:,.0f}
MTD spend: ${total_spend:,.0f} ({pct_used:.0f}% of budget used through day {days_elapsed} of {last_day})
Pace vs ideal: {delta_pct:+.1f}% {'ahead' if delta_pct >= 0 else 'behind'}

=== SEGMENTS ===
{chr(10).join(seg_lines) if seg_lines else '  (no segments)'}

=== CAMPAIGNS (by spend) ===
{chr(10).join(camp_lines) if camp_lines else '  (no campaigns with spend)'}

=== TOP SEARCH TERMS (by clicks) ===
{chr(10).join(st_lines) if st_lines else '  (search term data unavailable)'}"""

    return ctx


def _call_claude(api_key: str, account_name: str, month_name: str,
                 year: int, data_context: str, user_notes: str) -> str:
    """Call Claude API and return the generated summary text."""

    notes_block = f"""
USER NOTES (manager's own observations for this month — treat these as primary context):
{user_notes.strip()}
""" if user_notes and user_notes.strip() else """
USER NOTES: (none provided — synthesize from data only)
"""

    system_prompt = """You are a senior Google Ads strategist writing a monthly account summary for a digital marketing agency.

Your job is to write a clear, narrative summary that a client or account manager can read in 60 seconds. You are NOT a reporting bot — do not list metrics. Instead, synthesize what actually happened strategically this month.

Rules:
- Write in plain English, 2–4 paragraphs, no headers or bullet points
- Lead with the most important strategic story (what changed, why, what it means)
- Reference specific search terms, campaign names, and segments when they're interesting — not just to fill space
- If the user provided notes, they are the most important input — make sure those insights anchor the summary
- Avoid sentences like "CTR increased by X%" or "CPC decreased" — that's what the dashboard is for
- Do mention things like: bidding strategy changes, keyword themes emerging from search terms, audience signals, competitive patterns, what's working, what to watch
- Keep the tone professional but conversational — like a thoughtful colleague briefing someone, not a formal report
- End with 1–2 forward-looking sentences about what to watch or test next month"""

    user_message = f"""Write a monthly summary for {account_name} — {month_name} {year}.

{notes_block}

ACCOUNT DATA:
{data_context}

Write the summary now. No intro, no sign-off — just the narrative."""

    payload = {
        'model': ANTHROPIC_MODEL,
        'max_tokens': 800,
        'system': system_prompt,
        'messages': [{'role': 'user', 'content': user_message}],
    }

    resp = requests.post(
        ANTHROPIC_API_URL,
        json=payload,
        headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        timeout=60,
    )

    if not resp.ok:
        raise ValueError(f'Claude API error ({resp.status_code}): {resp.text[:300]}')

    data = resp.json()
    return data['content'][0]['text'].strip()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@reports_bp.route('/<int:account_id>/<int:year>/<int:month>', methods=['GET'])
@login_required
def get_report(account_id, year, month):
    Account.query.get_or_404(account_id)
    if not (1 <= month <= 12):
        return jsonify({'error': 'Invalid month'}), 400
    r = _get_or_create_report(account_id, year, month)
    return jsonify({'report': r.to_dict()})


@reports_bp.route('/<int:account_id>/<int:year>/<int:month>', methods=['PUT'])
@login_required
def update_report(account_id, year, month):
    Account.query.get_or_404(account_id)
    if not (1 <= month <= 12):
        return jsonify({'error': 'Invalid month'}), 400

    r = _get_or_create_report(account_id, year, month)
    data = request.get_json() or {}

    if 'notes' in data:
        r.notes = data['notes']
    if 'generated_summary' in data:
        r.generated_summary = data['generated_summary']

    db.session.commit()
    return jsonify({'report': r.to_dict()})


@reports_bp.route('/<int:account_id>/<int:year>/<int:month>/generate', methods=['POST'])
@login_required
def generate_report(account_id, year, month):
    """Generate an AI summary using Claude.

    Pulls search terms from Google Ads, combines with pacing data and user
    notes, and calls the Anthropic API. The result is saved to the DB.
    """
    from sqlalchemy.orm import selectinload

    if not (1 <= month <= 12):
        return jsonify({'error': 'Invalid month'}), 400

    user_id = session.get('user_id')

    # Check Anthropic API key
    user_settings = UserSettings.query.filter_by(user_id=user_id).first()
    api_key = (user_settings and user_settings.anthropic_api_key) or os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'No Anthropic API key set. Add it in Settings → AI Summaries.'}), 400

    # Load account with campaigns + pacing data
    account = (
        Account.query
        .options(
            selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
            selectinload(Account.settings),
        )
        .get_or_404(account_id)
    )

    # Get OAuth token for Google Ads API calls
    token = GoogleOAuthToken.query.filter_by(user_id=user_id, is_valid=True).first()

    today = date.today()
    start_date, end_date = _month_date_range(year, month)
    # For the current month, cap end date at yesterday (no complete data for today)
    if year == today.year and month == today.month:
        yesterday = date(today.year, today.month, max(today.day - 1, 1))
        end_date = min(end_date, yesterday)

    # Pull live spend + search terms from Google Ads (best-effort)
    search_terms = []
    live_spend   = None   # {campaign_id: {spend, clicks, conversions}}

    if token:
        from google_ads_client import (
            get_top_search_terms, get_campaign_spend_for_period,
            GoogleAdsError,
        )
        from database import canonical_campaigns

        # Live spend for the full month — this is the source of truth for the
        # summary, replacing stale pacing_data rows that only reflect run snapshots
        campaign_ids = [
            c.google_campaign_id
            for c in canonical_campaigns(account.campaigns or [])
            if c.google_campaign_id
        ]
        if campaign_ids and start_date <= end_date:
            try:
                live_spend = get_campaign_spend_for_period(
                    token.refresh_token,
                    account.google_customer_id,
                    campaign_ids,
                    start_date,
                    end_date,
                    mcc_customer_id=_effective_mcc_customer_id(account),
                )
                logger.info(
                    'generate_report: live spend fetched for account %s '
                    '(%d campaigns, %s–%s)',
                    account_id, len(campaign_ids), start_date, end_date,
                )
            except Exception as e:
                logger.warning('Live spend fetch failed for account %s: %s', account_id, e)

        try:
            search_terms = get_top_search_terms(
                token.refresh_token,
                account.google_customer_id,
                start_date,
                end_date,
                mcc_customer_id=_effective_mcc_customer_id(account),
            )
        except Exception as e:
            logger.warning('Search terms fetch failed for account %s: %s', account_id, e)

    # Get existing report for notes
    r = _get_or_create_report(account_id, year, month)

    # Build data context (live_spend takes precedence over DB pacing data)
    data_context = _build_context(account, year, month, search_terms, live_spend=live_spend)

    # Call Claude
    month_name = calendar.month_name[month]
    try:
        summary = _call_claude(
            api_key=api_key,
            account_name=account.account_name,
            month_name=month_name,
            year=year,
            data_context=data_context,
            user_notes=r.notes or '',
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 502

    # Save
    r.generated_summary = summary
    r.last_generated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({
        'report': r.to_dict(),
        'search_terms_used': len(search_terms),
    })


# ---------------------------------------------------------------------------
# User settings routes (Anthropic API key)
# ---------------------------------------------------------------------------

@reports_bp.route('/user-settings', methods=['GET'])
@login_required
def get_user_settings():
    user_id = session.get('user_id')
    s = UserSettings.query.filter_by(user_id=user_id).first()
    if not s:
        return jsonify({'anthropic_api_key_set': False, 'anthropic_api_key_hint': None})
    return jsonify(s.to_dict())


@reports_bp.route('/user-settings', methods=['PUT'])
@login_required
def update_user_settings():
    user_id = session.get('user_id')
    s = UserSettings.query.filter_by(user_id=user_id).first()
    if not s:
        s = UserSettings(user_id=user_id)
        db.session.add(s)

    data = request.get_json() or {}
    if 'anthropic_api_key' in data:
        s.anthropic_api_key = (data['anthropic_api_key'] or '').strip() or None

    db.session.commit()
    return jsonify(s.to_dict())
