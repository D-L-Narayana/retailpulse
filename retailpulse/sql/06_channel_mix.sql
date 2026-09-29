-- Channel mix by month (pivot via conditional aggregation) plus share of total.
WITH m AS (
    SELECT order_month, channel, SUM(net_revenue) AS revenue
    FROM v_sales GROUP BY order_month, channel
)
SELECT order_month,
       ROUND(SUM(CASE WHEN channel = 'in_store' THEN revenue END), 2) AS in_store,
       ROUND(SUM(CASE WHEN channel = 'online'   THEN revenue END), 2) AS online,
       ROUND(SUM(CASE WHEN channel = 'pickup'   THEN revenue END), 2) AS pickup,
       ROUND(SUM(CASE WHEN channel = 'online'   THEN revenue END) * 100.0 / SUM(revenue), 2) AS online_share_pct
FROM m
GROUP BY order_month
ORDER BY order_month;
