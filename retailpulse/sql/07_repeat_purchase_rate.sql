-- Share of customers with 2+ orders, split by acquisition channel of first order.
WITH first_orders AS (
    SELECT customer_id, channel AS first_channel,
           ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date, order_id) AS rn
    FROM orders
),
counts AS (
    SELECT o.customer_id, f.first_channel, COUNT(*) AS n_orders
    FROM orders o JOIN first_orders f ON f.customer_id = o.customer_id AND f.rn = 1
    GROUP BY o.customer_id
)
SELECT first_channel,
       COUNT(*) AS customers,
       SUM(CASE WHEN n_orders >= 2 THEN 1 ELSE 0 END) AS repeat_customers,
       ROUND(SUM(CASE WHEN n_orders >= 2 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS repeat_rate_pct,
       ROUND(AVG(n_orders), 2) AS avg_orders_per_customer
FROM counts
GROUP BY first_channel
ORDER BY repeat_rate_pct DESC;
