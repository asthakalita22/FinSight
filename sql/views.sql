-- =====================================================================
-- FinSight - reporting views (safe to re-run)
-- v_financial_performance and v_liquidity_summary work right after Phase 1.
-- The reconciliation views and v_exception_summary are empty until the
-- reconciliation and control engines have run (python automation/pipeline.py).
-- =====================================================================

CREATE OR REPLACE VIEW v_financial_performance AS
SELECT
    date_trunc('month', p.date)::date                              AS month,
    bu.name                                                        AS business_unit,
    SUM(p.revenue)                                                 AS revenue,
    SUM(p.cost + p.expense)                                        AS expense,
    SUM(p.actual_pnl)                                              AS actual_pnl,
    SUM(p.budget_pnl)                                              AS budget_pnl,
    SUM(p.actual_pnl) - SUM(p.budget_pnl)                          AS variance,
    ROUND(100.0 * (SUM(p.actual_pnl) - SUM(p.budget_pnl))
          / NULLIF(ABS(SUM(p.budget_pnl)), 0), 2)                  AS variance_pct
FROM pnl p
JOIN business_units bu ON bu.id = p.business_unit_id
GROUP BY 1, 2;

-- ---------------------------------------------------------------------
-- P&L reporting views (Power BI + cross-check for the Python analytics)
--   total_expenses = cost + expense        net_pnl = revenue - cost - expense
--   variance = net_pnl - budget_pnl        variance_pct = variance / |budget_pnl| x 100
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_pnl_daily AS
SELECT
    p.date, bu.name AS business_unit, p.product, p.currency,
    p.revenue, p.cost, p.expense,
    p.cost + p.expense                 AS total_expenses,
    p.revenue - p.cost                 AS gross_pnl,
    p.actual_pnl                       AS net_pnl,
    p.budget_pnl,
    p.actual_pnl - p.budget_pnl        AS variance
FROM pnl p
JOIN business_units bu ON bu.id = p.business_unit_id;

CREATE OR REPLACE VIEW v_pnl_monthly_trend AS
WITH m AS (
    SELECT date_trunc('month', date)::date AS month,
           SUM(revenue)          AS revenue,
           SUM(cost + expense)   AS total_expenses,
           SUM(actual_pnl)       AS net_pnl,
           SUM(budget_pnl)       AS budget_pnl,
           COUNT(DISTINCT date)  AS business_days
    FROM pnl
    GROUP BY 1
)
SELECT
    month, revenue, total_expenses, net_pnl, budget_pnl,
    net_pnl - budget_pnl                                                          AS variance,
    ROUND(100.0 * (net_pnl - budget_pnl) / NULLIF(ABS(budget_pnl), 0), 2)         AS variance_pct,
    ROUND(100.0 * (revenue - LAG(revenue) OVER w)
          / NULLIF(ABS(LAG(revenue) OVER w), 0), 2)                               AS revenue_mom_pct,
    ROUND(100.0 * (total_expenses - LAG(total_expenses) OVER w)
          / NULLIF(ABS(LAG(total_expenses) OVER w), 0), 2)                        AS expenses_mom_pct,
    ROUND(100.0 * (net_pnl - LAG(net_pnl) OVER w)
          / NULLIF(ABS(LAG(net_pnl) OVER w), 0), 2)                               AS net_pnl_mom_pct,
    business_days
FROM m
WINDOW w AS (ORDER BY month);

CREATE OR REPLACE VIEW v_pnl_quarterly AS
SELECT
    to_char(p.date, 'YYYY-"Q"Q')                                   AS quarter,
    bu.name                                                        AS business_unit,
    SUM(p.revenue)                                                 AS revenue,
    SUM(p.cost + p.expense)                                        AS total_expenses,
    SUM(p.actual_pnl)                                              AS net_pnl,
    SUM(p.budget_pnl)                                              AS budget_pnl,
    SUM(p.actual_pnl) - SUM(p.budget_pnl)                          AS variance,
    ROUND(100.0 * (SUM(p.actual_pnl) - SUM(p.budget_pnl))
          / NULLIF(ABS(SUM(p.budget_pnl)), 0), 2)                  AS variance_pct,
    COUNT(DISTINCT date_trunc('month', p.date))                    AS months_covered
FROM pnl p
JOIN business_units bu ON bu.id = p.business_unit_id
GROUP BY 1, 2;

CREATE OR REPLACE VIEW v_pnl_by_product AS
SELECT
    date_trunc('month', p.date)::date   AS month,
    p.product,
    SUM(p.revenue)                      AS revenue,
    SUM(p.cost + p.expense)             AS total_expenses,
    SUM(p.actual_pnl)                   AS net_pnl
FROM pnl p
GROUP BY 1, 2;

CREATE OR REPLACE VIEW v_reconciliation_summary AS
SELECT
    COUNT(*)                                                                          AS total_records,
    COUNT(*) FILTER (WHERE status = 'MATCHED')                                        AS matched,
    COUNT(*) FILTER (WHERE status IN ('AMOUNT_MISMATCH','CURRENCY_MISMATCH',
                                      'BUSINESS_UNIT_MISMATCH','DATE_MISMATCH'))      AS mismatched,
    COUNT(*) FILTER (WHERE status IN ('MISSING_IN_LEDGER','MISSING_IN_SOURCE'))       AS missing,
    COUNT(*) FILTER (WHERE status = 'DUPLICATE')                                      AS duplicates,
    COUNT(*) FILTER (WHERE status = 'INVALID')                                        AS invalid,
    ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'MATCHED')
          / NULLIF(COUNT(*), 0), 2)                                                   AS match_rate,
    COALESCE(SUM(exposure_inr) FILTER (WHERE status <> 'MATCHED'), 0)                 AS unreconciled_exposure_inr
FROM reconciliations;

CREATE OR REPLACE VIEW v_reconciliation_by_bu AS
SELECT
    COALESCE(bu.name, '(unassigned)')                                    AS business_unit,
    COUNT(*)                                                             AS total_records,
    COUNT(*) FILTER (WHERE r.status = 'MATCHED')                         AS matched,
    COUNT(*) FILTER (WHERE r.status <> 'MATCHED')                        AS breaks,
    ROUND(100.0 * COUNT(*) FILTER (WHERE r.status = 'MATCHED')
          / NULLIF(COUNT(*), 0), 2)                                      AS match_rate,
    COALESCE(SUM(r.exposure_inr) FILTER (WHERE r.status <> 'MATCHED'), 0) AS exposure_inr
FROM reconciliations r
LEFT JOIN business_units bu ON bu.id = r.business_unit_id
GROUP BY 1;

CREATE OR REPLACE VIEW v_reconciliation_daily AS
SELECT
    record_date                                                          AS date,
    COUNT(*)                                                             AS total_records,
    COUNT(*) FILTER (WHERE status = 'MATCHED')                           AS matched,
    COUNT(*) FILTER (WHERE status <> 'MATCHED')                          AS breaks,
    ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'MATCHED')
          / NULLIF(COUNT(*), 0), 2)                                      AS match_rate
FROM reconciliations
GROUP BY record_date;

CREATE OR REPLACE VIEW v_exception_summary AS
SELECT
    created_at::date                AS date,
    severity,
    category,
    COUNT(*)                        AS count,
    COALESCE(SUM(ABS(amount)), 0)   AS total_exposure
FROM exceptions
GROUP BY 1, 2, 3;

-- ---------------------------------------------------------------------
-- Liquidity views
--   liquidity_gap       = min_liquidity - closing_cash   (positive = shortfall)
--   funding_requirement = SUM over business units of MAX(0, gap)   (NOT netted across units)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_liquidity_position AS
SELECT
    c.date, bu.name AS business_unit,
    c.opening_cash, c.cash_inflow AS inflow, c.cash_outflow AS outflow,
    c.cash_inflow - c.cash_outflow                 AS net_cash_flow,
    c.closing_cash,
    t.min_liquidity,
    t.min_liquidity - c.closing_cash               AS liquidity_gap,
    GREATEST(t.min_liquidity - c.closing_cash, 0)  AS funding_requirement
FROM cash_flows c
JOIN business_units    bu ON bu.id = c.business_unit_id
JOIN liquidity_targets t  ON t.business_unit_id = c.business_unit_id;

CREATE OR REPLACE VIEW v_liquidity_summary AS
SELECT
    date,
    SUM(opening_cash)          AS opening_cash,
    SUM(inflow)                AS inflow,
    SUM(outflow)               AS outflow,
    SUM(net_cash_flow)         AS net_cash_flow,
    SUM(closing_cash)          AS closing_cash,
    SUM(min_liquidity)         AS min_liquidity,
    SUM(liquidity_gap)         AS liquidity_gap,
    SUM(funding_requirement)   AS funding_requirement,
    COUNT(*) FILTER (WHERE funding_requirement > 0) AS units_in_shortfall
FROM v_liquidity_position
GROUP BY date;
