# Portfolio Tracker (FastAPI + SQLite)

Single-user portfolio tracker with authentication, transaction-based accounting, live quote refresh, and dashboard charts.

## Features
- USD-only portfolio tracking.
- Username/password login with session auth.
- Asset types:
  - market assets (symbol-based)
  - manual-value assets
- Transaction types: `BUY`, `SELL`, `MANUAL_VALUE_UPDATE`.
- Deterministic position recomputation from transaction history using weighted-average cost basis.
- Dashboard with canonical totals, per-group subtotals, and allocation charts.
- Baskets as derived overlays on top of canonical assets.
- Charts:
  - portfolio value over time
  - unrealized PnL over time
  - per-asset performance history
  - basket normalized performance
- Quote provider abstraction with default `yfinance` implementation, SQLite caching, and stale-cache fallback.

## Requirements
- Python 3.11+
- Internet access for live quote refreshes (`yfinance`)

## Quick Start
1. Create a virtual environment and install dependencies.
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Optionally set environment variables.
```bash
export SECRET_KEY="replace-with-random-secret"
export DATABASE_URL="sqlite:///./portfolio.db"
export QUOTE_TTL_SECONDS="60"
export SESSION_HTTPS_ONLY="false"
```

3. Create the initial user (prompts for password; minimum 8 characters).
```bash
python3 manage.py create-user --username admin
```

4. Run the app.
```bash
uvicorn app.main:app --reload
```

5. Open `http://127.0.0.1:8000/login`.

Tables are created automatically by startup/management flow if they do not already exist.

## Configuration
- `APP_NAME` (default: `Portfolio Tracker`)
- `SECRET_KEY` (default: `change-me-in-production`)
- `DATABASE_URL` (default: `sqlite:///./portfolio.db`)
- `QUOTE_TTL_SECONDS` (default: `60`)
- `SESSION_COOKIE_NAME` (default: `portfolio_session`)
- `SESSION_HTTPS_ONLY` (default: `false`)

## Quote Behavior
- Default provider is `yfinance`.
- Refresh endpoint: `GET /api/quotes?symbols=AAPL,MSFT,BTC`
- Common crypto symbols (for example `BTC`, `ETH`, `SOL`) are mapped to provider `-USD` tickers automatically.
- On provider failure:
  - cached quote is reused and marked stale
  - if no cached quote exists, quote data is unavailable

## Basket Accounting Semantics
- Basket rows are display overlays derived from current member asset rows.
- Basket rows are excluded from canonical portfolio totals and canonical group allocation.
- `basket_assets.weight` is retained only for backward compatibility and is not used for valuation.

## Running Tests
```bash
pytest -q
pytest --cov=. --cov-report=term-missing
```

Test suite covers:
- weighted-average math and invalid transaction handling
- deterministic recomputation and allocation math
- basket CRUD and basket overlay behavior
- chart/series regressions around portfolio, asset, and basket views

## Maintenance
Permanently remove archived assets (and dependent rows) from the database:

```bash
python3 manage.py purge-archived-assets
# optional: only purge archived assets created at least 30 days ago
python3 manage.py purge-archived-assets --older-than-days 30
```

## Project Layout
```text
app/
  routes/         # FastAPI handlers (auth, dashboard, assets, baskets)
  services/       # Pricing and portfolio/accounting logic
  templates/      # Jinja2 templates
  static/         # JS/CSS
  config.py
  db.py
  models.py
manage.py         # CLI management commands (create-user)
tests/
```
