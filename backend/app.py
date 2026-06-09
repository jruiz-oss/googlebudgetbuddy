"""
Google BudgetBuddy — Flask application entry point.
(redeploy trigger: 2026-05-20)

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
  DISABLE_HOURLY_AUTOPAUSE — set to 'true' to skip the hourly auto-pause job only
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
        ('user_settings table',
         """CREATE TABLE IF NOT EXISTS user_settings (
             id SERIAL PRIMARY KEY,
             user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
             anthropic_api_key VARCHAR(500),
             created_at TIMESTAMP DEFAULT NOW(),
             updated_at TIMESTAMP DEFAULT NOW()
         )"""),
        ('monthly_reports table',
         """CREATE TABLE IF NOT EXISTS monthly_reports (
             id SERIAL PRIMARY KEY,
             account_id INTEGER NOT NULL REFERENCES accounts(id),
             year INTEGER NOT NULL,
             month INTEGER NOT NULL,
             notes TEXT,
             generated_summary TEXT,
             last_generated_at TIMESTAMP,
             created_at TIMESTAMP DEFAULT NOW(),
             updated_at TIMESTAMP DEFAULT NOW(),
             UNIQUE(account_id, year, month)
         )"""),
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
    from routes.reports import reports_bp

    for bp in [auth_bp, oauth_bp, accounts_bp, campaigns_bp,
               pacing_bp, settings_bp, history_bp, sheets_bp, leads_bp, webhook_bp, reports_bp]:
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
    """Run pacing for every account — fires every 2 hours at :00.

    Same fresh-reload-per-iteration pattern as _run_pacing_all_job: bulk-loading
    accounts up front leaves them stale after the first run_pacing_for_account()
    commit (expire_on_commit=True), which silently corrupts segment maps for
    accounts 2..N and reintroduces the 2x MTD spend bug. We re-query each
    account inside the loop with pacing_data pre-loaded.
    """
    from sqlalchemy.orm import selectinload

    with app.app_context():
        from database import Account, Campaign, GoogleOAuthToken
        from routes.sheets import sync_sheet_budgets_for_account
        from routes.pacing import run_pacing_for_account

        account_ids = [row[0] for row in db.session.query(Account.id).all()]
        logger.info('Scheduled pacing: processing %d account(s)', len(account_ids))

        for account_id in account_ids:
            try:
                account = (
                    Account.query
                    .options(
                        selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
                        selectinload(Account.settings),
                    )
                    .get(account_id)
                )
                if account is None:
                    continue

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

                # Sheet sync first so budgets are current. Reload account
                # afterwards regardless of success/failure so pacing always
                # sees fresh objects + post-sync monthly_budget.
                if settings.google_sheet_id:
                    try:
                        sync_sheet_budgets_for_account(account.id)
                    except Exception as e:
                        logger.warning('Sheet sync failed for account %s: %s', account.id, e)
                    account = (
                        Account.query
                        .options(
                            selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
                            selectinload(Account.settings),
                        )
                        .get(account_id)
                    )

                # Run pacing through the shared core.
                run_pacing_for_account(account, token.refresh_token, triggered_by='scheduler')

            except Exception as e:
                logger.error('Scheduled pacing failed for account %s: %s', account_id, e)


# ── Hourly auto-pause job ──────────────────────────────────────────────────────

def _hourly_auto_pause_job(app):
    """Pause over-budget accounts — fires every hour.

    The 06:00 UTC daily run only catches an account once a day; a campaign can
    blow well past its cap mid-afternoon and keep spending until the next
    morning. This lightweight hourly check closes that gap.

    For each account it fetches CURRENT MTD spend (one Google Ads call via the
    shared _execute_pacing_run core — no sheet sync/writeback), compares total
    spend to total budget at the segment level (identical math to the daily
    run), and if the account is at/over its auto-pause threshold it pauses every
    active campaign and records an AUTO PauseEvent.

    Skipped accounts:
      • auto_pause_enabled = False (the feature is opt-in per account),
      • Grant accounts ("grant" in the name) — exempt by business rule B,
      • accounts with no valid OAuth token or no budget.

    Once an account's campaigns are paused there are no active campaigns left, so
    the next hourly pass is a no-op for it — no duplicate PauseEvent spam.
    """
    import json as _json
    from datetime import datetime
    from sqlalchemy.orm import selectinload

    with app.app_context():
        from database import Account, Campaign, GoogleOAuthToken, PauseEvent
        from routes.pacing import _execute_pacing_run, _effective_mcc_customer_id
        from google_ads_client import pause_campaigns, GoogleAdsError

        today = datetime.utcnow().date()
        account_ids = [row[0] for row in db.session.query(Account.id).all()]
        logger.info('Hourly auto-pause: scanning %d account(s)', len(account_ids))

        for account_id in account_ids:
            try:
                account = (
                    Account.query
                    .options(
                        selectinload(Account.campaigns).selectinload(Campaign.pacing_data),
                        selectinload(Account.settings),
                    )
                    .get(account_id)
                )
                if account is None:
                    continue

                settings = account.settings
                if not settings or not settings.auto_pause_enabled:
                    continue

                # Business rule B: Grant accounts are exempt from auto-pause.
                if 'grant' in (account.account_name or '').lower():
                    continue

                token = GoogleOAuthToken.query.filter_by(
                    user_id=account.user_id, is_valid=True
                ).first()
                if not token:
                    continue

                result = _execute_pacing_run(
                    account, token.refresh_token, today,
                    log_prefix='hourly_auto_pause',
                )
                # _execute_pacing_run may delete/replace today's PacingData rows;
                # commit so that work isn't rolled back when we move on.
                db.session.commit()

                if not result['ok'] or result['no_campaigns'] or result['no_active']:
                    continue

                seg_budget_map = result['seg_budget_map']
                seg_spend_map  = result['seg_spend_map']
                total_budget = sum(seg_budget_map.values())
                total_spend  = sum(seg_spend_map.values())

                if total_budget > 0:
                    spend_pct = (total_spend / total_budget) * 100
                    if spend_pct < settings.auto_pause_threshold:
                        continue
                else:
                    # No budget configured for this account. A $0/blank budget is
                    # ambiguous — it can mean an intentional zero OR a sheet sync that
                    # hasn't run/failed — so this branch ONLY fires at the strictest
                    # 100% threshold. There the intent is "no spend without a budget,"
                    # and any spend at all is over a $0 cap → pause. At any lower
                    # threshold a zero budget is still skipped (can't compute a %).
                    if settings.auto_pause_threshold >= 100 and total_spend > 0:
                        spend_pct = 100.0
                        logger.warning(
                            'Hourly auto-pause: account_id=%s name=%r has $0/no budget '
                            'but spent %.2f at 100%% threshold — pausing. Confirm the '
                            'budget sheet actually synced (a failed sync looks identical).',
                            account.id, account.account_name, total_spend,
                        )
                    else:
                        continue

                active = [c for c in result['active_campaigns'] if c.is_active]
                campaign_ids = [c.google_campaign_id for c in active]
                if not campaign_ids:
                    continue

                logger.warning(
                    'Hourly auto-pause TRIGGERED: account_id=%s name=%r spend=%.2f budget=%.2f pct=%.1f threshold=%.1f',
                    account.id, account.account_name, total_spend, total_budget,
                    spend_pct, settings.auto_pause_threshold,
                )

                try:
                    pause_campaigns(
                        token.refresh_token,
                        account.google_customer_id,
                        campaign_ids,
                        mcc_customer_id=_effective_mcc_customer_id(account),
                    )
                except GoogleAdsError as e:
                    logger.error(
                        'Hourly auto-pause: pause failed for account %s: %s',
                        account.id, e,
                    )
                    continue

                db.session.add(PauseEvent(
                    account_id=account.id,
                    spend_at_pause=round(total_spend, 2),
                    budget_at_pause=round(total_budget, 2),
                    threshold_pct=settings.auto_pause_threshold,
                    paused_campaign_names=_json.dumps([c.campaign_name for c in active]),
                    triggered_by='AUTO',
                ))
                db.session.commit()

            except Exception as e:
                db.session.rollback()
                logger.error('Hourly auto-pause failed for account %s: %s', account_id, e)


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
            hour='*/2',
            minute=0,
            args=[app],
            id='hourly_pacing',
            replace_existing=True,
        )
        if not os.environ.get('DISABLE_HOURLY_AUTOPAUSE'):
            scheduler.add_job(
                _hourly_auto_pause_job,
                'cron',
                minute=30,
                args=[app],
                id='hourly_auto_pause',
                replace_existing=True,
            )
            logger.info('APScheduler — hourly auto-pause scheduled at :30 past each hour')
        scheduler.start()
        logger.info('APScheduler started — pacing every 2 hours at :00')
    else:
        logger.info('APScheduler not started in this worker — advisory lock held by another worker')


if __name__ == '__main__':
    app.run(debug=True, port=5000)
