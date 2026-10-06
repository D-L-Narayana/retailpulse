-- @title: Customer lifetime value by acquisition channel
-- @question: Do customers acquired online come back and spend more over their first twelve months than customers acquired in store or via pickup?
-- @technique: ROW_NUMBER first-order attribution, single-pass revenue aggregation, recursive CTE offset spine, cumulative SUM OVER, right-censoring filter
-- @chart: line x=month_offset y=cum_revenue_per_customer series=first_channel
-- @limit: 36
-- @portability: WITH RECURSIVE offsets(month_offset) AS (...) is standard SQL (Postgres, MySQL 8, Snowflake, DuckDB); BigQuery can use UNNEST(GENERATE_ARRAY(0, 11)) instead; SQL Server omits the RECURSIVE keyword.
-- @portability: The month index CAST(substr(d, 1, 4) AS INTEGER) * 12 + CAST(substr(d, 6, 2) AS INTEGER) relies on ISO-8601 text dates ('YYYY-MM-DD' / 'YYYY-MM') -> EXTRACT(YEAR FROM d) * 12 + EXTRACT(MONTH FROM d) on a DATE column (Postgres/BigQuery/Snowflake).
-- @portability: ROW_NUMBER() OVER (PARTITION BY ...) and SUM() OVER (... ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) are ANSI window functions; v_sales is this project's line-level revenue view (inline its definition on a warehouse).
--
-- Definitions:
--   first_channel       channel of the customer's first order (earliest order_date, lowest order_id on ties; same rule as 07)
--   cohort month        calendar month of that first order as an absolute month index (year * 12 + month)
--   eligible customer   cohort month + 11 <= last order month in the data, i.e. all twelve offsets are observable.
--                       This right-censoring guard means every point of a channel's curve is computed over the same
--                       customers, so the curve is non-decreasing by construction and later points are not deflated
--                       by cohorts that have not had time to come back.
--   month_offset        0..11 months since the first order
--   cum_revenue_per_customer = SUM(net_revenue of eligible customers in the channel with offset <= month_offset)
--                              / eligible_customers, rounded to cents
-- Channels with no eligible customers produce no rows; an empty or single-month database -> zero rows.
--
-- Shape: revenue is aggregated in ONE pass over v_sales (order_rev) *before* customer attribution is joined in.
-- Joining the first-order CTE inside the line-item scan let the planner enter v_sales through products and
-- re-probe first orders for every product x customer (seconds at 30 000 orders); every step below is linear
-- in the number of line items.
WITH RECURSIVE offsets(month_offset) AS (
    SELECT 0
    UNION ALL
    SELECT month_offset + 1 FROM offsets WHERE month_offset < 11
),
order_rev AS (
    SELECT order_id, customer_id, order_month, SUM(net_revenue) AS revenue
    FROM v_sales
    GROUP BY order_id, customer_id, order_month
),
first_orders AS (
    SELECT customer_id,
           channel AS first_channel,
           CAST(substr(order_date, 1, 4) AS INTEGER) * 12 + CAST(substr(order_date, 6, 2) AS INTEGER) AS cohort_idx,
           ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date, order_id) AS rn
    FROM orders
),
horizon AS (
    SELECT MAX(CAST(substr(order_date, 1, 4) AS INTEGER) * 12 + CAST(substr(order_date, 6, 2) AS INTEGER)) AS max_idx
    FROM orders
),
eligible AS (
    SELECT f.customer_id, f.first_channel, f.cohort_idx
    FROM first_orders f
    CROSS JOIN horizon h
    WHERE f.rn = 1 AND f.cohort_idx + 11 <= h.max_idx
),
channel_size AS (
    SELECT first_channel, COUNT(*) AS eligible_customers
    FROM eligible
    GROUP BY first_channel
),
offset_revenue AS (
    SELECT e.first_channel,
           CAST(substr(r.order_month, 1, 4) AS INTEGER) * 12 + CAST(substr(r.order_month, 6, 2) AS INTEGER) - e.cohort_idx AS month_offset,
           r.revenue
    FROM order_rev r
    JOIN eligible e ON e.customer_id = r.customer_id
),
monthly_revenue AS (
    SELECT first_channel, month_offset, SUM(revenue) AS revenue
    FROM offset_revenue
    WHERE month_offset BETWEEN 0 AND 11
    GROUP BY first_channel, month_offset
),
grid AS (
    -- One row per (channel, offset) even when nobody bought anything in that month.
    SELECT cs.first_channel, o.month_offset, cs.eligible_customers, COALESCE(m.revenue, 0) AS revenue
    FROM channel_size cs
    CROSS JOIN offsets o
    LEFT JOIN monthly_revenue m ON m.first_channel = cs.first_channel AND m.month_offset = o.month_offset
)
SELECT first_channel,
       month_offset,
       eligible_customers,
       ROUND(SUM(revenue) OVER (PARTITION BY first_channel ORDER BY month_offset
                                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) * 1.0
             / NULLIF(eligible_customers, 0), 2) AS cum_revenue_per_customer
FROM grid
ORDER BY first_channel, month_offset;
