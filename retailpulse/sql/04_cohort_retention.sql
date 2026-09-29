-- Monthly acquisition cohorts and the % of each cohort still purchasing
-- N months after their first order.
WITH first_order AS (
    SELECT customer_id, MIN(substr(order_date, 1, 7)) AS cohort_month
    FROM orders GROUP BY customer_id
),
activity AS (
    SELECT DISTINCT o.customer_id, f.cohort_month, substr(o.order_date, 1, 7) AS active_month
    FROM orders o JOIN first_order f USING (customer_id)
),
with_offset AS (
    SELECT cohort_month, customer_id,
           (CAST(substr(active_month, 1, 4) AS INTEGER) - CAST(substr(cohort_month, 1, 4) AS INTEGER)) * 12
         + (CAST(substr(active_month, 6, 2) AS INTEGER) - CAST(substr(cohort_month, 6, 2) AS INTEGER)) AS month_offset
    FROM activity
),
cohort_size AS (
    SELECT cohort_month, COUNT(*) AS size FROM first_order GROUP BY cohort_month
)
SELECT w.cohort_month, cs.size AS cohort_size, w.month_offset,
       COUNT(DISTINCT w.customer_id) AS active_customers,
       ROUND(COUNT(DISTINCT w.customer_id) * 100.0 / cs.size, 1) AS retention_pct
FROM with_offset w JOIN cohort_size cs USING (cohort_month)
WHERE w.month_offset <= 11
GROUP BY w.cohort_month, w.month_offset
ORDER BY w.cohort_month, w.month_offset;
