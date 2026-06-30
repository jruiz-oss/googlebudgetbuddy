from flask_sqlalchemy import SQLAlchemy
from datetime import date, datetime
from sqlalchemy.types import String, TypeDecorator

db = SQLAlchemy()


def campaign_identity_key(campaign):
    """Stable key for one real Google Ads campaign, tolerant of old ID formats."""
    raw = getattr(campaign, 'google_campaign_id', None)
    digits = ''.join(ch for ch in str(raw or '') if ch.isdigit())
    return digits or f"db:{getattr(campaign, 'id', None) or id(campaign)}"


def current_month_start():
    """First day of the current (UTC) calendar month.

    Used to scope dashboard visibility and MTD spend to the current month so
    that, at the start of a new month — before the first pacing run has written
    any rows for it — last month's pacing data is NOT mistaken for this month's.
    """
    return datetime.utcnow().date().replace(day=1)


def _campaign_latest_pacing(campaign, latest_date=None, month_start=None):
    """Return the latest pacing row, optionally constrained to a specific date.

    When ``month_start`` is given, pacing rows from before the current month are
    ignored entirely so stale prior-month data can't leak into current views.
    """
    rows = [
        p for p in (campaign.pacing_data or [])
        if p.date is not None
        and (latest_date is None or p.date == latest_date)
        and (month_start is None or p.date >= month_start)
    ]
    if not rows:
        return None
    return sorted(rows, key=lambda r: (r.date, r.id or 0))[-1]


def _campaign_latest_date(campaign):
    latest = _campaign_latest_pacing(campaign)
    return latest.date if latest else None


def canonical_campaigns(campaigns):
    """Return one campaign row per Google campaign ID.

    Duplicate DB rows can exist from older imports. For live pacing/dashboard
    math, one Google Ads campaign must contribute only once. Prefer rows that
    look active/current, then fall back deterministically to the newest DB row.
    """
    grouped = {}
    for campaign in campaigns or []:
        key = campaign_identity_key(campaign)
        grouped.setdefault(key, []).append(campaign)

    canonical = []
    for rows in grouped.values():
        canonical.append(max(rows, key=lambda c: (
            bool(c.is_active),
            _campaign_latest_date(c) or date.min,
            bool(c.budget_resource_name),
            c.monthly_budget or 0,
            c.created_at or datetime.min,
            c.id or 0,
        )))
    return sorted(canonical, key=lambda c: ((c.campaign_name or '').lower(), c.id or 0))


def latest_pacing_date(campaigns, month_start=None):
    """Return the latest pacing date across campaign rows.

    When ``month_start`` is given, only pacing rows on/after that date are
    considered — so the "latest run" used by the dashboard is always within the
    current month, not last month's final run.
    """
    latest = None
    for campaign in campaigns or []:
        for p in (campaign.pacing_data or []):
            d = p.date
            if not d:
                continue
            if month_start is not None and d < month_start:
                continue
            if latest is None or d > latest:
                latest = d
    return latest


def _normalize_campaign_name(name):
    """Loose normalization so 'Foo | Search' and 'foo  |  search' collide."""
    return ' '.join((name or '').lower().split())


def dedupe_by_name(campaigns):
    """Collapse same-name twins within an account to a single row.

    Why we need this:
      Google Ads campaigns are unique by gid, but legacy imports left some
      accounts with TWO DB rows for one logical Google campaign (different
      gids, same name). Only one row gets refreshed by the live API each
      run — the other keeps stale status/budget and looks like a phantom
      "Live" twin next to the real (paused) row.

    Pick order (highest priority first):
      1. NOT flagged as REMOVED — phantoms get marked REMOVED at pacing time
         when the API doesn't recognize their gid.
      2. Most recently touched by a pacing run (latest_pacing date).
      3. Has google_status set (means the API saw it at some point).
      4. Most recently created.
    """
    by_name = {}
    for c in campaigns or []:
        key = (c.account_id, _normalize_campaign_name(c.campaign_name))
        by_name.setdefault(key, []).append(c)

    def _freshness_score(c):
        not_phantom = 0 if (c.google_status or '').upper() == 'REMOVED' else 1
        latest      = _campaign_latest_date(c) or date.min
        has_status  = 1 if c.google_status else 0
        created     = c.created_at or datetime.min
        return (not_phantom, latest, has_status, created, c.id or 0)

    return [max(twins, key=_freshness_score) for twins in by_name.values()]


def visible_latest_campaigns(campaigns, month_start=None, show_all=False):
    """Campaigns visible to dashboards. Mirrors the pacing-run inclusion rule.

    When ``show_all`` is True (lockdown / "must stay OFF" accounts), the
    spend-based filter is bypassed and every canonical campaign is returned with
    its real status. This lets the user confirm a locked account is genuinely
    off even when nothing has spent this month — campaigns with $0 spend would
    otherwise be hidden by the normal rule below.

    The rule (matches what the user wants on the per-account dashboard):
      INCLUDE if:
        • has spend > 0 in the most recent pacing run  → covers paused/ended
          campaigns that did spend this month (shown with their real status).
        • truly live: is_active AND (no google_end_date OR end_date >= today)
          AND has no prior pacing history (truly brand-new this month).
      EXCLUDE:
        • "zombie" campaigns — flagged is_active=True but actually past their
          end_date and with $0 spend this month.
        • Campaigns with prior pacing history but $0 in the latest run
          (stale/dead, regardless of is_active flag).
        • A paused/ended row that shares a name (within the same account) with
          an ENABLED row — Google Ads sometimes splits one logical campaign
          across two gids when it's deleted-and-recreated. Without this pass
          the dashboard shows the same campaign twice.
    """
    from datetime import date as _date
    if month_start is None:
        month_start = current_month_start()
    canonical = canonical_campaigns(campaigns)
    if show_all:
        # Lockdown view: show everything (deduped by name), no spend gate.
        return dedupe_by_name(canonical)
    # Only consider pacing rows from the current month. At the start of a new
    # month (before its first run) this is None, so campaigns that merely spent
    # *last* month are no longer treated as "spending now".
    latest_date = latest_pacing_date(canonical, month_start=month_start)
    today = _date.today()

    # First pass: standard inclusion rule.
    visible = []
    for campaign in canonical:
        latest = _campaign_latest_pacing(campaign, latest_date, month_start=month_start) if latest_date else None
        has_spend_latest    = latest and (latest.actual_spend or 0) > 0
        has_ever_been_paced = any(
            p.date is not None and p.date >= month_start
            for p in (campaign.pacing_data or [])
        )

        if has_spend_latest:
            visible.append(campaign)
            continue

        if has_ever_been_paced:
            # Paced before, $0 in latest run → zombie / dormant, skip.
            continue

        if not campaign.is_active:
            # Inactive (paused / ended in sync) and no spend → skip.
            continue

        # Truly brand-new branch: is_active=True, never paced. Still exclude if
        # the Google end_date has already passed — that means it's a zombie
        # that hasn't been cleaned up by sync yet.
        if campaign.google_end_date is not None and campaign.google_end_date < today:
            continue

        visible.append(campaign)

    # Second pass: collapse same-name twins (legacy duplicate-gid rows).
    return dedupe_by_name(visible)


def segment_budget_total(campaigns):
    """Sum one monthly budget per segment, not once per campaign row."""
    budgets = {}
    for campaign in campaigns or []:
        label = (campaign.budget_label or 'Primary').strip().lower()
        budgets[label] = max(budgets.get(label, 0), campaign.monthly_budget or 0)
    return sum(budgets.values())


def campaign_mtd_spend_total(campaigns, latest_date=None, month_start=None):
    """Sum latest-run MTD spend once per normalized Google campaign ID.

    Scoped to the current month so a prior-month run can't be summed as this
    month's spend before the new month's first run.
    """
    if month_start is None:
        month_start = current_month_start()
    latest_date = latest_date or latest_pacing_date(campaigns, month_start=month_start)
    if not latest_date:
        return 0.0

    total = 0.0
    seen = set()
    for campaign in campaigns or []:
        key = campaign_identity_key(campaign)
        if key in seen:
            continue
        latest = _campaign_latest_pacing(campaign, latest_date, month_start=month_start)
        if not latest:
            continue
        seen.add(key)
        total += latest.actual_spend or 0.0
    return total


def segment_spend_summaries(campaigns, month_start=None):
    """Return deduped segment budget/spend/current-daily summaries.

    Spend is scoped to the current month so segment MTD totals reset at the
    month boundary instead of carrying last month's final run.
    """
    if month_start is None:
        month_start = current_month_start()
    latest_date = latest_pacing_date(campaigns, month_start=month_start)
    summaries = {}
    seen_spend = set()

    for campaign in campaigns or []:
        label = (campaign.budget_label or 'Primary').strip()
        # Normalise to lowercase so "aquatopia" and "Aquatopia" collapse into
        # one segment on initial load (before pacing re-syncs the sheet).
        label_key = label.lower()
        if label_key not in summaries:
            summaries[label_key] = {
                'name': label,
                'monthly': 0.0,
                'spend': 0.0,
                'current_daily': 0.0,
                'campaign_count': 0,
            }
        row = summaries[label_key]
        row['monthly'] = max(row['monthly'], campaign.monthly_budget or 0.0)
        row['current_daily'] += campaign.current_daily_budget or 0.0
        row['campaign_count'] += 1

        key = campaign_identity_key(campaign)
        if key in seen_spend:
            continue
        latest = _campaign_latest_pacing(campaign, latest_date, month_start=month_start) if latest_date else None
        if latest:
            seen_spend.add(key)
            row['spend'] += latest.actual_spend or 0.0

    return [
        {
            'name': row['name'],
            'monthly': round(row['monthly'], 2),
            'spend': round(row['spend'], 2),
            'current_daily': round(row['current_daily'], 2),
            'campaign_count': row['campaign_count'],
            'pace_pct': round((row['spend'] / row['monthly']) * 100, 1) if row['monthly'] > 0 else 0.0,
        }
        for row in sorted(summaries.values(), key=lambda r: r['name'].lower())
    ]


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
        return {
            'id': self.id,
            'email': self.email,
            # Service account auth — always connected; no per-user token needed.
            'has_google_token': True,
            'created_at': self.created_at.isoformat(),
        }


class GoogleOAuthToken(db.Model):
    """Legacy OAuth token table — retained for schema compatibility only.

    BudgetBuddy now uses service account authentication (GOOGLE_CREDENTIALS_JSON).
    No new rows are written to this table; existing rows are ignored at runtime.
    The table is kept to avoid a destructive migration on existing deployments.
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

        # Dashboard data must reflect only the latest pacing run and only one DB
        # row per Google campaign ID. Older duplicate rows are retained for
        # history but never participate in live totals.
        month_start = current_month_start()
        # Locked ("must stay off") accounts show all campaigns regardless of
        # spend so the user can verify everything is genuinely paused.
        show_all = bool(self.settings and getattr(self.settings, 'lockdown_enabled', False))
        visible_campaigns = visible_latest_campaigns(self.campaigns, month_start=month_start, show_all=show_all)
        total_monthly_budget = segment_budget_total(visible_campaigns)
        latest_date = latest_pacing_date(visible_campaigns, month_start=month_start)
        total_mtd_spend = campaign_mtd_spend_total(visible_campaigns, latest_date, month_start=month_start)
        segments = segment_spend_summaries(visible_campaigns, month_start=month_start)

        on_track = over_pacing = under_pacing = 0
        for c in visible_campaigns:
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

        last_run = (
            PacingRun.query
            .filter_by(account_id=self.id)
            .order_by(PacingRun.run_at.desc())
            .first()
        )

        return {
            'id': self.id,
            'user_id': self.user_id,
            'account_name': self.account_name,
            'google_customer_id': self.google_customer_id,
            'mcc_customer_id': self.mcc_customer_id,
            'created_at': self.created_at.isoformat(),
            'campaign_count': len(visible_campaigns),
            'total_monthly_budget': round(total_monthly_budget, 2),
            'mtd_spend': round(total_mtd_spend, 2),
            'latest_pacing_date': latest_date.isoformat() if latest_date else None,
            'last_pacing_run_at': last_run.run_at.isoformat() if last_run and last_run.run_at else None,
            'segment_summaries': segments,
            'status_category': status_category,
            'pacing_status': {
                'on_track': on_track,
                'over_pacing': over_pacing,
                'under_pacing': under_pacing,
            },
            'settings': self.settings.to_dict() if self.settings else None,
            'campaigns': [c.to_dict() for c in visible_campaigns],
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
    __table_args__ = (
        db.UniqueConstraint('account_id', 'google_campaign_id', name='uq_campaign_account_google_id'),
    )

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
    current_daily_budget = db.Column(db.Float, nullable=True)
    # Raw Google Ads campaign status string: 'ENABLED', 'PAUSED', 'REMOVED', etc.
    # Stored so the frontend can show the real status while still including paused
    # campaigns with MTD spend in pacing calculations.
    google_status = db.Column(db.String(50), nullable=True)
    # Google Ads campaign end_date — stored so we can detect campaigns that ended before
    # the current month without re-querying the API. NULL means no end date (always-on).
    google_end_date = db.Column(db.Date, nullable=True)
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

    def has_spend_this_month(self):
        """True if this campaign has any PacingData spend > 0 in the current calendar month."""
        month_start = datetime.utcnow().date().replace(day=1)
        return any(
            p.actual_spend and p.actual_spend > 0 and p.date and p.date >= month_start
            for p in (self.pacing_data or [])
        )

    def is_visible(self):
        """True if the campaign should appear in dashboard views.

        Visible ONLY if the campaign actually spent money this calendar
        month. Google Ads frequently leaves old campaigns set to ENABLED
        for years even after they stop running — `is_active` alone lets
        those zombies pollute the dashboard. Spend is the only reliable
        signal that a campaign actually ran this month.

        Trade-off: a brand-new campaign with $0 spend so far won't appear
        until its first spend rolls in (typically same day).
        """
        return self.has_spend_this_month()

    def to_dict(self):
        campaign_rows = sorted(
            (p for p in self.pacing_data),
            key=lambda r: (r.date or datetime.min.date(), r.id or 0),
        )
        latest = campaign_rows[-1].to_dict() if campaign_rows else None

        # Find the most recent pacing row on a *different* (earlier) date so the
        # frontend can compute "yesterday's" pace % for the trend indicator.
        prev = None
        if latest and len(campaign_rows) >= 2:
            latest_date = campaign_rows[-1].date
            for row in reversed(campaign_rows[:-1]):
                if row.date != latest_date:
                    prev = row.to_dict()
                    break

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
            'google_status': self.google_status,
            'google_end_date': self.google_end_date.isoformat() if self.google_end_date else None,
            'budget_resource_name': self.budget_resource_name,
            'current_daily_budget': round(self.current_daily_budget, 2) if self.current_daily_budget is not None else None,
            'budget_label': self.budget_label,
            'campaign_filter': self.campaign_filter,
            'created_at': self.created_at.isoformat(),
            'latest_pacing': latest,
            'prev_pacing': prev,
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
    auto_pause_enabled = db.Column(db.Boolean, default=True)  # backstop on by default; turn OFF per account to disable
    auto_pause_threshold = db.Column(db.Float, default=104.0)  # percent — backstop; script pauses at 100%, app only if spend slips past 104%
    # Lockdown / "must stay OFF": this account is supposed to spend $0. When True,
    # the hourly job pauses EVERY campaign the moment any MTD spend > $0 is seen.
    # This overrides both the Grant bypass (rule B) and auto_pause_enabled — a
    # locked account is never allowed to spend a penny. Locked accounts also show
    # all their campaigns on the dashboard (regardless of spend) so the user can
    # confirm at a glance that everything is genuinely off.
    lockdown_enabled = db.Column(db.Boolean, default=False, nullable=False)
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
            'lockdown_enabled': bool(self.lockdown_enabled),
            'google_sheet_id': self.google_sheet_id or '',
            'daily_digest_enabled': bool(self.daily_digest_enabled),
            'track_leads': bool(self.track_leads),
            'created_at': self.created_at.isoformat(),
        }


class UserSettings(db.Model):
    """Per-user global configuration (e.g. Anthropic API key for AI summaries)."""
    __tablename__ = 'user_settings'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    anthropic_api_key = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            # Never return the full key — just a masked hint so the UI knows one is set.
            'anthropic_api_key_set': bool(self.anthropic_api_key),
            'anthropic_api_key_hint': (
                '…' + self.anthropic_api_key[-4:] if self.anthropic_api_key else None
            ),
        }


class MonthlyReport(db.Model):
    """AI-generated monthly summary per account.

    One row per (account_id, year, month). The user can add notes which are
    sent to Claude along with pacing data and search terms to generate a
    plain-language narrative summary. The output is editable and saved here.
    """
    __tablename__ = 'monthly_reports'
    __table_args__ = (
        db.UniqueConstraint('account_id', 'year', 'month', name='uq_report_account_month'),
    )

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False, index=True)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)   # 1–12
    notes = db.Column(db.Text, nullable=True)       # user-written context
    generated_summary = db.Column(db.Text, nullable=True)  # Claude output
    last_generated_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'account_id': self.account_id,
            'year': self.year,
            'month': self.month,
            'notes': self.notes or '',
            'generated_summary': self.generated_summary or '',
            'last_generated_at': self.last_generated_at.isoformat() if self.last_generated_at else None,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
        }


class JobState(db.Model):
    """Cross-worker state for long-running background jobs (e.g. run-all pacing).

    Why this exists: the backend runs under gunicorn with multiple worker
    processes. An in-memory lock/counter only lives in one process, so a status
    poll load-balanced to a *different* worker than the one running the job sees
    no job in progress and reports "done" prematurely. Persisting state in
    Postgres makes every worker agree on the truth.

    One row per job_key (e.g. 'run_all_pacing'). `heartbeat` lets us detect a
    crashed worker: if a job claims to be running but hasn't beat in a while,
    it's treated as stale so the UI never gets stuck on "in progress".
    """
    __tablename__ = 'job_state'

    # Consider a "running" job stale if its heartbeat is older than this.
    # Matches the gunicorn --timeout so a single slow account between heartbeats
    # can't be misread as a crashed worker. Past this, we assume the worker died
    # and let a new run reclaim the job rather than lock the UI forever.
    STALE_AFTER_SECONDS = 300

    id = db.Column(db.Integer, primary_key=True)
    job_key = db.Column(db.String(100), nullable=False, unique=True, index=True)
    is_running = db.Column(db.Boolean, default=False, nullable=False)
    completed = db.Column(db.Integer, default=0, nullable=False)
    total = db.Column(db.Integer, default=0, nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    heartbeat = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    def is_stale(self):
        """True if marked running but the worker hasn't checked in recently."""
        if not self.is_running:
            return False
        if self.heartbeat is None:
            return True
        age = (datetime.utcnow() - self.heartbeat).total_seconds()
        return age > self.STALE_AFTER_SECONDS

    def to_dict(self):
        return {
            'job_key': self.job_key,
            # A stale job is reported as not running so the UI can recover.
            'running': bool(self.is_running) and not self.is_stale(),
            'completed': self.completed or 0,
            'total': self.total or 0,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'heartbeat': self.heartbeat.isoformat() if self.heartbeat else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
        }
