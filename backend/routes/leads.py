"""
Leads routes — pull and export Google Ads lead form submissions.

GET  /api/leads/<account_id>/pull   — fetch leads from Google Ads API for a date range
GET  /api/leads/<account_id>/export — return leads as CSV download
"""

import csv
import io
import logging
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, jsonify, request, session

from database import Account, AccountSettings, GoogleOAuthToken, LeadExport, db
from google_ads_client import GoogleAdsError, get_lead_form_submissions
from routes.auth import login_required

logger = logging.getLogger(__name__)

leads_bp = Blueprint('leads', __name__, url_prefix='/api/leads')


@leads_bp.route('/<int:account_id>/pull', methods=['GET'])
@login_required
def pull_leads(account_id):
    """Pull lead form submissions from Google Ads for a date range.

    Query params:
      start_date: YYYY-MM-DD (default: first of current month)
      end_date:   YYYY-MM-DD (default: yesterday)
    """
    account = Account.query.get_or_404(account_id)
    settings = AccountSettings.query.filter_by(account_id=account_id).first()

    if not settings or not settings.track_leads:
        return jsonify({'error': 'Lead tracking is not enabled for this account. Enable it in Settings.'}), 400

    user_id = session['user_id']
    token = GoogleOAuthToken.query.filter_by(user_id=user_id, is_valid=True).first()
    if not token:
        return jsonify({'error': 'Google account not connected'}), 401

    today = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)

    start_str = request.args.get('start_date', month_start.isoformat())
    end_str = request.args.get('end_date', yesterday.isoformat())

    try:
        start_date = date.fromisoformat(start_str)
        end_date = date.fromisoformat(end_str)
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD.'}), 400

    try:
        leads = get_lead_form_submissions(
            token.refresh_token,
            account.google_customer_id,
            start_date,
            end_date,
            mcc_customer_id=account.mcc_customer_id,
        )
    except GoogleAdsError as e:
        logger.error('Lead pull failed for account %s: %s', account_id, e)
        return jsonify({'error': str(e)}), 502

    return jsonify({
        'leads': leads,
        'count': len(leads),
        'date_range': {'start': start_str, 'end': end_str},
    })


@leads_bp.route('/<int:account_id>/export', methods=['GET'])
@login_required
def export_leads_csv(account_id):
    """Export lead form submissions as a CSV file download.

    Query params: same as /pull (start_date, end_date)
    """
    account = Account.query.get_or_404(account_id)
    settings = AccountSettings.query.filter_by(account_id=account_id).first()

    if not settings or not settings.track_leads:
        return jsonify({'error': 'Lead tracking is not enabled for this account.'}), 400

    user_id = session['user_id']
    token = GoogleOAuthToken.query.filter_by(user_id=user_id, is_valid=True).first()
    if not token:
        return jsonify({'error': 'Google account not connected'}), 401

    today = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)

    start_str = request.args.get('start_date', month_start.isoformat())
    end_str = request.args.get('end_date', yesterday.isoformat())

    try:
        start_date = date.fromisoformat(start_str)
        end_date = date.fromisoformat(end_str)
    except ValueError:
        return jsonify({'error': 'Invalid date format'}), 400

    try:
        leads = get_lead_form_submissions(
            token.refresh_token,
            account.google_customer_id,
            start_date,
            end_date,
            mcc_customer_id=account.mcc_customer_id,
        )
    except GoogleAdsError as e:
        return jsonify({'error': str(e)}), 502

    # Build CSV
    output = io.StringIO()
    writer = csv.writer(output)

    # Header row
    writer.writerow(['Submitted At', 'Name', 'Email', 'Phone', 'City', 'Campaign'])

    for lead in leads:
        # Extract campaign name from resource name (last segment)
        campaign_resource = lead.get('campaign_resource', '')
        campaign_name = campaign_resource.split('/')[-1] if campaign_resource else ''
        writer.writerow([
            lead.get('submitted_at', ''),
            lead.get('name', ''),
            lead.get('email', ''),
            lead.get('phone', ''),
            lead.get('city', ''),
            campaign_name,
        ])

    # Log the export
    export_month = today.strftime('%Y-%m')
    log = LeadExport(
        account_id=account_id,
        export_month=export_month,
        lead_count=len(leads),
        status='COMPLETED',
    )
    db.session.add(log)
    db.session.commit()

    filename = f'leads_{account.account_name.replace(" ", "_")}_{start_str}_{end_str}.csv'
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )
