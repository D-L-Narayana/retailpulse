-- @title: Basket affinity (products bought together)
-- @question: Which product pairs land in the same basket far more often than chance predicts, and how strongly does buying one predict the other?
-- @technique: order_items self-join (market-basket analysis), support / confidence / lift, relative minimum-support floor
-- @chart: bar x=pair_label y=lift
-- @limit: 15
-- @portability: The order_items self-join on order_id with a.product_id < b.product_id is ANSI SQL and runs unchanged on Postgres, BigQuery, Snowflake and DuckDB.
-- @portability: MAX(3, total_orders / 1000) uses SQLite's scalar multi-argument MAX -> GREATEST(3, total_orders / 1000) on Postgres/BigQuery/Snowflake; the integer division is intentional (BigQuery needs DIV(total_orders, 1000)).
-- @portability: '||' concatenation for pair_label -> CONCAT(sku_a, ' + ', sku_b) on BigQuery/MySQL/SQL Server.
--
-- Metric definitions for each unordered product pair {a, b}, oriented so that sku_a < sku_b:
--   pair_orders    = distinct orders containing both products
--   support_pct    = 100 * pair_orders / total_orders      share of all orders (orders table)
--   confidence_pct = 100 * pair_orders / orders_with_a     P(b in basket | a in basket)
--   lift           = confidence / P(b) = pair_orders * total_orders / (orders_with_a * orders_with_b)
-- Minimum support: pair_orders >= MAX(3, total_orders / 1000) keeps one-off coincidences out while
-- still returning rows for small fixtures and large datasets alike. Empty database -> zero rows.
WITH totals AS (
    SELECT COUNT(*) AS total_orders FROM orders
),
product_orders AS (
    SELECT product_id, COUNT(DISTINCT order_id) AS n_orders
    FROM order_items
    GROUP BY product_id
),
pairs AS (
    SELECT a.product_id AS product_lo,
           b.product_id AS product_hi,
           COUNT(DISTINCT a.order_id) AS pair_orders
    FROM order_items a
    JOIN order_items b ON b.order_id = a.order_id AND a.product_id < b.product_id
    GROUP BY a.product_id, b.product_id
),
oriented AS (
    -- Orient each pair by SKU text (not by product_id) so that sku_a < sku_b holds by construction.
    SELECT CASE WHEN plo.sku < phi.sku THEN plo.sku      ELSE phi.sku      END AS sku_a,
           CASE WHEN plo.sku < phi.sku THEN phi.sku      ELSE plo.sku      END AS sku_b,
           CASE WHEN plo.sku < phi.sku THEN plo.category ELSE phi.category END AS category_a,
           CASE WHEN plo.sku < phi.sku THEN phi.category ELSE plo.category END AS category_b,
           CASE WHEN plo.sku < phi.sku THEN olo.n_orders ELSE ohi.n_orders END AS orders_with_a,
           CASE WHEN plo.sku < phi.sku THEN ohi.n_orders ELSE olo.n_orders END AS orders_with_b,
           pr.pair_orders,
           t.total_orders
    FROM pairs pr
    JOIN products plo       ON plo.product_id = pr.product_lo
    JOIN products phi       ON phi.product_id = pr.product_hi
    JOIN product_orders olo ON olo.product_id = pr.product_lo
    JOIN product_orders ohi ON ohi.product_id = pr.product_hi
    CROSS JOIN totals t
    WHERE pr.pair_orders >= MAX(3, t.total_orders / 1000)
)
SELECT sku_a || ' + ' || sku_b                                                                  AS pair_label,
       sku_a,
       sku_b,
       category_a,
       category_b,
       pair_orders,
       ROUND(100.0 * pair_orders / NULLIF(total_orders, 0), 2)                                  AS support_pct,
       ROUND(100.0 * pair_orders / NULLIF(orders_with_a, 0), 2)                                 AS confidence_pct,
       ROUND(pair_orders * total_orders * 1.0 / NULLIF(orders_with_a * orders_with_b, 0), 3)    AS lift
FROM oriented
ORDER BY lift DESC, pair_orders DESC, sku_a, sku_b;
