"""
Google Ads script webhook receiver.

The MCC script (scripts/google_ads_budget_pacer.js) posts a JSON payload here
every time it runs — once per account-segment pair, every hour. This endpoint
replaces the old Supabase edge function.

Authentication: x-api-key header must match the WEBHOOK_API_KEY env var.
If WEBHOOK_API_KEY is not set the endpoint is open (dev only — always set it
in production).

Payload shape (from the script):
  unique_id          — "account_id_segment_label" composite key
  account_id         — Google Ads customer ID, may have dashes (e.g. "123-456-7890")
  account_name       — human-readable account name
  event_type         — "STATUS_UPDATE" | "BUDGET_EXCEEDED"
  current_month      — e.g. "May 2026"
  budget_label       — segment label e.g. "IndyCar", "Primary"
  spend              — MTD spend USD for this segment
  budget             — monthly budget USD for this segment
  clicks             — MTD clicks
  conversions        — MTD conversions
  cpc                — cost-per-click
  paused_campaigns   — list of campaign names paused (BUDGET_EXCEEDED only)
  campaign_breakdown — [{name, status, spend, daily_budget}, ...]
"""

import json
import logging
import os
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from database import (
    Account, AccountSettings, Campaign, GoogleOAuthToken,
    PacingData, PacingRun, PauseEvent, db,
)

logger = logging.getLogger(__name__)

webhook_bp = Blueprint('webhook', __name__, url_prefix='/api/webhook')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_customer_id(raw: str) -> str:
    """Strip dashes from a Google Ads customer ID."""
    return (raw or '').replace('-', '').strip()


def _month_bounds_for(today):
    month_start = today.replace(day=1)
    if today.month == 12:
        next_month = today.replace(year=today.year + 1, month=1, day=1)
    else:
        next_month = today.replace(month=today.month + 1, day=1)
    month_end = next_month - timedelta(days=1)
    return month_start, month_end


def _compute_pacing_status(actual_spend, monthly_budget, today):
    """Derive ON_PACE / INCREASE / DECREASE from spend vs expected."""
    month_start, month_end = _month_bounds_for(today)
    days_in_month = (month_end - month_start).days + 1
    days_elapsed = (today - month_start).days + 1

    daily_target = monthly_budget / days_in_month if days_in_month else 0
    expected_mtd = daily_target * days_elapsed
    pace_ratio = (actual_spend / monthly_budget) if monthly_budget > 0 else 0.0

    if days_in_month <= 0 or monthly_budget <= 0:
        recommended = 0.0
    else:
        recommended = max(0.0, (monthly_budget - actual_spend) / days_in_month)

    return expected_mtd, pace_ratio, recommended


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@webhook_bp.route('/google-ads', methods=['POST'])
def receive_google_ads():
    """Receive data from the MCC script and upsert it into BudgetBuddy."""

    # 1. Authenticate
    expected_key = os.environ.get('WEBHOOK_API_KEY', '')
    incoming_key = request.headers.get('x-api-key', '')
    if expected_key and incoming_key != expected_key:
        logger.warning('Webhook: rejected request with bad API key')
        return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(silent=True) or {}

    customer_id = _normalize_customer_id(payload.get('account_id', ''))
    account_name = (payload.get('account_name') or '').strip()
    event_type = payload.get('event_type', 'STATUS_UPDATE')
    budget_label = (payload.get('budget_label') or 'Primary').strip()
    segment_spend = float(payload.get('spend') or 0)
    segment_budget = float(payload.get('budget') or 0)
    segment_clicks = int(payload.get('clicks') or 0) or None
    segment_conversions = float(payload.get('conversions') or 0) or None
    segment_cpc = float(payload.get('cpc') or 0) or None
    paused_campaigns = payload.get('paused_campaigns') or []
    campaign_breakdown = payload.get('campaign_breakdown') or []

    if not customer_id:
        return jsonify({'error': 'account_id is required'}), 400

    today = datetime.utcnow().date()

    # 2. Find or auto-create the account
    account = Account.query.filter_by(google_customer_id=customer_id).first()
    if not account:
        if not account_name:
            account_name = customer_id
        logger.info('Webhook: auto-creating account %s (%s)', account_name, customer_id)
        # Use user_id=1 as owner (first user in the system — single-tenant agency tool)
        from database import User
        first_user = User.query.order_by(User.id).first()
        if not first_user:
            return jsonify({'error': 'No users in system — register first'}), 422
        account = Account(
            user_id=first_user.id,
            account_name=account_name,
            google_customer_id=customer_id,
        )
        db.session.add(account)
        db.session.flush()
        settings = AccountSettings(account_id=account.id)
        db.session.add(settings)
        db.session.flush()

    # 3. Process each campaign in the breakdown
    processed = 0
    for camp_data in campaign_breakdown:
        camp_name = (camp_data.get('name') or '').strip()
        camp_status = (camp_data.get('status') or 'ENABLED').upper()
        camp_spend = float(camp_data.get('spend') or 0)
        # Dead campaign protection: script sets daily_budget=0 for non-ENABLED campaigns
        camp_daily_budget = float(camp_data.get('daily_budget') or 0)

        if not camp_name:
            continue

        # Find or auto-create campaign
        campaign = Campaign.query.filter_by(
            account_id=account.id,
            campaign_name=camp_name,
        ).first()

        if not campaign:
            logger.debug('Webhook: auto-creating campaign "%s" for account %s', camp_name, account.id)
            campaign = Campaign(
                account_id=account.id,
                campaign_name=camp_name,
                google_campaign_id='0',  # unknown until a manual sync fills it in
                monthly_budget=segment_budget / max(len(campaign_breakdown), 1),
                budget_label=budget_label,
                is_active=(camp_status == 'ENABLED'),
            )
            db.session.add(campaign)
            db.session.flush()

        # Update segment label if it changed
        if campaign.budget_label != budget_label:
            campaign.budget_label = budget_label

        # Calculate pacing metrics
        monthly_budget = campaign.monthly_budget or 0
        expected_mtd, pace_ratio, recommended = _compute_pacing_status(
            camp_spend, monthly_budget, today
        )

        # Derive status
        diff = recommended - camp_daily_budget
        if abs(diff) < 0.01:
            status = 'ON_PACE'
        elif diff > 0:
            status = 'INCREASE'
        else:
            status = 'DECREASE'

        # Upsert PacingData for today (overwrite if we already wrote one today)
        existing = (
            PacingData.query
            .filter_by(campaign_id=campaign.id, date=today)
            .order_by(PacingData.id.desc())
            .first()
        )
        if existing:
            existing.actual_spend = round(camp_spend, 2)
            existing.expected_spend = round(expected_mtd, 2)
            existing.pace_ratio = round(pace_ratio, 3)
            existing.current_daily_budget = camp_daily_budget
            existing.recommended_daily_budget = round(recommended, 2)
            existing.status = status
            # Update metrics only if the script sent non-zero values
            if segment_clicks is not None:
                existing.clicks = segment_clicks
            if segment_conversions is not None:
                existing.conversions = segment_conversions
            if segment_cpc is not None:
                existing.cpc = segment_cpc
        else:
            snap = PacingData(
                campaign_id=campaign.id,
                date=today,
                actual_spend=round(camp_spend, 2),
                expected_spend=round(expected_mtd, 2),
                pace_ratio=round(pace_ratio, 3),
                current_daily_budget=camp_daily_budget,
                recommended_daily_budget=round(recommended, 2),
                status=status,
                clicks=segment_clicks,
                conversions=segment_conversions,
                cpc=segment_cpc,
            )
            db.session.add(snap)

        processed += 1

    # 4. Log a PacingRun record for this webhook call
    run = PacingRun(
        account_id=account.id,
        run_type='WEBHOOK',
        triggered_by='mcc_script',
        campaigns_processed=processed,
        adjustments_made=len(paused_campaigns),
        status='COMPLETED',
    )
    db.session.add(run)

    # 5. Log a PauseEvent if campaigns were actually paused
    if event_type == 'BUDGET_EXCEEDED' and paused_campaigns:
        settings = account.settings
        pause_event = PauseEvent(
            account_id=account.id,
            spend_at_pause=round(segment_spend, 2),
            budget_at_pause=round(segment_budget, 2),
            threshold_pct=float(payload.get('threshold', 1.0)) * 100,
            paused_campaign_names=json.dumps(paused_campaigns),
            triggered_by='SCRIPT',
        )
        db.session.add(pause_event)
        logger.warning(
            'Webhook BUDGET_EXCEEDED: account=%s segment=%s spend=%.2f budget=%.2f paused=%d',
            account_name, budget_label, segment_spend, segment_budget, len(paused_campaigns)
        )

    db.session.commit()

    logger.info(
        'Webhook OK: account=%s segment=%s event=%s campaigns=%d',
        account_name, budget_label, event_type, processed
    )
    return jsonify({'status': 'ok', 'campaigns_processed': processed}), 200
