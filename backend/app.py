"""
Google BudgetBuddy — Flask application entry point.

Start order:
  1. Create Flask app and configure from env vars
  2. Init SQLAlchemy (Neon Postgres)
  3. Register all blueprints
  4. Create tables on first boot (db.create_all)
  5. Start APScheduler for daily pacing (prod only)

Environment variables required in production (set on Railway):
  DATABASE_URL           — Neon PostgreSQL connection string
  SECRET_KEY             — random string for session signing
  GOOGLE_ADS_CLIENT_ID
  GOOGLE_ADS_CLIENT_SECRET
  GOOGLE_ADS_DEVELOPER_TOKEN
  GOOGLE_ADS_MCC_ID      — your MCC customer ID (no dashes)
  BACKEND_URL            — full Railway URL, e.g. https://xxx.railway.app
  FRONTEND_URL           — full Vercel URL, e.g. https://xxx.vercel.app
  CORS_ORIGINS           — same as FRONTEND_URL (comma-separated if multiple)

Optional:
  INVITE_CODE            — if set, registration requires this code
  CRON_SECRET            — protects POST /api/cron/run-all-accounts
  GOOGLE_CREDENTIALS_JSON — service account JSON for Google Sheets
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM — for digest emails
  DISABLE_SCHEDULER      — set to 'true' to skip APScheduler
  SKIP_CREATE_ALL        — set to 'true' to skip db.create_all() on boot
"""

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request
from flask_cors import CORS

from database import db

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s — %(message)s',
)
logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)

    # ── Configuration ─────────────────────────────────────────────────────────
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///dev.db')
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
        'connect_args': {'sslmode': 'require'} if 'postgresql' in os.environ.get('DATABASE_URL', '') else {},
    }
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Sessions last 30 days
    from datetime import timedelta
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
    app.config['SESSION_COOKIE_SAMESITE'] = 'None'
    app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins_raw = os.environ.get('CORS_ORIGINS', 'http://localhost:3000')
    cors_origins = [o.strip() for o in cors_origins_raw.split(',') if o.strip()]
    CORS(app, origins=cors_origins, supports_credentials=True)

    # ── Database ──────────────────────────────────────────────────────────────
    db.init_app(app)

    # ── Blueprints ────────────────────────────────────────────────────────────
    from routes.auth import auth_bp
    from routes.oauth import oauth_bp
    from routes.accounts import accounts_bp
    from routes.campaigns import campaigns_bp
    from routes.pacing import pacing_bp
    from routes.settings import settings_bp
    from routes.history import history_bp
    from routes.sheets import sheets_bp
    from routes.leads import leads_bp

    for bp in [auth_bp, oauth_bp, accounts_bp, campaigns_bp,
               pacing_bp, settings_bp, history_bp, sheets_bp, leads_bp]:
        app.register_blueprint(bp)

    # ── Health check ──────────────────────────────────────────────────────────
    @app.route('/api/health')
    def health():
        return jsonify({'status': 'ok', 'service': 'google-budgetbuddy'})

    # ── Create tables ─────────────────────────────────────────────────────────
    if not os.environ.get('SKIP_CREATE_ALL'):
        with app.app_context():
            import sqlalchemy
            try:
                with db.engine.connect() as conn:
                    conn.execute(sqlalchemy.text(
                        'SELECT pg_try_advisory_lock(9876543210)'
                    ))
                db.create_all()
                logger.info('db.create_all() completed')
            except Exception as e:
                logger.warning('db.create_all() skipped (SQLite or lock held): %s', e)
                db.create_all()

    return app


# ── Scheduled pacing job ──────────────────────────────────────────────────────

def _scheduled_pacing_job(app):
    """Run pacing for every account — fires daily at 06:00 UTC."""
    with app.app_context():
        from database import Account, AccountSettings, GoogleOAuthToken, PacingRun
        from routes.sheets import sync_budgets_for_account

        accounts = Account.query.all()
        logger.info('Scheduled pacing: processing %d account(s)', len(accounts))

        for account in accounts:
            try:
                settings = account.settings
                if not settings:
                    continue

                # Find a valid OAuth token (use the account owner's token)
                token = GoogleOAuthToken.query.filter_by(
                    user_id=account.user_id, is_valid=True
                ).first()
                if not token:
                    logger.warning('No valid token for account %s — skipping', account.id)
                    continue

                # Sheet sync first
                if settings.google_sheet_id:
                    try:
                        sync_budgets_for_account(account.id)
                    except Exception as e:
                        logger.warning('Sheet sync failed for account %s: %s', account.id, e)

                # Run pacing (reuse the /run route logic directly)
                from routes.pacing import run_pacing as _run_pacing
                # Simulate a request context — use internal helper instead
                _run_pacing_for_account(account, token, app)

            except Exception as e:
                logger.error('Scheduled pacing failed for account %s: %s', account.id, e)


def _run_pacing_for_account(account, token, app):
    """Internal pacing runner used by the scheduler (no Flask request context needed)."""
    from datetime import datetime, timedelta
    from database import (
        AccountSettings, Campaign, GoogleOAuthToken,
        PacingData, PacingRun, db,
    )
    from google_ads_client import get_campaign_mtd_spend, GoogleAdsError
    from routes.pacing import _month_bounds, _compute_recommendation, _campaign_is_active_today

    today = datetime.utcnow().date()
    month_start, _ = _month_bounds(today)
    settings = account.settings

    is_grant_account = 'grant' in account.account_name.lower()
    if is_grant_account:
        logger.info('Scheduled pacing: account %s is a Grant account — auto-pause exempt', account.id)

    active_campaigns = [c for c in account.campaigns if c.is_active and _campaign_is_active_today(c, today)]
    if not active_campaigns:
        return

    campaign_ids = [c.google_campaign_id for c in active_campaigns]
    try:
        # Returns {campaign_id: {'spend': float, 'clicks': int, 'conversions': float}}
        metrics_by_id = get_campaign_mtd_spend(
            token.refresh_token,
            account.google_customer_id,
            campaign_ids,
            month_start,
            mcc_customer_id=account.mcc_customer_id,
        )
    except GoogleAdsError as e:
        logger.error('Scheduled spend fetch failed for account %s: %s', account.id, e)
        run = PacingRun(
            account_id=account.id,
            run_type='AUTO',
            triggered_by='scheduler',
            campaigns_processed=0,
            adjustments_made=0,
            status='FAILED',
            error_message=str(e),
        )
        db.session.add(run)
        db.session.commit()
        return

    processed = 0
    for campaign in active_campaigns:
        campaign_metrics = metrics_by_id.get(campaign.google_campaign_id, {})
        actual_spend = campaign_metrics.get('spend', 0.0)
        clicks = campaign_metrics.get('clicks', None)
        conversions = campaign_metrics.get('conversions', None)
        cpc = round(actual_spend / clicks, 2) if clicks and clicks > 0 else None

        latest_rows = sorted(
            campaign.pacing_data,
            key=lambda r: (r.date or datetime.min.date(), r.id or 0),
        )
        current_daily = latest_rows[-1].current_daily_budget if latest_rows and latest_rows[-1].current_daily_budget else 0.0

        rec, status, pace_ratio, expected_mtd, daily_target = _compute_recommendation(
            campaign.monthly_budget, actual_spend, current_daily, today
        )
        _, month_end = _month_bounds(today)
        days_in_month = (month_end - month_start).days + 1
        days_elapsed = (today - month_start).days + 1
        change_pct = ((rec - current_daily) / current_daily * 100) if current_daily > 0 else 0.0

        snap = PacingData(
            campaign_id=campaign.id,
            date=today,
            current_daily_budget=current_daily,
            actual_spend=round(actual_spend, 2),
            expected_spend=round(expected_mtd, 2),
            pace_ratio=round(pace_ratio, 3),
            recommended_daily_budget=round(rec, 2),
            change_percent=round(change_pct, 1),
            status=status,
            clicks=clicks,
            conversions=round(conversions, 1) if conversions is not None else None,
            cpc=cpc,
        )
        db.session.add(snap)
        processed += 1

    run = PacingRun(
        account_id=account.id,
        run_type='AUTO',
        triggered_by='scheduler',
        campaigns_processed=processed,
        adjustments_made=0,
        status='COMPLETED',
    )
    db.session.add(run)
    db.session.commit()
    logger.info('Scheduled pacing completed for account %s: %d campaigns', account.id, processed)

    # Write spend back to sheet
    if settings and settings.google_sheet_id:
        try:
            from routes.sheets import write_spend_for_account
            write_spend_for_account(account.id)
        except Exception as e:
            logger.warning('Scheduled sheet write failed for account %s: %s', account.id, e)


# ── Manual cron endpoint ───────────────────────────────────────────────────────

app = create_app()


@app.route('/api/cron/run-all-accounts', methods=['POST'])
def cron_run_all():
    """External cron trigger. Protected by X-Cron-Secret header."""
    secret = os.environ.get('CRON_SECRET', '')
    if secret and request.headers.get('X-Cron-Secret') != secret:
        return jsonify({'error': 'Unauthorized'}), 401
    _scheduled_pacing_job(app)
    return jsonify({'message': 'Pacing job completed'})


# ── Start scheduler ────────────────────────────────────────────────────────────

if os.environ.get('FLASK_ENV') == 'production' and not os.environ.get('DISABLE_SCHEDULER'):
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _scheduled_pacing_job,
        'cron',
        hour=6,
        minute=0,
        args=[app],
        id='daily_pacing',
        replace_existing=True,
    )
    scheduler.start()
    logger.info('APScheduler started — daily pacing at 06:00 UTC')


if __name__ == '__main__':
    app.run(debug=True, port=5000)
