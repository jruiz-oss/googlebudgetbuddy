"""
Account settings routes — pacing config, auto-pause, sheets, leads tracking.
"""

import logging
import re

from flask import Blueprint, jsonify, request, session

from database import Account, AccountSettings, db
from routes.auth import login_required

logger = logging.getLogger(__name__)

settings_bp = Blueprint('settings', __name__, url_prefix='/api/settings')


def _get_or_create_settings(account_id):
    s = AccountSettings.query.filter_by(account_id=account_id).first()
    if not s:
        s = AccountSettings(account_id=account_id)
        db.session.add(s)
        db.session.commit()
    return s


def _sheet_id_from_url_or_id(value):
    """Extract a Google Sheets ID from either a raw ID or a full sheet URL."""
    raw = (value or '').strip()
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", raw)
    return match.group(1) if match else raw


@settings_bp.route('/<int:account_id>', methods=['GET'])
@login_required
def get_settings(account_id):
    Account.query.get_or_404(account_id)
    s = _get_or_create_settings(account_id)
    return jsonify({'settings': s.to_dict()})


@settings_bp.route('/<int:account_id>', methods=['PUT'])
@login_required
def update_settings(account_id):
    Account.query.get_or_404(account_id)
    s = _get_or_create_settings(account_id)
    data = request.get_json() or {}

    if 'auto_pause_enabled' in data:
        s.auto_pause_enabled = bool(data['auto_pause_enabled'])
    if 'auto_pause_threshold' in data:
        val = float(data['auto_pause_threshold'])
        if not (50.0 <= val <= 100.0):
            return jsonify({'error': 'auto_pause_threshold must be between 50 and 100'}), 400
        s.auto_pause_threshold = val
    if 'lockdown_enabled' in data:
        s.lockdown_enabled = bool(data['lockdown_enabled'])
    if 'google_sheet_id' in data:
        raw_sheet_id = (data['google_sheet_id'] or '').strip()
        s.google_sheet_id = _sheet_id_from_url_or_id(raw_sheet_id) if raw_sheet_id else None
    if 'daily_digest_enabled' in data:
        s.daily_digest_enabled = bool(data['daily_digest_enabled'])
    if 'track_leads' in data:
        s.track_leads = bool(data['track_leads'])

    db.session.commit()
    return jsonify({'settings': s.to_dict()})


@settings_bp.route('/apply-sheet-to-all', methods=['POST'])
@login_required
def apply_sheet_to_all_accounts():
    """Apply one Google Sheet ID to every account in the workspace."""
    data = request.get_json() or {}
    raw_sheet_id = (data.get('google_sheet_id') or '').strip()
    if not raw_sheet_id:
        return jsonify({'error': 'google_sheet_id is required'}), 400

    sheet_id = _sheet_id_from_url_or_id(raw_sheet_id)
    accounts = Account.query.order_by(Account.account_name).all()

    updated_accounts = []
    for account in accounts:
        settings = AccountSettings.query.filter_by(account_id=account.id).first()
        if not settings:
            settings = AccountSettings(account_id=account.id)
            db.session.add(settings)
        settings.google_sheet_id = sheet_id
        updated_accounts.append({
            'account_id': account.id,
            'account_name': account.account_name,
        })

    db.session.commit()
    logger.info(
        "Applied shared sheet to all accounts: sheet_id=%r account_count=%d",
        sheet_id,
        len(updated_accounts),
    )
    return jsonify({
        'message': f'Applied sheet to {len(updated_accounts)} account(s).',
        'google_sheet_id': sheet_id,
        'updated_count': len(updated_accounts),
        'updated_accounts': updated_accounts,
    })
