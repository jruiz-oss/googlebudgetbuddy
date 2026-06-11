"""
One-off diagnostic for the lead pull returning count: 0.

Runs the lead_form_submission_data GAQL query for an account WITHOUT any date
filter, so we can tell whether the account has ANY native Lead Form asset
submissions at all (vs. the date window being the problem).

Reads the account + OAuth refresh token straight from the DB (DATABASE_URL),
so it uses the same credentials the app uses.

Usage (run from repo root, with backend env vars loaded):
    python scripts/debug_lead_pull.py <account_id>
    python scripts/debug_lead_pull.py <account_id> --days 7   # last N days instead of all

Delete this file once the question is answered.
"""

import os
import sys

# Make the backend package importable when run from repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from google_ads_client import get_access_token, _gaql, GoogleAdsError  # noqa: E402


def _effective_mcc(account_mcc):
    return (account_mcc or os.environ.get('GOOGLE_ADS_MCC_ID', '')).replace('-', '').strip() or None


def main():
    if len(sys.argv) < 2:
        print('Usage: python scripts/debug_lead_pull.py <account_id> [--days N]')
        sys.exit(1)

    account_id = int(sys.argv[1])
    days = None
    if '--days' in sys.argv:
        days = int(sys.argv[sys.argv.index('--days') + 1])

    # Pull account + token from the DB.
    from app import create_app  # noqa: E402
    from database import Account, GoogleOAuthToken  # noqa: E402

    app = create_app()
    with app.app_context():
        account = Account.query.get(account_id)
        if not account:
            print(f'No account with id {account_id}')
            sys.exit(1)
        token = GoogleOAuthToken.query.filter_by(user_id=account.user_id, is_valid=True).first()
        if not token:
            print(f'No valid OAuth token for user {account.user_id}')
            sys.exit(1)

        customer_id = account.google_customer_id
        mcc = _effective_mcc(account.mcc_customer_id)
        refresh_token = token.refresh_token
        print(f'Account: {account.account_name} (cid={customer_id}, login-customer-id={mcc})')

    developer_token = os.environ.get('GOOGLE_ADS_DEVELOPER_TOKEN', '')
    access_token = get_access_token(refresh_token)

    where = ''
    if days:
        where = f'WHERE segments.date DURING LAST_{days}_DAYS'

    # No field is filtered by date here unless --days is passed. Just see if ANY
    # lead-form-asset submissions exist for this account.
    query = f"""
        SELECT
          lead_form_submission_data.id,
          lead_form_submission_data.campaign,
          lead_form_submission_data.submission_date_time
        FROM lead_form_submission_data
        {where}
        ORDER BY lead_form_submission_data.submission_date_time DESC
        LIMIT 50
    """

    print('\n--- GAQL ---')
    print(query.strip())
    print('------------\n')

    try:
        rows = _gaql(access_token, customer_id, developer_token, query, mcc_customer_id=mcc)
    except GoogleAdsError as e:
        print(f'QUERY ERROR: {e}')
        sys.exit(1)

    print(f'ROWS RETURNED: {len(rows)}')
    for r in rows[:50]:
        lf = r.get('leadFormSubmissionData', {})
        print(f"  {lf.get('submissionDateTime', '?')}  id={lf.get('id', '?')}  campaign={lf.get('campaign', '?')}")

    if not rows:
        print('\nNo lead-form-asset submissions found AT ALL for this account.')
        print('=> This account almost certainly does not use Google native Lead Form assets.')
        print('   The "leads" you see are likely website/conversion leads, which live')
        print('   in conversions, NOT in lead_form_submission_data.')


if __name__ == '__main__':
    main()
