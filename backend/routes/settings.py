"""
Account settings routes — pacing config, auto-pause, sheets, leads tracking.
"""

import logging

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
    if 'google_sheet_id' in data:
        s.google_sheet_id = (data['google_sheet_id'] or '').strip() or None
    if 'daily_digest_enabled' in data:
        s.daily_digest_enabled = bool(data['daily_digest_enabled'])
    if 'track_leads' in data:
        s.track_leads = bool(data['track_leads'])

    db.session.commit()
    return jsonify({'settings': s.to_dict()})
