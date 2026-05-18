"""
Account management routes.

Accounts = Google Ads customer accounts tracked in the app.
Each account has a google_customer_id (10-digit, no dashes).
"""

import logging

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


@accounts_bp.route('/sync-from-mcc', methods=['POST'])
@login_required
def sync_from_mcc():
    """One-button reconcile: pull the live MCC account list and:
      - Try to resolve real names for all accounts (resolve_names=True)
      - Update names for accounts found in the MCC with a real name
      - Delete DB accounts whose customer ID is not in the MCC at all
      - Delete DB accounts that are in the MCC but still have placeholder names
        after resolution (so nameless/ID-only accounts are cleaned up)
      - Auto-sync all campaigns for every remaining account

    Body (optional): { "mcc_id": "123-456-7890" }
    """
    import os
    user_id = session['user_id']
    token = _get_token_or_401(user_id)
    if not token:
        return jsonify({'error': 'Google account not connected. Connect via Settings → Google Account.'}), 401

    data = request.get_json() or {}
    mcc_id = (data.get('mcc_id') or os.environ.get('GOOGLE_ADS_MCC_ID', '')).replace('-', '')

    try:
        # resolve_names=True does a secondary per-account GAQL lookup for any
        # account whose descriptive_name comes back empty from the MCC batch call.
        live_accounts = list_mcc_child_accounts(
            token.refresh_token, mcc_id or None, resolve_names=True
        )
    except GoogleAdsError as e:
        return jsonify({'error': str(e)}), 502

    # Build a lookup: customer_id (no dashes) → real name
    live_by_id = {a['customer_id'].replace('-', ''): a['name'] for a in live_accounts}

    db_accounts = Account.query.all()

    updated = []
    deleted = []
    kept_accounts = []

    for account in db_accounts:
        cid = (account.google_customer_id or '').replace('-', '')
        if cid not in live_by_id:
            # Not in MCC at all → remove
            deleted.append({'id': account.id, 'customer_id': cid, 'name': account.account_name, 'reason': 'not_in_mcc'})
            db.session.delete(account)
            continue

        real_name = live_by_id[cid]
        name_is_real = real_name and not _name_looks_like_placeholder(real_name, cid)

        if name_is_real:
            # Got a good name — update if different
            if real_name != account.account_name:
                old = account.account_name
                account.account_name = real_name
                updated.append({'id': account.id, 'customer_id': cid, 'old': old, 'new': real_name})
            kept_accounts.append(account)
        else:
            # Still a placeholder name after resolution
            if _name_looks_like_placeholder(account.account_name, cid):
                # DB name is also a placeholder → no real name anywhere, remove it
                deleted.append({'id': account.id, 'customer_id': cid, 'name': account.account_name, 'reason': 'no_real_name'})
                db.session.delete(account)
            else:
                # DB already has a real name the user set manually — keep it
                kept_accounts.append(account)

    db.session.commit()

    # Auto-sync campaigns for all surviving accounts — run in parallel so
    # N accounts take ~1× latency instead of N× latency.
    campaigns_added = campaigns_updated = 0
    if kept_accounts:
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

        # Fetch live campaigns concurrently; returns (added, updated, [Campaign objects])
        def _fetch_campaigns(account):
            try:
                live = list_campaigns(
                    token.refresh_token,
                    account.google_customer_id,
                    mcc_customer_id=mcc_id or account.mcc_customer_id,
                )
                return account.id, live, None
            except GoogleAdsError as e:
                logger.warning('Campaign fetch failed for account %s: %s', account.id, e)
                return account.id, [], None

        account_by_id = {a.id: a for a in kept_accounts}
        live_by_account = {}
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = {pool.submit(_fetch_campaigns, a): a for a in kept_accounts}
            for fut in _as_completed(futs):
                acct_id, live, _ = fut.result()
                live_by_account[acct_id] = live

        # Write to DB (single-threaded to avoid SQLAlchemy session conflicts)
        for acct_id, live in live_by_account.items():
            account = account_by_id[acct_id]
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

    if campaigns_added or campaigns_updated:
        db.session.commit()

    return jsonify({
        'message': (
            f'Updated {len(updated)} name(s), removed {len(deleted)} account(s). '
            f'Campaigns: {campaigns_added} added, {campaigns_updated} updated.'
        ),
        'updated': updated,
        'deleted': deleted,
        'live_account_count': len(live_accounts),
        'campaigns_added': campaigns_added,
        'campaigns_updated': campaigns_updated,
    }), 200


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
