"""
Google OAuth routes — stub.

BudgetBuddy now authenticates via a service account (GOOGLE_CREDENTIALS_JSON)
instead of per-user OAuth refresh tokens.  The service account is added as a
user directly in Google Ads (Standard or Admin access) so no consent screen or
stored refresh token is needed.

These routes are kept as stubs so any existing frontend calls don't 404.
/status always reports connected; /authorize and /disconnect are no-ops.
"""

import logging

from flask import Blueprint, jsonify
from routes.auth import login_required

logger = logging.getLogger(__name__)

oauth_bp = Blueprint('oauth', __name__, url_prefix='/api/oauth')


@oauth_bp.route('/status', methods=['GET'])
@login_required
def status():
    """Always connected — service account auth needs no per-user token."""
    return jsonify({
        'connected': True,
        'auth_method': 'service_account',
    })


@oauth_bp.route('/authorize', methods=['GET'])
@login_required
def authorize():
    """No-op — service account auth requires no OAuth consent screen."""
    return jsonify({
        'message': 'BudgetBuddy uses service account authentication. No OAuth flow needed.',
        'auth_method': 'service_account',
    })


@oauth_bp.route('/disconnect', methods=['POST'])
@login_required
def disconnect():
    """No-op — service account credentials live in env vars, not per-user DB rows."""
    return jsonify({
        'message': 'Service account auth is managed via GOOGLE_CREDENTIALS_JSON env var.',
        'auth_method': 'service_account',
    })
