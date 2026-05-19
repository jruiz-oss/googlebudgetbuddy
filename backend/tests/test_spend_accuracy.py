import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from database import (  # noqa: E402
    Account,
    AccountSettings,
    Campaign,
    PacingData,
    canonical_campaigns,
    db,
    visible_latest_campaigns,
)
from routes.pacing import _delete_today_pacing_data  # noqa: E402
from routes.pacing import _allocated_recommendation  # noqa: E402
from routes.sheets import write_google_ads_spend_for_account  # noqa: E402


class SpendAccuracyTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            TESTING=True,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_canonical_campaigns_prefers_active_latest_row(self):
        old = Campaign(
            id=1,
            account_id=1,
            campaign_name='Duplicate',
            google_campaign_id='123',
            is_active=False,
            monthly_budget=100,
            created_at=datetime(2026, 5, 1),
        )
        current = Campaign(
            id=2,
            account_id=1,
            campaign_name='Duplicate Current',
            google_campaign_id='123',
            is_active=True,
            monthly_budget=100,
            budget_resource_name='customers/1/campaignBudgets/1',
            created_at=datetime(2026, 5, 2),
        )
        current.pacing_data = [PacingData(date=date(2026, 5, 19), actual_spend=25)]
        old.pacing_data = [PacingData(date=date(2026, 5, 18), actual_spend=999)]

        result = canonical_campaigns([old, current])

        self.assertEqual([c.id for c in result], [2])

    def test_visible_latest_campaigns_excludes_stale_prior_run_rows(self):
        latest = date(2026, 5, 19)
        stale = date(2026, 5, 18)
        current = Campaign(id=1, campaign_name='Current', google_campaign_id='1', is_active=True)
        current.pacing_data = [PacingData(date=latest, actual_spend=100)]
        old = Campaign(id=2, campaign_name='Old', google_campaign_id='2', is_active=False)
        old.pacing_data = [PacingData(date=stale, actual_spend=1600)]

        result = visible_latest_campaigns([current, old])

        self.assertEqual([c.google_campaign_id for c in result], ['1'])

    def test_visible_latest_campaigns_includes_live_zero_spend_campaigns(self):
        live_zero = Campaign(id=1, campaign_name='Live Zero', google_campaign_id='1', is_active=True)
        spent = Campaign(id=2, campaign_name='Spent', google_campaign_id='2', is_active=True)
        spent.pacing_data = [PacingData(date=date(2026, 5, 19), actual_spend=100)]

        result = visible_latest_campaigns([live_zero, spent])

        self.assertEqual([c.google_campaign_id for c in result], ['1', '2'])

    def test_visible_latest_campaigns_ignores_noncanonical_duplicate_dates(self):
        latest = date(2026, 5, 19)
        stale_duplicate_date = date(2026, 5, 20)
        canonical = Campaign(id=1, campaign_name='Canonical', google_campaign_id='1', is_active=True)
        canonical.pacing_data = [PacingData(date=latest, actual_spend=100)]
        duplicate = Campaign(id=2, campaign_name='Duplicate', google_campaign_id='1', is_active=False)
        duplicate.pacing_data = [PacingData(date=stale_duplicate_date, actual_spend=999)]

        result = visible_latest_campaigns([canonical, duplicate])

        self.assertEqual([c.id for c in result], [1])

    def test_delete_today_pacing_data_removes_stale_same_day_rows(self):
        account = Account(user_id=1, account_name='Acct', google_customer_id='111')
        db.session.add(account)
        db.session.flush()
        campaign = Campaign(account_id=account.id, campaign_name='Campaign', google_campaign_id='1')
        db.session.add(campaign)
        db.session.flush()

        today = date.today()
        yesterday = today - timedelta(days=1)
        db.session.add(PacingData(campaign_id=campaign.id, date=today, actual_spend=999, expected_spend=0, pace_ratio=1))
        db.session.add(PacingData(campaign_id=campaign.id, date=yesterday, actual_spend=100, expected_spend=0, pace_ratio=1))
        db.session.commit()

        deleted = _delete_today_pacing_data([campaign], today)
        db.session.commit()

        remaining = PacingData.query.filter_by(campaign_id=campaign.id).all()
        self.assertEqual(deleted, 1)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].date, yesterday)

    def test_recommendation_allocation_preserves_current_daily_ratio(self):
        self.assertEqual(_allocated_recommendation(300, 30, 100, 2), 90)
        self.assertEqual(_allocated_recommendation(300, 70, 100, 2), 210)
        self.assertEqual(_allocated_recommendation(300, 0, 0, 2), 150)

    def test_sheet_writeback_uses_current_run_totals_and_manual_rejects_stale_rows(self):
        account = Account(user_id=1, account_name='Goodwill AZ - Retail Grant', google_customer_id='3525872801')
        db.session.add(account)
        db.session.flush()
        db.session.add(AccountSettings(account_id=account.id, google_sheet_id='sheet-id'))
        campaign = Campaign(
            account_id=account.id,
            campaign_name='Goodwill AZ | Near Me',
            google_campaign_id='1666006076',
            budget_label='Primary',
            monthly_budget=10000,
        )
        db.session.add(campaign)
        db.session.flush()
        db.session.add(PacingData(
            campaign_id=campaign.id,
            date=date.today() - timedelta(days=1),
            actual_spend=7600,
            expected_spend=0,
            pace_ratio=1,
        ))
        db.session.commit()

        ws = Mock()
        gc = Mock()
        gc.open_by_key.return_value = object()
        rows = [{
            'row_index': 16,
            'account_name': account.account_name,
            'campaign_filter': '',
            'monthly_budget': 10000,
            'mtd_spend': None,
        }]

        with patch('routes.sheets._get_gspread_client', return_value=gc), \
                patch('routes.sheets._sheets_retry', side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
                patch('routes.sheets._open_month_worksheet', return_value=(ws, 'May 2026')), \
                patch('routes.sheets._get_google_ads_section', return_value=rows):
            manual = write_google_ads_spend_for_account(account.id)
            current = write_google_ads_spend_for_account(
                account.id,
                segment_spend_by_label={'Primary': 6021.08},
            )

        self.assertEqual(manual['written_count'], 0)
        self.assertEqual(manual['skipped_count'], 1)
        self.assertEqual(current['written'][0]['spend'], 6021.08)
        ws.batch_update.assert_called_once_with(
            [{'range': 'D16', 'values': [[6021.08]]}],
            value_input_option='USER_ENTERED',
        )


if __name__ == '__main__':
    unittest.main()
