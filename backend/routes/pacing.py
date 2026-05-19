"""
Pacing routes — run pacing and apply budget recommendations.

Math (mirrors the Google Sheet exactly):
  daily_target        = monthly_budget / days_in_month       (informational)
  expected_mtd        = daily_target * days_elapsed          (informational)
  pace_ratio          = actual_spend / expected_mtd          (shown in UI)
  recommended_daily   = (monthly_budget - actual_spend) / days_in_month

  Dividing by days_in_month (not days_remaining) matches the Google Sheet
  formula: =(C-D)/$E$2 where $E$2 = total days in the month.

Status:
  ON_PACE  when |recommended - current_daily| < $0.01
  INCREASE when recommended > current_daily
  DECREASE when recommended < current_daily

The Google Sheet is the source of truth for monthly_budget.
Before computing pacing, we sync budgets from the sheet (if configured).
"""

import json
import logging
import os
import threading
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

# Prevents concurrent run-all calls (double-click / retry storms).
# acquire(blocking=False) in the route; released at end of background worker.
_pacing_all_lock = threading.Lock()


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

    if days_in_month <= 0 or monthly_budget <= 0:
        recommended = 0.0
    else:
        # Mirrors the Google Sheet formula: (Budget - Spend) / days_in_month.
        # Dividing by the full month length (not just remaining days) gives a
        # conservative daily target that stays consistent across the month.
        recommended = max(0.0, (monthly_budget - actual_spend) / days_in_month)

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


def _effective_mcc_customer_id(account):
    """Use the account-specific MCC when present, otherwise fall back to env."""
    return (account.mcc_customer_id or os.environ.get('GOOGLE_ADS_MCC_ID', '')).replace('-', '').strip() or None


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
    effective_mcc_id = _effective_mcc_customer_id(account)

    logger.info(
        "Run pacing start: account_id=%s account_name=%r customer_id=%r mcc_id=%r has_sheet_id=%s",
        account.id,
        account.account_name,
        account.google_customer_id,
        effective_mcc_id,
        bool((settings.google_sheet_id or "").strip()),
    )

    today = datetime.utcnow().date()
    month_start, _ = _month_bounds(today)

    # 1. Pull budgets from Google Sheet (if configured) — sheet is source of truth
    sheet_sync = None
    sheet_write = None
    if settings.google_sheet_id:
        try:
            from routes.sheets import sync_sheet_budgets_for_account
            sheet_sync = sync_sheet_budgets_for_account(account_id)
            logger.info(
                "Run pacing sheet sync result: account_id=%s updated=%s skipped=%s",
                account.id,
                sheet_sync.get('updated_count'),
                sheet_sync.get('skipped_count'),
            )
            db.session.expire_all()  # Reload campaigns after budget sync
            account = Account.query.options(
                selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
                selectinload(Account.settings),
            ).get(account_id)
            effective_mcc_id = _effective_mcc_customer_id(account)
        except Exception as e:
            logger.warning('Sheet sync failed for account %s: %s', account_id, e)
            sheet_sync = {'error': str(e)}
    else:
        sheet_sync = {'warning': 'Google Sheet not configured for this account.'}
        logger.warning(
            "Run pacing skipping sheet sync: account_id=%s account_name=%r reason=%r",
            account.id,
            account.account_name,
            "No google_sheet_id configured",
        )

    # 2. Split campaigns into live vs inactive.
    #    Live  = is_active True (ENABLED + not expired, set during sync).
    #    Inactive = paused, expired-ENABLED, or REMOVED — stored in DB but
    #               only included in pacing if they have MTD spend > 0.
    live_campaigns = [c for c in account.campaigns if c.is_active and _campaign_is_active_today(c, today)]
    inactive_campaigns = [c for c in account.campaigns if not c.is_active]

    # Bail early only if there are truly no campaigns at all
    if not account.campaigns:
        return jsonify({
            'recommendations': [],
            'summary': {'total': 0, 'increase': 0, 'decrease': 0, 'on_pace': 0},
            'sheet_sync': sheet_sync,
        })

    # Grant account safeguard: log exemption status upfront
    is_grant_account = 'grant' in account.account_name.lower()
    if is_grant_account:
        logger.info('Account %s is a Grant account — auto-pause will be skipped', account_id)

    # 3. Fetch MTD spend for ALL campaigns (live + inactive).
    #    Inactive campaigns with spend > 0 this month are included in pacing.
    #    Returns {campaign_id: {'spend': float, 'clicks': int, 'conversions': float}}
    all_campaign_ids = [c.google_campaign_id for c in account.campaigns]
    logger.info(
        "Run pacing spend fetch: account_id=%s live=%d inactive=%d customer_id=%r mcc_id=%r",
        account.id,
        len(live_campaigns),
        len(inactive_campaigns),
        account.google_customer_id,
        effective_mcc_id,
    )
    try:
        metrics_by_id = get_campaign_mtd_spend(
            token.refresh_token,
            account.google_customer_id,
            all_campaign_ids,
            month_start,
            mcc_customer_id=effective_mcc_id,
        )
    except GoogleAdsError as e:
        logger.error(
            "Spend fetch failed: account_id=%s account_name=%r customer_id=%r mcc_id=%r error=%s",
            account.id,
            account.account_name,
            account.google_customer_id,
            effective_mcc_id,
            e,
        )
        return jsonify({
            'error': (
                f"Google Ads API error for '{account.account_name}' "
                f"(customer {account.google_customer_id}, MCC {effective_mcc_id or 'none'}): {str(e)}"
            )
        }), 502

    # Only pace live campaigns that actually spent money this month.
    # Many Google Ads accounts have years of old ENABLED campaigns that have
    # never been deleted — they're is_active=True but have $0 MTD spend.
    # Including them inflates seg_count and splits recommendations across
    # phantom campaigns.  Mirrors the dashboard's is_visible() logic.
    live_campaigns = [
        c for c in live_campaigns
        if metrics_by_id.get(c.google_campaign_id, {}).get('spend', 0) > 0
    ]
    # Also include inactive (paused/expired) campaigns that did spend this month
    spending_inactive = [
        c for c in inactive_campaigns
        if metrics_by_id.get(c.google_campaign_id, {}).get('spend', 0) > 0
    ]
    active_campaigns = live_campaigns + spending_inactive

    if not active_campaigns:
        return jsonify({
            'recommendations': [],
            'summary': {'total': 0, 'increase': 0, 'decrease': 0, 'on_pace': 0},
            'sheet_sync': sheet_sync,
        })

    # 4. Compute recommendations and save PacingData snapshots
    #
    # Segment-aware pacing: campaigns that share a budget_label share one
    # segment budget pulled from the sheet.  Pacing must be computed at the
    # segment level (sum of all campaign spends vs the segment budget) so that
    # pace_ratio and recommended_daily are meaningful.  The per-campaign
    # recommended daily budget is then split equally among the segment's
    # campaigns (matching how Google Ads typically distributes a segment budget).
    from collections import defaultdict

    # --- Build segment aggregates (spend, current daily, count) --------------
    seg_spend_map   = defaultdict(float)   # label → total MTD spend
    seg_daily_map   = defaultdict(float)   # label → sum of current daily budgets
    seg_count_map   = defaultdict(int)     # label → number of campaigns
    seg_budget_map  = {}                   # label → segment monthly budget

    # Deduplicate spend by google_campaign_id — if two DB campaign rows share the
    # same Google campaign ID (e.g. an active + a duplicate/re-imported row), the
    # API returns one spend value for that ID. Without dedup, both rows claim the
    # same spend and double-count it in seg_spend_map.
    _counted_gids = set()  # google_campaign_ids already added to seg_spend_map

    for _c in active_campaigns:
        _label = _c.budget_label or 'Primary'
        _latest = sorted(
            _c.pacing_data,
            key=lambda r: (r.date or datetime.min.date(), r.id or 0),
        )
        _cdaily = _latest[-1].current_daily_budget if _latest and _latest[-1].current_daily_budget else 0.0

        # Only add spend/count once per unique Google campaign ID to avoid
        # double-counting when duplicate DB rows share the same campaign ID.
        if _c.google_campaign_id not in _counted_gids:
            _cspend = metrics_by_id.get(_c.google_campaign_id, {}).get('spend', 0.0)
            seg_spend_map[_label] += _cspend
            seg_count_map[_label] += 1   # count unique campaigns, not DB rows
            _counted_gids.add(_c.google_campaign_id)
            logger.info(
                "Run pacing spend item: account_id=%s gid=%s name=%r label=%r spend=%.2f",
                account.id, _c.google_campaign_id, _c.campaign_name, _label, _cspend,
            )

        seg_daily_map[_label]  += _cdaily
        # Use max so an inactive campaign with monthly_budget=0 never overwrites
        # the correct budget that was synced from the sheet onto an active campaign.
        if _c.monthly_budget and _c.monthly_budget > seg_budget_map.get(_label, 0):
            seg_budget_map[_label] = _c.monthly_budget

    logger.info(
        "Run pacing seg totals: account_id=%s seg_spend=%s seg_count=%s seg_budget=%s",
        account.id,
        dict(seg_spend_map),
        dict(seg_count_map),
        seg_budget_map,
    )
    # --- Per-campaign loop ---------------------------------------------------
    recommendations = []
    month_start, month_end = _month_bounds(today)
    days_in_month  = (month_end - month_start).days + 1
    days_elapsed   = (today - month_start).days + 1
    days_remaining = (month_end - today).days + 1

    for campaign in active_campaigns:
        label = campaign.budget_label or 'Primary'
        campaign_metrics = metrics_by_id.get(campaign.google_campaign_id, {})
        actual_spend = campaign_metrics.get('spend', 0.0)
        clicks       = campaign_metrics.get('clicks', None)
        conversions  = campaign_metrics.get('conversions', None)
        cpc = round(actual_spend / clicks, 2) if clicks and clicks > 0 else None

        # Current daily for this individual campaign
        latest_rows = sorted(
            campaign.pacing_data,
            key=lambda r: (r.date or datetime.min.date(), r.id or 0),
        )
        current_daily = latest_rows[-1].current_daily_budget if latest_rows and latest_rows[-1].current_daily_budget else 0.0

        # --- Segment-level computation ----------------------------------------
        seg_budget = seg_budget_map.get(label, campaign.monthly_budget)
        seg_spend  = seg_spend_map.get(label, actual_spend)
        seg_daily  = seg_daily_map.get(label, current_daily)
        seg_count  = seg_count_map.get(label, 1)

        seg_rec, status, pace_ratio, expected_mtd, daily_target = _compute_recommendation(
            seg_budget, seg_spend, seg_daily, today
        )

        # Split recommended daily equally across all campaigns in the segment.
        # This matches how Google Ads distributes a shared segment budget.
        rec = round(seg_rec / seg_count, 2) if seg_count > 0 else seg_rec

        # For display: each campaign's proportional share of expected MTD spend
        campaign_expected_mtd = round(expected_mtd / seg_count, 2) if seg_count > 0 else expected_mtd

        change_pct = ((rec - current_daily) / current_daily * 100) if current_daily > 0 else 0.0

        # Save snapshot — actual_spend is per-campaign; budget/pacing metrics are segment-level
        snap = PacingData(
            campaign_id=campaign.id,
            date=today,
            current_daily_budget=current_daily,
            actual_spend=round(actual_spend, 2),
            expected_spend=campaign_expected_mtd,
            pace_ratio=round(pace_ratio, 3),
            recommended_daily_budget=rec,
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
            'budget_label': label,
            'monthly_budget': round(seg_budget, 2),         # full segment budget
            'segment_spend': round(seg_spend, 2),           # total segment MTD spend
            'segment_campaign_count': seg_count,
            'actual_spend': round(actual_spend, 2),         # this campaign's spend
            'expected_spend': campaign_expected_mtd,
            'pace_ratio': round(pace_ratio, 3),             # segment-level pace ratio
            'current_daily_budget': round(current_daily, 2),
            'recommended_daily_budget': rec,
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
            logger.info(
                "Run pacing sheet write result: account_id=%s written=%s skipped=%s",
                account.id,
                sheet_write.get('written_count'),
                sheet_write.get('skipped_count'),
            )
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
# /run-all background worker + route
# ---------------------------------------------------------------------------

def _run_pacing_all_job(app, refresh_token_str):
    """Run pacing for every account — executes in a background thread.

    Mirrors the scheduler's logic: sheet sync → MTD spend fetch → PacingData
    write → sheet spend writeback, one account at a time.
    Releases _pacing_all_lock when done so a subsequent run-all can proceed.
    """
    try:
        with app.app_context():
            accounts = Account.query.options(
                selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
                selectinload(Account.settings),
            ).all()

            logger.info('run-all background: processing %d account(s)', len(accounts))

            for account in accounts:
                try:
                    settings = account.settings
                    if not settings:
                        settings = AccountSettings(account_id=account.id)
                        db.session.add(settings)
                        db.session.commit()

                    # Sheet sync first so budgets are current
                    if settings.google_sheet_id:
                        try:
                            from routes.sheets import sync_sheet_budgets_for_account
                            sync_sheet_budgets_for_account(account.id)
                            db.session.expire_all()
                            account = Account.query.options(
                                selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
                                selectinload(Account.settings),
                            ).get(account.id)
                            settings = account.settings
                        except Exception as e:
                            logger.warning('run-all: sheet sync failed for account %s: %s', account.id, e)

                    run_pacing_for_account(account, refresh_token_str, triggered_by='run_all')

                except Exception as e:
                    logger.error('run-all background: pacing failed for account %s: %s', account.id, e)

            logger.info('run-all background: completed all accounts')

    except Exception as e:
        logger.error('run-all background: unexpected error: %s', e, exc_info=True)
    finally:
        _pacing_all_lock.release()


@pacing_bp.route('/run-all', methods=['POST'])
@login_required
def run_all_pacing():
    """Kick off pacing for every account in the background and return 202.

    Running all accounts sequentially can take 60-120 s (one Google Ads API
    call per account).  Doing it synchronously risks a Gunicorn worker timeout
    on larger MCCs, so we fire-and-forget exactly like the MCC sync endpoint.

    Returns 409 if a run-all is already in progress.
    """
    from flask import current_app

    user_id = session['user_id']
    token = GoogleOAuthToken.query.filter_by(user_id=user_id, is_valid=True).first()
    if not token:
        return jsonify({'error': 'Google account not connected. Connect via Settings → Google Account.'}), 401

    if not _pacing_all_lock.acquire(blocking=False):
        return jsonify({'message': 'Pacing already in progress — refresh in about a minute.'}), 409

    app = current_app._get_current_object()
    refresh_token_str = token.refresh_token  # extract before thread spawns

    t = threading.Thread(
        target=_run_pacing_all_job,
        args=(app, refresh_token_str),
        daemon=True,
    )
    t.start()

    return jsonify({'message': 'Pacing started — refresh the page in about 60 seconds.'}), 202


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


# ---------------------------------------------------------------------------
# Internal pacing runner — used by scheduler and MCC sync (no request context)
# ---------------------------------------------------------------------------

def run_pacing_for_account(account, refresh_token_str, triggered_by='mcc_sync'):
    """Fetch MTD spend and write PacingData for one account.

    Accepts a plain refresh_token_str so it can be called from background
    threads that don't have a full OAuth token ORM object handy.
    Mirrors the logic in the scheduler (_run_pacing_for_account in app.py).
    """
    from datetime import datetime

    today = datetime.utcnow().date()
    month_start, _ = _month_bounds(today)
    settings = account.settings
    effective_mcc_id = _effective_mcc_customer_id(account)

    is_grant_account = 'grant' in account.account_name.lower()

    live_campaigns = [
        c for c in account.campaigns
        if c.is_active and _campaign_is_active_today(c, today)
    ]
    inactive_campaigns = [c for c in account.campaigns if not c.is_active]

    if not account.campaigns:
        logger.info('run_pacing_for_account: account %s has no campaigns — skipping', account.id)
        return

    # Fetch spend for ALL campaigns so inactive ones with MTD spend get included
    all_campaign_ids = [c.google_campaign_id for c in account.campaigns]
    try:
        metrics_by_id = get_campaign_mtd_spend(
            refresh_token_str,
            account.google_customer_id,
            all_campaign_ids,
            month_start,
            mcc_customer_id=effective_mcc_id,
        )
    except GoogleAdsError as e:
        logger.error(
            'run_pacing_for_account spend fetch failed: account_id=%s name=%r error=%s',
            account.id, account.account_name, e,
        )
        run = PacingRun(
            account_id=account.id,
            run_type='AUTO',
            triggered_by=triggered_by,
            campaigns_processed=0,
            adjustments_made=0,
            status='FAILED',
            error_message=str(e),
        )
        db.session.add(run)
        db.session.commit()
        return

    # Only pace live campaigns that actually spent money this month (mirrors run_pacing route).
    live_campaigns = [
        c for c in live_campaigns
        if metrics_by_id.get(c.google_campaign_id, {}).get('spend', 0) > 0
    ]
    # Also include inactive (paused/expired) campaigns that did spend this month
    spending_inactive = [
        c for c in inactive_campaigns
        if metrics_by_id.get(c.google_campaign_id, {}).get('spend', 0) > 0
    ]
    active_campaigns = live_campaigns + spending_inactive

    if not active_campaigns:
        logger.info('run_pacing_for_account: account %s has no campaigns to pace — skipping', account.id)
        return

    # Segment-aware pacing (mirrors the logic in run_pacing route).
    # Build segment aggregates first so the per-campaign loop can use them.
    from collections import defaultdict

    seg_spend_map  = defaultdict(float)
    seg_daily_map  = defaultdict(float)
    seg_count_map  = defaultdict(int)
    seg_budget_map = {}

    # Deduplicate spend by google_campaign_id (same fix as in run_pacing route).
    _counted_gids = set()

    for _c in active_campaigns:
        _label = _c.budget_label or 'Primary'
        _latest = sorted(
            _c.pacing_data,
            key=lambda r: (r.date or datetime.min.date(), r.id or 0),
        )
        _cdaily = _latest[-1].current_daily_budget if _latest and _latest[-1].current_daily_budget else 0.0

        if _c.google_campaign_id not in _counted_gids:
            _cspend = metrics_by_id.get(_c.google_campaign_id, {}).get('spend', 0.0)
            seg_spend_map[_label] += _cspend
            seg_count_map[_label] += 1   # count unique campaigns, not DB rows
            _counted_gids.add(_c.google_campaign_id)

        seg_daily_map[_label]  += _cdaily
        # Use max so an inactive campaign with monthly_budget=0 never overwrites
        # the correct budget that was synced from the sheet onto an active campaign.
        if _c.monthly_budget and _c.monthly_budget > seg_budget_map.get(_label, 0):
            seg_budget_map[_label] = _c.monthly_budget

    processed = 0
    for campaign in active_campaigns:
        label = campaign.budget_label or 'Primary'
        campaign_metrics = metrics_by_id.get(campaign.google_campaign_id, {})
        actual_spend = campaign_metrics.get('spend', 0.0)
        clicks = campaign_metrics.get('clicks', None)
        conversions = campaign_metrics.get('conversions', None)
        cpc = round(actual_spend / clicks, 2) if clicks and clicks > 0 else None

        latest_rows = sorted(
            campaign.pacing_data,
            key=lambda r: (r.date or datetime.min.date(), r.id or 0),
        )
        current_daily = (
            latest_rows[-1].current_daily_budget
            if latest_rows and latest_rows[-1].current_daily_budget
            else 0.0
        )

        seg_budget = seg_budget_map.get(label, campaign.monthly_budget)
        seg_spend  = seg_spend_map.get(label, actual_spend)
        seg_daily  = seg_daily_map.get(label, current_daily)
        seg_count  = seg_count_map.get(label, 1)

        seg_rec, status, pace_ratio, expected_mtd, daily_target = _compute_recommendation(
            seg_budget, seg_spend, seg_daily, today
        )
        rec = round(seg_rec / seg_count, 2) if seg_count > 0 else seg_rec
        campaign_expected_mtd = round(expected_mtd / seg_count, 2) if seg_count > 0 else expected_mtd

        _, month_end = _month_bounds(today)
        change_pct = ((rec - current_daily) / current_daily * 100) if current_daily > 0 else 0.0

        snap = PacingData(
            campaign_id=campaign.id,
            date=today,
            current_daily_budget=current_daily,
            actual_spend=round(actual_spend, 2),
            expected_spend=campaign_expected_mtd,
            pace_ratio=round(pace_ratio, 3),
            recommended_daily_budget=rec,
            change_percent=round(change_pct, 1),
            status=status,
            clicks=clicks,
            conversions=round(conversions, 1) if conversions is not None else None,
            cpc=cpc,
        )
        db.session.add(snap)
        processed += 1

    run = PacingRun(
        account_id=account.id,
        run_type='AUTO',
        triggered_by=triggered_by,
        campaigns_processed=processed,
        adjustments_made=0,
        status='COMPLETED',
    )
    db.session.add(run)
    db.session.commit()
    logger.info(
        'run_pacing_for_account completed: account_id=%s name=%r campaigns=%d',
        account.id, account.account_name, processed,
    )

    # Write spend back to sheet
    if settings and settings.google_sheet_id:
        try:
            from routes.sheets import write_sheet_spend_for_account
            write_sheet_spend_for_account(account.id)
        except Exception as e:
            logger.warning('Sheet write-back failed for account %s: %s', account.id, e)
