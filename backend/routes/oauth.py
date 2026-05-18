"""
Google OAuth 2.0 routes.

Flow:
  1. Frontend calls GET /api/oauth/authorize → receives a Google OAuth URL
  2. User is redirected to Google, approves access
  3. Google redirects back to GET /api/oauth/callback?code=xxx
  4. Backend exchanges the code for refresh + access tokens
  5. Tokens are stored in google_oauth_tokens table
  6. User is redirected to the frontend home page

Required env vars:
  GOOGLE_ADS_CLIENT_ID
  GOOGLE_ADS_CLIENT_SECRET
  FRONTEND_URL  (e.g. https://your-app.vercel.app)
"""

import logging
import os
from datetime import datetime, timedelta

import requests
from flask import Blueprint, jsonify, redirect, request, session

from database import GoogleOAuthToken, db
from routes.auth import login_required

logger = logging.getLogger(__name__)

oauth_bp = Blueprint('oauth', __name__, url_prefix='/api/oauth')

GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'

# Scopes needed for Google Ads API access
SCOPES = [
    'https://www.googleapis.com/auth/adwords',
    'openid',
    'email',
]


def _get_redirect_uri():
    """Build the OAuth redirect URI from env (Railway URL in prod, localhost in dev)."""
    base = os.environ.get('BACKEND_URL', 'http://localhost:5000')
    return f'{base}/api/oauth/callback'


@oauth_bp.route('/authorize', methods=['GET'])
@login_required
def authorize():
    """Return the Google OAuth authorization URL for the frontend to redirect to."""
    client_id = os.environ.get('GOOGLE_ADS_CLIENT_ID')
    if not client_id:
        return jsonify({'error': 'Google OAuth not configured — set GOOGLE_ADS_CLIENT_ID'}), 500

    redirect_uri = _get_redirect_uri()
    scope = ' '.join(SCOPES)

    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': scope,
        'access_type': 'offline',   # needed to get a refresh token
        'prompt': 'consent',         # force consent screen so we always get a refresh token
        'state': str(session.get('user_id', '')),
    }

    from urllib.parse import urlencode
    url = f'{GOOGLE_AUTH_URL}?{urlencode(params)}'
    return jsonify({'url': url})


@oauth_bp.route('/callback', methods=['GET'])
def callback():
    """Handle Google's redirect after the user approves access."""
    code = request.args.get('code')
    error = request.args.get('error')
    state = request.args.get('state')  # this is the user_id we passed

    frontend_url = os.environ.get('FRONTEND_URL', 'http://localhost:3000')

    if error:
        logger.warning('OAuth error from Google: %s', error)
        return redirect(f'{frontend_url}?oauth_error={error}')

    if not code:
        return redirect(f'{frontend_url}?oauth_error=no_code')

    # Exchange auth code for tokens
    client_id = os.environ.get('GOOGLE_ADS_CLIENT_ID')
    client_secret = os.environ.get('GOOGLE_ADS_CLIENT_SECRET')
    redirect_uri = _get_redirect_uri()

    resp = requests.post(GOOGLE_TOKEN_URL, data={
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': redirect_uri,
        'code': code,
        'grant_type': 'authorization_code',
    }, timeout=15)

    if not resp.ok:
        logger.error('Token exchange failed: %s', resp.text)
        return redirect(f'{frontend_url}?oauth_error=token_exchange_failed')

    token_data = resp.json()
    refresh_token = token_data.get('refresh_token')
    access_token = token_data.get('access_token')
    expires_in = token_data.get('expires_in', 3600)

    if not refresh_token:
        logger.error('No refresh token in response — did you use prompt=consent?')
        return redirect(f'{frontend_url}?oauth_error=no_refresh_token')

    # Determine user_id from state (set in authorize()) or from active session
    user_id = None
    if state and state.isdigit():
        user_id = int(state)
    elif 'user_id' in session:
        user_id = session['user_id']

    if not user_id:
        return redirect(f'{frontend_url}?oauth_error=no_session')

    # Upsert the token record
    existing = GoogleOAuthToken.query.filter_by(user_id=user_id).first()
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    if existing:
        existing.refresh_token = refresh_token
        existing.access_token = access_token
        existing.token_expires_at = expires_at
        existing.is_valid = True
        existing.updated_at = datetime.utcnow()
    else:
        token_record = GoogleOAuthToken(
            user_id=user_id,
            refresh_token=refresh_token,
            access_token=access_token,
            token_expires_at=expires_at,
            is_valid=True,
        )
        db.session.add(token_record)

    db.session.commit()
    logger.info('OAuth tokens saved for user_id=%s', user_id)

    return redirect(f'{frontend_url}?oauth_success=1')


@oauth_bp.route('/status', methods=['GET'])
@login_required
def status():
    """Return whether the current user has a valid Google OAuth token."""
    user_id = session['user_id']
    token = GoogleOAuthToken.query.filter_by(user_id=user_id).first()
    return jsonify({
        'connected': token is not None and token.is_valid,
        'token_expires_at': token.token_expires_at.isoformat() if token and token.token_expires_at else None,
    })


@oauth_bp.route('/disconnect', methods=['POST'])
@login_required
def disconnect():
    """Revoke and delete the stored OAuth token."""
    user_id = session['user_id']
    token = GoogleOAuthToken.query.filter_by(user_id=user_id).first()
    if token:
        # Optionally revoke via Google
        try:
            requests.post('https://oauth2.googleapis.com/revoke',
                          params={'token': token.refresh_token}, timeout=5)
        except Exception:
            pass
        db.session.delete(token)
        db.session.commit()
    return jsonify({'message': 'Disconnected'})
