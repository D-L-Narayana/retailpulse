-- RFM segmentation: quintile scores for Recency, Frequency and Monetary value
-- relative to the latest order date in the dataset.
WITH anchor AS (SELECT MAX(order_date) AS as_of FROM orders),
per_customer AS (
    SELECT s.customer_id,
           CAST(julianday((SELECT as_of FROM anchor)) - julianday(MAX(s.order_date)) AS INTEGER) AS recency_days,
           COUNT(DISTINCT s.order_id)   AS frequency,
           ROUND(SUM(s.net_revenue), 2) AS monetary
    FROM v_sales s
    GROUP BY s.customer_id
),
scored AS (
    SELECT *,
           6 - NTILE(5) OVER (ORDER BY recency_days)  AS r_score,   -- lower recency = better
           NTILE(5)     OVER (ORDER BY frequency)     AS f_score,
           NTILE(5)     OVER (ORDER BY monetary)      AS m_score
    FROM per_customer
)
SELECT customer_id, recency_days, frequency, monetary, r_score, f_score, m_score,
       r_score + f_score + m_score AS rfm_total,
       CASE
         WHEN r_score >= 4 AND f_score >= 4 AND m_score >= 4 THEN 'Champion'
         WHEN r_score >= 3 AND f_score >= 3                  THEN 'Loyal'
         WHEN r_score >= 4 AND f_score <= 2                  THEN 'New'
         WHEN r_score <= 2 AND f_score >= 3                  THEN 'At Risk'
         WHEN r_score <= 2                                   THEN 'Lost'
         ELSE 'Regular'
       END AS segment
FROM scored
ORDER BY rfm_total DESC, monetary DESC;
