"""History routes — pacing runs, budget adjustments, pause events."""

from flask import Blueprint, jsonify, request

from database import Account, BudgetAdjustment, Campaign, PacingRun, PauseEvent, db
from routes.auth import login_required

history_bp = Blueprint('history', __name__, url_prefix='/api/history')


@history_bp.route('/<int:account_id>/pacing-runs', methods=['GET'])
@login_required
def pacing_runs(account_id):
    Account.query.get_or_404(account_id)
    limit = min(int(request.args.get('limit', 50)), 200)
    runs = (
        PacingRun.query
        .filter_by(account_id=account_id)
        .order_by(PacingRun.run_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({'runs': [r.to_dict() for r in runs]})


@history_bp.route('/<int:account_id>/adjustments', methods=['GET'])
@login_required
def adjustments(account_id):
    account = Account.query.get_or_404(account_id)
    campaign_ids = [c.id for c in account.campaigns]
    limit = min(int(request.args.get('limit', 100)), 500)
    adjs = (
        BudgetAdjustment.query
        .filter(BudgetAdjustment.campaign_id.in_(campaign_ids))
        .order_by(BudgetAdjustment.applied_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({'adjustments': [a.to_dict() for a in adjs]})


@history_bp.route('/<int:account_id>/pause-events', methods=['GET'])
@login_required
def pause_events(account_id):
    Account.query.get_or_404(account_id)
    limit = min(int(request.args.get('limit', 50)), 200)
    events = (
        PauseEvent.query
        .filter_by(account_id=account_id)
        .order_by(PauseEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({'events': [e.to_dict() for e in events]})
