"""
Pacing routes — run pacing and apply budget recommendations.

Math (mirrors the Google Sheet exactly):
  daily_target        = monthly_budget / days_in_month       (informational)
  expected_mtd        = daily_target * days_elapsed          (informational)
  pace_ratio          = actual_spend / expected_mtd          (shown in UI)
  recommended_daily   = (monthly_budget - actual_spend) / days_remaining

Status:
  ON_PACE  when |recommended - current_daily| < $0.01
  INCREASE when recommended > current_daily
  DECREASE when recommended < current_daily

The Google Sheet is the source of truth for monthly_budget.
Before computing pacing, we sync budgets from the sheet (if configured).
"""

import json
import logging
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, session
from sqlalchemy.orm import selectinload

from database import (
    Account, AccountSettings, BudgetAdjustment, Campaign,
    GoogleOAuthToken, PacingData, PacingRun, PauseEvent, db,
)
from google_ads_client import (
    GoogleAdsError, get_campaign_mtd_spend, get_campaign_daily_spend,
    update_campaign_budget, pause_campaigns,
)
from routes.auth import login_required

logger = logging.getLogger(__name__)

pacing_bp = Blueprint('pacing', __name__, url_prefix='/api/pacing')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _month_bounds(today):
    month_start = today.replace(day=1)
    if today.month == 12:
        next_month = today.replace(year=today.year + 1, month=1, day=1)
    else:
        next_month = today.replace(month=today.month + 1, day=1)
    month_end = next_month - timedelta(days=1)
    return month_start, month_end


def _compute_recommendation(monthly_budget, actual_spend, current_daily, today):
    """Return (recommended_daily, status, pace_ratio, expected_mtd, daily_target)."""
    month_start, month_end = _month_bounds(today)
    days_in_month = (month_end - month_start).days + 1
    days_elapsed = (today - month_start).days + 1
    days_remaining = (month_end - today).days + 1

    daily_target = monthly_budget / days_in_month if days_in_month else 0
    expected_mtd = daily_target * days_elapsed
    pace_ratio = (actual_spend / expected_mtd) if expected_mtd > 0 else 1.0

    if days_remaining <= 0 or monthly_budget <= 0:
        recommended = 0.0
    else:
        recommended = max(0.0, (monthly_budget - actual_spend) / days_remaining)

    diff = recommended - (current_daily or 0)
    if abs(diff) < 0.01:
        status = 'ON_PACE'
    elif diff > 0:
        status = 'INCREASE'
    else:
        status = 'DECREASE'

    return recommended, status, pace_ratio, expected_mtd, daily_target


def _campaign_is_active_today(campaign, today):
    """Returns True if the campaign should be paced today based on flight dates."""
    if campaign.flight_type == 'ALWAYS_ON':
        return True
    if campaign.flight_start_date and campaign.flight_end_date:
        return campaign.flight_start_date <= today <= campaign.flight_end_date
    return False


# ---------------------------------------------------------------------------
# /run — dry run, returns recommendations without touching Google Ads
# ---------------------------------------------------------------------------

@pacing_bp.route('/<int:account_id>/run', methods=['POST'])
@login_required
def run_pacing(account_id):
    account = (
        Account.query
        .options(
            selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
            selectinload(Account.settings),
        )
        .get_or_404(account_id)
    )

    user_id = session['user_id']
    token = GoogleOAuthToken.query.filter_by(user_id=user_id, is_valid=True).first()
    if not token:
        return jsonify({'error': 'Google account not connected. Connect via Settings → Google Account.'}), 401

    settings = account.settings
    if not settings:
        settings = AccountSettings(account_id=account.id)
        db.session.add(settings)
        db.session.commit()

    today = datetime.utcnow().date()
    month_start, _ = _month_bounds(today)

    # 1. Pull budgets from Google Sheet (if configured) — sheet is source of truth
    sheet_sync = None
    sheet_write = None
    if settings.google_sheet_id:
        try:
            from routes.sheets import sync_sheet_budgets_for_account
            sheet_sync = sync_sheet_budgets_for_account(account_id)
            db.session.expire_all()  # Reload campaigns after budget sync
            account = Account.query.options(
                selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
                selectinload(Account.settings),
            ).get(account_id)
        except Exception as e:
            logger.warning('Sheet sync failed for account %s: %s', account_id, e)
            sheet_sync = {'error': str(e)}

    # 2. Get active campaigns to pace
    active_campaigns = [c for c in account.campaigns if c.is_active and _campaign_is_active_today(c, today)]

    if not active_campaigns:
        return jsonify({
            'recommendations': [],
            'summary': {'total': 0, 'increase': 0, 'decrease': 0, 'on_pace': 0},
            'sheet_sync': sheet_sync,
        })

    # Grant account safeguard: log exemption status upfront
    is_grant_account = 'grant' in account.account_name.lower()
    if is_grant_account:
        logger.info('Account %s is a Grant account — auto-pause will be skipped', account_id)

    # 3. Fetch MTD spend from Google Ads API
    # Returns {campaign_id: {'spend': float, 'clicks': int, 'conversions': float}}
    campaign_ids = [c.google_campaign_id for c in active_campaigns]
    try:
        metrics_by_id = get_campaign_mtd_spend(
            token.refresh_token,
            account.google_customer_id,
            campaign_ids,
            month_start,
            mcc_customer_id=account.mcc_customer_id,
        )
    except GoogleAdsError as e:
        logger.error('Spend fetch failed for account %s: %s', account_id, e)
        return jsonify({'error': f'Google Ads API error: {str(e)}'}), 502

    # 4. Compute recommendations and save PacingData snapshots
    recommendations = []
    for campaign in active_campaigns:
        campaign_metrics = metrics_by_id.get(campaign.google_campaign_id, {})
        actual_spend = campaign_metrics.get('spend', 0.0)
        clicks = campaign_metrics.get('clicks', None)
        conversions = campaign_metrics.get('conversions', None)
        cpc = round(actual_spend / clicks, 2) if clicks and clicks > 0 else None

        # Get current daily budget from most recent PacingData or default to 0
        latest_rows = sorted(
            campaign.pacing_data,
            key=lambda r: (r.date or datetime.min.date(), r.id or 0),
        )
        current_daily = latest_rows[-1].current_daily_budget if latest_rows and latest_rows[-1].current_daily_budget else 0.0

        rec, status, pace_ratio, expected_mtd, daily_target = _compute_recommendation(
            campaign.monthly_budget, actual_spend, current_daily, today
        )

        month_start, month_end = _month_bounds(today)
        days_in_month = (month_end - month_start).days + 1
        days_elapsed = (today - month_start).days + 1
        days_remaining = (month_end - today).days + 1
        change_pct = ((rec - current_daily) / current_daily * 100) if current_daily > 0 else 0.0

        # Save snapshot (with new performance metrics)
        snap = PacingData(
            campaign_id=campaign.id,
            date=today,
            current_daily_budget=current_daily,
            actual_spend=round(actual_spend, 2),
            expected_spend=round(expected_mtd, 2),
            pace_ratio=round(pace_ratio, 3),
            recommended_daily_budget=round(rec, 2),
            change_percent=round(change_pct, 1),
            status=status,
            clicks=clicks,
            conversions=round(conversions, 1) if conversions is not None else None,
            cpc=cpc,
        )
        db.session.add(snap)

        recommendations.append({
            'campaign_id': campaign.id,
            'campaign_name': campaign.campaign_name,
            'google_campaign_id': campaign.google_campaign_id,
            'budget_resource_name': campaign.budget_resource_name,
            'budget_label': campaign.budget_label,
            'monthly_budget': round(campaign.monthly_budget, 2),
            'actual_spend': round(actual_spend, 2),
            'expected_spend': round(expected_mtd, 2),
            'pace_ratio': round(pace_ratio, 3),
            'current_daily_budget': round(current_daily, 2),
            'recommended_daily_budget': round(rec, 2),
            'change_percent': round(change_pct, 1),
            'status': status,
            'days_elapsed': days_elapsed,
            'days_remaining': days_remaining,
            'days_in_month': days_in_month,
            'clicks': clicks,
            'conversions': round(conversions, 1) if conversions is not None else None,
            'cpc': cpc,
        })

    db.session.commit()

    if settings.google_sheet_id:
        try:
            from routes.sheets import write_sheet_spend_for_account
            sheet_write = write_sheet_spend_for_account(account_id)
        except Exception as e:
            logger.warning('Sheet writeback failed for account %s: %s', account_id, e)
            sheet_write = {'error': str(e)}

    summary = {
        'total': len(recommendations),
        'increase': sum(1 for r in recommendations if r['status'] == 'INCREASE'),
        'decrease': sum(1 for r in recommendations if r['status'] == 'DECREASE'),
        'on_pace': sum(1 for r in recommendations if r['status'] == 'ON_PACE'),
    }

    # 5. Check auto-pause threshold
    # Grant accounts are exempt from auto-pause (they can safely exceed caps).
    auto_pause_triggered = None
    if settings.auto_pause_enabled and active_campaigns and not is_grant_account:
        total_budget = sum(c.monthly_budget for c in active_campaigns)
        total_spend = sum(r['actual_spend'] for r in recommendations)
        if total_budget > 0:
            spend_pct = (total_spend / total_budget) * 100
            if spend_pct >= settings.auto_pause_threshold:
                auto_pause_triggered = {
                    'spend_pct': round(spend_pct, 1),
                    'threshold': settings.auto_pause_threshold,
                    'message': f'Account has reached {spend_pct:.1f}% of monthly budget.',
                }
    elif is_grant_account and settings.auto_pause_enabled:
        auto_pause_triggered = {
            'spend_pct': 0,
            'threshold': settings.auto_pause_threshold,
            'message': 'Grant account — auto-pause is disabled for this account type.',
            'grant_exempt': True,
        }

    return jsonify({
        'recommendations': recommendations,
        'summary': summary,
        'sheet_sync': sheet_sync,
        'sheet_write': sheet_write,
        'auto_pause_warning': auto_pause_triggered,
    })


# ---------------------------------------------------------------------------
# /apply — push recommended budgets to Google Ads
# ---------------------------------------------------------------------------

@pacing_bp.route('/<int:account_id>/apply', methods=['POST'])
@login_required
def apply_recommendations(account_id):
    """Apply selected budget recommendations to Google Ads.

    Body: {
      "adjustments": [
        {
          "campaign_id": 1,           (DB id)
          "budget_resource_name": "customers/x/campaignBudgets/y",
          "new_daily_budget": 25.00
        },
        ...
      ]
    }
    """
    account = Account.query.get_or_404(account_id)
    user_id = session['user_id']
    token = GoogleOAuthToken.query.filter_by(user_id=user_id, is_valid=True).first()
    if not token:
        return jsonify({'error': 'Google account not connected'}), 401

    data = request.get_json() or {}
    adjustments = data.get('adjustments') or []
    if not adjustments:
        return jsonify({'error': 'No adjustments provided'}), 400

    applied = []
    errors = []
    today = datetime.utcnow().date()

    for adj in adjustments:
        campaign_id = adj.get('campaign_id')
        budget_resource = adj.get('budget_resource_name')
        new_daily = float(adj.get('new_daily_budget', 0))

        if not campaign_id or not budget_resource or new_daily < 0:
            errors.append({'campaign_id': campaign_id, 'error': 'Invalid adjustment data'})
            continue

        campaign = Campaign.query.get(campaign_id)
        if not campaign or campaign.account_id != account_id:
            errors.append({'campaign_id': campaign_id, 'error': 'Campaign not found'})
            continue

        # Get old budget from latest pacing snapshot
        latest = (
            PacingData.query
            .filter_by(campaign_id=campaign_id)
            .order_by(PacingData.date.desc(), PacingData.id.desc())
            .first()
        )
        old_daily = latest.current_daily_budget if latest and latest.current_daily_budget else 0.0

        try:
            update_campaign_budget(
                token.refresh_token,
                account.google_customer_id,
                budget_resource,
                new_daily,
                mcc_customer_id=account.mcc_customer_id,
            )
        except GoogleAdsError as e:
            logger.error('Budget update failed for campaign %s: %s', campaign_id, e)
            errors.append({'campaign_id': campaign_id, 'error': str(e)})
            continue

        # Log the adjustment
        change_pct = ((new_daily - old_daily) / old_daily * 100) if old_daily > 0 else 0.0
        adjustment = BudgetAdjustment(
            campaign_id=campaign_id,
            old_budget=round(old_daily, 2),
            new_budget=round(new_daily, 2),
            change_percent=round(change_pct, 2),
            reason='Manual apply from pacing run',
            applied_by=session.get('user_email', 'user'),
            applied_at=datetime.utcnow(),
        )
        db.session.add(adjustment)

        # Update the latest pacing snapshot so the UI reflects the new budget
        if latest:
            latest.current_daily_budget = new_daily
            latest.recommended_daily_budget = new_daily
            latest.status = 'ON_PACE'
            latest.change_percent = 0.0

        applied.append({'campaign_id': campaign_id, 'new_daily_budget': new_daily})

    db.session.commit()

    return jsonify({
        'applied': applied,
        'errors': errors,
        'message': f'{len(applied)} budget(s) updated, {len(errors)} failed.',
    })


# ---------------------------------------------------------------------------
# /summary — quick pacing summary for the Home page
# ---------------------------------------------------------------------------

@pacing_bp.route('/<int:account_id>/summary', methods=['GET'])
@login_required
def pacing_summary(account_id):
    account = (
        Account.query
        .options(
            selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
            selectinload(Account.settings),
        )
        .get_or_404(account_id)
    )

    active_campaigns = [c for c in account.campaigns if c.is_active]
    today = datetime.utcnow().date()
    last_run = (
        PacingRun.query
        .filter_by(account_id=account_id)
        .order_by(PacingRun.run_at.desc())
        .first()
    )

    rows = []
    for c in active_campaigns:
        sorted_rows = sorted(
            c.pacing_data,
            key=lambda r: (r.date or datetime.min.date(), r.id or 0),
        )
        latest = sorted_rows[-1].to_dict() if sorted_rows else None
        rows.append({
            'campaign': c.to_dict(),
            'latest_pacing': latest,
        })

    return jsonify({
        'account': account.to_dict(lite=True),
        'campaigns': rows,
        'last_run': last_run.to_dict() if last_run else None,
    })


# ---------------------------------------------------------------------------
# /pause — manual auto-pause for an account
# ---------------------------------------------------------------------------

@pacing_bp.route('/<int:account_id>/pause', methods=['POST'])
@login_required
def manual_pause(account_id):
    """Pause all active campaigns for an account."""
    account = Account.query.get_or_404(account_id)
    user_id = session['user_id']
    token = GoogleOAuthToken.query.filter_by(user_id=user_id, is_valid=True).first()
    if not token:
        return jsonify({'error': 'Google account not connected'}), 401

    active = [c for c in account.campaigns if c.is_active]
    campaign_ids = [c.google_campaign_id for c in active]

    try:
        pause_campaigns(
            token.refresh_token,
            account.google_customer_id,
            campaign_ids,
            mcc_customer_id=account.mcc_customer_id,
        )
    except GoogleAdsError as e:
        return jsonify({'error': str(e)}), 502

    # Log the pause event
    settings = account.settings
    total_budget = sum(c.monthly_budget for c in active)
    event = PauseEvent(
        account_id=account_id,
        spend_at_pause=0.0,
        budget_at_pause=total_budget,
        threshold_pct=settings.auto_pause_threshold if settings else 95.0,
        paused_campaign_names=json.dumps([c.campaign_name for c in active]),
        triggered_by='MANUAL',
    )
    db.session.add(event)
    db.session.commit()

    return jsonify({
        'message': f'{len(campaign_ids)} campaign(s) paused.',
        'paused_count': len(campaign_ids),
    })
