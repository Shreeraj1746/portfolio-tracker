# Portfolio Tracker Technical Design

## 1. Purpose And Audience
This document explains how the application is designed and implemented so a new engineer can:
- Understand the current architecture and core invariants.
- Trace how data moves from HTTP request to persisted state to rendered UI.
- Safely add or modify features with minimal regression risk.

Scope:
- Current implementation in this repository.
- Backend, frontend integration points, data model, and test strategy.
- Operational behavior for local-first deployment.

## 2. System Overview
The app is a server-rendered FastAPI monolith with a thin JavaScript layer for:
- Chart rendering (Chart.js).
- Live quote polling and dashboard table recomputation.
- Dynamic transaction and asset form behavior.

Core stack:
- FastAPI + Starlette sessions.
- SQLAlchemy ORM.
- SQLite (default).
- Jinja2 templates.
- `yfinance` quote provider through an internal abstraction.

Primary design characteristics:
- Single-user, single-portfolio model.
- Event-sourced portfolio state derived from transaction history.
- Deterministic recomputation for accounting correctness.
- Basket rows are derived overlays, not canonical holdings.

## 3. Codebase Layout And Responsibilities
Key files/modules:
- `app/__init__.py`: app factory, middleware, template filters, router registration, startup initialization.
- `app/config.py`: environment-based settings.
- `app/db.py`: SQLAlchemy engine/session setup, table creation, lightweight schema backfill.
- `app/models.py`: ORM schema and relationships.
- `app/security.py`: password hashing and verification.
- `app/web.py`: shared web helpers (CSRF, flash messaging, template rendering, session user loading).
- `app/routes/auth.py`: login/logout endpoints.
- `app/routes/dashboard.py`: dashboard page, group/asset creation, quote refresh API.
- `app/routes/assets.py`: asset detail/edit/archive/delete and transaction CRUD.
- `app/routes/baskets.py`: basket CRUD and basket detail/series pages.
- `app/services/pricing.py`: quote provider protocol, yfinance adapter, cache-aware pricing service.
- `app/services/portfolio.py`: accounting logic, snapshot builders, chart series builders, domain validation.
- `app/static/main.js`: chart bootstrapping, live quote polling, dashboard recalculation, form UX logic.
- `app/templates/*.html`: server-rendered pages and JS payload embedding.
- `manage.py`: management CLI commands (`create-user`, `purge-archived-assets`).
- `tests/`: unit and regression tests with in-memory SQLite and mock quote provider.

Entrypoints:
- `app/main.py` exposes `app = create_app()`.
- `main.py` re-exports `app.main:app`.

## 4. Runtime And Initialization Flow
Startup flow (`app/__init__.py`):
1. Create `FastAPI`.
2. Add session middleware (`SessionMiddleware`).
3. Configure Jinja environment and custom filters:
   - `currency`, `number`, `pct`, `dt`.
4. Initialize pricing service:
   - Default: `YFinanceProvider` wrapped by `PricingService`.
   - Fallback: `_UnavailableProvider` if yfinance import/init fails.
5. Mount static files at `/static`.
6. Include routers.
7. Startup hook:
   - `init_db()` to create tables and run lightweight schema backfill.
   - `ensure_default_portfolio()` to enforce existence of one default portfolio row.

Database initialization details (`app/db.py`):
- Calls `Base.metadata.create_all`.
- Performs manual compatibility backfill:
  - Adds `transactions.invested_override` column if missing.
- No Alembic migration framework is used.

## 5. Security Model
Authentication:
- Session-based auth using `user_id` stored in signed cookie-backed session.
- Login checks hashed password (`werkzeug.security`).

CSRF:
- Server generates session CSRF token (`app/web.py`).
- All state-changing form endpoints call `form_with_csrf`.
- Invalid token raises HTTP 400.

Session config:
- `same_site="lax"`.
- Cookie name and HTTPS-only behavior controlled via settings.

Notes:
- This is not a multi-tenant permission system; user-to-portfolio scoping is implicit and single-user.

## 6. Data Model
Tables (`app/models.py`):

1. `users`
- `id`, `username` (unique), `password_hash`, `created_at`.

2. `portfolios`
- `id`, `name`, `created_at`.
- Parent of groups, assets, transactions, baskets.

3. `groups`
- `id`, `portfolio_id`, `name`.
- Unique constraint on (`portfolio_id`, `name`).

4. `assets`
- `id`, `portfolio_id`, `symbol`, `name`, `asset_type`, `group_id`, `created_at`, `is_archived`.
- `asset_type`: `market` or `manual`.
- Archive is soft-delete behavior for most UI paths.

5. `transactions`
- `id`, `portfolio_id`, `asset_id`, `type`, `timestamp`, `quantity`, `price`, `fees`, `manual_value`, `invested_override`, `note`.
- `type`: `BUY`, `SELL`, `MANUAL_VALUE_UPDATE`.

6. `quote_cache`
- `symbol` (unique), `price`, `fetched_at`.
- Used by `PricingService` for TTL and stale fallback.

7. `baskets`
- `id`, `portfolio_id`, `name`, `created_at`.

8. `basket_assets`
- `basket_id`, `asset_id`, optional `weight` (legacy/deprecated).
- Unique constraint on (`basket_id`, `asset_id`).

Relationship/cascade behavior:
- Portfolio delete cascades to children.
- Asset delete cascades to transactions and basket links.
- Basket delete cascades to basket links.

## 7. Core Domain And Accounting Logic
All core accounting lives in `app/services/portfolio.py`.

### 7.1 Deterministic Ordering
`sort_transactions` sorts by `(timestamp, id)` for stable replay.

### 7.2 Market Asset Position Logic
`compute_market_position`:
- Accepts `BUY` and `SELL`.
- Ignores `MANUAL_VALUE_UPDATE`.
- `BUY` updates weighted average cost:
  - `new_cost_total = (qty * avg_cost) + (buy_qty * buy_price) + fees`
  - `qty += buy_qty`
  - `avg_cost = new_cost_total / qty`
- `SELL` reduces quantity only:
  - Validates cannot sell more than held.
  - Resets qty and avg cost to zero when position closes.

### 7.3 Manual Asset Position Logic
`compute_manual_position`:
- Supports `BUY`, `SELL`, `MANUAL_VALUE_UPDATE`.
- Tracks:
  - `held_quantity`
  - `invested_total`
  - `avg_cost` for quantity replay
  - latest manual current value and timestamp
- `BUY` increases invested and held quantity.
- `SELL` reduces invested by average-cost basis of sold quantity.
- `MANUAL_VALUE_UPDATE` sets current value.
- `MANUAL_VALUE_UPDATE` can optionally set `invested_override`:
  - Resets invested basis directly.
  - Re-bases internal `held_quantity` so future sells work from the new basis.
- Unrealized PnL:
  - `current_value - invested_total` when invested is positive.
  - `None` when invested is zero.

### 7.4 Asset Transaction Validation
`validate_asset_transactions` replays full history before persisting changes:
- Market assets reject `MANUAL_VALUE_UPDATE`.
- Manual assets replay through `compute_manual_position`.

Route handlers validate candidate transaction timelines by:
- Building a temporary candidate row.
- Merging into history.
- Replaying full sequence.

This prevents partial validation bugs where one transaction appears valid in isolation.

## 8. Dashboard Snapshot Semantics
`build_dashboard_snapshot` produces all dashboard table totals/charts.

Important design split:
- Canonical rows: actual assets.
- Derived rows: basket overlays.

Canonical rows:
- Included in grand totals and canonical allocation.

Derived basket rows:
- Synthesized from member asset rows.
- Excluded from canonical totals and canonical allocation.
- Included in dedicated "derived subtotal" display.

This avoids double counting while still showing basket views.

Allocation semantics:
- Group allocation uses canonical rows only.
- Asset allocation excludes member assets that belong to a displayed basket overlay and uses basket labels in their place.

## 9. Time-Series Algorithms
Portfolio and PnL chart logic is built in service functions:

1. `compute_portfolio_series`
- Builds daily range.
- Market assets:
  - Replays quantity by day.
  - Fetches historical closes.
  - Uses forward-fill for missing closes inside selected range.
- Manual assets:
  - Uses step function from manual value updates.
- Returns `PortfolioSeriesResult` with missing symbols and optional error message.

2. `compute_overlay_pnl_series`
- Market assets:
  - Replays `(quantity, avg_cost)` state by day.
  - Computes `(current_price - avg_cost) * qty`.
- Manual assets:
  - Uses manual value step minus invested-by-day.
- Basket overlays:
  - Sum member market-asset PnL series into basket label.
  - Remove basket-member assets from direct selectors to avoid double counting.

3. `compute_basket_series`
- Selects active market members with positive quantities.
- Fetches per-member historical closes.
- Requires strict date intersection across members.
- Builds normalized base-100 weighted composite series.
- Weights are derived from live held shares (not stored weights).

## 10. Quote Layer Design
`app/services/pricing.py`:
- `QuoteProvider` protocol defines live and historical methods.
- `YFinanceProvider` is default provider.
- `PricingService` adds:
  - Symbol normalization (`BTC` -> `BTC-USD`, etc.).
  - DB cache lookup (`quote_cache`).
  - TTL behavior.
  - Stale-cache fallback if provider call fails.

Behavior details:
- Fresh cached quote within TTL returns immediately.
- On refresh failure:
  - Returns stale cached quote if present.
  - Returns `None` if no cache exists.

## 11. HTTP Route Design
Authentication (`app/routes/auth.py`):
- `GET /login`
- `POST /login`
- `POST /logout`

Dashboard (`app/routes/dashboard.py`):
- `GET /`: render full dashboard and chart payloads.
- `POST /groups`: create group.
- `POST /assets`: create asset plus optional initial transactions.
- `GET /api/quotes`: authenticated quote refresh endpoint used by polling JS.

Assets (`app/routes/assets.py`):
- `GET /assets/{asset_id}`: detail page with summary/history/chart.
- `GET /assets/{asset_id}/edit`
- `POST /assets/{asset_id}/edit`
- `POST /assets/{asset_id}/delete`
- `POST /assets/{asset_id}/archive`
- `POST /assets/{asset_id}/transactions`
- `GET /assets/{asset_id}/transactions/{tx_id}/edit`
- `POST /assets/{asset_id}/transactions/{tx_id}/edit`
- `POST /assets/{asset_id}/transactions/{tx_id}/delete`

Baskets (`app/routes/baskets.py`):
- `GET /baskets`
- `POST /baskets/create`
- `GET /baskets/{basket_id}`
- `GET /baskets/{basket_id}/edit`
- `POST /baskets/{basket_id}/edit`
- `POST /baskets/{basket_id}/delete`

Route conventions:
- Unauthenticated requests redirect to `/login` for HTML pages.
- Most POST actions redirect with 303 and flash message.
- Validation failures flash error and redirect back to source page.

## 12. Frontend Integration Design
Templates are server-rendered and inject JSON data into globals:
- `window.PORTFOLIO_DASHBOARD`
- `window.ASSET_CHART`
- `window.BASKET_CHART`

`app/static/main.js` responsibilities:
- Chart rendering wrappers (`renderAllocationPie`, `renderLineChart`, `renderPnlChart`).
- Dashboard recalculation from DOM `data-*` attributes:
  - Group subtotals.
  - Canonical grand totals.
  - Derived basket subtotal.
  - Allocation percentages.
- Live quote polling every 60 seconds:
  - Calls `/api/quotes`.
  - Updates market rows.
  - Recomputes derived basket rows and totals.
- Form UX:
  - Show/hide market/manual asset-create fields.
  - Show/hide transaction fields by tx type.
  - Set `required` attributes dynamically.

## 13. Management CLI
`manage.py`:
- `create-user --username <name>`
  - Prompts for password.
  - Ensures default portfolio exists.
- `purge-archived-assets [--older-than-days N]`
  - Hard-deletes archived assets and dependent rows.
  - Optional age filter uses `Asset.created_at`.

Important:
- `app/cli.py` is legacy and currently references missing imports (`app.helpers`).
- Use `manage.py` as the active management interface.

## 14. Testing Strategy
Test framework:
- `pytest` with in-memory SQLite and `TestClient`.

Key fixtures (`tests/conftest.py`):
- In-memory DB with shared static pool.
- Dependency override for `get_db`.
- Mock quote provider for deterministic live/historical data.
- Authenticated client and CSRF token helper fixtures.

Test coverage structure:
- `tests/test_portfolio.py`:
  - Core accounting/unit tests.
  - Deterministic replay and edge-case math.
- `tests/test_regressions.py`:
  - Route-level and integration regressions.
  - Basket behavior, overlays, chart payloads, allocation and PnL semantics.
- `tests/test_manage.py`:
  - Archive purge command behavior and age filtering.

## 15. Feature Development Guidelines
### 15.1 If Adding A New Transaction Type
Update all of the following:
- `TransactionType` enum in `app/models.py`.
- Parsing/validation in `app/routes/assets.py`.
- Replay logic in `compute_market_position` or `compute_manual_position`.
- History validators in `validate_asset_transactions`.
- Time-series helpers for portfolio and PnL charts.
- Form fields and dynamic behavior in templates and `app/static/main.js`.
- Unit/regression tests.

### 15.2 If Adding New Derived Dashboard Rows
Touch points:
- Snapshot build in `build_dashboard_snapshot`.
- `counts_in_totals` and `counts_in_allocation` flags.
- Frontend recomputation in `updateDashboardTable`.
- Allocation and PnL selector semantics to avoid double counting.

### 15.3 If Changing Pricing Providers
Implement `QuoteProvider` methods:
- `get_latest_quote`.
- `get_historical_daily`.

Then wire provider construction in `create_app` and keep cache contract intact.

### 15.4 If Changing Schema
Current project has no migration framework.
- Safe pattern used today: `create_all` + explicit runtime backfill (`_ensure_transaction_columns`).
- For non-trivial schema evolution, introducing Alembic is recommended before further complexity.

## 16. Operational Notes
Environment variables (`app/config.py`):
- `APP_NAME`
- `SECRET_KEY`
- `DATABASE_URL`
- `QUOTE_TTL_SECONDS`
- `SESSION_COOKIE_NAME`
- `SESSION_HTTPS_ONLY`
- `SQLITE_BUSY_TIMEOUT_MS`
- `SQLITE_JOURNAL_MODE`

Default deployment assumptions:
- Single process and single SQLite instance.
- Local-first usage.

## 17. Known Limitations And Technical Debt
1. Single-user/single-portfolio assumptions are hard-coded across flows.
2. No formal migration system; runtime DDL backfills are minimal and manual.
3. Startup hook uses deprecated FastAPI `on_event` API (works now, should migrate to lifespan handlers).
4. Legacy `app/cli.py` is stale and should be removed or fixed.
5. SQLite limits horizontal scaling and concurrent write throughput.
6. Manual invested-by-day series helper currently replays buys only; when adding richer manual accounting behavior, verify time-series parity with point-in-time summaries.

## 18. Suggested Next Improvements
1. Introduce Alembic migrations and versioned schema history.
2. Move startup to FastAPI lifespan.
3. Remove or repair `app/cli.py`.
4. Add typed DTOs/view-model builders for template payloads to reduce context key sprawl.
5. Add contract tests for chart payload invariants whenever accounting logic changes.
