# Portfolio Tracker (FastAPI + SQLite)

Private, single-user portfolio tracker with login, weighted-average cost basis, quote caching, and desktop-focused UI.

## Features (MVP)
- USD-only portfolio tracking.
- Username/password auth with secure password hashing.
- Market assets (symbol-based) and manual-value assets.
- Transactions: `BUY`, `SELL`, `MANUAL_VALUE_UPDATE`.
- Weighted-average cost basis and deterministic recomputation from transaction history.
- Quote provider abstraction with default `yfinance` implementation.
- SQLite quote cache with 60s TTL and stale fallback on provider failure.
- Dashboard auto-refresh every 60 seconds via `/api/quotes`.
- Group subtotals, grand totals, allocation pie chart.
- Asset performance line chart and basket normalized chart.

## Tech Stack
- Python 3.11+
- FastAPI + Jinja2 templates
- SQLAlchemy + SQLite
- Chart.js via CDN

## Project Layout
```text
app/
  __init__.py
  main.py
  config.py
  db.py
  models.py
  security.py
  web.py
  services/
    pricing.py
    portfolio.py
  routes/
    auth.py
    dashboard.py
    assets.py
    baskets.py
  templates/
  static/
main.py
manage.py
tests/
requirements.txt
README.md
```

## Local Setup
1. Create a virtual environment and install dependencies:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Configure environment variables (optional):
```bash
export SECRET_KEY="replace-with-random-secret"
export DATABASE_URL="sqlite:///./portfolio.db"
export QUOTE_TTL_SECONDS="60"
export SESSION_HTTPS_ONLY="false"
```

3. Create the initial user:
```bash
python3 manage.py create-user --username admin
```

4. Run the app locally:
```bash
uvicorn app.main:app --reload
```

5. Open:
- `http://127.0.0.1:8000/login`

## Environment Variables
- `APP_NAME` (default: `Portfolio Tracker`)
- `SECRET_KEY` (default: `change-me-in-production`)
- `DATABASE_URL` (default: `sqlite:///./portfolio.db`)
- `QUOTE_TTL_SECONDS` (default: `60`)
- `SESSION_COOKIE_NAME` (default: `portfolio_session`)
- `SESSION_HTTPS_ONLY` (default: `false`)

## Quote Provider Notes
- Default provider is `yfinance`.
- On quote fetch failure:
  - Cached quote is reused (marked stale).
  - If no cached quote exists, the UI shows unavailable data.
- Refresh endpoint:
  - `GET /api/quotes?symbols=AAPL,MSFT,BTC-USD`

## Running Tests
```bash
pytest -q

pytest --cov=. --cov-report=term-missing
```

Current tests cover:
- Weighted-average cost basis.
- Deterministic recomputation after transaction edits.
- Allocation percentages summing to approximately 100%.

## Deployment Options ($0-friendly)
- Local-first (primary): run on one local machine with SQLite.
- Optional AWS serverless direction (not implemented):
  - FastAPI behind API Gateway + Lambda adapter.
  - Static assets/templates served by app or static hosting.
  - Note: SQLite is not ideal for truly distributed serverless workloads; keep usage single-instance.
