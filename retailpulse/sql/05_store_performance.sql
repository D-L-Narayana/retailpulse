-- Store league table: revenue, basket size, margin and rank within region.
WITH per_store AS (
    SELECT st.store_id, st.name, st.region, st.format,
           COUNT(DISTINCT s.order_id)                        AS orders,
           ROUND(SUM(s.net_revenue), 2)                      AS revenue,
           ROUND(SUM(s.net_revenue) / COUNT(DISTINCT s.order_id), 2) AS avg_basket,
           ROUND(SUM(s.gross_margin) * 100.0 / SUM(s.net_revenue), 2) AS margin_pct
    FROM v_sales s JOIN stores st ON st.store_id = s.store_id
    GROUP BY st.store_id
)
SELECT *,
       DENSE_RANK() OVER (PARTITION BY region ORDER BY revenue DESC) AS rank_in_region,
       ROUND(revenue * 100.0 / SUM(revenue) OVER (PARTITION BY region), 2) AS region_share_pct
FROM per_store
ORDER BY region, rank_in_region;
