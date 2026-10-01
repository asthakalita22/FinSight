-- =====================================================================
-- FinSight - PostgreSQL schema (Phase 1)
-- Re-runnable: drops and recreates everything.
--
-- DESIGN NOTE
-- transactions / ledger_entries are the *raw* landing tables. They are
-- deliberately permissive (nullable transaction_id, currency,
-- business_unit_id, no UNIQUE on transaction_id) so that bad data can be
-- loaded and then *detected* by the control + reconciliation engines
-- in later phases, instead of being rejected by the database.
-- =====================================================================

DROP VIEW  IF EXISTS v_financial_performance CASCADE;
DROP VIEW  IF EXISTS v_pnl_daily CASCADE;
DROP VIEW  IF EXISTS v_pnl_monthly_trend CASCADE;
DROP VIEW  IF EXISTS v_pnl_quarterly CASCADE;
DROP VIEW  IF EXISTS v_pnl_by_product CASCADE;
DROP VIEW  IF EXISTS v_liquidity_position CASCADE;
DROP VIEW  IF EXISTS v_reconciliation_by_bu CASCADE;
DROP VIEW  IF EXISTS v_reconciliation_daily CASCADE;
DROP VIEW  IF EXISTS v_reconciliation_summary CASCADE;
DROP VIEW  IF EXISTS v_exception_summary CASCADE;
DROP VIEW  IF EXISTS v_liquidity_summary CASCADE;

DROP TABLE IF EXISTS anomalies        CASCADE;
DROP TABLE IF EXISTS exceptions       CASCADE;
DROP TABLE IF EXISTS reconciliations  CASCADE;
DROP TABLE IF EXISTS cash_flows       CASCADE;
DROP TABLE IF EXISTS pnl              CASCADE;
DROP TABLE IF EXISTS budgets          CASCADE;
DROP TABLE IF EXISTS ledger_entries   CASCADE;
DROP TABLE IF EXISTS transactions     CASCADE;
DROP TABLE IF EXISTS business_units   CASCADE;
DROP TABLE IF EXISTS fx_rates         CASCADE;
DROP TABLE IF EXISTS liquidity_targets CASCADE;

-- ---------------------------------------------------------------------
-- Dimension: business units
-- ---------------------------------------------------------------------
CREATE TABLE business_units (
    id        INTEGER PRIMARY KEY,
    name      VARCHAR(100) NOT NULL UNIQUE,
    division  VARCHAR(100) NOT NULL,
    region    VARCHAR(100) NOT NULL
);

-- ---------------------------------------------------------------------
-- Dimension: currencies + fixed FX rates to the reporting currency (INR)
-- (also serves as DimCurrency in Power BI)
-- ---------------------------------------------------------------------
CREATE TABLE fx_rates (
    currency     CHAR(3) PRIMARY KEY,
    name         VARCHAR(50)   NOT NULL,
    rate_to_inr  NUMERIC(12,4) NOT NULL CHECK (rate_to_inr > 0)
);

-- ---------------------------------------------------------------------
-- Minimum liquidity buffer per business unit (INR).
--   liquidity gap        = min_liquidity - closing_cash   (positive = shortfall, negative = surplus)
--   funding requirement  = MAX(0, liquidity gap)          (this is what cash_flows.funding stores)
-- ---------------------------------------------------------------------
CREATE TABLE liquidity_targets (
    business_unit_id  INTEGER PRIMARY KEY REFERENCES business_units(id),
    min_liquidity     NUMERIC(18,2) NOT NULL CHECK (min_liquidity >= 0)
);

-- ---------------------------------------------------------------------
-- Source system transactions (e.g. trading system)  -- RAW / permissive
-- ---------------------------------------------------------------------
CREATE TABLE transactions (
    id                BIGSERIAL PRIMARY KEY,
    transaction_id    VARCHAR(30),                       -- nullable on purpose (control Rule 1)
    transaction_date  TIMESTAMP   NOT NULL,
    business_unit_id  INTEGER REFERENCES business_units(id),  -- nullable on purpose (Rule 5)
    product           VARCHAR(30) NOT NULL,
    currency          CHAR(3),                           -- nullable on purpose (Rule 4)
    amount            NUMERIC(18,2) NOT NULL,
    amount_inr        NUMERIC(18,2),                     -- derived by ETL (NULL if currency missing/unknown)
    transaction_type  VARCHAR(20) NOT NULL
        CHECK (transaction_type IN ('REVENUE','COST','EXPENSE','ADJUSTMENT')),
    source_system     VARCHAR(30) NOT NULL,
    created_at        TIMESTAMP   NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Finance ledger entries (the "other side" for reconciliation)
-- ---------------------------------------------------------------------
CREATE TABLE ledger_entries (
    id                BIGSERIAL PRIMARY KEY,
    transaction_id    VARCHAR(30) NOT NULL,
    ledger_date       DATE        NOT NULL,
    amount            NUMERIC(18,2) NOT NULL,
    amount_inr        NUMERIC(18,2),                     -- derived by ETL
    currency          CHAR(3)     NOT NULL,
    business_unit_id  INTEGER     NOT NULL REFERENCES business_units(id)
);

-- ---------------------------------------------------------------------
-- Monthly budgets per business unit
-- ---------------------------------------------------------------------
CREATE TABLE budgets (
    id                BIGSERIAL PRIMARY KEY,
    month             DATE        NOT NULL,              -- first day of month
    business_unit_id  INTEGER     NOT NULL REFERENCES business_units(id),
    budget_revenue    NUMERIC(18,2) NOT NULL,
    budget_expense    NUMERIC(18,2) NOT NULL,            -- cost + expense
    budget_pnl        NUMERIC(18,2) NOT NULL,
    UNIQUE (month, business_unit_id)
);

-- ---------------------------------------------------------------------
-- Daily P&L per business unit and product (reporting currency = INR)
-- Section 15 of the spec also requires product + currency, so they are here.
-- ---------------------------------------------------------------------
CREATE TABLE pnl (
    id                BIGSERIAL PRIMARY KEY,
    date              DATE        NOT NULL,
    business_unit_id  INTEGER     NOT NULL REFERENCES business_units(id),
    product           VARCHAR(30) NOT NULL,
    currency          CHAR(3)     NOT NULL DEFAULT 'INR',
    revenue           NUMERIC(18,2) NOT NULL,
    cost              NUMERIC(18,2) NOT NULL,
    expense           NUMERIC(18,2) NOT NULL,
    actual_pnl        NUMERIC(18,2) NOT NULL,            -- revenue - cost - expense
    budget_pnl        NUMERIC(18,2) NOT NULL,
    UNIQUE (date, business_unit_id, product)
);

-- ---------------------------------------------------------------------
-- Daily cash position per business unit (reporting currency = INR)
-- ---------------------------------------------------------------------
CREATE TABLE cash_flows (
    id                BIGSERIAL PRIMARY KEY,
    date              DATE        NOT NULL,
    business_unit_id  INTEGER     NOT NULL REFERENCES business_units(id),
    opening_cash      NUMERIC(18,2) NOT NULL,
    cash_inflow       NUMERIC(18,2) NOT NULL,
    cash_outflow      NUMERIC(18,2) NOT NULL,
    closing_cash      NUMERIC(18,2) NOT NULL,            -- opening + inflow - outflow
    funding           NUMERIC(18,2) NOT NULL DEFAULT 0,  -- funding needed to restore the liquidity buffer
    UNIQUE (date, business_unit_id)
);

-- ---------------------------------------------------------------------
-- Output tables (filled by the reconciliation engine, control engine and ML model)
-- ---------------------------------------------------------------------
CREATE TABLE reconciliations (
    id                BIGSERIAL PRIMARY KEY,
    transaction_id    VARCHAR(30),
    source_amount     NUMERIC(18,2),
    ledger_amount     NUMERIC(18,2),
    difference        NUMERIC(18,2),                    -- ledger - source, in transaction currency (only when both sides share the currency)
    status            VARCHAR(30) NOT NULL
        CHECK (status IN ('MATCHED','AMOUNT_MISMATCH','CURRENCY_MISMATCH','BUSINESS_UNIT_MISMATCH',
                          'DATE_MISMATCH','MISSING_IN_LEDGER','MISSING_IN_SOURCE','DUPLICATE','INVALID')),
    reconciled_at     TIMESTAMP NOT NULL DEFAULT now(),
    -- extras (beyond the spec) that make the results drillable and filterable
    source_row_id     BIGINT  UNIQUE REFERENCES transactions(id),    -- each source row is reconciled exactly once
    ledger_row_id     BIGINT  UNIQUE REFERENCES ledger_entries(id),  -- each ledger row is reconciled exactly once
    business_unit_id  INTEGER REFERENCES business_units(id),
    currency          CHAR(3),
    record_date       DATE    NOT NULL,                              -- transaction date (ledger date for ledger-only records)
    exposure_inr      NUMERIC(18,2) NOT NULL DEFAULT 0,              -- value at stake, INR (0 when MATCHED)
    reason            TEXT
);

-- ---------------------------------------------------------------------
-- Output tables filled by later phases
-- ---------------------------------------------------------------------
CREATE TABLE exceptions (
    id                BIGSERIAL PRIMARY KEY,
    exception_code    VARCHAR(30) NOT NULL,
    source_ref        VARCHAR(60) NOT NULL,              -- identifies the offending record (makes control runs idempotent)
    transaction_id    VARCHAR(30),
    category          VARCHAR(50) NOT NULL,
    severity          VARCHAR(10) NOT NULL
        CHECK (severity IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    amount            NUMERIC(18,2),                                   -- exposure in INR
    business_unit_id  INTEGER REFERENCES business_units(id),   -- extra: spec section 14
    description       TEXT,
    status            VARCHAR(15) NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN','IN_REVIEW','RESOLVED','IGNORED')),
    owner             VARCHAR(100),                              -- extra: spec section 14
    created_at        TIMESTAMP NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMP,
    UNIQUE (exception_code, source_ref)
);

CREATE TABLE anomalies (
    id              BIGSERIAL PRIMARY KEY,
    transaction_id  VARCHAR(30) NOT NULL,
    anomaly_score   NUMERIC(8,4) NOT NULL,
    risk_level      VARCHAR(10) NOT NULL
        CHECK (risk_level IN ('LOW','MEDIUM','HIGH')),
    model_version   VARCHAR(30) NOT NULL,
    detected_at     TIMESTAMP NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------
CREATE INDEX idx_txn_transaction_id ON transactions (transaction_id);
CREATE INDEX idx_txn_date           ON transactions (transaction_date);
CREATE INDEX idx_txn_bu_date        ON transactions (business_unit_id, transaction_date);
CREATE INDEX idx_txn_product        ON transactions (product);
CREATE INDEX idx_txn_currency       ON transactions (currency);

CREATE INDEX idx_ledger_transaction_id ON ledger_entries (transaction_id);
CREATE INDEX idx_ledger_date           ON ledger_entries (ledger_date);
CREATE INDEX idx_ledger_bu             ON ledger_entries (business_unit_id);

CREATE INDEX idx_pnl_date        ON pnl (date);
CREATE INDEX idx_pnl_bu_date     ON pnl (business_unit_id, date);
CREATE INDEX idx_pnl_product     ON pnl (product);

CREATE INDEX idx_cash_date       ON cash_flows (date);
CREATE INDEX idx_cash_bu_date    ON cash_flows (business_unit_id, date);

CREATE INDEX idx_recon_txn       ON reconciliations (transaction_id);
CREATE INDEX idx_recon_status    ON reconciliations (status);
CREATE INDEX idx_recon_bu        ON reconciliations (business_unit_id);
CREATE INDEX idx_recon_date      ON reconciliations (record_date);

CREATE INDEX idx_exc_txn         ON exceptions (transaction_id);
CREATE INDEX idx_exc_severity    ON exceptions (severity);
CREATE INDEX idx_exc_status      ON exceptions (status);
CREATE INDEX idx_exc_category    ON exceptions (category);
CREATE INDEX idx_exc_created     ON exceptions (created_at);
CREATE INDEX idx_exc_bu          ON exceptions (business_unit_id);

CREATE INDEX idx_anom_txn        ON anomalies (transaction_id);
CREATE INDEX idx_anom_risk       ON anomalies (risk_level);
