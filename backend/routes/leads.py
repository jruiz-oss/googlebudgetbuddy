"""
Leads routes — pull and export Google Ads lead form submissions.

GET  /api/leads/<account_id>/pull   — fetch leads from Google Ads API for a date range
GET  /api/leads/<account_id>/export — return leads as CSV download
"""

import io
import logging
import os
from datetime import date, datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font

from flask import Blueprint, Response, jsonify, request, session

from database import Account, AccountSettings, GoogleOAuthToken, LeadExport, db
from google_ads_client import GoogleAdsError, diagnose_lead_form_setup, get_lead_form_submissions
from routes.auth import login_required

logger = logging.getLogger(__name__)

leads_bp = Blueprint('leads', __name__, url_prefix='/api/leads')


def _effective_mcc_customer_id(account):
    """Use the account-specific MCC when present, otherwise fall back to env.

    Mirrors routes.pacing._effective_mcc_customer_id so the lead pull sends the
    same login-customer-id header pacing uses. Without this, accounts with a
    null mcc_customer_id get no manager header and Google returns
    USER_PERMISSION_DENIED on client-account queries.
    """
    return (account.mcc_customer_id or os.environ.get('GOOGLE_ADS_MCC_ID', '')).replace('-', '').strip() or None


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
            mcc_customer_id=_effective_mcc_customer_id(account),
        )
    except GoogleAdsError as e:
        logger.error('Lead pull failed for account %s: %s', account_id, e)
        return jsonify({'error': str(e)}), 502

    payload = {
        'leads': leads,
        'count': len(leads),
        'date_range': {'start': start_str, 'end': end_str},
    }

    # Zero leads with a clean 200 is ambiguous — run probes to say WHY.
    if not leads:
        try:
            diag = diagnose_lead_form_setup(
                token.refresh_token,
                account.google_customer_id,
                mcc_customer_id=_effective_mcc_customer_id(account),
            )
            logger.info('Lead pull diagnostics for account %s: %s', account_id, diag)
            payload['diagnostic_data'] = diag

            if diag['errors']:
                payload['diagnostic'] = 'Diagnostic probes hit errors: ' + '; '.join(diag['errors'])
            elif diag['lead_form_asset_count'] == 0:
                payload['diagnostic'] = (
                    'No lead form assets are visible on this Google Ads account. '
                    'Either the account has no native lead form assets, or BudgetBuddy is '
                    'querying the wrong customer ID for this account.'
                )
            elif diag['unfiltered_submission_count'] == 0:
                payload['diagnostic'] = (
                    f"Found {diag['lead_form_asset_count']} lead form asset(s), but Google "
                    'returned zero submissions even with no date filter. Note: the API only '
                    'retains lead form submissions for 60 days (UI download: 30 days). If '
                    'leads from the last 60 days exist in the Google Ads UI but not here, '
                    'this is an API visibility issue (check the OAuth user and '
                    'login-customer-id).'
                )
            else:
                samples = ', '.join(diag['sample_submission_datetimes'])
                payload['diagnostic'] = (
                    f"Google has {diag['unfiltered_submission_count']}+ submissions, but the "
                    'date filter matched none. Most recent submission timestamps: '
                    f'{samples}. Compare these against your selected range — if they fall '
                    'inside it, the date filter format is the bug.'
                )
        except Exception as e:  # diagnostics must never break the pull itself
            logger.warning('Lead pull diagnostics failed for account %s: %s', account_id, e)

    return jsonify(payload)


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
            mcc_customer_id=_effective_mcc_customer_id(account),
        )
    except GoogleAdsError as e:
        return jsonify({'error': str(e)}), 502

    # Build Excel workbook — base columns plus one column per extra answer
    # found across all leads (standard fields beyond the base four + custom
    # questions). Real .xlsx so it opens in Excel/Numbers, not a text editor.
    BASE_FIELDS = ('FULL_NAME', 'EMAIL', 'PHONE_NUMBER', 'CITY')
    extra_std = sorted({k for lead in leads for k in lead.get('fields', {}) if k and k not in BASE_FIELDS})
    custom_qs = sorted({q for lead in leads for q in lead.get('custom_fields', {}) if q})

    header = ['Submitted At', 'Name', 'Email', 'Phone', 'City', 'Campaign']
    header += [k.replace('_', ' ').title() for k in extra_std]
    header += custom_qs

    wb = Workbook()
    ws = wb.active
    ws.title = 'Leads'
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for lead in leads:
        # Extract campaign name from resource name (last segment)
        campaign_resource = lead.get('campaign_resource', '')
        campaign_name = campaign_resource.split('/')[-1] if campaign_resource else ''
        row = [
            lead.get('submitted_at', ''),
            lead.get('name', ''),
            lead.get('email', ''),
            lead.get('phone', ''),
            lead.get('city', ''),
            campaign_name,
        ]
        row += [lead.get('fields', {}).get(k, '') for k in extra_std]
        row += [lead.get('custom_fields', {}).get(q, '') for q in custom_qs]
        ws.append(row)

    # Reasonable column widths
    for col in ws.columns:
        width = max(len(str(c.value)) if c.value else 0 for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 50)

    output = io.BytesIO()
    wb.save(output)

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

    filename = f'leads_{account.account_name.replace(" ", "_")}_{start_str}_{end_str}.xlsx'
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )
