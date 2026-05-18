"""
Account management routes.

Accounts = Google Ads customer accounts tracked in the app.
Each account has a google_customer_id (10-digit, no dashes).
"""

import logging

from flask import Blueprint, jsonify, request, session
from sqlalchemy.orm import selectinload

from database import Account, AccountSettings, Campaign, GoogleOAuthToken, db
from google_ads_client import GoogleAdsError, list_mcc_child_accounts, list_campaigns
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
# MCC account browser
# ---------------------------------------------------------------------------

@accounts_bp.route('/mcc/list', methods=['GET'])
@login_required
def list_mcc_accounts():
    """List all client accounts under the user's MCC."""
    import os
    mcc_id = request.args.get('mcc_id') or os.environ.get('GOOGLE_ADS_MCC_ID', '')
    if not mcc_id:
        return jsonify({'error': 'mcc_id query param or GOOGLE_ADS_MCC_ID env var required'}), 400

    user_id = session['user_id']
    token = _get_token_or_401(user_id)
    if not token:
        return jsonify({'error': 'Google account not connected. Please connect via Settings.'}), 401

    try:
        accounts = list_mcc_child_accounts(token.refresh_token, mcc_id)
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
