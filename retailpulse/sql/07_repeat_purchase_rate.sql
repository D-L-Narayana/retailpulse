-- @title: Repeat purchase rate by acquisition channel
-- @question: Do customers acquired online, in store or via pickup come back for a second order, and how quickly?
-- @technique: ROW_NUMBER() to isolate the first order, LEAD() for the second, median via ROW_NUMBER/COUNT window over per-customer gaps
-- @chart: bar x=first_channel y=repeat_rate_pct unit=%
-- @limit: 15
-- @portability: julianday(a) - julianday(b) → a::date - b::date (Postgres) / DATE_DIFF(a, b, DAY) (BigQuery)
-- @portability: ROW_NUMBER()/COUNT(*) OVER median with integer division in (cnt + 1) / 2 → percentile_cont(0.5) WITHIN GROUP (ORDER BY days) (Postgres) / PERCENTILE_CONT(days, 0.5) OVER (PARTITION BY first_channel) or DIV(cnt + 1, 2) (BigQuery)
-- @portability: ROUND(x, 2) on a double → ROUND(x::numeric, 2) (Postgres); ROW_NUMBER(), LEAD() and CASE aggregation are standard SQL on both warehouses
--
-- Share of customers with 2+ orders, split by the channel of each customer's
-- first order (acquisition channel).  The first order is the earliest
-- order_date, ties broken by order_id — the same rule 10_customer_ltv uses.
--
-- median_days_to_second_order is the median gap in days between a repeat
-- customer's first and second order, NULL for a channel without repeaters; an
-- even number of gaps averages the two middle values.
--
-- All three window functions in `ranked` share one partition/order/frame so the
-- engine sorts orders once; the explicit whole-partition frame on COUNT(*) is
-- what keeps it in that single pass (a frameless COUNT(*) OVER (PARTITION BY
-- customer_id) is a second window and costs a second sort).
WITH ranked AS (
    SELECT customer_id, channel, order_date,
           ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date, order_id) AS rn,
           LEAD(order_date) OVER (PARTITION BY customer_id ORDER BY order_date, order_id) AS next_order_date,
           COUNT(*) OVER (PARTITION BY customer_id ORDER BY order_date, order_id
                          ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS n_orders
    FROM orders
),
per_customer AS (
    SELECT customer_id,
           channel AS first_channel,
           n_orders,
           CAST(julianday(next_order_date) - julianday(order_date) AS INTEGER) AS days_to_second_order
    FROM ranked
    WHERE rn = 1
),
by_channel AS (
    SELECT first_channel,
           COUNT(*) AS customers,
           SUM(CASE WHEN n_orders >= 2 THEN 1 ELSE 0 END) AS repeat_customers,
           ROUND(SUM(CASE WHEN n_orders >= 2 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 2) AS repeat_rate_pct,
           ROUND(AVG(n_orders), 2) AS avg_orders_per_customer
    FROM per_customer
    GROUP BY first_channel
),
gaps AS (
    SELECT first_channel, days_to_second_order,
           ROW_NUMBER() OVER (PARTITION BY first_channel ORDER BY days_to_second_order, customer_id) AS pos,
           COUNT(*) OVER (PARTITION BY first_channel) AS cnt
    FROM per_customer
    WHERE days_to_second_order IS NOT NULL
),
medians AS (
    -- the middle row for an odd count, the two middle rows (averaged) for an even count
    SELECT first_channel, AVG(days_to_second_order) AS median_days
    FROM gaps
    WHERE pos IN ((cnt + 1) / 2, (cnt + 2) / 2)
    GROUP BY first_channel
)
SELECT b.first_channel,
       b.customers,
       b.repeat_customers,
       b.repeat_rate_pct,
       b.avg_orders_per_customer,
       m.median_days AS median_days_to_second_order
FROM by_channel b
LEFT JOIN medians m ON m.first_channel = b.first_channel
ORDER BY b.repeat_rate_pct DESC, b.first_channel;
