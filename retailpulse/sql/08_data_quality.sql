-- Data-quality checks: each row should be 0 for a healthy dataset.
SELECT 'orders_without_items' AS check_name,
       COUNT(*) AS violations
FROM orders o LEFT JOIN order_items oi USING (order_id)
WHERE oi.item_id IS NULL
UNION ALL
SELECT 'orders_before_signup',
       COUNT(*)
FROM orders o JOIN customers c USING (customer_id)
WHERE o.order_date < c.signup_date
UNION ALL
SELECT 'negative_net_revenue', COUNT(*) FROM v_sales WHERE net_revenue < 0
UNION ALL
SELECT 'orphan_items',
       COUNT(*)
FROM order_items oi LEFT JOIN orders o USING (order_id) WHERE o.order_id IS NULL
UNION ALL
SELECT 'duplicate_emails',
       COUNT(*) - COUNT(DISTINCT email) FROM customers;
