"""
Campaign routes — list, update, remove tracked campaigns, pacing history chart data.
"""

import logging

from flask import Blueprint, jsonify, request, session
from sqlalchemy.orm import selectinload

from database import Account, Campaign, GoogleOAuthToken, PacingData, db, visible_latest_campaigns
from routes.auth import login_required

logger = logging.getLogger(__name__)

campaigns_bp = Blueprint('campaigns', __name__, url_prefix='/api/campaigns')


@campaigns_bp.route('/account/<int:account_id>', methods=['GET'])
@login_required
def get_campaigns(account_id):
    """Return campaigns for an account that should be visible in the dashboard.

    Includes canonical live campaigns even at $0 MTD spend so current daily
    budgets/segment membership are visible, plus inactive campaigns that spent
    in the latest pacing run.
    """
    Account.query.get_or_404(account_id)
    all_campaigns = (
        Campaign.query
        .options(selectinload(Campaign.pacing_data))
        .filter_by(account_id=account_id)
        .order_by(Campaign.campaign_name)
        .all()
    )
    visible = visible_latest_campaigns(all_campaigns)
    return jsonify({'campaigns': [c.to_dict() for c in visible]})


@campaigns_bp.route('/all', methods=['GET'])
@login_required
def get_all_campaigns():
    """Return all accounts with their campaigns — used by the Home page."""
    accounts = (
        Account.query
        .options(
            selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
            selectinload(Account.settings),
        )
        .order_by(Account.account_name)
        .all()
    )
    return jsonify({'accounts': [a.to_dict() for a in accounts]})


@campaigns_bp.route('/<int:campaign_id>', methods=['GET'])
@login_required
def get_campaign(campaign_id):
    """Return a single campaign with full pacing history."""
    campaign = (
        Campaign.query
        .options(selectinload(Campaign.pacing_data))
        .get_or_404(campaign_id)
    )
    return jsonify({'campaign': campaign.to_dict()})


@campaigns_bp.route('/<int:campaign_id>', methods=['PUT'])
@login_required
def update_campaign(campaign_id):
    """Update campaign settings (monthly_budget, flight dates)."""
    campaign = Campaign.query.get_or_404(campaign_id)
    data = request.get_json() or {}

    if 'monthly_budget' in data:
        val = float(data['monthly_budget'])
        if val < 0:
            return jsonify({'error': 'monthly_budget cannot be negative'}), 400
        campaign.monthly_budget = val
    if 'flight_type' in data:
        if data['flight_type'] not in ('ALWAYS_ON', 'LIMITED'):
            return jsonify({'error': 'flight_type must be ALWAYS_ON or LIMITED'}), 400
        campaign.flight_type = data['flight_type']
    if 'flight_start_date' in data:
        try:
            from datetime import date
            campaign.flight_start_date = date.fromisoformat(data['flight_start_date']) if data['flight_start_date'] else None
        except ValueError:
            return jsonify({'error': 'Invalid flight_start_date format (use YYYY-MM-DD)'}), 400
    if 'flight_end_date' in data:
        try:
            from datetime import date
            campaign.flight_end_date = date.fromisoformat(data['flight_end_date']) if data['flight_end_date'] else None
        except ValueError:
            return jsonify({'error': 'Invalid flight_end_date format (use YYYY-MM-DD)'}), 400
    if 'is_active' in data:
        campaign.is_active = bool(data['is_active'])

    db.session.commit()
    return jsonify({'campaign': campaign.to_dict()})


@campaigns_bp.route('/<int:campaign_id>', methods=['DELETE'])
@login_required
def remove_campaign(campaign_id):
    """Soft-remove a campaign (sets is_active=False)."""
    campaign = Campaign.query.get_or_404(campaign_id)
    campaign.is_active = False
    db.session.commit()
    return jsonify({'message': 'Campaign removed from tracking'})


@campaigns_bp.route('/<int:campaign_id>/pacing-history', methods=['GET'])
@login_required
def pacing_history(campaign_id):
    """Return daily pacing snapshots for the spend-vs-target chart."""
    campaign = Campaign.query.get_or_404(campaign_id)
    rows = (
        PacingData.query
        .filter_by(campaign_id=campaign_id)
        .order_by(PacingData.date.asc(), PacingData.id.asc())
        .all()
    )
    return jsonify({
        'campaign_id': campaign_id,
        'campaign_name': campaign.campaign_name,
        'monthly_budget': campaign.monthly_budget,
        'history': [r.to_dict() for r in rows],
    })
