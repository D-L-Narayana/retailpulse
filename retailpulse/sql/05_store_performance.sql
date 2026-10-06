-- @title: Store performance league table
-- @question: Which stores lead their region on revenue, how large are their baskets and margins, and which stores took no orders at all?
-- @technique: LEFT JOIN from stores, DENSE_RANK within region with a deterministic tie-break, share via SUM() OVER (PARTITION BY)
-- @chart: table
-- @limit: 12
-- @portability: GROUP BY lists every non-aggregated column (required by BigQuery; Postgres also accepts grouping by the primary key alone)
-- @portability: ROUND(x, 2) on a double -> ROUND(x::numeric, 2) (Postgres); COUNT(DISTINCT ...), DENSE_RANK() OVER (PARTITION BY ...) and SUM() OVER (PARTITION BY ...) are standard SQL on both warehouses
-- @portability: x / NULLIF(y, 0): SQLite returns NULL for x / 0, Postgres and BigQuery raise, so every ratio is NULLIF-guarded and the share is COALESCEd to 0.0
--
-- Store league table: revenue, basket size, margin and rank within region.
--
-- Every store appears (LEFT JOIN from stores).  A store without orders shows
-- orders 0, revenue 0.0, NULL avg_basket and margin_pct (nothing to average),
-- a real integer rank at the tail of its region and region_share_pct 0.0.
-- Ranks order by (revenue DESC, store_id) so tied stores rank the same way
-- on every build.  The share is 0.0 rather than NULL when a region has no
-- revenue at all, so consumers can always sum the column.
WITH sales AS (
    SELECT store_id,
           COUNT(DISTINCT order_id) AS orders,
           SUM(net_revenue)         AS revenue,
           SUM(gross_margin)        AS margin
    FROM v_sales
    GROUP BY store_id
),
per_store AS (
    SELECT st.store_id, st.name, st.region, st.format,
           COALESCE(s.orders, 0)                               AS orders,
           ROUND(COALESCE(s.revenue, 0), 2)                    AS revenue,
           ROUND(s.revenue / NULLIF(s.orders, 0), 2)           AS avg_basket,
           ROUND(s.margin * 100.0 / NULLIF(s.revenue, 0), 2)   AS margin_pct
    FROM stores st
    LEFT JOIN sales s ON s.store_id = st.store_id
)
SELECT store_id, name, region, format, orders, revenue, avg_basket, margin_pct,
       DENSE_RANK() OVER (PARTITION BY region ORDER BY revenue DESC, store_id)  AS rank_in_region,
       COALESCE(ROUND(revenue * 100.0
                      / NULLIF(SUM(revenue) OVER (PARTITION BY region), 0), 2), 0.0) AS region_share_pct
FROM per_store
ORDER BY region, rank_in_region;
