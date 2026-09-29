-- Monthly net revenue, margin and order count with month-over-month growth
-- and a 3-month trailing average using window functions.
WITH monthly AS (
    SELECT order_month,
           COUNT(DISTINCT order_id)      AS orders,
           ROUND(SUM(net_revenue), 2)    AS revenue,
           ROUND(SUM(gross_margin), 2)   AS margin
    FROM v_sales
    GROUP BY order_month
)
SELECT order_month, orders, revenue, margin,
       ROUND(margin * 100.0 / revenue, 2)                                    AS margin_pct,
       ROUND(revenue - LAG(revenue) OVER (ORDER BY order_month), 2)          AS mom_change,
       ROUND((revenue - LAG(revenue) OVER (ORDER BY order_month)) * 100.0
             / NULLIF(LAG(revenue) OVER (ORDER BY order_month), 0), 2)       AS mom_pct,
       ROUND(AVG(revenue) OVER (ORDER BY order_month
             ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 2)                   AS trailing_3m_avg
FROM monthly
ORDER BY order_month;
