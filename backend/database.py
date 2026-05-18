from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from sqlalchemy.types import String, TypeDecorator

db = SQLAlchemy()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    accounts = db.relationship('Account', backref='user', lazy=True, cascade='all, delete-orphan')
    oauth_token = db.relationship('GoogleOAuthToken', backref='user', lazy=True, uselist=False, cascade='all, delete-orphan')

    def to_dict(self):
        has_token = self.oauth_token is not None and self.oauth_token.is_valid
        return {
            'id': self.id,
            'email': self.email,
            'has_google_token': has_token,
            'created_at': self.created_at.isoformat(),
        }


class GoogleOAuthToken(db.Model):
    """Stores the Google OAuth refresh token for a user.

    One token per user — all accounts under the same user share it.
    The refresh token is long-lived; access tokens are short-lived (1 hour)
    and are fetched on demand via google_ads_client.get_access_token().
    """
    __tablename__ = 'google_oauth_tokens'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    refresh_token = db.Column(db.String(1000), nullable=False)
    access_token = db.Column(db.String(1000), nullable=True)
    token_expires_at = db.Column(db.DateTime, nullable=True)
    is_valid = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'is_valid': self.is_valid,
            'token_expires_at': self.token_expires_at.isoformat() if self.token_expires_at else None,
            'created_at': self.created_at.isoformat(),
        }


class Account(db.Model):
    """A Google Ads account (customer) tracked in the app.

    google_customer_id is the 10-digit customer ID without dashes, e.g. '1234567890'.
    This can be either a standalone account or a client account under an MCC.
    """
    __tablename__ = 'accounts'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    account_name = db.Column(db.String(255), nullable=False)
    google_customer_id = db.Column(db.String(50), nullable=False)  # e.g. '1234567890'
    mcc_customer_id = db.Column(db.String(50), nullable=True)      # manager account ID if applicable
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    campaigns = db.relationship('Campaign', backref='account', lazy=True, cascade='all, delete-orphan')
    settings = db.relationship('AccountSettings', backref='account', lazy=True, uselist=False, cascade='all, delete-orphan')
    pacing_runs = db.relationship('PacingRun', backref='account', lazy=True, cascade='all, delete-orphan')
    pause_events = db.relationship('PauseEvent', backref='account', lazy=True, cascade='all, delete-orphan')

    def to_dict(self, lite=False):
        if lite:
            return {
                'id': self.id,
                'user_id': self.user_id,
                'account_name': self.account_name,
                'google_customer_id': self.google_customer_id,
                'mcc_customer_id': self.mcc_customer_id,
                'created_at': self.created_at.isoformat() if self.created_at else None,
            }

        active_campaigns = [c for c in self.campaigns if c.is_active]
        total_monthly_budget = sum((c.monthly_budget or 0) for c in active_campaigns)

        on_track = over_pacing = under_pacing = 0
        for c in active_campaigns:
            rows = sorted(
                (p for p in (c.pacing_data or [])),
                key=lambda r: (r.date or datetime.min.date(), r.id or 0),
            )
            latest = rows[-1] if rows else None
            status = getattr(latest, 'status', None)
            if status == 'ON_PACE':
                on_track += 1
            elif status == 'INCREASE':
                under_pacing += 1
            elif status == 'DECREASE':
                over_pacing += 1

        if over_pacing:
            status_category = 'over_pacing'
        elif under_pacing:
            status_category = 'under_pacing'
        else:
            status_category = 'on_track'

        return {
            'id': self.id,
            'user_id': self.user_id,
            'account_name': self.account_name,
            'google_customer_id': self.google_customer_id,
            'mcc_customer_id': self.mcc_customer_id,
            'created_at': self.created_at.isoformat(),
            'campaign_count': len(active_campaigns),
            'total_monthly_budget': round(total_monthly_budget, 2),
            'status_category': status_category,
            'pacing_status': {
                'on_track': on_track,
                'over_pacing': over_pacing,
                'under_pacing': under_pacing,
            },
            'settings': self.settings.to_dict() if self.settings else None,
        }


class Campaign(db.Model):
    """A Google Ads campaign tracked for pacing.

    monthly_budget is pulled from the Google Sheet (source of truth).
    google_campaign_id is the numeric campaign ID from Google Ads.

    budget_label / campaign_filter: composite segmentation (from the MCC script).
      budget_label   — the human-readable segment name (e.g. "IndyCar", "Brand", "Primary")
      campaign_filter — the keyword used to group campaigns into this segment
                        (e.g. "IndyCar" → any campaign whose name contains "IndyCar").
                        Empty / null means this is the catch-all "Primary" segment.
    """
    __tablename__ = 'campaigns'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False, index=True)
    campaign_name = db.Column(db.String(255), nullable=False)
    google_campaign_id = db.Column(db.String(50), nullable=False, index=True)
    monthly_budget = db.Column(db.Float, nullable=False, default=0.0)
    flight_type = db.Column(db.String(50), default='ALWAYS_ON')  # ALWAYS_ON or LIMITED
    flight_start_date = db.Column(db.Date, nullable=True)
    flight_end_date = db.Column(db.Date, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    # Google Ads budget resource name, e.g. 'customers/123/campaignBudgets/456'
    # Needed to update the budget via the API.
    budget_resource_name = db.Column(db.String(500), nullable=True)
    # Segment tracking — mirrors the Google Ads script's campaignFilter concept
    budget_label = db.Column(db.String(100), nullable=True)    # e.g. "IndyCar", "Brand", "Primary"
    campaign_filter = db.Column(db.String(100), nullable=True) # keyword that assigns campaigns to this segment
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    pacing_data = db.relationship('PacingData', backref='campaign', lazy=True, cascade='all, delete-orphan')
    adjustments = db.relationship('BudgetAdjustment', backref='campaign', lazy=True, cascade='all, delete-orphan')

    @property
    def flight_status(self):
        if self.flight_type == 'ALWAYS_ON':
            return 'active'
        today = datetime.utcnow().date()
        if self.flight_start_date and self.flight_end_date:
            if today < self.flight_start_date:
                return 'pending'
            elif today > self.flight_end_date:
                return 'ended'
            else:
                return 'active'
        return 'pending'

    def to_dict(self):
        campaign_rows = sorted(
            (p for p in self.pacing_data),
            key=lambda r: (r.date or datetime.min.date(), r.id or 0),
        )
        latest = campaign_rows[-1].to_dict() if campaign_rows else None

        return {
            'id': self.id,
            'account_id': self.account_id,
            'campaign_name': self.campaign_name,
            'google_campaign_id': self.google_campaign_id,
            'monthly_budget': self.monthly_budget,
            'flight_type': self.flight_type,
            'flight_start_date': self.flight_start_date.isoformat() if self.flight_start_date else None,
            'flight_end_date': self.flight_end_date.isoformat() if self.flight_end_date else None,
            'flight_status': self.flight_status,
            'is_active': self.is_active,
            'budget_resource_name': self.budget_resource_name,
            'budget_label': self.budget_label,
            'campaign_filter': self.campaign_filter,
            'created_at': self.created_at.isoformat(),
            'latest_pacing': latest,
        }


class PacingData(db.Model):
    """One pacing snapshot per campaign per run.

    clicks / conversions / cpc are populated when the Google Ads API returns
    them (new pacing runs). They will be NULL for rows written before this
    feature was added — the UI should treat NULL as 'not available'.
    """
    __tablename__ = 'pacing_data'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaigns.id'), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    current_daily_budget = db.Column(db.Float, nullable=True)
    actual_spend = db.Column(db.Float, nullable=False)
    expected_spend = db.Column(db.Float, nullable=False)
    pace_ratio = db.Column(db.Float, nullable=False)
    recommended_daily_budget = db.Column(db.Float, nullable=True)
    change_percent = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(50))  # ON_PACE, INCREASE, DECREASE
    # Performance metrics (mirrors the MCC script's campaign_breakdown payload)
    clicks = db.Column(db.Integer, nullable=True)
    conversions = db.Column(db.Float, nullable=True)
    cpc = db.Column(db.Float, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'date': self.date.isoformat() if self.date else None,
            'current_daily_budget': round(self.current_daily_budget, 2) if self.current_daily_budget is not None else None,
            'actual_spend': round(self.actual_spend, 2),
            'expected_spend': round(self.expected_spend, 2),
            'pace_ratio': round(self.pace_ratio, 3),
            'recommended_daily_budget': round(self.recommended_daily_budget, 2) if self.recommended_daily_budget is not None else None,
            'change_percent': round(self.change_percent, 1) if self.change_percent is not None else None,
            'status': self.status,
            'clicks': self.clicks,
            'conversions': round(self.conversions, 1) if self.conversions is not None else None,
            'cpc': round(self.cpc, 2) if self.cpc is not None else None,
        }


class BudgetAdjustment(db.Model):
    """Audit log of every budget change pushed to Google Ads."""
    __tablename__ = 'budget_adjustments'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaigns.id'), nullable=False, index=True)
    old_budget = db.Column(db.Float, nullable=False)
    new_budget = db.Column(db.Float, nullable=False)
    change_percent = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(255))
    applied_by = db.Column(db.String(255))
    applied_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'old_budget': round(self.old_budget, 2),
            'new_budget': round(self.new_budget, 2),
            'change_percent': round(self.change_percent, 2),
            'reason': self.reason,
            'applied_by': self.applied_by,
            'applied_at': self.applied_at.isoformat(),
        }


class PacingRun(db.Model):
    """Log of each pacing run (manual or scheduled)."""
    __tablename__ = 'pacing_runs'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False, index=True)
    run_type = db.Column(db.String(50))       # MANUAL, AUTO
    triggered_by = db.Column(db.String(255))  # user email
    campaigns_processed = db.Column(db.Integer)
    adjustments_made = db.Column(db.Integer)
    status = db.Column(db.String(50))         # COMPLETED, PARTIAL, FAILED
    error_message = db.Column(db.Text)
    run_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'account_id': self.account_id,
            'run_type': self.run_type,
            'triggered_by': self.triggered_by,
            'campaigns_processed': self.campaigns_processed,
            'adjustments_made': self.adjustments_made,
            'status': self.status,
            'error_message': self.error_message,
            'run_at': self.run_at.isoformat() if self.run_at else None,
        }


class PauseEvent(db.Model):
    """Log of auto-pause events when an account hits its spend threshold."""
    __tablename__ = 'pause_events'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False, index=True)
    spend_at_pause = db.Column(db.Float, nullable=False)
    budget_at_pause = db.Column(db.Float, nullable=False)
    threshold_pct = db.Column(db.Float, nullable=False)
    paused_campaign_names = db.Column(db.Text)  # JSON list of campaign names paused
    triggered_by = db.Column(db.String(50), default='AUTO')  # AUTO or MANUAL
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'account_id': self.account_id,
            'spend_at_pause': round(self.spend_at_pause, 2),
            'budget_at_pause': round(self.budget_at_pause, 2),
            'threshold_pct': round(self.threshold_pct, 2),
            'paused_campaign_names': self.paused_campaign_names,
            'triggered_by': self.triggered_by,
            'created_at': self.created_at.isoformat(),
        }


class LeadExport(db.Model):
    """Monthly leads export record — one row per account per month exported."""
    __tablename__ = 'lead_exports'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False, index=True)
    export_month = db.Column(db.String(7), nullable=False)   # e.g. '2026-05'
    lead_count = db.Column(db.Integer, default=0)
    exported_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(50), default='COMPLETED')  # COMPLETED, FAILED

    def to_dict(self):
        return {
            'id': self.id,
            'account_id': self.account_id,
            'export_month': self.export_month,
            'lead_count': self.lead_count,
            'exported_at': self.exported_at.isoformat(),
            'status': self.status,
        }


class AccountSettings(db.Model):
    """Per-account configuration for pacing and integrations."""
    __tablename__ = 'account_settings'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False, unique=True)
    # Auto-pause: pause all campaigns when MTD spend hits this % of monthly budget
    auto_pause_enabled = db.Column(db.Boolean, default=False)
    auto_pause_threshold = db.Column(db.Float, default=95.0)  # percent, e.g. 95.0 = 95%
    # Google Sheets integration
    google_sheet_id = db.Column(db.String(500), nullable=True)
    # Daily digest email after each scheduled pacing run
    daily_digest_enabled = db.Column(db.Boolean, default=False, nullable=False)
    # Track leads for this account
    track_leads = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'account_id': self.account_id,
            'auto_pause_enabled': bool(self.auto_pause_enabled),
            'auto_pause_threshold': self.auto_pause_threshold,
            'google_sheet_id': self.google_sheet_id or '',
            'daily_digest_enabled': bool(self.daily_digest_enabled),
            'track_leads': bool(self.track_leads),
            'created_at': self.created_at.isoformat(),
        }
