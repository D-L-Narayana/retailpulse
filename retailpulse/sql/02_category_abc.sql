-- @title: ABC product classification
-- @question: Which SKUs make up the top 70 % (A), next 20 % (B) and last 10 % (C) of revenue, and which never sold at all?
-- @technique: LEFT JOIN from products, ROW_NUMBER and running SUM() OVER with a deterministic tie-break
-- @chart: table
-- @limit: 10
-- @portability: ROUND(x, 2) on a double -> ROUND(x::numeric, 2) (Postgres); SUM() OVER (ORDER BY ... ROWS UNBOUNDED PRECEDING) is standard SQL on Postgres and BigQuery
-- @portability: x / NULLIF(total, 0): SQLite returns NULL for x / 0, Postgres and BigQuery raise, so the NULLIF guard is required there
-- @portability: v_sales is a SQLite view; on a warehouse materialise it as a CTE or model with the same line-level formula
--
-- ABC inventory classification: rank products by revenue, take the running
-- cumulative share and bucket into A (top 70 %), B (next 20 %), C (last 10 %).
--
-- Every product appears (LEFT JOIN from products): a SKU that never sold has
-- revenue 0.0, units 0, its own rank at the tail, cum_share_pct 100.0 and
-- class C.  Every window orders by (revenue DESC, product_id) so tied revenues
-- get stable ranks, shares and classes on any SQLite build.  When nothing has
-- sold at all, cum_share_pct is NULL (no total to share) and every SKU is C.
WITH sales AS (
    SELECT product_id,
           SUM(net_revenue) AS revenue,
           SUM(quantity)    AS units
    FROM v_sales
    GROUP BY product_id
),
by_product AS (
    SELECT p.product_id, p.sku, p.category,
           COALESCE(s.revenue, 0) AS revenue,
           COALESCE(s.units, 0)   AS units
    FROM products p
    LEFT JOIN sales s ON s.product_id = p.product_id
),
ranked AS (
    SELECT product_id, sku, category, revenue, units,
           ROW_NUMBER() OVER (ORDER BY revenue DESC, product_id)       AS revenue_rank,
           SUM(revenue) OVER (ORDER BY revenue DESC, product_id
                              ROWS UNBOUNDED PRECEDING) * 1.0
             / NULLIF(SUM(revenue) OVER (), 0)                         AS cum_share
    FROM by_product
)
SELECT product_id, sku, category, ROUND(revenue, 2) AS revenue, units, revenue_rank,
       ROUND(cum_share * 100, 2) AS cum_share_pct,
       CASE WHEN cum_share <= 0.70 THEN 'A'
            WHEN cum_share <= 0.90 THEN 'B'
            ELSE 'C' END AS abc_class
FROM ranked
ORDER BY revenue_rank;
