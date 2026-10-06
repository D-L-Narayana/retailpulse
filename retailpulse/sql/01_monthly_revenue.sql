-- @title: Monthly revenue & MoM growth
-- @question: How is net revenue trending month over month and year over year, and what margin does it carry?
-- @technique: LAG (1 and 12 rows back), trailing-window AVG, NULL-safe percentages
-- @chart: line x=order_month y=revenue
-- @limit: 24
-- @portability: substr(order_date,1,7) month bucket (in v_sales) -> to_char(order_date,'YYYY-MM') (Postgres) / FORMAT_DATE('%Y-%m', order_date) (BigQuery)
-- @portability: (CAST(substr(order_month,1,4) AS INTEGER) - 1) || substr(order_month,5) -> to_char(to_date(order_month,'YYYY-MM') - interval '1 year','YYYY-MM') (Postgres) / FORMAT_DATE('%Y-%m', DATE_SUB(PARSE_DATE('%Y-%m', order_month), INTERVAL 1 YEAR)) (BigQuery)
-- @portability: ROUND(x, 2) on a double -> ROUND(x::numeric, 2) (Postgres); LAG(expr, n), AVG() OVER (... ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) and NULLIF are standard SQL on both warehouses
--
-- Monthly net revenue, margin and order count with month-over-month growth,
-- a 3-month trailing average and year-over-year growth.
--
-- yoy_pct compares a month with the row 12 months back and is NULL for the
-- first 12 months.  It is also NULL when that row is not the same calendar
-- month one year earlier (a month without any orders produces no row and
-- would otherwise shift the comparison), so a gap never yields a misleading
-- figure.  All divisions are NULLIF-guarded: a zero-revenue month gives NULL
-- percentages instead of an error on warehouses that raise on x / 0.
WITH monthly AS (
    SELECT order_month,
           COUNT(DISTINCT order_id)      AS orders,
           ROUND(SUM(net_revenue), 2)    AS revenue,
           ROUND(SUM(gross_margin), 2)   AS margin
    FROM v_sales
    GROUP BY order_month
),
windowed AS (
    SELECT order_month, orders, revenue, margin,
           LAG(revenue)         OVER (ORDER BY order_month)                   AS prev_revenue,
           LAG(revenue, 12)     OVER (ORDER BY order_month)                   AS revenue_12_back,
           LAG(order_month, 12) OVER (ORDER BY order_month)                   AS month_12_back,
           AVG(revenue)         OVER (ORDER BY order_month
                                      ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS trailing_3m
    FROM monthly
)
SELECT order_month, orders, revenue, margin,
       ROUND(margin * 100.0 / NULLIF(revenue, 0), 2)                            AS margin_pct,
       ROUND(revenue - prev_revenue, 2)                                         AS mom_change,
       ROUND((revenue - prev_revenue) * 100.0 / NULLIF(prev_revenue, 0), 2)     AS mom_pct,
       ROUND(trailing_3m, 2)                                                    AS trailing_3m_avg,
       CASE WHEN month_12_back = (CAST(substr(order_month, 1, 4) AS INTEGER) - 1) || substr(order_month, 5)
            THEN ROUND((revenue - revenue_12_back) * 100.0 / NULLIF(revenue_12_back, 0), 2)
       END                                                                      AS yoy_pct
FROM windowed
ORDER BY order_month;
