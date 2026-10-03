# FinSight — AI-Powered Financial Reporting, Reconciliation & Risk Analytics

> **Status: Phases 0–5 complete** — skeleton, PostgreSQL + synthetic data, ETL pipeline, control engine, reconciliation engine, financial analytics, ML anomaly detection.

Later phases (Streamlit, Power BI, AI commentary, automation) build on this foundation.

## What exists so far

| Phase | What you get |
|---|---|
| 0 / 1 | Docker PostgreSQL 16 · 13 tables · FKs, CHECKs, 28 indexes, 13 views · synthetic generator (~150k transactions, 12 months, 8 business units, 5 currencies) with injected data problems and an answer key |
| 2 | ETL (extract → validate → transform → load) · control engine with **9 rules** · exceptions with severity, owner, status · one-command pipeline |
| 3 | Reconciliation engine: source vs ledger on ID, currency, amount, business unit and date · **9 statuses** · metrics + match rate · 3 views · cross-checked against the control engine |
| 4 | Financial analytics: P&L, budget variance, MoM / QoQ trends, business-unit and product breakdowns, liquidity gap and funding requirement · `pnl_service` + `liquidity_service` · terminal finance report · 6 SQL views · validated against independent SQL and the raw transactions |
| 5 | Anomaly detection: Isolation Forest over cleaned transactions · anomaly score, flag and **HIGH / MEDIUM / LOW** risk · plain-English **explanation** for every anomaly · saved model (`ml/model.pkl`) · measured against the answer key |
| 6 | FastAPI read-only REST API over the validated service layer · P&L · liquidity · reconciliation · controls/exceptions · anomalies · filter/reference endpoints · OpenAPI docs · local Streamlit CORS · API contract tests |

Verification: 22 + 23 + 38 + 48 + 53 = 184 automated checks and 86 unit tests.

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
python data/verify_phase5.py     # expect: 53/53 checks passed
python -m pytest                 # expect: 86 passed
python automation/finance_report.py    # read the Phase 4 analytics in the terminal
python automation/anomaly_report.py    # read the Phase 5 anomalies (with explanations) in the terminal
uvicorn backend.main:app --reload --port 8000    # Phase 6 API: http://localhost:8000/docs
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
python automation/pipeline.py --ml-only          # re-train and re-score the anomaly model on what is in the DB
python automation/pipeline.py --ml-only --no-retrain   # re-score with the saved ml/model.pkl (no training)
python automation/pipeline.py --skip-ml          # everything except the anomaly model
python automation/pipeline.py --no-workflow-sim  # leave every exception OPEN
```

The ETL step **rebuilds the database**. The reconciliation step **replaces** the `reconciliations` table (it is fully derived). The controls step is **idempotent** (see below). The anomaly step **replaces** `anomaly_scores` and `anomalies` and **appends** a row to `model_runs`.

> **Upgrading from an earlier phase?** The schema changed, so run the *full* `python automation/pipeline.py` once (not `--recon-only` / `--ml-only`). The same seed regenerates identical data.

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

Views: `v_pnl_daily`, `v_pnl_monthly_trend`, `v_pnl_quarterly`, `v_pnl_by_product`, `v_financial_performance`, `v_liquidity_position`, `v_liquidity_summary`, `v_reconciliation_summary`, `v_reconciliation_by_bu`, `v_reconciliation_daily`, `v_exception_summary`, `v_anomaly_summary`, `v_anomaly_detail`.

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

## Phase 5 — Anomaly detection

An **Isolation Forest** (scikit-learn): unsupervised, so it never sees a fraud label. It learns what ordinary transactions look like and scores how *easy a transaction is to isolate* with random splits — few splits needed means unusual. Code: `ml/anomaly_detection.py` (pure ML) and `backend/services/anomaly_service.py` (database + queries).

```
transactions + ledger ──> clean ──> features ──> Isolation Forest ──> score ──> flag ──> risk level ──> explain ──> PostgreSQL
                                                       └──> ml/model.pkl (forest + thresholds + group statistics)
```

### What is scored (and what is not)

149,450 of 152,250 transactions. The control engine already owns the rest, so the model does not re-flag them: **250** with no transaction ID (CTL-001), **2,250** extra duplicate copies (CTL-002), **300** with an invalid amount (CTL-003). Business unit and INR amount that the source system lost are **recovered from the ledger**. The model run records exactly what was left out (`model_runs.excluded`) and the verifier proves `scored + left out = all transactions`.

### Output

| Field | Meaning |
|---|---|
| `anomaly_score` | Isolation Forest score in (0, 1). ~0.5 = nothing stands out; higher = more isolated. |
| flagged (`anomalies` table) | score ≥ the (1 − `ANOMALY_CONTAMINATION`) quantile of the training scores — **the top 1%** by default |
| `risk_level` | tail bands of the training scores: **LOW** = top 1%, **MEDIUM** = top 0.5%, **HIGH** = top 0.1% |
| `reasons` | plain-English explanation, e.g. *"Amount INR 4.82M is 14.2x the typical Equities REVENUE transaction (median INR 0.34M); Booked at 02:14, outside the normal 08:00-17:59 window (only 0.00% of transactions occur at this hour)"* |

Thresholds are learned at training time and **saved with the model**, so scoring new data uses the *same* cut-offs (the anomaly rate can then rise or fall, which is what you want to monitor). `anomaly_scores` keeps the score of every scored transaction (needed for anomaly **rates** by business unit / date, and the score distribution).

### Features (the spec's list)

| Feature | Definition |
|---|---|
| `amount_log` | log(1 + \|amount in INR\|) — INR so currencies are comparable |
| `amount_deviation` | robust z-score of the amount inside its **business-unit × transaction-type** group — "how far from this group's historical average" |
| `hour`, `day_of_month` | from the transaction timestamp |
| `transaction_frequency` | how busy today is for **this business unit** versus its own normal day (robust z-score of the square-root daily count) |
| business unit | enters through the two group-relative features above |
| historical average | the group median behind `amount_deviation`; also used in the explanations |

No scaling is applied: Isolation Forest picks split points uniformly inside each feature's range, so it is invariant to monotonic rescaling.

### Two model-design findings (both caught by measurement, both now regression-tested)

1. **One-hot business-unit dummies make small units look anomalous.** Isolation Forest isolates rare binary features easily, so Treasury and Investment Banking (8% of volume each) were flagged **~3× as often** as large units (per-unit flag rates 0.64%–2.20%, spread 1.56 points). The default has no dummies — business unit enters through group-relative features — and the spread is **0.50 points** (0.79%–1.29%). The literal spec reading is still available as `ANOMALY_FEATURE_SET=onehot`. No labels were needed to find this: it is a *fairness* check.
2. **A raw daily transaction count has the same flaw** (a small unit's every day looks "quiet"), and a small unit's near-Poisson counts are skewed. The frequency feature is therefore relative to each unit's own normal day, on a square-root (variance-stabilising) scale.

### Feature profiles (`ANOMALY_FEATURE_SET` in `.env`)

| Profile | Inputs | ROC-AUC | Recall @ top 1% | Per-unit flag-rate spread |
|---|---|---|---|---|
| `standard` (default) | amount_log, amount_deviation, hour, day_of_month, transaction_frequency | 0.980 | 78% | 0.50 pts |
| `onehot` | standard + business-unit dummies | 0.980 | 76% | 1.56 pts |
| `compact` | amount_log, amount_deviation, hour | 0.986 | 84.5% | 0.42 pts |

**Read the ablation honestly.** In a 4-seed experiment, leaner feature sets scored better on every seed (a 2-feature model reached average precision 0.85 vs 0.66 for the full list) because Isolation Forest chooses split features at random, so features carrying no signal dilute those that do. In *this* synthetic data the planted anomalies are defined by amount and hour only; day-of-month and daily volume are independent noise by construction, so `compact` is partly "teaching to the test". On real data day-of-month or volume may well matter, so the default keeps the spec's features and drops only what is redundant by construction (the group-constant `historical_average_log`, already inside `amount_deviation`). **Choose your profile with analyst feedback on your own data.**

### How well does it work? (measured against the generator's answer key — the model never sees it)

| | |
|---|---|
| ROC-AUC / average precision | **0.980** / 0.72 (base rate 0.50%) |
| Flagged (top 1%) | 1,495 — recall **78.4%** (588 of 750 planted unusual transactions), precision 39.3%, **78× lift** over random |
| **HIGH** tier (150) | **100%** real unusual transactions |
| MEDIUM tier (598) / LOW tier (747) | 56.9% / 13.1% |
| Missed (162) | 104 happened in business hours with only a moderate amount — they sit inside the natural heavy tail of normal transactions |
| Value flagged | ₹894.8 M (₹314.9 M in the HIGH tier) |

Flagging 1% when the true rate is 0.5% guarantees false alarms; that is what the risk tiers are for — **review HIGH first**. The contamination rate is a *review-capacity* decision, not a measurement.

### Explanations you can trust

Every flagged transaction is explained from the same features (amount vs the group's typical amount, rare hour of day, unusual daily volume), strongest signal first. 92% name a concrete signal; the rest say "unusual combination of features". The verifier recomputes **200 random "N× the typical amount" claims independently from the data** (0 wrong) and checks every "Booked at HH:MM" claim against the actual timestamp.

### Using it

```python
from backend.database import engine
from backend.services import anomaly_service as anomalies
from backend.utils.filters import Filters

anomalies.summary(engine, Filters.for_period(quarter="2026-Q1"))   # counts by risk, anomaly rate, value flagged
anomalies.by_business_unit(engine)                                  # rate per unit
anomalies.trend(engine, grain="month")                              # scored, anomalies, rate per month
anomalies.top_anomalies(engine, n=10, order_by="amount")            # highest-value, with explanations
anomalies.score_histogram(engine)                                   # score distribution
```

```bash
python automation/anomaly_report.py                     # summary, by unit, by month, top anomalies with reasons
python automation/anomaly_report.py --risk HIGH --top 15
python automation/anomaly_report.py --quarter 2026-Q1 --bu Equities
```

### Tuning (`.env`)

```
ANOMALY_CONTAMINATION=0.01      # share flagged (top N% by score)
ANOMALY_FEATURE_SET=standard    # standard | onehot | compact
```

### Limits worth knowing

* **Batch scoring.** Group statistics (typical amounts, normal daily volume, hours) are computed over the whole window, and thresholds are fixed at training time. There is no rolling window or drift monitoring yet.
* The explanations describe *what is unusual*, not *why it happened* — they are triage hints, not conclusions.
* `ml/model.pkl` is a joblib/pickle file: **only load model files you created yourself.** It is git-ignored.
* `detected_at` is a simulated detection time (18:00 the day after the transaction), as for control exceptions.

## Phase 7 — Streamlit Foundation

Phase 7 establishes the polished Streamlit application shell without implementing the business dashboards yet. It adds the shared institutional-finance visual system, reusable KPI/chart/table components, a read-only FastAPI client, API health states, and the planned multipage navigation structure.

### Frontend structure

```text
frontend/
├── app.py
├── components/
│   ├── charts.py
│   ├── kpis.py
│   ├── styles.py
│   └── tables.py
├── pages/
│   ├── 01_Overview.py
│   ├── 02_Financial_Performance.py
│   ├── 03_Reconciliation.py
│   ├── 04_Exceptions.py
│   ├── 05_Risk_Anomalies.py
│   ├── 06_Liquidity.py
│   └── 07_Reports.py
└── services/
    └── api_client.py
```

The shell intentionally does not duplicate finance calculations. Phase 8 implements the executive dashboard, followed by reconciliation/exception and remaining dashboard pages.

### Running Phase 7

```bash
uvicorn backend.main:app --reload --port 8000
streamlit run frontend/app.py
```

See `docs/PHASE7_IMPLEMENTATION.md` for the phase boundary and implementation details.

## Phase 6 — FastAPI

Phase 6 exposes the validated analytics through a **read-only REST API**. The API
does not reimplement finance calculations; it translates HTTP parameters into
the existing service-layer functions and returns JSON-safe records.

### API surface

| Route | Purpose |
|---|---|
| `GET /health` | API + PostgreSQL health check |
| `GET /api/v1/options` | Business units, products and currencies for dashboard filters |
| `GET /api/v1/pnl/summary` | P&L totals, margins and budget variance |
| `GET /api/v1/pnl/trend` | Day/month/quarter trends |
| `GET /api/v1/pnl/breakdown` | Business-unit or product breakdown |
| `GET /api/v1/pnl/budget-variance` | Business-unit/month budget variance |
| `GET /api/v1/liquidity/summary` | Cash, liquidity gap, funding and coverage |
| `GET /api/v1/liquidity/daily` | Daily liquidity position |
| `GET /api/v1/liquidity/monthly` | Monthly liquidity roll-up |
| `GET /api/v1/liquidity/business-units` | Liquidity by business unit |
| `GET /api/v1/reconciliation/summary` | Match/mismatch/missing/duplicate/invalid metrics |
| `GET /api/v1/reconciliation/by-business-unit` | Reconciliation performance by business unit |
| `GET /api/v1/reconciliation/daily` | Daily reconciliation metrics |
| `GET /api/v1/exceptions/summary` | Exception counts/exposure by severity |
| `GET /api/v1/exceptions/by-category` | Exception counts/exposure by category |
| `GET /api/v1/exceptions/recent` | Filterable exception queue |
| `GET /api/v1/anomalies/summary` | Anomaly/risk counts and exposure |
| `GET /api/v1/anomalies/by-business-unit` | Anomaly rates by business unit |
| `GET /api/v1/anomalies/trend` | Daily/monthly/quarterly anomaly trend |
| `GET /api/v1/anomalies/top` | Highest-value/score anomalies with explanations |
| `GET /api/v1/anomalies/score-histogram` | Score distribution |
| `GET /api/v1/anomalies/latest-run` | Latest model-run metadata and thresholds |

P&L/anomaly endpoints support period, business-unit and product filters where
applicable. Liquidity deliberately rejects product/currency filters because
those dimensions do not apply. Invalid filters and conflicting period
parameters return HTTP 400.

### Running Phase 6

```bash
uvicorn backend.main:app --reload --port 8000
```

Open `http://localhost:8000/docs` for the interactive OpenAPI UI.

Phase 6 is intentionally read-only. Pipeline execution, model retraining and
database mutation remain explicit CLI workflows.

### Validation

`tests/test_api.py` checks route registration, filter translation, validation,
JSON serialization and the OpenAPI surface without requiring PostgreSQL.

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
| `No module named 'ml'` | Run the scripts from the project folder (`FinSight`), and make sure `ml/__init__.py` exists. |
| `KeyError: 'liquidity_targets'` or `Missing .../liquidity_targets.csv` | You have CSVs from an earlier phase. Run `python automation/pipeline.py --generate`. |
| Exceptions table is empty | Run `python automation/pipeline.py` (or `--controls-only`). |
| `pip install` fails on `psycopg2-binary` | Use Python 3.11 or 3.12 (not 3.14). |

## Roadmap

~~Phase 2 ETL + controls~~ ✅ → ~~3 Reconciliation~~ ✅ → ~~4 Financial analytics~~ ✅ → ~~5 ML anomaly detection~~ ✅ → ~~6 FastAPI~~ ✅ → 7–10 Streamlit → 11 Power BI → 12 AI commentary → 13 Automation → 14–17 Testing, polish, docs.
