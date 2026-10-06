-- @title: Channel mix by month
-- @question: How is monthly revenue split between in-store, online and pickup, and is the online share growing?
-- @technique: Conditional-aggregation pivot (SUM(CASE WHEN ...)), COALESCE so a channel with no sales reads 0
-- @chart: line x=order_month y=online_share_pct unit=%
-- @limit: 24
-- @portability: substr(order_date,1,7) month bucket (in v_sales) -> to_char(order_date,'YYYY-MM') (Postgres) / FORMAT_DATE('%Y-%m', order_date) (BigQuery)
-- @portability: SUM(CASE WHEN channel = 'online' THEN revenue END) -> SUM(revenue) FILTER (WHERE channel = 'online') (Postgres 9.4+) / SUM(IF(channel = 'online', revenue, 0)) or the PIVOT operator (BigQuery)
-- @portability: x / NULLIF(total, 0): SQLite returns NULL for x / 0, Postgres and BigQuery raise, so the NULLIF guard is required there; ROUND(x, 2) -> ROUND(x::numeric, 2) (Postgres)
--
-- Channel mix by month (pivot via conditional aggregation) plus each
-- channel's share of the month's revenue.
--
-- No column is ever NULL: a channel with no sales in a month reads 0.0 and
-- its share 0.0, and a month whose net revenue is 0 has every share at 0.0.
-- online_share_pct is the original column; in_store_share_pct and
-- pickup_share_pct are appended so the three shares sum to 100.
WITH m AS (
    SELECT order_month, channel, SUM(net_revenue) AS revenue
    FROM v_sales
    GROUP BY order_month, channel
),
by_month AS (
    SELECT order_month,
           COALESCE(SUM(CASE WHEN channel = 'in_store' THEN revenue END), 0) AS in_store,
           COALESCE(SUM(CASE WHEN channel = 'online'   THEN revenue END), 0) AS online,
           COALESCE(SUM(CASE WHEN channel = 'pickup'   THEN revenue END), 0) AS pickup,
           SUM(revenue)                                                      AS total
    FROM m
    GROUP BY order_month
)
SELECT order_month,
       ROUND(in_store, 2) AS in_store,
       ROUND(online, 2)   AS online,
       ROUND(pickup, 2)   AS pickup,
       COALESCE(ROUND(online   * 100.0 / NULLIF(total, 0), 2), 0.0) AS online_share_pct,
       COALESCE(ROUND(in_store * 100.0 / NULLIF(total, 0), 2), 0.0) AS in_store_share_pct,
       COALESCE(ROUND(pickup   * 100.0 / NULLIF(total, 0), 2), 0.0) AS pickup_share_pct
FROM by_month
ORDER BY order_month;
