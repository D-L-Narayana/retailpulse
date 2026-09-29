-- ABC inventory classification: rank products by revenue, take the running
-- cumulative share and bucket into A (top 70%), B (next 20%), C (last 10%).
WITH by_product AS (
    SELECT p.product_id, p.sku, p.category,
           SUM(s.net_revenue) AS revenue,
           SUM(s.quantity)    AS units
    FROM v_sales s JOIN products p ON p.product_id = s.product_id
    GROUP BY p.product_id
),
ranked AS (
    SELECT *,
           RANK() OVER (ORDER BY revenue DESC)                          AS revenue_rank,
           SUM(revenue) OVER (ORDER BY revenue DESC
                              ROWS UNBOUNDED PRECEDING) * 1.0
             / SUM(revenue) OVER ()                                     AS cum_share
    FROM by_product
)
SELECT product_id, sku, category, ROUND(revenue, 2) AS revenue, units, revenue_rank,
       ROUND(cum_share * 100, 2) AS cum_share_pct,
       CASE WHEN cum_share <= 0.70 THEN 'A'
            WHEN cum_share <= 0.90 THEN 'B'
            ELSE 'C' END AS abc_class
FROM ranked
ORDER BY revenue_rank;
