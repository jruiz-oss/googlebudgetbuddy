# Running Google BudgetBuddy Locally

## Prerequisites
- Python 3.11+
- Node.js 18+
- A Neon database (get connection string from neon.tech)

## Backend setup (Terminal 1)

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create a .env file with your credentials:
cat > .env << 'EOF'
DATABASE_URL=postgresql://your-neon-connection-string
SECRET_KEY=any-random-string-for-local-dev
GOOGLE_ADS_CLIENT_ID=your-client-id
GOOGLE_ADS_CLIENT_SECRET=your-client-secret
GOOGLE_ADS_DEVELOPER_TOKEN=your-developer-token
GOOGLE_ADS_MCC_ID=1234567890
BACKEND_URL=http://localhost:5000
FRONTEND_URL=http://localhost:3000
FLASK_ENV=development
EOF

# Load env and run:
source .env  # or use python-dotenv
python app.py
```

Backend runs at http://localhost:5000

## Frontend setup (Terminal 2)

```bash
cd frontend
npm install
npm run dev
```

Frontend runs at http://localhost:3000

## First time setup

1. Go to http://localhost:3000/register and create your account
2. Go to Settings → Connect Google Account → follow the OAuth flow
3. Add a Google Ads account (your MCC child account customer ID)
4. Import campaigns from Google Ads
5. Add your Google Sheet ID in Settings
6. Run Pacing!
