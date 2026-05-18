"""
Account management routes.

Accounts = Google Ads customer accounts tracked in the app.
Each account has a google_customer_id (10-digit, no dashes).
"""

import logging
import threading

from flask import Blueprint, jsonify, request, session
from sqlalchemy.orm import selectinload

from database import Account, AccountSettings, Campaign, GoogleOAuthToken, db
from google_ads_client import (
    GoogleAdsError, list_mcc_child_accounts, list_campaigns,
    _fetch_customer_name, _fmt_customer_id, get_access_token,
)
from routes.auth import login_required

logger = logging.getLogger(__name__)

accounts_bp = Blueprint('accounts', __name__, url_prefix='/api/accounts')

# Guards against concurrent MCC syncs (double-click / retry storms).
# acquire(blocking=False) in the route; released in the background worker.
_mcc_sync_lock = threading.Lock()


def _get_token_or_401(user_id):
    """Return the user's GoogleOAuthToken or raise a 401-able error."""
    token = GoogleOAuthToken.query.filter_by(user_id=user_id, is_valid=True).first()
    return token


def _ensure_settings(account):
    """Create default AccountSettings if they don't exist yet."""
    if not account.settings:
        s = AccountSettings(account_id=account.id)
        db.session.add(s)
        db.session.commit()
    return account.settings


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@accounts_bp.route('', methods=['GET'])
@login_required
def get_accounts():
    """Return all accounts in the workspace (shared across all users)."""
    accounts = Account.query.order_by(Account.account_name).all()
    return jsonify({'accounts': [a.to_dict(lite=True) for a in accounts]})


@accounts_bp.route('', methods=['POST'])
@login_required
def create_account():
    """Manually add an account by customer ID."""
    data = request.get_json() or {}
    name = (data.get('account_name') or '').strip()
    customer_id = (data.get('google_customer_id') or '').replace('-', '').strip()
    mcc_id = (data.get('mcc_customer_id') or '').replace('-', '').strip() or None

    if not name or not customer_id:
        return jsonify({'error': 'account_name and google_customer_id are required'}), 400

    user_id = session['user_id']
    account = Account(
        user_id=user_id,
        account_name=name,
        google_customer_id=customer_id,
        mcc_customer_id=mcc_id,
    )
    db.session.add(account)
    db.session.flush()
    _ensure_settings(account)
    db.session.commit()

    return jsonify({'account': account.to_dict()}), 201


@accounts_bp.route('/<int:account_id>', methods=['GET'])
@login_required
def get_account(account_id):
    account = Account.query.get_or_404(account_id)
    _ensure_settings(account)
    return jsonify({'account': account.to_dict()})


@accounts_bp.route('/<int:account_id>', methods=['PUT'])
@login_required
def update_account(account_id):
    account = Account.query.get_or_404(account_id)
    data = request.get_json() or {}

    if 'account_name' in data:
        account.account_name = data['account_name'].strip()
    if 'mcc_customer_id' in data:
        account.mcc_customer_id = (data['mcc_customer_id'] or '').replace('-', '').strip() or None

    db.session.commit()
    return jsonify({'account': account.to_dict()})


@accounts_bp.route('/<int:account_id>', methods=['DELETE'])
@login_required
def delete_account(account_id):
    account = Account.query.get_or_404(account_id)
    db.session.delete(account)
    db.session.commit()
    return jsonify({'message': 'Account deleted'})


# ---------------------------------------------------------------------------
# Summary (for home page)
# ---------------------------------------------------------------------------

@accounts_bp.route('/<int:account_id>/summary', methods=['GET'])
@login_required
def account_summary(account_id):
    """Return pacing summary for an account — used on the Home page."""
    account = (
        Account.query
        .options(
            selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
            selectinload(Account.settings),
        )
        .get_or_404(account_id)
    )
    _ensure_settings(account)
    return jsonify({'account': account.to_dict()})


# ---------------------------------------------------------------------------
# Global MCC sync — reconcile DB against live MCC
# ---------------------------------------------------------------------------

def _sync_all_campaigns_for_account(account, token, mcc_customer_id=None):
    """Upsert all live campaigns from Google Ads for a single account.

    Returns (added, updated) counts. Skips silently if the API call fails.
    """
    try:
        live = list_campaigns(
            token.refresh_token,
            account.google_customer_id,
            mcc_customer_id=mcc_customer_id or account.mcc_customer_id,
        )
    except GoogleAdsError as e:
        logger.warning('Campaign sync failed for account %s (%s): %s', account.id, account.google_customer_id, e)
        return 0, 0

    live_ids = {str(lc['campaign_id']) for lc in live}

    # Bulk-deactivate campaigns no longer returned by the API (single UPDATE,
    # no dirty ORM objects, avoids autoflush/deadlock issues).
    if live_ids:
        Campaign.query.filter(
            Campaign.account_id == account.id,
            Campaign.is_active == True,
            ~Campaign.google_campaign_id.in_(live_ids),
        ).update({'is_active': False}, synchronize_session=False)
        db.session.flush()

    added = updated = 0
    for lc in live:
        cid = str(lc['campaign_id'])
        existing = Campaign.query.filter_by(account_id=account.id, google_campaign_id=cid).first()
        if existing:
            existing.campaign_name = lc['campaign_name']
            existing.budget_resource_name = lc.get('budget_resource_name')
            existing.is_active = True
            updated += 1
        else:
            db.session.add(Campaign(
                account_id=account.id,
                campaign_name=lc['campaign_name'],
                google_campaign_id=cid,
                monthly_budget=0.0,
                budget_resource_name=lc.get('budget_resource_name'),
                is_active=True,
            ))
            added += 1
    return added, updated


def _run_mcc_sync_job(app, refresh_token_str, mcc_id):
    """Full MCC sync — runs in a background thread with its own Flask app context.

    Releases _mcc_sync_lock when done so a subsequent sync can proceed.
    All heavy Google Ads API calls and DB writes happen here, keeping the
    HTTP handler free to return 202 immediately.
    """
    import os
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    try:
        with app.app_context():
            try:
                live_accounts = list_mcc_child_accounts(
                    refresh_token_str, mcc_id or None, resolve_names=True
                )
            except GoogleAdsError as e:
                logger.error('MCC sync background job failed (account list): %s', e)
                return

            live_by_id = {a['customer_id'].replace('-', ''): a['name'] for a in live_accounts}
            logger.info('MCC sync start: %d live accounts found in MCC', len(live_accounts))

            db_accounts = Account.query.all()
            updated = []
            deleted = []
            kept_accounts = []

            for account in db_accounts:
                cid = (account.google_customer_id or '').replace('-', '')
                if cid not in live_by_id:
                    deleted.append({'id': account.id, 'customer_id': cid, 'name': account.account_name, 'reason': 'not_in_mcc'})
                    db.session.delete(account)
                    continue

                real_name = live_by_id[cid]
                name_is_real = real_name and not _name_looks_like_placeholder(real_name, cid)

                if name_is_real:
                    if real_name != account.account_name:
                        old = account.account_name
                        account.account_name = real_name
                        updated.append({'id': account.id, 'customer_id': cid, 'old': old, 'new': real_name})
                    kept_accounts.append(account)
                else:
                    if _name_looks_like_placeholder(account.account_name, cid):
                        deleted.append({'id': account.id, 'customer_id': cid, 'name': account.account_name, 'reason': 'no_real_name'})
                        db.session.delete(account)
                    else:
                        kept_accounts.append(account)

            db.session.commit()

            # Fetch campaigns for all surviving accounts in parallel
            campaigns_added = campaigns_updated = 0
            if kept_accounts:
                account_tuples = [
                    (a.id, a.google_customer_id, a.mcc_customer_id)
                    for a in kept_accounts
                ]

                def _fetch_campaigns(acct_id, customer_id, acct_mcc_id):
                    try:
                        live = list_campaigns(
                            refresh_token_str,
                            customer_id,
                            mcc_customer_id=mcc_id or acct_mcc_id,
                        )
                        return acct_id, live
                    except GoogleAdsError as e:
                        logger.warning('Campaign fetch failed for account %s: %s', acct_id, e)
                        return acct_id, []

                live_by_account = {}
                with ThreadPoolExecutor(max_workers=10) as pool:
                    futs = {pool.submit(_fetch_campaigns, *t): t for t in account_tuples}
                    for fut in _as_completed(futs):
                        acct_id, live = fut.result()
                        live_by_account[acct_id] = live

                # Write to DB single-threaded
                for acct_id, live in live_by_account.items():
                    live_ids = {str(lc['campaign_id']) for lc in live}

                    if live_ids:
                        Campaign.query.filter(
                            Campaign.account_id == acct_id,
                            Campaign.is_active == True,
                            ~Campaign.google_campaign_id.in_(live_ids),
                        ).update({'is_active': False}, synchronize_session=False)

                    db.session.flush()

                    for lc in live:
                        cid = str(lc['campaign_id'])
                        existing = Campaign.query.filter_by(account_id=acct_id, google_campaign_id=cid).first()
                        if existing:
                            existing.campaign_name = lc['campaign_name']
                            existing.budget_resource_name = lc.get('budget_resource_name')
                            existing.is_active = True
                            campaigns_updated += 1
                        else:
                            db.session.add(Campaign(
                                account_id=acct_id,
                                campaign_name=lc['campaign_name'],
                                google_campaign_id=cid,
                                monthly_budget=0.0,
                                budget_resource_name=lc.get('budget_resource_name'),
                                is_active=True,
                            ))
                            campaigns_added += 1

            logger.info(
                'MCC sync campaigns: %d added, %d updated across %d accounts',
                campaigns_added, campaigns_updated, len(kept_accounts),
            )
            if campaigns_added or campaigns_updated:
                db.session.commit()

            # Sync sheet budgets for accounts that have a sheet configured
            sheet_synced = 0
            kept_account_ids = [a.id for a in kept_accounts]
            if kept_account_ids:
                try:
                    from routes.sheets import sync_sheet_budgets_for_account
                    accounts_with_sheets = (
                        Account.query
                        .join(AccountSettings, Account.id == AccountSettings.account_id)
                        .filter(
                            Account.id.in_(kept_account_ids),
                            AccountSettings.google_sheet_id.isnot(None),
                            AccountSettings.google_sheet_id != '',
                        )
                        .all()
                    )
                    logger.info(
                        'MCC sync sheet step: %d/%d accounts have a sheet configured',
                        len(accounts_with_sheets), len(kept_account_ids),
                    )
                    for account in accounts_with_sheets:
                        try:
                            logger.info('MCC sync: syncing sheet budgets for account %s (%s)', account.id, account.account_name)
                            sync_sheet_budgets_for_account(account.id)
                            sheet_synced += 1
                        except Exception as e:
                            logger.warning(
                                'Sheet budget sync failed for account %s during MCC sync: %s',
                                account.id, e,
                            )
                except Exception as e:
                    logger.warning('Sheet sync step skipped during MCC sync: %s', e)

            logger.info(
                'MCC sync complete: %d name(s) updated, %d account(s) removed, '
                '%d campaigns added, %d updated, %d sheet(s) synced',
                len(updated), len(deleted), campaigns_added, campaigns_updated, sheet_synced,
            )
    except Exception as e:
        logger.error('MCC sync background job unexpected error: %s', e, exc_info=True)
    finally:
        _mcc_sync_lock.release()


@accounts_bp.route('/sync-from-mcc', methods=['POST'])
@login_required
def sync_from_mcc():
    """Kick off an MCC sync in the background and return 202 immediately.

    The sync can take 60–120 s for large MCCs (parallel API calls + sheet syncs).
    Running it synchronously causes Railway's 30 s HTTP timeout to kill the
    request, so we fire-and-forget and let the client refresh after a delay.

    Returns 409 if a sync is already running (prevents double-click storms).
    Body (optional): { "mcc_id": "123-456-7890" }
    """
    import os
    from flask import current_app

    user_id = session['user_id']
    token = _get_token_or_401(user_id)
    if not token:
        return jsonify({'error': 'Google account not connected. Connect via Settings → Google Account.'}), 401

    data = request.get_json() or {}
    mcc_id = (data.get('mcc_id') or os.environ.get('GOOGLE_ADS_MCC_ID', '')).replace('-', '')

    # Prevent concurrent syncs — if lock is already held return 409
    if not _mcc_sync_lock.acquire(blocking=False):
        return jsonify({'message': 'Sync already in progress — refresh in about a minute.'}), 409

    app = current_app._get_current_object()
    refresh_token_str = token.refresh_token  # extract before thread spawns

    t = threading.Thread(
        target=_run_mcc_sync_job,
        args=(app, refresh_token_str, mcc_id),
        daemon=True,
    )
    t.start()

    return jsonify({'message': 'Sync started — refresh the page in about 60 seconds.'}), 202


# ---------------------------------------------------------------------------
# Bulk name refresh
# ---------------------------------------------------------------------------

def _name_looks_like_placeholder(name: str, customer_id: str) -> bool:
    """Return True if the account name looks like an auto-generated placeholder."""
    if not name:
        return True
    stripped = name.strip()
    # Pure digits (e.g. "1234567890")
    if stripped.isdigit():
        return True
    # "Account XXXXXXXX" fallback
    if stripped.lower().startswith('account ') and stripped[8:].replace('-', '').isdigit():
        return True
    # Formatted customer ID (e.g. "123-456-7890")
    if stripped.replace('-', '') == (customer_id or '').replace('-', ''):
        return True
    return False


@accounts_bp.route('/refresh-names', methods=['POST'])
@login_required
def refresh_account_names():
    """For every account with a placeholder name, fetch the real name from Google Ads.

    Called automatically by the Home page on mount when suspicious names are
    detected. Silently skips accounts if no OAuth token is available.
    """
    import os
    user_id = session['user_id']
    token = GoogleOAuthToken.query.filter_by(user_id=user_id, is_valid=True).first()
    if not token:
        return jsonify({'refreshed': 0, 'message': 'No Google account connected — skipped'}), 200

    developer_token = os.environ.get('GOOGLE_ADS_DEVELOPER_TOKEN', '')
    mcc_id = os.environ.get('GOOGLE_ADS_MCC_ID', '').replace('-', '')

    try:
        access_token = get_access_token(token.refresh_token)
    except Exception as e:
        return jsonify({'refreshed': 0, 'message': f'Token refresh failed: {e}'}), 200

    accounts = Account.query.all()
    refreshed = []

    for account in accounts:
        if not _name_looks_like_placeholder(account.account_name, account.google_customer_id):
            continue
        try:
            real_name = _fetch_customer_name(
                access_token,
                account.google_customer_id,
                developer_token,
                mcc_id,
            )
            if real_name and not _name_looks_like_placeholder(real_name, account.google_customer_id):
                old = account.account_name
                account.account_name = real_name
                refreshed.append({'id': account.id, 'old': old, 'new': real_name})
                logger.info('Refreshed name: %s → %s', old, real_name)
        except Exception as e:
            logger.warning('Name refresh failed for account %s: %s', account.id, e)

    if refreshed:
        db.session.commit()

    return jsonify({'refreshed': len(refreshed), 'updated': refreshed}), 200


# ---------------------------------------------------------------------------
# MCC account browser
# ---------------------------------------------------------------------------

@accounts_bp.route('/mcc/list', methods=['GET'])
@login_required
def list_mcc_accounts():
    """List all accounts accessible to the authenticated user."""
    import os
    mcc_id = request.args.get('mcc_id') or os.environ.get('GOOGLE_ADS_MCC_ID', '')

    user_id = session['user_id']
    token = _get_token_or_401(user_id)
    if not token:
        return jsonify({'error': 'Google account not connected. Please connect via Settings.'}), 401

    try:
        accounts = list_mcc_child_accounts(token.refresh_token, mcc_id or None)
        return jsonify({'accounts': accounts})
    except GoogleAdsError as e:
        logger.error('MCC list failed: %s', e)
        return jsonify({'error': str(e)}), 502


# ---------------------------------------------------------------------------
# Campaign sync (import from Google Ads)
# ---------------------------------------------------------------------------

@accounts_bp.route('/<int:account_id>/sync-campaigns', methods=['GET'])
@login_required
def preview_campaigns(account_id):
    """Preview live campaigns from Google Ads (dry run — nothing saved)."""
    account = Account.query.get_or_404(account_id)
    user_id = session['user_id']
    token = _get_token_or_401(user_id)
    if not token:
        return jsonify({'error': 'Google account not connected'}), 401

    try:
        campaigns = list_campaigns(
            token.refresh_token,
            account.google_customer_id,
            mcc_customer_id=account.mcc_customer_id,
        )
        return jsonify({'campaigns': campaigns})
    except GoogleAdsError as e:
        logger.error('Campaign preview failed for account %s: %s', account_id, e)
        return jsonify({'error': str(e)}), 502


@accounts_bp.route('/<int:account_id>/sync-campaigns', methods=['POST'])
@login_required
def import_campaigns(account_id):
    """Save selected campaigns from Google Ads into the DB.

    Body: { "campaign_ids": ["123", "456"] }
    Campaigns not in the list are left alone (not deleted).
    """
    account = Account.query.get_or_404(account_id)
    user_id = session['user_id']
    token = _get_token_or_401(user_id)
    if not token:
        return jsonify({'error': 'Google account not connected'}), 401

    data = request.get_json() or {}
    selected_ids = [str(cid) for cid in (data.get('campaign_ids') or [])]

    if not selected_ids:
        return jsonify({'error': 'campaign_ids list is required'}), 400

    # Fetch live campaigns to get names and budget resource names
    try:
        live = list_campaigns(
            token.refresh_token,
            account.google_customer_id,
            mcc_customer_id=account.mcc_customer_id,
        )
    except GoogleAdsError as e:
        return jsonify({'error': str(e)}), 502

    live_by_id = {c['campaign_id']: c for c in live}

    added = 0
    skipped = 0
    for cid in selected_ids:
        if cid not in live_by_id:
            skipped += 1
            continue
        lc = live_by_id[cid]
        existing = Campaign.query.filter_by(
            account_id=account_id,
            google_campaign_id=cid,
        ).first()
        if existing:
            # Update budget resource name in case it changed
            existing.budget_resource_name = lc.get('budget_resource_name')
            existing.is_active = True
            skipped += 1
        else:
            c = Campaign(
                account_id=account_id,
                campaign_name=lc['campaign_name'],
                google_campaign_id=cid,
                monthly_budget=0.0,  # Will be set by sheet sync
                budget_resource_name=lc.get('budget_resource_name'),
                is_active=True,
            )
            db.session.add(c)
            added += 1

    db.session.commit()
    return jsonify({
        'message': f'Imported {added} campaign(s), {skipped} already existed.',
        'added': added,
        'skipped': skipped,
    })
