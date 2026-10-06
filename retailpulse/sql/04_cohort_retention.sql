-- @title: Cohort retention
-- @question: Of the customers acquired in a given month, what share bought again 1 to 11 months later?
-- @technique: Recursive CTE (offsets 0-11) cross-joined with cohorts for a full observable grid, LEFT JOIN to activity, right-censoring at the latest order month
-- @chart: heatmap row=cohort_month col=month_offset value=retention_pct size=cohort_size
-- @limit: 24
-- @portability: WITH RECURSIVE offsets(n) AS (SELECT 0 UNION ALL SELECT n + 1 FROM offsets WHERE n < 11) → generate_series(0, 11) AS n (Postgres) / UNNEST(GENERATE_ARRAY(0, 11)) AS n (BigQuery); both warehouses also accept WITH RECURSIVE
-- @portability: substr(order_date,1,7) month bucket → to_char(order_date,'YYYY-MM') (Postgres) / FORMAT_DATE('%Y-%m', order_date) (BigQuery)
-- @portability: year*12+month index from CAST(substr(ym,1,4) AS INTEGER) and CAST(substr(ym,6,2) AS INTEGER) → same expressions work on both, or EXTRACT(YEAR FROM d) * 12 + EXTRACT(MONTH FROM d) (Postgres) / DATE_DIFF(d, cohort_d, MONTH) (BigQuery)
-- @portability: ROUND(x, 1) on a double → ROUND(x::numeric, 1) (Postgres)
--
-- Monthly acquisition cohorts and the % of each cohort still purchasing N months
-- after their first order.
--
-- Grid semantics.  The result is a *full observable grid*: every
-- (cohort_month, month_offset 0..11) cell whose calendar month is on or before
-- the latest order month in the data gets a row, with active_customers = 0 and
-- retention_pct = 0.0 when nobody from the cohort came back that month.  Cells
-- beyond the latest order month cannot be observed yet (right-censored) and are
-- deliberately absent, so a renderer can tell "0 % retained" from "not yet
-- observable".  Month 0 is the acquisition month itself and is always 100.0.
WITH RECURSIVE offsets(n) AS (
    SELECT 0
    UNION ALL
    SELECT n + 1 FROM offsets WHERE n < 11
),
first_order AS (
    SELECT customer_id, substr(MIN(order_date), 1, 7) AS cohort_month
    FROM orders
    GROUP BY customer_id
),
cohorts AS (
    SELECT cohort_month,
           COUNT(*) AS cohort_size,
           CAST(substr(cohort_month, 1, 4) AS INTEGER) * 12 + CAST(substr(cohort_month, 6, 2) AS INTEGER) AS cohort_index
    FROM first_order
    GROUP BY cohort_month
),
horizon AS (
    -- month index (year * 12 + month) of the latest order; NULL on an empty database, which empties the grid
    SELECT CAST(substr(MAX(order_date), 1, 4) AS INTEGER) * 12 + CAST(substr(MAX(order_date), 6, 2) AS INTEGER) AS max_index
    FROM orders
),
grid AS (
    -- one row per observable cell: the cell's calendar month is on or before the latest order month
    SELECT c.cohort_month, c.cohort_size, o.n AS month_offset
    FROM cohorts c
    CROSS JOIN offsets o
    CROSS JOIN horizon h
    WHERE c.cohort_index + o.n <= h.max_index
),
activity AS (
    -- distinct (customer, month offset) pairs; offsets beyond 11 simply find no grid cell below
    SELECT DISTINCT f.cohort_month, o.customer_id,
           (CAST(substr(o.order_date, 1, 4) AS INTEGER) - CAST(substr(f.cohort_month, 1, 4) AS INTEGER)) * 12
         + (CAST(substr(o.order_date, 6, 2) AS INTEGER) - CAST(substr(f.cohort_month, 6, 2) AS INTEGER)) AS month_offset
    FROM orders o
    JOIN first_order f ON f.customer_id = o.customer_id
),
active AS (
    SELECT cohort_month, month_offset, COUNT(*) AS active_customers
    FROM activity
    GROUP BY cohort_month, month_offset
)
SELECT g.cohort_month,
       g.cohort_size,
       g.month_offset,
       COALESCE(a.active_customers, 0) AS active_customers,
       ROUND(COALESCE(a.active_customers, 0) * 100.0 / NULLIF(g.cohort_size, 0), 1) AS retention_pct
FROM grid g
LEFT JOIN active a ON a.cohort_month = g.cohort_month AND a.month_offset = g.month_offset
ORDER BY g.cohort_month, g.month_offset;
