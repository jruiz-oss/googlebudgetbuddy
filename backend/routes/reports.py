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
ANTHROPIC_MODEL   = 'claude-opus-4-6'


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
                   search_terms: list) -> str:
    """Build the data block sent to Claude."""
    month_name = calendar.month_name[month]
    today = date.today()

    # Pacing data scoped to this month
    month_start = date(year, month, 1)
    campaigns = account.campaigns or []
    visible = visible_latest_campaigns(campaigns, month_start=month_start)
    latest_date = latest_pacing_date(visible, month_start=month_start)
    total_spend  = campaign_mtd_spend_total(visible, latest_date, month_start=month_start)
    segments     = segment_spend_summaries(visible, month_start=month_start)

    total_budget = sum(s['monthly'] for s in segments)
    pct_used     = (total_spend / total_budget * 100) if total_budget > 0 else 0

    # Days info
    last_day = calendar.monthrange(year, month)[1]
    # For current month use yesterday; for past months use last day
    if year == today.year and month == today.month:
        days_elapsed = max(today.day - 1, 1)
    else:
        days_elapsed = last_day

    ideal_spend = total_budget * (days_elapsed / last_day) if total_budget > 0 else 0
    delta_pct   = ((total_spend / ideal_spend) - 1) * 100 if ideal_spend > 0 else 0

    # Build segments block
    seg_lines = []
    for s in segments:
        seg_lines.append(
            f"  • {s['name']}: ${s['spend']:,.0f} of ${s['monthly']:,.0f} "
            f"({s['pace_pct']:.0f}% used)"
        )

    # Build campaigns block (top 15 by spend)
    camp_lines = []
    seen = set()
    _min_date = date(2000, 1, 1)

    def _latest_spend(c):
        rows = sorted(c.pacing_data or [], key=lambda p: (p.date or _min_date, p.id or 0))
        return rows[-1].actual_spend if rows else 0

    sorted_campaigns = sorted(visible, key=_latest_spend, reverse=True) if visible else []

    for c in sorted_campaigns[:15]:
        key = c.google_campaign_id
        if key in seen:
            continue
        seen.add(key)
        lp = sorted(c.pacing_data or [], key=lambda p: (p.date or _min_date, p.id or 0))
        latest = lp[-1] if lp else None
        spend   = latest.actual_spend if latest else 0
        clicks  = latest.clicks if latest else None
        convs   = latest.conversions if latest else None
        status  = (c.google_status or '').upper()
        status_label = 'Live' if status == 'ENABLED' else ('Paused' if status == 'PAUSED' else status or 'Unknown')
        line = f"  • {c.campaign_name} [{status_label}] — ${spend:,.0f} MTD"
        if c.budget_label:
            line += f" (segment: {c.budget_label})"
        if clicks is not None:
            line += f", {clicks:,} clicks"
        if convs is not None and convs > 0:
            line += f", {convs:.0f} conversions"
        camp_lines.append(line)

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

    # Pull search terms (best-effort — don't block on failure)
    search_terms = []
    if token:
        from google_ads_client import get_top_search_terms, GoogleAdsError
        start_date, end_date = _month_date_range(year, month)
        # For current month, cap end date at yesterday
        today = date.today()
        if year == today.year and month == today.month:
            yesterday = date(today.year, today.month, max(today.day - 1, 1))
            end_date = min(end_date, yesterday)
        try:
            search_terms = get_top_search_terms(
                token.refresh_token,
                account.google_customer_id,
                start_date,
                end_date,
                mcc_customer_id=account.mcc_customer_id,
            )
        except Exception as e:
            logger.warning('Search terms fetch failed for account %s: %s', account_id, e)

    # Get existing report for notes
    r = _get_or_create_report(account_id, year, month)

    # Build data context
    data_context = _build_context(account, year, month, search_terms)

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
