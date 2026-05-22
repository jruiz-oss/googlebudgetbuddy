"""
Pacing routes — run pacing and apply budget recommendations.

Math:
  daily_target        = monthly_budget / days_in_month       (informational)
  expected_mtd        = daily_target * days_elapsed          (informational)
  pace_ratio          = actual_spend / monthly_budget        (sheet "pace %" / budget used)
  recommended_daily   = (monthly_budget - actual_spend) / days_remaining

  Dividing by days_remaining (not days_in_month) gives a daily rate that
  will actually reach the monthly budget by end of month.

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
    campaign_identity_key, canonical_campaigns, dedupe_by_name,
    visible_latest_campaigns,
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
    # The Google Sheet's pace percentage is budget utilization, not variance
    # against ideal MTD spend. Keep expected_mtd for charts/projection only.
    pace_ratio = (actual_spend / monthly_budget) if monthly_budget > 0 else 0.0

    if days_remaining <= 0 or monthly_budget <= 0:
        recommended = 0.0
    else:
        # Divide by days_remaining so the recommended daily rate will actually
        # reach the monthly budget by end of month.
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


def _effective_mcc_customer_id(account):
    """Use the account-specific MCC when present, otherwise fall back to env."""
    return (account.mcc_customer_id or os.environ.get('GOOGLE_ADS_MCC_ID', '')).replace('-', '').strip() or None


def _current_daily_from_history(campaign, today):
    """Use the latest pre-run daily budget snapshot, ignoring rows being replaced today."""
    rows = sorted(
        (
            p for p in (campaign.pacing_data or [])
            if p.date is not None and p.date < today
        ),
        key=lambda r: (r.date, r.id or 0),
    )
    latest = rows[-1] if rows else None
    return latest.current_daily_budget if latest and latest.current_daily_budget else 0.0


def _current_daily_for_run(campaign, campaign_metrics, today):
    """Current daily budget for ratio-based recommendations."""
    if not campaign.is_active:
        return 0.0
    api_daily = campaign_metrics.get('daily_budget_usd')
    if api_daily is not None:
        return float(api_daily or 0.0)
    if campaign.current_daily_budget is not None:
        return float(campaign.current_daily_budget or 0.0)
    return _current_daily_from_history(campaign, today)


def _allocated_recommendation(seg_rec, current_daily, seg_daily, seg_count):
    """Preserve each campaign's current daily-budget share when reallocating."""
    if seg_daily and seg_daily > 0:
        return round(seg_rec * (current_daily / seg_daily), 2)
    return round(seg_rec / seg_count, 2) if seg_count > 0 else round(seg_rec, 2)


def _segment_summaries_from_maps(seg_budget_map, seg_spend_map, seg_daily_map, seg_count_map):
    """Serialize one summary row per sheet segment."""
    labels = sorted(
        set(seg_budget_map.keys())
        | set(seg_spend_map.keys())
        | set(seg_daily_map.keys())
        | set(seg_count_map.keys()),
        key=lambda label: (label or 'Primary').lower(),
    )
    summaries = []
    for label in labels:
        budget = float(seg_budget_map.get(label, 0.0) or 0.0)
        spend = float(seg_spend_map.get(label, 0.0) or 0.0)
        current_daily = float(seg_daily_map.get(label, 0.0) or 0.0)
        summaries.append({
            'name': label,
            'monthly': round(budget, 2),
            'spend': round(spend, 2),
            'current_daily': round(current_daily, 2),
            'campaign_count': int(seg_count_map.get(label, 0) or 0),
            'pace_pct': round((spend / budget) * 100, 1) if budget > 0 else 0.0,
        })
    return summaries


def _delete_today_pacing_data(campaigns, today):
    """Remove same-day snapshots before writing the fresh API-backed run.

    Cleans BOTH the canonical Campaign row's same-day snapshots AND any
    duplicate DB rows that share the same google_campaign_id within the same
    account. Without this, a stale same-day row left over on a duplicate row
    (from a buggy prior run) could re-enter dashboard totals — which is the
    classic "2x MTD" symptom on the home dashboard.
    """
    if not campaigns:
        return 0

    account_ids = {c.account_id for c in campaigns if c.account_id}
    gids        = {c.google_campaign_id for c in campaigns if c.google_campaign_id}
    canonical_ids = {c.id for c in campaigns if c.id}

    target_ids = set(canonical_ids)
    if account_ids and gids:
        # Expand to every Campaign row in these accounts that shares any of
        # the canonical google_campaign_ids — covers duplicate rows.
        sibling_rows = (
            Campaign.query
            .filter(Campaign.account_id.in_(account_ids),
                    Campaign.google_campaign_id.in_(gids))
            .with_entities(Campaign.id)
            .all()
        )
        for (sid,) in sibling_rows:
            target_ids.add(sid)

    if not target_ids:
        return 0
    deleted = (
        PacingData.query
        .filter(PacingData.campaign_id.in_(target_ids), PacingData.date == today)
        .delete(synchronize_session=False)
    )
    db.session.flush()
    return deleted


def _is_zombie_campaign(campaign, today, api_spend):
    """True if this campaign is effectively dead and should be skipped.

    Google Ads often leaves campaigns ENABLED long after their end_date passes.
    The user's rule (matches dashboard expectations):
      • Any campaign with MTD spend > 0 is INCLUDED, regardless of status.
        That covers ENABLED-with-end-date-this-month-and-spend AND paused-but-
        spent-this-month.
      • A campaign with $0 MTD spend is a zombie if either:
          (1) google_end_date is populated and is before today, or
          (2) it has prior pacing history but $0 in this MTD pull
              (covers legacy rows where google_end_date is still NULL).
    """
    if (api_spend or 0) > 0:
        return False  # has spend → not a zombie, even if paused/ended

    # Path 1: end_date is in the past → zombie, regardless of is_active flag.
    # Use `< today` (not `< month_start`) so a campaign that ended yesterday
    # with no spend is hidden immediately rather than waiting until next month.
    if campaign.google_end_date is not None and campaign.google_end_date < today:
        return True

    # Path 2: legacy fallback for rows without google_end_date populated.
    # A campaign that has been paced before but pulls $0 today is dead.
    if campaign.google_end_date is None and campaign.pacing_data:
        return True

    return False


def _norm_name(name):
    return ' '.join((name or '').lower().split())


def _refresh_campaign_state_from_api(db_campaigns, metrics_by_id):
    """Sync live Google Ads state (status, end_date, daily budget, budget RN)
    onto canonical DB campaigns before the include/exclude filter runs.

    Also flags legacy "phantom" rows: a DB campaign whose gid is NOT recognized
    by the Google Ads API but which shares a name with a campaign that IS
    recognized. These come from pre-uniqueness-constraint imports where one
    Google campaign got stored under two gids. We force the phantom to
    is_active=False so it can never out-rank the real row in the name-based
    dedup that runs next.

    Without this, a campaign the user just paused in Google Ads still shows
    is_active=True from the last sync, slips into the "live" bucket, and the
    Status pill incorrectly says "Live" instead of "Paused".
    """
    recognized_names = {
        _norm_name(c.campaign_name)
        for c in db_campaigns
        if c.google_campaign_id in metrics_by_id
    }

    for c in db_campaigns:
        m = metrics_by_id.get(c.google_campaign_id, {}) or {}
        is_recognized = bool(m)

        # Phantom: API doesn't know this gid, but a same-name campaign IS in
        # the API response. Force it inactive so dedup picks the real row.
        if not is_recognized and _norm_name(c.campaign_name) in recognized_names:
            c.is_active = False
            c.google_status = 'REMOVED'
            c.current_daily_budget = 0
            continue

        api_status = (m.get('status') or '').upper() or None
        if api_status:
            c.google_status = api_status
            c.is_active = (api_status == 'ENABLED')
        api_end = m.get('end_date')
        if api_end:
            try:
                c.google_end_date = datetime.strptime(api_end, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                pass
        if m.get('daily_budget_usd') is not None:
            c.current_daily_budget = m.get('daily_budget_usd')
        if m.get('budget_resource_name'):
            c.budget_resource_name = m.get('budget_resource_name')


def _campaigns_for_pacing(account, today, metrics_by_id):
    """Return canonical live campaigns plus inactive campaigns with MTD spend.

    Zombie campaigns (end_date < today AND $0 API spend) are excluded from
    live_campaigns so they don't inflate segment counts or distort budget ratios.
    Inactive campaigns (paused / ended in sync) are included only if they spent
    this month — those are shown on the dashboard with their real status.

    Also collapses same-name twins from legacy duplicate-gid imports so the
    pacing run doesn't write PacingData for the phantom row alongside the real
    one.
    """
    campaigns = dedupe_by_name(canonical_campaigns(account.campaigns))
    live_campaigns = [
        c for c in campaigns
        if c.is_active
        and _campaign_is_active_today(c, today)
        and not _is_zombie_campaign(
            c, today, metrics_by_id.get(c.google_campaign_id, {}).get('spend', 0)
        )
    ]
    inactive_campaigns = [c for c in campaigns if not c.is_active]

    spending_inactive = [
        c for c in inactive_campaigns
        if metrics_by_id.get(c.google_campaign_id, {}).get('spend', 0) > 0
    ]
    return campaigns, live_campaigns, inactive_campaigns, live_campaigns + spending_inactive


# ---------------------------------------------------------------------------
# Core pacing executor — ONE shared implementation for every entry point.
#
# Whether pacing is triggered by:
#   • POST /api/pacing/<account_id>/run (per-account dashboard button),
#   • POST /api/pacing/run-all          (home page "Run All"),
#   • the MCC sync background job,      or
#   • the 06:00 UTC APScheduler,
# they ALL flow through _execute_pacing_run() below. That guarantees the
# campaign filtering, dedup, segment math, and PacingData writes are
# byte-identical across paths — the "fix one, break the other" treadmill
# that produced the alternating 2x-MTD / zombie-campaign bugs was a direct
# result of having two near-duplicate implementations drift apart.
# ---------------------------------------------------------------------------

def _execute_pacing_run(account, refresh_token_str, today, log_prefix='pacing'):
    """Fetch MTD spend, filter zombies, compute segment maps, write PacingData.

    The caller handles sheet sync (before) and sheet writeback (after), plus
    HTTP response or PacingRun audit row construction.

    Returns dict with:
      ok               — True if the run completed; False on fatal API error.
      no_campaigns     — True if the account has no DB campaigns at all.
      no_active        — True if no campaigns were eligible after filtering.
      error            — error string when ok is False.
      recommendations  — list of per-campaign rec dicts (for the JSON route).
      seg_*_map        — segment aggregate maps used by sheet writeback.
      active_campaigns / live_campaigns / inactive_campaigns / metrics_by_id.
      processed        — count of PacingData rows written.
    """
    from collections import defaultdict

    month_start, month_end = _month_bounds(today)
    days_in_month  = (month_end - month_start).days + 1
    days_elapsed   = (today - month_start).days + 1
    days_remaining = (month_end - today).days + 1
    effective_mcc_id = _effective_mcc_customer_id(account)

    db_campaigns = canonical_campaigns(account.campaigns)
    if not db_campaigns:
        logger.info('%s: account_id=%s has no campaigns — skipping', log_prefix, account.id)
        return {
            'ok': True,
            'no_campaigns': True,
            'no_active': True,
            'recommendations': [],
            'seg_spend_map': {},
            'seg_budget_map': {},
            'seg_daily_map': {},
            'seg_count_map': {},
            'active_campaigns': [],
            'live_campaigns': [],
            'inactive_campaigns': [],
            'metrics_by_id': {},
            'processed': 0,
            'days_in_month': days_in_month,
            'days_elapsed': days_elapsed,
            'days_remaining': days_remaining,
        }

    all_campaign_ids = [c.google_campaign_id for c in db_campaigns]
    logger.info(
        '%s spend fetch: account_id=%s canonical=%d customer_id=%r mcc_id=%r',
        log_prefix, account.id, len(db_campaigns),
        account.google_customer_id, effective_mcc_id,
    )
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
            '%s spend fetch failed: account_id=%s name=%r customer_id=%r mcc_id=%r error=%s',
            log_prefix, account.id, account.account_name,
            account.google_customer_id, effective_mcc_id, e,
        )
        return {
            'ok': False,
            'error': str(e),
            'no_campaigns': False,
            'no_active': False,
            'recommendations': [],
            'seg_spend_map': {},
            'seg_budget_map': {},
            'seg_daily_map': {},
            'seg_count_map': {},
            'active_campaigns': [],
            'live_campaigns': [],
            'inactive_campaigns': [],
            'metrics_by_id': {},
            'processed': 0,
            'days_in_month': days_in_month,
            'days_elapsed': days_elapsed,
            'days_remaining': days_remaining,
        }

    # Refresh live state (status + end_date) on every canonical campaign BEFORE
    # the include/exclude filter runs. This is what keeps "Status" pills accurate
    # and what lets the zombie filter catch campaigns that the user just paused.
    _refresh_campaign_state_from_api(db_campaigns, metrics_by_id)

    _, live_campaigns, inactive_campaigns, active_campaigns = _campaigns_for_pacing(
        account, today, metrics_by_id
    )

    # Always clean today's snapshots — even if there are no active campaigns —
    # so stale duplicate same-day rows can't survive across runs.
    deleted_today = _delete_today_pacing_data(db_campaigns, today)
    if deleted_today:
        logger.info(
            '%s replaced same-day snapshots: account_id=%s deleted=%d',
            log_prefix, account.id, deleted_today,
        )

    if not active_campaigns:
        logger.info('%s: account_id=%s has no campaigns to pace', log_prefix, account.id)
        return {
            'ok': True,
            'no_campaigns': False,
            'no_active': True,
            'recommendations': [],
            'seg_spend_map': {},
            'seg_budget_map': {},
            'seg_daily_map': {},
            'seg_count_map': {},
            'active_campaigns': [],
            'live_campaigns': live_campaigns,
            'inactive_campaigns': inactive_campaigns,
            'metrics_by_id': metrics_by_id,
            'processed': 0,
            'days_in_month': days_in_month,
            'days_elapsed': days_elapsed,
            'days_remaining': days_remaining,
        }

    # --- Build segment aggregates (spend, current daily, count) --------------
    seg_spend_map   = defaultdict(float)   # label → total MTD spend
    seg_daily_map   = defaultdict(float)   # label → sum of current daily budgets
    seg_count_map   = defaultdict(int)     # label → number of active campaigns
    seg_budget_map  = {}                   # label → segment monthly budget
    _counted_gids   = set()                # dedup by google_campaign_id

    for _c in active_campaigns:
        _label   = _c.budget_label or 'Primary'
        _metrics = metrics_by_id.get(_c.google_campaign_id, {})
        _cdaily  = _current_daily_for_run(_c, _metrics, today)
        # State (status, end_date, daily budget, budget RN) was already
        # written back to the campaign by _refresh_campaign_state_from_api.

        if _c.google_campaign_id not in _counted_gids:
            _cspend = _metrics.get('spend', 0.0)
            seg_spend_map[_label] += _cspend
            if _c.is_active:
                # Only ENABLED campaigns inflate the equal-split fallback divisor.
                seg_count_map[_label] += 1
            _counted_gids.add(_c.google_campaign_id)
            logger.info(
                '%s spend item: account_id=%s gid=%s name=%r label=%r spend=%.2f is_active=%s',
                log_prefix, account.id, _c.google_campaign_id, _c.campaign_name,
                _label, _cspend, _c.is_active,
            )

        seg_daily_map[_label] += _cdaily
        if _c.monthly_budget and _c.monthly_budget > seg_budget_map.get(_label, 0):
            seg_budget_map[_label] = _c.monthly_budget

    logger.info(
        '%s seg totals: account_id=%s seg_spend=%s seg_count=%s seg_budget=%s',
        log_prefix, account.id, dict(seg_spend_map),
        dict(seg_count_map), seg_budget_map,
    )

    # --- Per-campaign loop ---------------------------------------------------
    recommendations = []
    processed = 0

    for campaign in active_campaigns:
        label            = campaign.budget_label or 'Primary'
        campaign_metrics = metrics_by_id.get(campaign.google_campaign_id, {})
        actual_spend     = campaign_metrics.get('spend', 0.0)
        clicks           = campaign_metrics.get('clicks', None)
        conversions      = campaign_metrics.get('conversions', None)
        cpc              = round(actual_spend / clicks, 2) if clicks and clicks > 0 else None
        current_daily    = _current_daily_for_run(campaign, campaign_metrics, today)

        seg_budget = seg_budget_map.get(label, campaign.monthly_budget)
        seg_spend  = seg_spend_map.get(label, actual_spend)
        seg_daily  = seg_daily_map.get(label, current_daily)
        seg_count  = seg_count_map.get(label, 1)

        seg_rec, status, pace_ratio, expected_mtd, daily_target = _compute_recommendation(
            seg_budget, seg_spend, seg_daily, today
        )
        rec = _allocated_recommendation(seg_rec, current_daily, seg_daily, seg_count)
        campaign_expected_mtd = round(expected_mtd / seg_count, 2) if seg_count > 0 else expected_mtd
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

        recommendations.append({
            'campaign_id': campaign.id,
            'campaign_name': campaign.campaign_name,
            'date': today.isoformat(),
            'google_campaign_id': campaign.google_campaign_id,
            'budget_resource_name': campaign.budget_resource_name,
            'budget_label': label,
            'monthly_budget': round(seg_budget, 2),
            'segment_spend': round(seg_spend, 2),
            'segment_campaign_count': seg_count,
            'actual_spend': round(actual_spend, 2),
            'expected_spend': campaign_expected_mtd,
            'pace_ratio': round(pace_ratio, 3),
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

    return {
        'ok': True,
        'no_campaigns': False,
        'no_active': False,
        'recommendations': recommendations,
        'seg_spend_map': dict(seg_spend_map),
        'seg_budget_map': dict(seg_budget_map),
        'seg_daily_map': dict(seg_daily_map),
        'seg_count_map': dict(seg_count_map),
        'active_campaigns': active_campaigns,
        'live_campaigns': live_campaigns,
        'inactive_campaigns': inactive_campaigns,
        'metrics_by_id': metrics_by_id,
        'processed': processed,
        'days_in_month': days_in_month,
        'days_elapsed': days_elapsed,
        'days_remaining': days_remaining,
    }


# ---------------------------------------------------------------------------
# /run — dry run, returns recommendations without touching Google Ads
# ---------------------------------------------------------------------------

@pacing_bp.route('/<int:account_id>/run', methods=['POST'])
@login_required
def run_pacing(account_id):
    """Per-account dashboard "Run Pacing" button.

    Thin HTTP wrapper around _execute_pacing_run(). Handles:
      • OAuth token lookup + 401 if missing
      • Optional Google Sheet budget sync (before) + spend writeback (after)
      • Auto-pause warning calculation (Grant accounts exempt)
      • JSON response shape consumed by AccountDashboard.jsx
    """
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
        # Reload after committing AccountSettings so we don't operate on an
        # expired account object further down (expire_on_commit=True fires here).
        account = (
            Account.query
            .options(
                selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
                selectinload(Account.settings),
            )
            .get(account_id)
        )
        settings = account.settings

    effective_mcc_id = _effective_mcc_customer_id(account)
    logger.info(
        "Run pacing start: account_id=%s account_name=%r customer_id=%r mcc_id=%r has_sheet_id=%s",
        account.id, account.account_name, account.google_customer_id,
        effective_mcc_id, bool((settings.google_sheet_id or "").strip()),
    )

    today = datetime.utcnow().date()
    is_grant_account = 'grant' in (account.account_name or '').lower()
    if is_grant_account:
        logger.info('Account %s is a Grant account — auto-pause will be skipped', account_id)

    # 1. Sheet sync (budgets) — sheet is source of truth.
    sheet_sync = None
    sheet_write = None
    if settings.google_sheet_id:
        try:
            from routes.sheets import sync_sheet_budgets_for_account
            sheet_sync = sync_sheet_budgets_for_account(account_id)
            logger.info(
                "Run pacing sheet sync result: account_id=%s updated=%s skipped=%s",
                account.id, sheet_sync.get('updated_count'), sheet_sync.get('skipped_count'),
            )
        except Exception as e:
            logger.warning('Sheet sync failed for account %s: %s', account_id, e)
            sheet_sync = {'error': str(e)}
        # Always reload after sheet sync attempt (success or failure) so
        # monthly_budget values are current and pacing_data is fully pre-loaded.
        account = (
            Account.query
            .options(
                selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
                selectinload(Account.settings),
            )
            .get(account_id)
        )
        settings = account.settings
    else:
        sheet_sync = {'warning': 'Google Sheet not configured for this account.'}
        logger.warning(
            "Run pacing skipping sheet sync: account_id=%s account_name=%r reason=%r",
            account.id, account.account_name, "No google_sheet_id configured",
        )

    # 2. Core pacing run — same code path as scheduler / run-all / mcc_sync.
    result = _execute_pacing_run(account, token.refresh_token, today, log_prefix='Run pacing')

    if not result['ok']:
        return jsonify({
            'error': (
                f"Google Ads API error for '{account.account_name}' "
                f"(customer {account.google_customer_id}, MCC {_effective_mcc_customer_id(account) or 'none'}): {result['error']}"
            )
        }), 502

    if result['no_campaigns'] or result['no_active']:
        db.session.commit()  # persist any same-day delete from _execute_pacing_run
        return jsonify({
            'recommendations': [],
            'summary': {'total': 0, 'increase': 0, 'decrease': 0, 'on_pace': 0},
            'sheet_sync': sheet_sync,
        })

    db.session.commit()

    recommendations = result['recommendations']
    seg_spend_map   = result['seg_spend_map']
    seg_budget_map  = result['seg_budget_map']
    seg_daily_map   = result['seg_daily_map']
    seg_count_map   = result['seg_count_map']
    active_campaigns = result['active_campaigns']

    # 3. Sheet writeback (MTD spend).
    if settings.google_sheet_id:
        try:
            from routes.sheets import write_sheet_spend_for_account
            sheet_write = write_sheet_spend_for_account(
                account_id, segment_spend_by_label=dict(seg_spend_map),
            )
            logger.info(
                "Run pacing sheet write result: account_id=%s written=%s skipped=%s",
                account.id, sheet_write.get('written_count'), sheet_write.get('skipped_count'),
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
    segment_summaries = _segment_summaries_from_maps(
        seg_budget_map, seg_spend_map, seg_daily_map, seg_count_map
    )

    # 4. Auto-pause threshold check (Grant accounts exempt).
    auto_pause_triggered = None
    if settings.auto_pause_enabled and active_campaigns and not is_grant_account:
        total_budget = sum(seg_budget_map.values())
        total_spend  = sum(seg_spend_map.values())
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
        'segment_summaries': segment_summaries,
        'mtd_spend': round(sum(seg_spend_map.values()), 2),
        'total_monthly_budget': round(sum(seg_budget_map.values()), 2),
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
        campaign.current_daily_budget = new_daily
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

    active_campaigns = visible_latest_campaigns(account.campaigns)
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
            # Fetch only IDs up front — each account is reloaded fresh inside the
            # loop so we never operate on expired objects left over from a previous
            # account's db.session.commit() (expire_on_commit=True fires after every
            # commit and leaves the bulk-loaded objects stale).
            account_ids = [row[0] for row in db.session.query(Account.id).all()]

            logger.info('run-all background: processing %d account(s)', len(account_ids))

            for account_id in account_ids:
                try:
                    # Always load fresh with pacing_data pre-joined so that
                    # canonical_campaigns() and _is_zombie_campaign() never
                    # fall back to N+1 lazy queries against stale session state.
                    account = Account.query.options(
                        selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
                        selectinload(Account.settings),
                    ).get(account_id)
                    if account is None:
                        continue

                    settings = account.settings
                    if not settings:
                        settings = AccountSettings(account_id=account.id)
                        db.session.add(settings)
                        db.session.commit()
                        # Reload after creating settings so the account object is fresh
                        account = Account.query.options(
                            selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
                            selectinload(Account.settings),
                        ).get(account_id)
                        settings = account.settings

                    # Sheet sync first so budgets are current.
                    # Reload account afterwards regardless of success/failure so
                    # pacing always works with a fully-loaded, post-sync object.
                    if settings.google_sheet_id:
                        try:
                            from routes.sheets import sync_sheet_budgets_for_account
                            sync_sheet_budgets_for_account(account.id)
                        except Exception as e:
                            logger.warning('run-all: sheet sync failed for account %s: %s', account.id, e)
                        # Always reload after sheet sync attempt (success or failure)
                        # so campaign.monthly_budget values are current and
                        # pacing_data is fully pre-loaded.
                        account = Account.query.options(
                            selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
                            selectinload(Account.settings),
                        ).get(account_id)
                        settings = account.settings

                    run_pacing_for_account(account, refresh_token_str, triggered_by='run_all')

                except Exception as e:
                    logger.error('run-all background: pacing failed for account %s: %s', account_id, e)

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


@pacing_bp.route('/run-all/status', methods=['GET'])
@login_required
def run_all_status():
    """Return whether a run-all pacing job is currently in progress.

    The frontend polls this after kicking off /run-all so it knows exactly
    when to reload rather than guessing with a fixed timeout.
    """
    running = not _pacing_all_lock.acquire(blocking=False)
    if not running:
        # We acquired the lock just to check — release it immediately.
        _pacing_all_lock.release()
    return jsonify({'running': running})


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
    """Fetch MTD spend and write PacingData for one account (no HTTP context).

    Used by:
      • the 06:00 UTC scheduler (app.py),
      • the run-all background worker (_run_pacing_all_job above), and
      • the MCC sync background job (routes/accounts.py).

    Thin wrapper around _execute_pacing_run() — the same core that powers the
    per-account /run route. Adds the PacingRun audit row + sheet writeback that
    the HTTP route handles inline.
    """
    today = datetime.utcnow().date()
    settings = account.settings

    result = _execute_pacing_run(
        account, refresh_token_str, today, log_prefix='run_pacing_for_account',
    )

    if not result['ok']:
        run = PacingRun(
            account_id=account.id,
            run_type='AUTO',
            triggered_by=triggered_by,
            campaigns_processed=0,
            adjustments_made=0,
            status='FAILED',
            error_message=result.get('error'),
        )
        db.session.add(run)
        db.session.commit()
        return

    if result['no_campaigns'] or result['no_active']:
        db.session.commit()  # persists same-day delete if _execute_pacing_run did one
        logger.info(
            'run_pacing_for_account: account %s nothing to pace (no_campaigns=%s no_active=%s)',
            account.id, result['no_campaigns'], result['no_active'],
        )
        return

    processed = result['processed']
    seg_spend_map  = result['seg_spend_map']
    seg_budget_map = result['seg_budget_map']
    seg_daily_map  = result['seg_daily_map']
    seg_count_map  = result['seg_count_map']

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

    # Sheet writeback (MTD spend back to Google Sheet column D).
    if settings and settings.google_sheet_id:
        try:
            from routes.sheets import write_sheet_spend_for_account
            write_sheet_spend_for_account(
                account.id, segment_spend_by_label=dict(seg_spend_map),
            )
        except Exception as e:
            logger.warning('Sheet write-back failed for account %s: %s', account.id, e)

    return {
        'processed': processed,
        'segment_summaries': _segment_summaries_from_maps(
            seg_budget_map, seg_spend_map, seg_daily_map, seg_count_map
        ),
        'segment_spend_by_label': {k: round(v, 2) for k, v in seg_spend_map.items()},
    }
