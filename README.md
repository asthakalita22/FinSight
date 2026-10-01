# FinSight — AI-Powered Financial Reporting, Reconciliation & Risk Analytics

> **Status: Phases 0–4 complete** — skeleton, PostgreSQL + synthetic data, ETL pipeline, control engine, reconciliation engine, financial analytics.

Later phases (ML anomaly detection, FastAPI, Streamlit, Power BI) build on this foundation.

## What exists so far

| Phase | What you get |
|---|---|
| 0 / 1 | Docker PostgreSQL 16 · 11 tables · FKs, CHECKs, 25 indexes, 11 views · synthetic generator (~150k transactions, 12 months, 8 business units, 5 currencies) with injected data problems and an answer key |
| 2 | ETL (extract → validate → transform → load) · control engine with **9 rules** · exceptions with severity, owner, status · one-command pipeline |
| 3 | Reconciliation engine: source vs ledger on ID, currency, amount, business unit and date · **9 statuses** · metrics + match rate · 3 views · cross-checked against the control engine |
| 4 | Financial analytics: P&L, budget variance, MoM / QoQ trends, business-unit and product breakdowns, liquidity gap and funding requirement · `pnl_service` + `liquidity_service` · terminal finance report · 6 SQL views · validated against independent SQL and the raw transactions |

Verification: 22 + 23 + 38 + 48 automated checks and 71 unit tests.

## Quick start (after installing Docker Desktop + Python 3.12)

```bash
python -m venv .venv
# Windows PowerShell:   .venv\Scripts\Activate.ps1
# macOS / Linux:        source .venv/bin/activate
pip install -r requirements.txt

# Windows: copy .env.example .env      macOS/Linux: cp .env.example .env
docker compose up -d

python automation/pipeline.py    # generate data (if needed) -> ETL -> reconciliation -> controls   (~30 s)
python data/verify_phase1.py     # expect: 22/22 checks passed
python data/verify_phase2.py     # expect: 23/23 checks passed
python data/verify_phase3.py     # expect: 38/38 checks passed
python data/verify_phase4.py     # expect: 48/48 checks passed
python -m pytest                 # expect: 71 passed
python automation/finance_report.py    # read the Phase 4 analytics in the terminal
```

Optional:

```bash
streamlit run frontend/app.py                    # http://localhost:8501  (status page)
uvicorn backend.main:app --reload --port 8000    # http://localhost:8000/health  and /docs
```

## Pipeline

```
data/generate_data.py ──> data/raw/*.csv ──> ETL ──> PostgreSQL ──┬──> reconciliation engine ──> reconciliations
                                              │                     └──> control engine        ──> exceptions
                                              └─ structurally invalid rows ──> data/processed/rejected_records.csv
```

```bash
python automation/pipeline.py                    # full run
python automation/pipeline.py --generate         # force new CSV data first
python automation/pipeline.py --recon-only       # re-run only the reconciliation on what is in the DB
python automation/pipeline.py --controls-only    # re-run only the controls on what is in the DB
python automation/pipeline.py --no-workflow-sim  # leave every exception OPEN
```

The ETL step **rebuilds the database**. The reconciliation step **replaces** the `reconciliations` table (it is fully derived). The controls step is **idempotent** (see below).

> **Upgrading from an earlier phase?** The schema and CSV files changed, so run `python automation/pipeline.py --generate` once (the full pipeline, not `--recon-only`). The same seed regenerates identical data.

## Database connection details (DBeaver / pgAdmin / Power BI later)

| Setting | Value |
|---|---|
| Host | `localhost` |
| Port | `5433` |
| Database | `finsight` |
| User | `finsight` |
| Password | `finsight_pw` |

## Data model

```
business_units (id, name, division, region)      fx_rates (currency, name, rate_to_inr)
      │                                              liquidity_targets (business_unit_id, min_liquidity)
      │
      ├── transactions      raw source-system records (nullable id/currency/BU on purpose)
      ├── ledger_entries    finance-ledger side used for reconciliation
      ├── budgets           monthly budget per BU
      ├── pnl               daily P&L per BU x product (INR)
      ├── cash_flows        daily cash position per BU (INR)
      ├── reconciliations   one row per reconciled record (Phase 3)
      ├── exceptions        (filled by the control engine - Phase 2)
      └── anomalies         (filled in Phase 5)
```

Views: `v_pnl_daily`, `v_pnl_monthly_trend`, `v_pnl_quarterly`, `v_pnl_by_product`, `v_financial_performance`, `v_liquidity_position`, `v_liquidity_summary`, `v_reconciliation_summary`, `v_reconciliation_by_bu`, `v_reconciliation_daily`, `v_exception_summary`.

### Design decisions worth knowing

* **Raw tables are permissive.** `transactions.transaction_id`, `currency` and `business_unit_id` are nullable and `transaction_id` is not unique. Bad data is *loaded* and then *detected* by controls (Phase 2), exactly like a real landing layer.
* **P&L is the ground truth.** It is computed from the clean economics of every transaction, *before* problems are injected. Reconciliation breaks therefore do not distort reported P&L.
* **Reporting currency is INR.** Transactions keep their own currency; P&L and cash flows are in INR using fixed FX rates (USD 83.5, EUR 90.5, GBP 106, SGD 62).
* **Transaction types:** `REVENUE`, `COST`, `EXPENSE`, `ADJUSTMENT`. Adjustments may legitimately be negative and are excluded from P&L. `amount <= 0` is only invalid for the other three types.
* **Ledger posting lag:** the ledger date is 0–2 business days after the transaction date (so up to 4 calendar days across a weekend). The Phase 3 date tolerance must allow for this.
* **Cash flow:** `closing = opening + inflow − outflow`; the next day's opening equals the previous closing. Outflows include costs, occasional lumpy settlements and a month-end treasury sweep. `funding` is the top-up needed when closing cash falls below a liquidity buffer.
* **Budgets** are monthly per BU. Three BU-months carry a deliberately large budget error so the "excessive variance" control (Rule 7) has something to catch.

## Injected problems (base = 150,000 transactions)

| Problem | Count | Rate | How it shows up |
|---|---|---|---|
| `DUPLICATE` | 2,250 | 1.5% | same `transaction_id` twice in `transactions` |
| `MISSING_IN_LEDGER` | 1,500 | 1.0% | no row in `ledger_entries` |
| `AMOUNT_MISMATCH` | 1,500 | 1.0% | ledger amount ≠ source amount |
| `MISSING_TRANSACTION_ID` / `_CURRENCY` / `_BUSINESS_UNIT` | 250 each | 0.5% total | NULL in the source row |
| `UNUSUAL_TRANSACTION` | 750 | 0.5% | 8–30× normal amount, mostly 22:00–04:59 |
| `INVALID_AMOUNT` | 300 | 0.2% | amount ≤ 0 on REVENUE/COST/EXPENSE |
| `ORPHAN_LEDGER_ENTRY` | 450 | 0.3% | ledger row with no source transaction |

Rows whose source `transaction_id` was blanked keep their ledger entry, so they also appear as ledger rows without a source row (700 total unmatched ledger rows).

## Phase 2 — ETL and control engine

### ETL: two kinds of bad data, handled differently on purpose

| Kind | Examples | What happens |
|---|---|---|
| **Structural** — cannot be stored | unparseable date, non-numeric amount, missing NOT-NULL field, unknown transaction type, unknown business unit on the ledger / P&L / budget / cash tables | Row is **quarantined** to `data/processed/rejected_records.csv` with a reason. |
| **Business-rule** — storable but wrong | missing transaction ID / currency / business unit, duplicates, amount ≤ 0, ledger mismatches | Row is **loaded as-is**; the control engine turns it into an exception. Deleting these in ETL would hide them. |

Transform also trims/upper-cases text, sets an unknown business unit on a transaction to NULL (→ Rule CTL-005), and derives `amount_inr` from the `fx_rates` table. Each run writes `data/processed/etl_report.json`.

### Control rules

| Code | Category | Fires when | Base severity | Owner |
|---|---|---|---|---|
| CTL-001 | Missing Transaction ID | source row has no `transaction_id` | HIGH | Data Quality Team |
| CTL-002 | Duplicate Transaction | same `transaction_id` loaded again (every extra copy) | LOW | Data Quality Team |
| CTL-003 | Invalid Amount | amount ≤ 0 on REVENUE / COST / EXPENSE (adjustments may be negative) | HIGH | Financial Control |
| CTL-004 | Missing Currency | source row has no currency | MEDIUM | Data Quality Team |
| CTL-005 | Missing Business Unit | source row has no business unit | MEDIUM | Data Quality Team |
| CTL-006A | Amount Mismatch | source amount ≠ ledger amount (tolerance 0.01) | MEDIUM | Reconciliation Team |
| CTL-006B | Missing in Ledger | source transaction has no ledger entry | MEDIUM | Reconciliation Team |
| CTL-006C | Missing in Source | ledger entry has no source transaction | HIGH | Reconciliation Team |
| CTL-007 | Excessive Variance | \|actual P&L − budget P&L\| / \|budget\| > 15% for a business-unit month | HIGH (CRITICAL at ≥ 2× threshold) | FP&A |

### Exceptions table conventions

* `amount` is the **exposure in INR** (so it can be summed across currencies). For mismatches it is the size of the difference; if the source currency is missing, the ledger's currency is used.
* **Severity** = base severity, raised one level when exposure ≥ ₹100,000 and two levels when ≥ ₹250,000 (max CRITICAL). Thresholds are in `.env`.
* `source_ref` identifies the offending record (`TXN:<row id>`, `LED:<row id>`, `BUM:<bu>:<yyyy-mm>`); `(exception_code, source_ref)` is unique.
* **Idempotent:** re-running the controls inserts only *new* problems and never touches existing rows, so an analyst's status/owner changes survive.
* **Simulated timeline:** `created_at` is a *simulated detection time* (18:00 the day after the record's business date; month-close for variance) because the data is a historical simulation. By default a workflow is also simulated: old exceptions are mostly RESOLVED, recent ones mostly OPEN, critical ones linger longest. Use `--no-workflow-sim` for an all-OPEN table.
* A source row with a blank `transaction_id` raises CTL-001 **and** its orphaned ledger entry raises CTL-006C — one root cause, seen from both sides.
* Unusual transactions (8–30× normal size) deliberately raise **no** control exception: they are the machine-learning model's job in Phase 5.

### Tuning the controls (`.env`)

```
CONTROL_VARIANCE_THRESHOLD_PCT=15
CONTROL_HIGH_VALUE_INR=100000
CONTROL_CRITICAL_VALUE_INR=250000
CONTROL_AMOUNT_TOLERANCE=0.01
```

After changing them: `python automation/pipeline.py` (rebuilds everything with the new thresholds).

## Phase 3 — Reconciliation engine

Compares the **source system** (`transactions`) with the **finance ledger** (`ledger_entries`). A pair is `MATCHED` only when *all five* conditions hold: same **transaction ID**, same **currency**, same **amount** (±0.01), same **business unit**, and the ledger date is **0–4 calendar days after** the transaction date (`RECON_DATE_TOLERANCE_DAYS`).

### Statuses (exactly one per record)

| Status | Meaning |
|---|---|
| `MATCHED` | all five conditions hold |
| `AMOUNT_MISMATCH` | same ID and currency, amounts differ |
| `CURRENCY_MISMATCH` | both currencies present but different *(extra status — the spec asks for currency comparison)* |
| `BUSINESS_UNIT_MISMATCH` | both business units present but different *(extra)* |
| `DATE_MISMATCH` | ledger posted outside the window, or *before* the transaction *(extra)* |
| `MISSING_IN_LEDGER` | source transaction has no ledger entry |
| `MISSING_IN_SOURCE` | ledger entry has no source transaction |
| `DUPLICATE` | an extra copy of a transaction ID (source side or ledger side) |
| `INVALID` | cannot be evaluated: source is missing its transaction ID, currency or business unit |

When one pair has several problems the status follows the precedence `INVALID > CURRENCY > AMOUNT > BUSINESS_UNIT > DATE`, and **all** problems are listed in `reason`.

### Rules of the engine worth knowing

* **Exactly once.** Every source row and every ledger row appears in exactly one reconciliation record. This is enforced by `UNIQUE` constraints on `source_row_id` and `ledger_row_id`, so double counting is impossible.
* **Duplicates:** only the *oldest* copy of a duplicated ID is paired with the ledger; later copies are `DUPLICATE`.
* **Agreement, not validity.** An amount of 0 that is identical in both systems is `MATCHED`; whether the value is sensible is control rule CTL-003's job. Likewise, unusual transactions reconcile fine (they belong to the ML model).
* **`difference`** = ledger − source, in the transaction currency, only when both sides share that currency. **`exposure_inr`** is the value at stake in INR (0 for MATCHED).
* **Metrics:** Total, Matched, Mismatched, Missing, Duplicates, Invalid, **Match Rate = Matched ÷ Total × 100** — see `v_reconciliation_summary`.

### Expected results on the generated data

| | Records | |
|---|---|---|
| Total | 152,950 | 152,250 source rows + 700 ledger-only entries |
| Matched | 146,250 | **match rate 95.62%** (≈ 4.4% of records carry an injected problem) |
| Mismatched (amount) | 1,500 | |
| Missing | 2,200 | 1,500 in ledger + 700 in source |
| Duplicates | 2,250 | |
| Invalid | 750 | 250 each: no ID / no currency / no business unit |

### Two engines, one truth

The reconciliation engine and the control engine were written independently, and `verify_phase3.py` requires them to **agree ID by ID** (mismatch = CTL-006A, missing = CTL-006B/C, duplicate = CTL-002, invalid = CTL-001/004/005), with exposures equal to within rounding. The same script also **plants 250 known breaks** into the real data (in memory only) — wrong amount, currency, business unit, late/early posting, deleted or duplicated records, blanked currency — and confirms each is detected with the right status and that nothing else changed.

### Tuning (`.env`)

```
RECON_DATE_TOLERANCE_DAYS=4
CONTROL_AMOUNT_TOLERANCE=0.01
```

## Phase 4 — Financial analytics

Two services, both **read-only** and both split into *database loaders* and *pure functions* (so every formula is unit-tested against hand-computed numbers):

| Service | Functions |
|---|---|
| `backend/services/pnl_service.py` | `summary`, `trend(grain=day\|month\|quarter, by=business_unit\|product)`, `breakdown(dimension)`, `budget_variance` |
| `backend/services/liquidity_service.py` | `summary`, `daily`, `monthly`, `by_business_unit` |

```python
from backend.database import engine
from backend.services import pnl_service, liquidity_service
from backend.utils.filters import Filters

f = Filters.for_period(quarter="2026-Q1", business_units=("Equities", "Derivatives"))
pnl_service.summary(engine, f)                      # dict of totals, margins, variance
pnl_service.trend(engine, f, "month")               # DataFrame with MoM growth
pnl_service.breakdown(engine, f, "business_unit")   # ranked, with revenue share
liquidity_service.summary(engine, f)                # cash position, gap, funding requirement
```

Filters: **date range** (a month or a quarter is just a date range: `Filters.for_period(month="2026-03")`), **business unit**, **product**, **currency**. Unknown names raise a `ValueError` listing the valid values; a range with no data returns zeros / `None`, never an error.

### Definitions

| Measure | Formula |
|---|---|
| Total expenses | Cost + Operating expense |
| Gross P&L | Revenue − Cost |
| Net P&L | Revenue − Cost − Expense |
| Variance | Actual − Budget |
| Variance % | Variance ÷ \|Budget\| × 100 *(identical to the spec when the budget is positive)* |
| MoM / QoQ growth | (This period − previous) ÷ \|previous\| × 100 — first period is empty, never 0 |
| Net cash flow | Inflows − Outflows |
| Closing cash | Opening + Inflows − Outflows |
| **Liquidity gap** | **Minimum liquidity − Closing cash** (positive = shortfall, negative = surplus) |
| **Funding requirement** | **Σ over business units of MAX(0, gap)** |

Undefined ratios (zero budget, zero revenue, no previous period) are `NaN` / `None`, never 0 or ∞.

### Decisions worth knowing

* **Funding requirement is not netted across business units.** Cash sits in separate pools, so a surplus in one unit cannot fund another. The company-level `liquidity_gap` (which *does* net) can therefore be negative while `funding_requirement` is positive — e.g. on 2026-07-08 the company was ₹59.5 M above its buffer while one unit still needed ₹6.9 M. (`sql/analytics.sql` query 26.)
* **Over a date range**, opening cash is the first day's opening and closing cash is the last day's closing; flows are summed; **peak** funding requirement is the worst *single day* (funding is a level, not a flow, so summing days would double-count).
* **Minimum liquidity** per business unit lives in the new `liquidity_targets` table (85% of the starting cash pool).
* **Budget granularity.** Budgets exist per business unit and month. The daily / product budget in `pnl` is that monthly figure split *equally*, so **business-unit and month variances are exact, product-level and daily variances are indicative only**. `budget_variance()` therefore refuses a product filter, and the terminal report says so.
* **Quarters are calendar quarters**; the data starts in September, so the first (2025-Q3) and last (2026-Q3) are partial. Every quarterly row carries `months_covered`.
* Per-month **business days** are returned next to MoM growth, because a 20-day month naturally trails a 23-day month.
* The P&L is stored in the reporting currency (INR), so the currency filter only matches `INR`.

### How the analytics are validated

`data/verify_phase4.py` (48 checks) does not just re-run the same code:

1. **Rebuilds the P&L from the raw transactions**, cell by cell (date × business unit × product), and requires every cell to match to the paisa — except the 289 cells that hold a deliberately corrupted amount, whose total gap (0.25% of activity) it quantifies.
2. Ties cash flows to the P&L (inflow = revenue on every unit-day; outflow ≥ costs; funding = MAX(0, buffer − closing)).
3. Compares **every service output with an independent SQL implementation** (`v_pnl_monthly_trend` computes MoM with `LAG()`, `v_pnl_quarterly`, `v_liquidity_summary`, …).
4. Checks invariants: business units, products, months, quarters and days each add up to the same grand total; the cash identity holds over **25 random date ranges × random business-unit subsets**, verified against raw SQL.
5. Requires the budget-variance breaches to equal the CTL-007 exceptions raised in Phase 2.

### Headline numbers (generated data, 12 months)

| | |
|---|---|
| Revenue | ₹4,736.6 M |
| Net P&L | ₹1,569.6 M (33.1% margin), +1.6% vs budget |
| Best / weakest unit (net P&L) | Derivatives / Wealth Management |
| Cash position at 2026-08-31 | ₹175.2 M (1.42× the liquidity buffer) |
| Funding | 21 shortfall days; peak requirement ₹6.9 M on 2026-07-08 |

### Terminal report

```bash
python automation/finance_report.py
python automation/finance_report.py --quarter 2026-Q1
python automation/finance_report.py --month 2026-03 --bu Equities --bu Derivatives
python automation/finance_report.py --start 2026-01-15 --end 2026-02-10 --product Equity
```

## Handy commands

```bash
docker compose ps                      # is Postgres healthy?
docker compose logs postgres           # database logs
docker compose down                    # stop (data is kept in a Docker volume)
docker compose down -v                 # stop AND delete all database data
docker exec -it finsight_postgres psql -U finsight -d finsight    # open a SQL shell
python data/generate_data.py --transactions 200000 --seed 7       # different dataset
```

`python data/load_data.py` runs only the ETL (no controls) and, like the pipeline, drops and recreates every table.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not connect to PostgreSQL` | Start Docker Desktop, run `docker compose up -d`, wait until `docker compose ps` shows `healthy`. |
| `port is already allocated` | Change `POSTGRES_PORT` in `.env` (e.g. 5434), then `docker compose down` and `docker compose up -d`. |
| PowerShell: *running scripts is disabled* | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then activate the venv again. |
| `ModuleNotFoundError` | The venv is not active (you should see `(.venv)` in the prompt) — activate it and `pip install -r requirements.txt`. |
| `Missing .../transactions.csv` | Run `python data/generate_data.py` (or just `python automation/pipeline.py`, which generates when needed). |
| `Missing .../fx_rates.csv` | You have Phase 1 CSVs. Run `python automation/pipeline.py --generate`. |
| `KeyError: 'liquidity_targets'` or `Missing .../liquidity_targets.csv` | You have CSVs from an earlier phase. Run `python automation/pipeline.py --generate`. |
| Exceptions table is empty | Run `python automation/pipeline.py` (or `--controls-only`). |
| `pip install` fails on `psycopg2-binary` | Use Python 3.11 or 3.12 (not 3.14). |

## Roadmap

~~Phase 2 ETL + controls~~ ✅ → ~~3 Reconciliation~~ ✅ → ~~4 Financial analytics~~ ✅ → 5 ML anomaly detection → 6 FastAPI → 7–10 Streamlit → 11 Power BI → 12 AI commentary → 13 Automation → 14–17 Testing, polish, docs.
