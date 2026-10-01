-- =====================================================================
-- FinSight - starter analytics queries (try these in DBeaver / psql)
-- =====================================================================

-- 1. Company P&L by month vs budget
SELECT month,
       ROUND(SUM(revenue) / 1e6, 1)    AS revenue_m,
       ROUND(SUM(actual_pnl) / 1e6, 1) AS net_pnl_m,
       ROUND(SUM(budget_pnl) / 1e6, 1) AS budget_pnl_m,
       ROUND(100.0 * (SUM(actual_pnl) - SUM(budget_pnl)) / SUM(budget_pnl), 1) AS variance_pct
FROM v_financial_performance
GROUP BY month
ORDER BY month;

-- 2. Business units ranked by variance (worst first)
SELECT business_unit,
       ROUND(SUM(actual_pnl) / 1e6, 1) AS actual_m,
       ROUND(SUM(budget_pnl) / 1e6, 1) AS budget_m,
       ROUND(100.0 * (SUM(actual_pnl) - SUM(budget_pnl)) / SUM(budget_pnl), 1) AS variance_pct
FROM v_financial_performance
GROUP BY business_unit
ORDER BY variance_pct;

-- 3. BU-months with excessive variance (> 15%)  -> becomes control Rule 7
SELECT month, business_unit, variance_pct
FROM v_financial_performance
WHERE ABS(variance_pct) > 15
ORDER BY ABS(variance_pct) DESC;

-- 4. Duplicate transaction IDs in the source system  -> control Rule 2
SELECT transaction_id, COUNT(*) AS copies
FROM transactions
WHERE transaction_id IS NOT NULL
GROUP BY transaction_id
HAVING COUNT(*) > 1
ORDER BY copies DESC
LIMIT 20;

-- 5. Source transactions with NO ledger entry  -> reconciliation: MISSING_IN_LEDGER
SELECT t.transaction_id, t.transaction_date, t.amount, t.currency
FROM transactions t
LEFT JOIN ledger_entries l ON l.transaction_id = t.transaction_id
WHERE t.transaction_id IS NOT NULL AND l.transaction_id IS NULL
LIMIT 20;

-- 6. Source amount <> ledger amount  -> reconciliation: AMOUNT_MISMATCH
SELECT t.transaction_id, t.amount AS source_amount, l.amount AS ledger_amount,
       l.amount - t.amount AS difference
FROM transactions t
JOIN ledger_entries l ON l.transaction_id = t.transaction_id
WHERE t.amount <> l.amount
ORDER BY ABS(l.amount - t.amount) DESC
LIMIT 20;

-- 7. Largest transactions by raw amount (mixed currencies) - candidates for anomaly detection
SELECT transaction_id, transaction_date, transaction_type, currency, amount,
       EXTRACT(HOUR FROM transaction_date) AS hour
FROM transactions
ORDER BY amount DESC
LIMIT 20;

-- 8. Days where a business unit needed funding (liquidity stress)
SELECT c.date, bu.name AS business_unit, ROUND(c.closing_cash / 1e6, 1) AS closing_cash_m,
       ROUND(c.funding / 1e6, 1) AS funding_needed_m
FROM cash_flows c
JOIN business_units bu ON bu.id = c.business_unit_id
WHERE c.funding > 0
ORDER BY c.date;

-- =====================================================================
-- Phase 2: exploring the exceptions raised by the control engine
-- =====================================================================

-- 9. Exceptions by category and severity
SELECT category,
       COUNT(*)                                   AS total,
       COUNT(*) FILTER (WHERE severity = 'CRITICAL') AS critical,
       COUNT(*) FILTER (WHERE severity = 'HIGH')     AS high,
       COUNT(*) FILTER (WHERE severity = 'MEDIUM')   AS medium,
       COUNT(*) FILTER (WHERE severity = 'LOW')      AS low,
       ROUND(SUM(amount) / 1e6, 2)                AS exposure_inr_m
FROM exceptions
GROUP BY category
ORDER BY total DESC;

-- 10. Unresolved CRITICAL / HIGH exceptions, oldest first (age measured from the latest detection date)
SELECT e.id, e.exception_code, e.category, e.severity, e.status, e.owner,
       ROUND(e.amount, 0) AS exposure_inr, bu.name AS business_unit,
       (SELECT MAX(created_at)::date FROM exceptions) - e.created_at::date AS age_days,
       LEFT(e.description, 90) AS description
FROM exceptions e
LEFT JOIN business_units bu ON bu.id = e.business_unit_id
WHERE e.status IN ('OPEN', 'IN_REVIEW') AND e.severity IN ('CRITICAL', 'HIGH')
ORDER BY (e.severity = 'CRITICAL') DESC, e.created_at
LIMIT 25;

-- 11. Which business units carry the most exception exposure? (NULL = unassigned)
SELECT COALESCE(bu.name, '(unassigned)') AS business_unit,
       COUNT(*) AS exceptions,
       ROUND(SUM(e.amount) / 1e6, 2) AS exposure_inr_m
FROM exceptions e
LEFT JOIN business_units bu ON bu.id = e.business_unit_id
GROUP BY 1
ORDER BY exposure_inr_m DESC;

-- 12. Exceptions raised per month, and how many of them are still open
SELECT date_trunc('month', created_at)::date AS month,
       COUNT(*) AS raised,
       COUNT(*) FILTER (WHERE status IN ('OPEN', 'IN_REVIEW')) AS still_open
FROM exceptions
GROUP BY 1
ORDER BY 1;

-- 13. The 4 budget-variance exceptions, in full
SELECT created_at::date AS raised, severity, description FROM exceptions
WHERE exception_code = 'CTL-007' ORDER BY created_at;

-- =====================================================================
-- Phase 3: exploring the reconciliation results
-- =====================================================================

-- 14. The headline metrics (Total, Matched, Mismatched, Missing, Duplicates, Match Rate)
SELECT * FROM v_reconciliation_summary;

-- 15. Status breakdown with share and exposure
SELECT status,
       COUNT(*)                                          AS records,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_total,
       ROUND(SUM(exposure_inr) / 1e6, 2)                 AS exposure_inr_m
FROM reconciliations
GROUP BY status
ORDER BY records DESC;

-- 16. Match rate by business unit (worst first)
SELECT * FROM v_reconciliation_by_bu ORDER BY match_rate;

-- 17. The 15 largest unreconciled items, with the reason the engine gives
SELECT id, transaction_id, status, ROUND(exposure_inr, 0) AS exposure_inr, LEFT(reason, 80) AS reason
FROM reconciliations
WHERE status <> 'MATCHED'
ORDER BY exposure_inr DESC
LIMIT 15;

-- 18. Monthly match-rate trend
SELECT date_trunc('month', date)::date AS month,
       SUM(total_records) AS records,
       SUM(breaks)        AS breaks,
       ROUND(100.0 * SUM(matched) / SUM(total_records), 2) AS match_rate
FROM v_reconciliation_daily
GROUP BY 1
ORDER BY 1;

-- 19. Drill-down: source row and ledger row side by side for amount mismatches
SELECT r.transaction_id,
       t.amount AS source_amount, l.amount AS ledger_amount, r.difference, t.currency,
       t.transaction_date::date AS txn_date, l.ledger_date
FROM reconciliations r
JOIN transactions   t ON t.id = r.source_row_id
JOIN ledger_entries l ON l.id = r.ledger_row_id
WHERE r.status = 'AMOUNT_MISMATCH'
ORDER BY ABS(r.difference) DESC
LIMIT 15;

-- 20. Reconciliation breaks that already have a control exception (same transaction), by severity
SELECT e.severity, r.status, COUNT(*) AS records
FROM reconciliations r
JOIN exceptions e ON e.transaction_id = r.transaction_id
WHERE r.status <> 'MATCHED' AND e.exception_code LIKE 'CTL-006%'
GROUP BY 1, 2
ORDER BY 1, 2;

-- =====================================================================
-- Phase 4: financial analytics (P&L, variance, trends, liquidity)
-- =====================================================================

-- 21. Monthly P&L with month-over-month growth (company level)
SELECT to_char(month, 'YYYY-MM')          AS month,
       ROUND(revenue / 1e6, 1)            AS revenue_m,
       ROUND(total_expenses / 1e6, 1)     AS expenses_m,
       ROUND(net_pnl / 1e6, 1)            AS net_pnl_m,
       variance_pct, revenue_mom_pct, expenses_mom_pct, net_pnl_mom_pct, business_days
FROM v_pnl_monthly_trend
ORDER BY month;

-- 22. Quarterly P&L by business unit (months_covered < 3 = partial quarter at the edge of the data)
SELECT quarter, business_unit, months_covered,
       ROUND(net_pnl / 1e6, 1) AS net_pnl_m, ROUND(budget_pnl / 1e6, 1) AS budget_m, variance_pct
FROM v_pnl_quarterly
ORDER BY quarter, net_pnl DESC;

-- 23. Product performance over the whole period
SELECT product,
       ROUND(SUM(revenue) / 1e6, 1)  AS revenue_m,
       ROUND(SUM(net_pnl) / 1e6, 1)  AS net_pnl_m,
       ROUND(100.0 * SUM(net_pnl) / SUM(revenue), 1) AS net_margin_pct
FROM v_pnl_by_product
GROUP BY product
ORDER BY net_pnl_m DESC;

-- 24. Revenue, expense and P&L versus budget for one month (Actual - Budget)
SELECT bu.name AS business_unit,
       ROUND(SUM(p.revenue) / 1e6, 1)                      AS revenue_m,
       ROUND(b.budget_revenue / 1e6, 1)                    AS budget_revenue_m,
       ROUND((SUM(p.revenue) - b.budget_revenue) / 1e6, 1) AS revenue_var_m,
       ROUND(SUM(p.cost + p.expense) / 1e6, 1)             AS expenses_m,
       ROUND(b.budget_expense / 1e6, 1)                    AS budget_expense_m,
       ROUND(SUM(p.actual_pnl) / 1e6, 1)                   AS net_pnl_m,
       ROUND(b.budget_pnl / 1e6, 1)                        AS budget_pnl_m
FROM pnl p
JOIN business_units bu ON bu.id = p.business_unit_id
JOIN budgets b ON b.business_unit_id = p.business_unit_id AND b.month = date_trunc('month', p.date)::date
WHERE p.date >= '2026-03-01' AND p.date < '2026-04-01'
GROUP BY bu.name, b.budget_revenue, b.budget_expense, b.budget_pnl
ORDER BY net_pnl_m DESC;

-- 25. Cash position at each month end (company level)
SELECT DISTINCT ON (date_trunc('month', date))
       date AS month_end,
       ROUND(closing_cash / 1e6, 1)        AS cash_position_m,
       ROUND(min_liquidity / 1e6, 1)       AS buffer_m,
       ROUND(liquidity_gap / 1e6, 1)       AS liquidity_gap_m,
       ROUND(funding_requirement / 1e6, 2) AS funding_requirement_m
FROM v_liquidity_summary
ORDER BY date_trunc('month', date), date DESC;

-- 26. Days when a business unit needed funding even though the COMPANY was above its buffer
--     (shortfalls are not netted against surpluses elsewhere)
SELECT date,
       ROUND(liquidity_gap / 1e6, 1)       AS company_gap_m,       -- negative = company surplus
       ROUND(funding_requirement / 1e6, 2) AS funding_requirement_m,
       units_in_shortfall
FROM v_liquidity_summary
WHERE funding_requirement > 0 AND liquidity_gap < 0
ORDER BY funding_requirement DESC
LIMIT 10;

-- 27. Which business units spend the most time below their liquidity buffer?
SELECT business_unit,
       COUNT(*) FILTER (WHERE funding_requirement > 0)        AS shortfall_days,
       ROUND(MAX(funding_requirement) / 1e6, 2)               AS peak_funding_m
FROM v_liquidity_position
GROUP BY business_unit
ORDER BY shortfall_days DESC, peak_funding_m DESC;
