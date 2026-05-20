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
    campaign_mtd_spend_total,
    canonical_campaigns,
    db,
    segment_spend_summaries,
    visible_latest_campaigns,
)
from routes.pacing import _delete_today_pacing_data  # noqa: E402
from routes.pacing import _allocated_recommendation, _compute_recommendation, _segment_summaries_from_maps  # noqa: E402
from routes.pacing import _campaigns_for_pacing, _execute_pacing_run, _is_zombie_campaign  # noqa: E402
from routes.pacing import _refresh_campaign_state_from_api  # noqa: E402
from routes.sheets import _google_ads_row_assignments, write_google_ads_spend_for_account  # noqa: E402


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

    def test_normalized_campaign_ids_are_counted_once_in_totals(self):
        latest = date(2026, 5, 19)
        first = Campaign(
            id=1,
            campaign_name='Campaign',
            google_campaign_id='123456',
            budget_label='Primary',
            monthly_budget=1000,
            is_active=True,
        )
        duplicate = Campaign(
            id=2,
            campaign_name='Campaign Duplicate',
            google_campaign_id='123-456',
            budget_label='Primary',
            monthly_budget=1000,
            is_active=True,
        )
        first.pacing_data = [PacingData(date=latest, actual_spend=250)]
        duplicate.pacing_data = [PacingData(date=latest, actual_spend=250)]

        canonical = canonical_campaigns([first, duplicate])
        total = campaign_mtd_spend_total(canonical)
        segments = segment_spend_summaries(canonical)

        self.assertEqual(len(canonical), 1)
        self.assertEqual(total, 250)
        self.assertEqual(segments[0]['spend'], 250)

    def test_multi_campaign_segment_rolls_up_to_one_segment_total(self):
        summaries = _segment_summaries_from_maps(
            seg_budget_map={'Brand': 5000},
            seg_spend_map={'Brand': 1200},
            seg_daily_map={'Brand': 150},
            seg_count_map={'Brand': 2},
        )

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]['name'], 'Brand')
        self.assertEqual(summaries[0]['spend'], 1200)
        self.assertEqual(summaries[0]['campaign_count'], 2)

    def test_google_ads_segment_assignments_do_not_double_claim_overlapping_filters(self):
        rows = [
            {'row_index': 10, 'campaign_filter': 'Brand', 'monthly_budget': 5000},
            {'row_index': 11, 'campaign_filter': 'Brand Shoes', 'monthly_budget': 1000},
            {'row_index': 12, 'campaign_filter': '', 'monthly_budget': 3000},
        ]
        campaigns = [
            Campaign(id=1, campaign_name='Brand Shoes Search', google_campaign_id='1'),
            Campaign(id=2, campaign_name='Brand General Search', google_campaign_id='2'),
            Campaign(id=3, campaign_name='Prospecting Search', google_campaign_id='3'),
        ]

        assignments = _google_ads_row_assignments(rows, campaigns)
        matched = {
            a['label']: [c.google_campaign_id for c in a['matched']]
            for a in assignments
        }

        self.assertEqual(matched['Brand Shoes'], ['1'])
        self.assertEqual(matched['Brand'], ['2'])
        self.assertEqual(matched['Primary'], ['3'])

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

    def test_pace_ratio_matches_sheet_budget_used_percent(self):
        recommended, status, pace_ratio, expected_mtd, daily_target = _compute_recommendation(
            monthly_budget=1000,
            actual_spend=250,
            current_daily=25,
            today=date(2026, 5, 15),
        )

        self.assertEqual(round(pace_ratio, 3), 0.25)
        self.assertEqual(round(recommended, 2), 24.19)
        self.assertEqual(round(expected_mtd, 2), 483.87)
        self.assertEqual(round(daily_target, 2), 32.26)
        self.assertEqual(status, 'DECREASE')

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


    # -----------------------------------------------------------------
    # Consolidation regressions: same core powers both per-account /run
    # and the background run_pacing_for_account, so behavior must match.
    # -----------------------------------------------------------------

    def _setup_account_with_campaigns(self, campaign_specs):
        """Build an Account + Campaigns in a real (in-memory) session.

        campaign_specs: list of dicts with keys:
          name, gid, is_active, budget, end_date (optional), prior_spend (optional)
        """
        account = Account(
            user_id=1,
            account_name='Test Account',
            google_customer_id='1234567890',
        )
        db.session.add(account)
        db.session.flush()
        db.session.add(AccountSettings(account_id=account.id))
        db.session.flush()

        for spec in campaign_specs:
            c = Campaign(
                account_id=account.id,
                campaign_name=spec['name'],
                google_campaign_id=spec['gid'],
                is_active=spec.get('is_active', True),
                monthly_budget=spec.get('budget', 1000),
                budget_label=spec.get('label', 'Primary'),
                budget_resource_name=f"customers/1234567890/campaignBudgets/{spec['gid']}",
                google_end_date=spec.get('end_date'),
                flight_type='ALWAYS_ON',
            )
            db.session.add(c)
            db.session.flush()
            if spec.get('prior_spend'):
                db.session.add(PacingData(
                    campaign_id=c.id,
                    date=date.today() - timedelta(days=2),
                    actual_spend=spec['prior_spend'],
                    expected_spend=0,
                    pace_ratio=spec['prior_spend'] / (spec.get('budget', 1000) or 1),
                ))
        db.session.commit()

        from sqlalchemy.orm import selectinload
        return (
            Account.query
            .options(selectinload(Account.campaigns).selectinload(Campaign.pacing_data))
            .get(account.id)
        )

    def test_is_zombie_uses_today_not_month_start(self):
        """A campaign that ended mid-month with $0 spend is a zombie."""
        from datetime import date as _date
        today = _date(2026, 5, 20)
        # Ended 10 days ago this month, no spend → zombie.
        c = Campaign(
            campaign_name='Ended mid-month',
            google_campaign_id='1',
            is_active=True,
            google_end_date=_date(2026, 5, 10),
        )
        self.assertTrue(_is_zombie_campaign(c, today, api_spend=0))
        # Same campaign WITH spend → not a zombie (kept for visibility).
        self.assertFalse(_is_zombie_campaign(c, today, api_spend=50))

    def test_is_zombie_keeps_future_end_date_with_zero_spend(self):
        """A live campaign with no spend yet should NOT be a zombie."""
        from datetime import date as _date
        today = _date(2026, 5, 20)
        c = Campaign(
            campaign_name='Live no-spend',
            google_campaign_id='2',
            is_active=True,
            google_end_date=_date(2026, 12, 31),
        )
        # No pacing_data → brand new, never paced.
        self.assertFalse(_is_zombie_campaign(c, today, api_spend=0))

    def test_campaigns_for_pacing_excludes_ended_no_spend_includes_paused_with_spend(self):
        """Exactly the user's two rules: zombies out, paused-with-spend in."""
        from datetime import date as _date
        today = _date.today()

        account = self._setup_account_with_campaigns([
            {'name': 'Live active', 'gid': '100', 'is_active': True},
            {'name': 'Zombie ended', 'gid': '200', 'is_active': True,
             'end_date': today - timedelta(days=5)},
            {'name': 'Paused with spend', 'gid': '300', 'is_active': False},
            {'name': 'Paused no spend', 'gid': '400', 'is_active': False},
        ])

        metrics_by_id = {
            '100': {'spend': 250},                  # live, normal
            '200': {'spend': 0},                    # ended + no spend → zombie
            '300': {'spend': 180},                  # paused but spent this month → kept
            '400': {'spend': 0},                    # paused + no spend → excluded
        }

        _, live, inactive, active = _campaigns_for_pacing(account, today, metrics_by_id)
        active_gids = sorted(c.google_campaign_id for c in active)
        live_gids   = sorted(c.google_campaign_id for c in live)

        self.assertEqual(live_gids, ['100'])           # zombie filtered out
        self.assertIn('300', active_gids)              # paused-with-spend kept
        self.assertNotIn('200', active_gids)           # zombie excluded
        self.assertNotIn('400', active_gids)           # paused-no-spend excluded

    def test_execute_pacing_run_sequential_calls_no_double_count(self):
        """Calling _execute_pacing_run twice (e.g. run-all loop) must NOT
        double-count MTD spend. The same-day delete needs to clean rows from
        the previous call within the same session."""
        from datetime import date as _date
        today = _date.today()

        account = self._setup_account_with_campaigns([
            {'name': 'Campaign A', 'gid': '100', 'budget': 5000, 'is_active': True},
            {'name': 'Campaign B', 'gid': '200', 'budget': 3000, 'is_active': True},
        ])

        metrics_by_id = {
            '100': {'spend': 1000, 'clicks': 50, 'conversions': 2},
            '200': {'spend': 600,  'clicks': 30, 'conversions': 1},
        }

        with patch('routes.pacing.get_campaign_mtd_spend', return_value=metrics_by_id):
            r1 = _execute_pacing_run(account, 'fake-refresh', today, log_prefix='t1')
            db.session.commit()
            r2 = _execute_pacing_run(account, 'fake-refresh', today, log_prefix='t2')
            db.session.commit()

        self.assertTrue(r1['ok'] and r2['ok'])
        self.assertEqual(r1['seg_spend_map'], r2['seg_spend_map'])
        self.assertEqual(sum(r2['seg_spend_map'].values()), 1600)  # NOT 3200

        rows = PacingData.query.filter_by(date=today).all()
        # Exactly one row per campaign for today — old same-day rows must be gone.
        self.assertEqual(len(rows), 2)
        gid_to_spend = {
            row.campaign.google_campaign_id: row.actual_spend
            for row in rows
        }
        self.assertEqual(gid_to_spend, {'100': 1000, '200': 600})

    def test_refresh_campaign_state_flips_paused_in_api_to_inactive(self):
        """A campaign the user just paused in Google Ads should be flipped to
        is_active=False the next pacing run, even if DB still has stale True."""
        c = Campaign(
            campaign_name='Recently paused',
            google_campaign_id='42',
            is_active=True,         # stale DB state
            google_status='ENABLED',
        )
        metrics_by_id = {
            '42': {'status': 'PAUSED', 'end_date': None, 'spend': 0},
        }
        _refresh_campaign_state_from_api([c], metrics_by_id)
        self.assertEqual(c.google_status, 'PAUSED')
        self.assertFalse(c.is_active)

    def test_visible_latest_campaigns_dedupes_same_name_legacy_twins(self):
        """One Google Ads campaign that's been imported under two gids should
        appear ONCE on the dashboard — the freshly-refreshed row wins."""
        from database import dedupe_by_name
        today = date.today()

        # Twin A: stale duplicate, still has is_active=True from a prior sync.
        # Never got refreshed by the latest pacing run (no google_status).
        stale = Campaign(
            id=1,
            account_id=10,
            campaign_name='Commit | Secondary Geo | Search',
            google_campaign_id='1111',
            is_active=True,
            current_daily_budget=200,
            google_status=None,
            created_at=datetime(2026, 1, 1),
        )
        stale.pacing_data = [
            PacingData(date=today - timedelta(days=1), actual_spend=4, expected_spend=0, pace_ratio=0),
        ]

        # Twin B: real Google campaign — user paused it in Google Ads. Refreshed
        # by the latest pacing run so it has today's pacing row + google_status.
        real = Campaign(
            id=2,
            account_id=10,
            campaign_name='Commit | Secondary Geo | Search',
            google_campaign_id='2222',
            is_active=False,
            current_daily_budget=0,
            google_status='PAUSED',
            created_at=datetime(2026, 5, 19),
        )
        real.pacing_data = [
            PacingData(date=today, actual_spend=4, expected_spend=0, pace_ratio=0),
        ]

        # dedupe_by_name picks the row with the freshest pacing date.
        result = dedupe_by_name([stale, real])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, 2)
        self.assertEqual(result[0].google_status, 'PAUSED')

    def test_refresh_campaign_state_stores_end_date_from_api(self):
        c = Campaign(
            campaign_name='Ends this month',
            google_campaign_id='99',
            is_active=True,
            google_end_date=None,
        )
        metrics_by_id = {
            '99': {'status': 'ENABLED', 'end_date': '2026-05-25', 'spend': 100},
        }
        _refresh_campaign_state_from_api([c], metrics_by_id)
        self.assertEqual(c.google_end_date, date(2026, 5, 25))
        self.assertTrue(c.is_active)

    # NOTE: A direct DB test for duplicate-row dedup inside _execute_pacing_run
    # isn't possible on fresh DBs anymore — the `uq_campaign_account_google_id`
    # constraint now blocks two rows from sharing the same (account_id, gid).
    # Helper-level dedup is covered by `test_normalized_campaign_ids_are_counted_once_in_totals`.


if __name__ == '__main__':
    unittest.main()
