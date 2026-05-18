"""
Google Ads API client.

Handles all communication with the Google Ads API v18.
Uses OAuth 2.0 refresh tokens (stored in google_oauth_tokens table).

Key responsibilities:
  - Refreshing short-lived access tokens from the stored refresh token
  - Pulling MTD spend per campaign using GAQL (Google Ads Query Language)
  - Listing campaigns under an account
  - Updating campaign budgets
  - Pulling lead form submissions for a date range
  - Listing child accounts under an MCC
"""

import logging
import os
from datetime import datetime, timedelta, date

import requests

logger = logging.getLogger(__name__)

GOOGLE_ADS_API_VERSION = 'v23'
GOOGLE_ADS_API_BASE = f'https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}'
TOKEN_URL = 'https://oauth2.googleapis.com/token'


class GoogleAdsError(Exception):
    """Raised when the Google Ads API returns an error."""
    pass


class InvalidTokenError(GoogleAdsError):
    """Raised when the refresh token is invalid or revoked."""
    pass


def get_access_token(refresh_token: str) -> str:
    """Exchange a refresh token for a short-lived access token.

    Called before every API request. In production you'd cache the access token
    until it expires, but for simplicity we refresh on every call — Google's
    token endpoint handles this gracefully.
    """
    client_id = os.environ.get('GOOGLE_ADS_CLIENT_ID')
    client_secret = os.environ.get('GOOGLE_ADS_CLIENT_SECRET')

    if not client_id or not client_secret:
        raise GoogleAdsError('GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET must be set.')

    resp = requests.post(TOKEN_URL, data={
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token',
    }, timeout=15)

    if not resp.ok:
        body = resp.text
        if 'invalid_grant' in body:
            raise InvalidTokenError(f'Refresh token is invalid or revoked: {body}')
        raise GoogleAdsError(f'Token refresh failed ({resp.status_code}): {body}')

    return resp.json()['access_token']


def _headers(access_token: str, customer_id: str, developer_token: str, mcc_customer_id: str = None) -> dict:
    """Build the standard headers required by the Google Ads API."""
    h = {
        'Authorization': f'Bearer {access_token}',
        'developer-token': developer_token,
        'Content-Type': 'application/json',
    }
    # login-customer-id is required when accessing a client account via an MCC.
    # It should be the MCC's customer ID (no dashes).
    if mcc_customer_id:
        h['login-customer-id'] = mcc_customer_id.replace('-', '')
    return h


def _gaql(access_token: str, customer_id: str, developer_token: str,
          query: str, mcc_customer_id: str = None) -> list:
    """Run a GAQL query and return all result rows as a list of dicts."""
    cid = customer_id.replace('-', '')
    url = f'{GOOGLE_ADS_API_BASE}/customers/{cid}/googleAds:searchStream'
    headers = _headers(access_token, cid, developer_token, mcc_customer_id)

    resp = requests.post(url, json={'query': query}, headers=headers, timeout=30)

    if not resp.ok:
        raise GoogleAdsError(f'GAQL query failed ({resp.status_code}): {resp.text[:500]}')

    rows = []
    for batch in resp.json():
        rows.extend(batch.get('results', []))
    return rows


# ---------------------------------------------------------------------------
# Account / MCC helpers
# ---------------------------------------------------------------------------

def _fetch_customer_name(access_token: str, customer_id: str,
                          developer_token: str, mcc_id: str) -> str:
    """Fetch a single customer's descriptive_name via a direct account query.

    Used as a fallback when customer_client.descriptive_name comes back empty
    (which happens for some account types / recently-created accounts).
    Returns empty string on any failure so the caller can decide the fallback.
    """
    try:
        rows = _gaql(
            access_token,
            customer_id,
            developer_token,
            'SELECT customer.descriptive_name FROM customer LIMIT 1',
            mcc_customer_id=mcc_id,
        )
        if rows:
            return (rows[0].get('customer', {}).get('descriptiveName') or '').strip()
    except Exception as exc:
        logger.debug('Secondary name lookup failed for %s: %s', customer_id, exc)
    return ''


def _fmt_customer_id(raw_id: str) -> str:
    """Format a 10-digit customer ID as XXX-XXX-XXXX for display."""
    digits = raw_id.replace('-', '')
    if len(digits) == 10:
        return f'{digits[:3]}-{digits[3:6]}-{digits[6:]}'
    return raw_id


def list_mcc_child_accounts(refresh_token: str, mcc_customer_id: str = None) -> list:
    """Return all active non-manager accounts under the MCC.

    Queries customer_client from the MCC account — same approach as the
    working Lovable app. Returns [{customer_id, name}, ...]

    If customer_client.descriptive_name is empty (common for some account
    types), a secondary per-account query is made to fetch
    customer.descriptive_name directly. Falls back to a formatted ID.
    """
    developer_token = os.environ.get('GOOGLE_ADS_DEVELOPER_TOKEN', '')
    mcc_id = (mcc_customer_id or os.environ.get('GOOGLE_ADS_MCC_ID', '')).replace('-', '')

    if not mcc_id:
        raise GoogleAdsError('MCC customer ID is required. Set GOOGLE_ADS_MCC_ID in Railway env vars.')

    access_token = get_access_token(refresh_token)

    query = """
        SELECT
          customer_client.id,
          customer_client.descriptive_name,
          customer_client.manager,
          customer_client.status
        FROM customer_client
        WHERE customer_client.status = 'ENABLED'
          AND customer_client.manager = FALSE
    """

    url = f'{GOOGLE_ADS_API_BASE}/customers/{mcc_id}/googleAds:searchStream'
    headers = {
        'Authorization': f'Bearer {access_token}',
        'developer-token': developer_token,
        'login-customer-id': mcc_id,
        'Content-Type': 'application/json',
    }

    resp = requests.post(url, json={'query': query}, headers=headers, timeout=30)
    if not resp.ok:
        raise GoogleAdsError(f'MCC account list failed ({resp.status_code}): {resp.text[:300]}')

    accounts = []
    for batch in resp.json():
        for row in (batch.get('results') or []):
            cc = row.get('customerClient', {})
            raw_id = str(cc.get('id', ''))
            name = (cc.get('descriptiveName') or '').strip()
            if raw_id:
                accounts.append({
                    'customer_id': raw_id,
                    'name': name,  # may be empty; resolved below
                })

    # Secondary pass: for any account with no descriptive name, query the
    # account directly for customer.descriptive_name.  We accept the extra
    # round-trips because the MCC list is typically small (< 100 accounts).
    for acct in accounts:
        if not acct['name']:
            fetched = _fetch_customer_name(
                access_token, acct['customer_id'], developer_token, mcc_id
            )
            acct['name'] = fetched if fetched else _fmt_customer_id(acct['customer_id'])

    accounts.sort(key=lambda a: a.get('name', '').lower())
    return accounts


# ---------------------------------------------------------------------------
# Campaign helpers
# ---------------------------------------------------------------------------

# Channel types that use non-standard budget structures and would cause phantom
# budget bloat if included. Mirrors the script's filtering logic.
_PHANTOM_CHANNEL_TYPES = {'LOCAL_SERVICES', 'SMART', 'HOTEL', 'LOCAL'}


def list_campaigns(refresh_token: str, customer_id: str, mcc_customer_id: str = None) -> list:
    """Return all ENABLED/PAUSED campaigns for the given customer.

    Excludes phantom-budget channel types (LOCAL_SERVICES, SMART, HOTEL, LOCAL)
    that use non-standard budget structures and would inflate totals.

    Returns: [{campaign_id, campaign_name, status, channel_type,
               budget_resource_name, daily_budget_micros, daily_budget_usd}, ...]
    """
    developer_token = os.environ.get('GOOGLE_ADS_DEVELOPER_TOKEN', '')
    access_token = get_access_token(refresh_token)

    query = """
        SELECT
          campaign.id,
          campaign.name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign.campaign_budget,
          campaign_budget.id,
          campaign_budget.amount_micros,
          campaign_budget.resource_name
        FROM campaign
        WHERE campaign.status IN ('ENABLED', 'PAUSED')
        ORDER BY campaign.name
    """

    rows = _gaql(access_token, customer_id, developer_token, query, mcc_customer_id=mcc_customer_id)

    campaigns = []
    for r in rows:
        c = r.get('campaign', {})
        b = r.get('campaignBudget', {})
        channel_type = (c.get('advertisingChannelType') or '').upper()
        # Skip phantom-budget channel types
        if channel_type in _PHANTOM_CHANNEL_TYPES:
            continue
        micros = int(b.get('amountMicros', 0) or 0)
        campaigns.append({
            'campaign_id': str(c.get('id', '')),
            'campaign_name': c.get('name', ''),
            'status': c.get('status', ''),
            'channel_type': channel_type,
            'budget_resource_name': b.get('resourceName', ''),
            'daily_budget_micros': micros,
            'daily_budget_usd': round(micros / 1_000_000, 2),
        })
    return campaigns


def get_campaign_mtd_spend(refresh_token: str, customer_id: str,
                            campaign_ids: list, month_start: date,
                            mcc_customer_id: str = None) -> dict:
    """Return MTD spend, clicks, and conversions per campaign ID for the current month.

    month_start: date object for the first of the month (e.g. date(2026, 5, 1))
    Returns: {campaign_id_str: {'spend': float, 'clicks': int, 'conversions': float}, ...}

    Note: historical spend from ended/paused campaigns is included (script parity) —
    a campaign that ran earlier in the month still contributes to the MTD total even
    if it's paused today. The 'daily_budget' on PacingData will be set to 0 for
    non-ENABLED campaigns (dead campaign protection, handled in pacing.py).
    """
    if not campaign_ids:
        return {}

    developer_token = os.environ.get('GOOGLE_ADS_DEVELOPER_TOKEN', '')
    access_token = get_access_token(refresh_token)

    # Yesterday is the last full day of data available
    yesterday = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
    start = month_start.isoformat()

    # Format campaign IDs for the IN clause
    id_list = ', '.join(str(cid) for cid in campaign_ids)

    query = f"""
        SELECT
          campaign.id,
          campaign.status,
          campaign.advertising_channel_type,
          metrics.cost_micros,
          metrics.clicks,
          metrics.conversions
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{yesterday}'
          AND campaign.id IN ({id_list})
    """

    rows = _gaql(access_token, customer_id, developer_token, query, mcc_customer_id=mcc_customer_id)

    result = {}
    for r in rows:
        cid = str(r.get('campaign', {}).get('id', ''))
        channel_type = (r.get('campaign', {}).get('advertisingChannelType') or '').upper()
        # Skip phantom channel types (mirrors script's phantom budget fix)
        if channel_type in _PHANTOM_CHANNEL_TYPES:
            continue
        m = r.get('metrics', {})
        micros = int(m.get('costMicros', 0) or 0)
        clicks = int(m.get('clicks', 0) or 0)
        conversions = float(m.get('conversions', 0) or 0)
        if cid not in result:
            result[cid] = {'spend': 0.0, 'clicks': 0, 'conversions': 0.0}
        result[cid]['spend'] += micros / 1_000_000
        result[cid]['clicks'] += clicks
        result[cid]['conversions'] += conversions

    return result


def get_campaign_daily_spend(refresh_token: str, customer_id: str,
                              campaign_ids: list, month_start: date,
                              mcc_customer_id: str = None) -> dict:
    """Return daily spend per campaign for the chart (spend-vs-target line).

    Returns: {campaign_id: {date_str: spend_usd}, ...}
    """
    if not campaign_ids:
        return {}

    developer_token = os.environ.get('GOOGLE_ADS_DEVELOPER_TOKEN', '')
    access_token = get_access_token(refresh_token)

    yesterday = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
    start = month_start.isoformat()
    id_list = ', '.join(str(cid) for cid in campaign_ids)

    query = f"""
        SELECT
          campaign.id,
          segments.date,
          metrics.cost_micros
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{yesterday}'
          AND campaign.id IN ({id_list})
        ORDER BY segments.date
    """

    rows = _gaql(access_token, customer_id, developer_token, query, mcc_customer_id=mcc_customer_id)

    result = {}
    for r in rows:
        cid = str(r.get('campaign', {}).get('id', ''))
        d = r.get('segments', {}).get('date', '')
        micros = int(r.get('metrics', {}).get('costMicros', 0) or 0)
        if cid not in result:
            result[cid] = {}
        result[cid][d] = result[cid].get(d, 0.0) + micros / 1_000_000

    return result


# ---------------------------------------------------------------------------
# Budget updates
# ---------------------------------------------------------------------------

def update_campaign_budget(refresh_token: str, customer_id: str,
                            budget_resource_name: str, new_daily_usd: float,
                            mcc_customer_id: str = None) -> bool:
    """Update a campaign's daily budget.

    budget_resource_name: e.g. 'customers/123/campaignBudgets/456'
    new_daily_usd: the new daily budget in USD (will be converted to micros)
    Returns True on success.
    """
    developer_token = os.environ.get('GOOGLE_ADS_DEVELOPER_TOKEN', '')
    access_token = get_access_token(refresh_token)
    cid = customer_id.replace('-', '')

    new_micros = int(round(new_daily_usd * 1_000_000))

    url = f'{GOOGLE_ADS_API_BASE}/customers/{cid}/campaignBudgets:mutate'
    headers = _headers(access_token, cid, developer_token, mcc_customer_id)

    payload = {
        'operations': [{
            'update': {
                'resourceName': budget_resource_name,
                'amountMicros': str(new_micros),
            },
            'updateMask': 'amountMicros',
        }]
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    if not resp.ok:
        raise GoogleAdsError(f'Budget update failed ({resp.status_code}): {resp.text[:500]}')

    return True


def pause_campaigns(refresh_token: str, customer_id: str,
                    campaign_ids: list, mcc_customer_id: str = None) -> list:
    """Pause a list of campaigns. Returns list of successfully paused campaign IDs."""
    developer_token = os.environ.get('GOOGLE_ADS_DEVELOPER_TOKEN', '')
    access_token = get_access_token(refresh_token)
    cid = customer_id.replace('-', '')

    url = f'{GOOGLE_ADS_API_BASE}/customers/{cid}/campaigns:mutate'
    headers = _headers(access_token, cid, developer_token, mcc_customer_id)

    operations = [{
        'update': {
            'resourceName': f'customers/{cid}/campaigns/{campaign_id}',
            'status': 'PAUSED',
        },
        'updateMask': 'status',
    } for campaign_id in campaign_ids]

    resp = requests.post(url, json={'operations': operations}, headers=headers, timeout=15)
    if not resp.ok:
        raise GoogleAdsError(f'Campaign pause failed ({resp.status_code}): {resp.text[:500]}')

    return campaign_ids


def enable_campaigns(refresh_token: str, customer_id: str,
                     campaign_ids: list, mcc_customer_id: str = None) -> list:
    """Re-enable a list of campaigns. Returns list of successfully enabled campaign IDs."""
    developer_token = os.environ.get('GOOGLE_ADS_DEVELOPER_TOKEN', '')
    access_token = get_access_token(refresh_token)
    cid = customer_id.replace('-', '')

    url = f'{GOOGLE_ADS_API_BASE}/customers/{cid}/campaigns:mutate'
    headers = _headers(access_token, cid, developer_token, mcc_customer_id)

    operations = [{
        'update': {
            'resourceName': f'customers/{cid}/campaigns/{campaign_id}',
            'status': 'ENABLED',
        },
        'updateMask': 'status',
    } for campaign_id in campaign_ids]

    resp = requests.post(url, json={'operations': operations}, headers=headers, timeout=15)
    if not resp.ok:
        raise GoogleAdsError(f'Campaign enable failed ({resp.status_code}): {resp.text[:500]}')

    return campaign_ids


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

def get_lead_form_submissions(refresh_token: str, customer_id: str,
                               start_date: date, end_date: date,
                               mcc_customer_id: str = None) -> list:
    """Pull lead form submission data for a date range.

    Returns a list of lead records with campaign, ad group, and submission date.
    Note: Lead form data is available via the lead_form_submission_data resource.
    """
    developer_token = os.environ.get('GOOGLE_ADS_DEVELOPER_TOKEN', '')
    access_token = get_access_token(refresh_token)

    start = start_date.isoformat()
    end = end_date.isoformat()

    query = f"""
        SELECT
          lead_form_submission_data.id,
          lead_form_submission_data.campaign,
          lead_form_submission_data.ad_group,
          lead_form_submission_data.submission_date_time,
          lead_form_submission_data.lead_form_submission_fields
        FROM lead_form_submission_data
        WHERE lead_form_submission_data.submission_date_time >= '{start} 00:00:00'
          AND lead_form_submission_data.submission_date_time <= '{end} 23:59:59'
        ORDER BY lead_form_submission_data.submission_date_time DESC
    """

    try:
        rows = _gaql(access_token, customer_id, developer_token, query, mcc_customer_id=mcc_customer_id)
    except GoogleAdsError as e:
        # Lead form submissions may not be available for all account types
        logger.warning('Could not fetch lead form submissions: %s', e)
        return []

    leads = []
    for r in rows:
        lf = r.get('leadFormSubmissionData', {})
        fields = lf.get('leadFormSubmissionFields', [])
        field_data = {f.get('fieldType', ''): f.get('fieldValue', '') for f in fields}
        leads.append({
            'id': lf.get('id', ''),
            'campaign_resource': lf.get('campaign', ''),
            'ad_group_resource': lf.get('adGroup', ''),
            'submitted_at': lf.get('submissionDateTime', ''),
            'name': field_data.get('FULL_NAME', ''),
            'email': field_data.get('EMAIL', ''),
            'phone': field_data.get('PHONE_NUMBER', ''),
            'city': field_data.get('CITY', ''),
            'fields': field_data,
        })
    return leads
