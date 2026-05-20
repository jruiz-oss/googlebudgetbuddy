"""
Google BudgetBuddy — Flask application entry point.

Start order:
  1. Create Flask app and configure from env vars
  2. Init SQLAlchemy (Neon Postgres)
  3. Register all blueprints
  4. Create tables on first boot (db.create_all)
  5. Start APScheduler for daily pacing (prod only)
  6. Run startup DB migrations for additive columns

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
import atexit

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request
from flask_cors import CORS
import sqlalchemy

from database import db

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s — %(message)s',
)
logger = logging.getLogger(__name__)
_scheduler_lock_conn = None


def _acquire_postgres_advisory_lock(lock_id: int, keep_open: bool = False):
    """Try to acquire a Postgres advisory lock.

    When keep_open=True, keeps the connection alive for the lifetime of the
    process so the advisory lock remains held.
    """
    global _scheduler_lock_conn

    database_url = os.environ.get('DATABASE_URL', '')
    if 'postgresql' not in database_url:
        return None, True

    conn = db.engine.connect()
    acquired = bool(conn.execute(
        sqlalchemy.text('SELECT pg_try_advisory_lock(:lock_id)'),
        {'lock_id': lock_id},
    ).scalar())

    if acquired:
        if not keep_open:
            return conn, True

        _scheduler_lock_conn = conn

        def _close_lock_conn():
            global _scheduler_lock_conn
            if _scheduler_lock_conn is not None:
                try:
                    _scheduler_lock_conn.close()
                except Exception:
                    pass
                _scheduler_lock_conn = None

        atexit.register(_close_lock_conn)
        return conn, True

    conn.close()
    return None, False


def _run_lightweight_migrations():
    """Apply additive Postgres columns that db.create_all() won't add later."""
    database_url = os.environ.get('DATABASE_URL', '')
    if 'postgresql' not in database_url:
        return

    statements = [
        ('campaigns.budget_label',
         'ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS budget_label VARCHAR(100)'),
        ('campaigns.campaign_filter',
         'ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS campaign_filter VARCHAR(100)'),
        ('campaigns.current_daily_budget',
         'ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS current_daily_budget DOUBLE PRECISION'),
        ('pacing_data.clicks',
         'ALTER TABLE pacing_data ADD COLUMN IF NOT EXISTS clicks INTEGER'),
        ('pacing_data.conversions',
         'ALTER TABLE pacing_data ADD COLUMN IF NOT EXISTS conversions DOUBLE PRECISION'),
        ('pacing_data.cpc',
         'ALTER TABLE pacing_data ADD COLUMN IF NOT EXISTS cpc DOUBLE PRECISION'),
        ('campaigns.google_end_date',
         'ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS google_end_date DATE'),
        ('campaigns.google_status',
         "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS google_status VARCHAR(50)"),
    ]

    with db.engine.begin() as conn:
        for label, statement in statements:
            conn.execute(sqlalchemy.text(statement))
            logger.info('Migration checked: %s', label)


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
    from routes.webhook import webhook_bp

    for bp in [auth_bp, oauth_bp, accounts_bp, campaigns_bp,
               pacing_bp, settings_bp, history_bp, sheets_bp, leads_bp, webhook_bp]:
        app.register_blueprint(bp)

    # ── Health check ──────────────────────────────────────────────────────────
    @app.route('/api/health')
    def health():
        return jsonify({'status': 'ok', 'service': 'google-budgetbuddy'})

    # ── Create tables ─────────────────────────────────────────────────────────
    if not os.environ.get('SKIP_CREATE_ALL'):
        with app.app_context():
            try:
                lock_conn, acquired = _acquire_postgres_advisory_lock(9876543210)
                if acquired:
                    db.create_all()
                    logger.info('db.create_all() completed')
                    _run_lightweight_migrations()
                    if lock_conn is not None:
                        lock_conn.close()
                else:
                    logger.info('db.create_all() skipped — advisory lock held by another worker')
            except Exception as e:
                logger.warning('db.create_all() skipped (SQLite or lock held): %s', e)
                db.create_all()
                _run_lightweight_migrations()

    return app


# ── Scheduled pacing job ──────────────────────────────────────────────────────

def _scheduled_pacing_job(app):
    """Run pacing for every account — fires daily at 06:00 UTC."""
    with app.app_context():
        from database import Account, AccountSettings, GoogleOAuthToken, PacingRun
        from routes.sheets import sync_sheet_budgets_for_account

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
                        sync_sheet_budgets_for_account(account.id)
                    except Exception as e:
                        logger.warning('Sheet sync failed for account %s: %s', account.id, e)

                # Run pacing
                from routes.pacing import run_pacing_for_account
                run_pacing_for_account(account, token.refresh_token, triggered_by='scheduler')

            except Exception as e:
                logger.error('Scheduled pacing failed for account %s: %s', account.id, e)


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
    with app.app_context():
        try:
            _lock_conn, acquired = _acquire_postgres_advisory_lock(9876543211, keep_open=True)
        except Exception as e:
            logger.warning('APScheduler lock check failed; starting scheduler anyway: %s', e)
            acquired = True

    if acquired:
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
    else:
        logger.info('APScheduler not started in this worker — advisory lock held by another worker')


if __name__ == '__main__':
    app.run(debug=True, port=5000)
