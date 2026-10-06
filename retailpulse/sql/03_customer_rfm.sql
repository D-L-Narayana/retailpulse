-- @title: Customer RFM segmentation
-- @question: Which customers are champions, loyal, new, at risk or lost, judged by how recently, how often and how much they buy?
-- @technique: Rank-based quintiles (RANK() + COUNT(*) OVER) so equal values get equal scores, CASE segmentation
-- @chart: table
-- @limit: 10
-- @portability: julianday(a) - julianday(b) → a::date - b::date (Postgres) / DATE_DIFF(a, b, DAY) (BigQuery)
-- @portability: integer division in (RANK() - 1) * 5 / COUNT(*) OVER () → identical in Postgres (integer operands); DIV((RANK() - 1) * 5, COUNT(*) OVER ()) in BigQuery, whose / returns FLOAT64
-- @portability: ROUND(x, 2) on a double → ROUND(x::numeric, 2) (Postgres); RANK(), COUNT(*) OVER () and CTEs are standard SQL on both warehouses
--
-- RFM segmentation: quintile scores for Recency, Frequency and Monetary value
-- relative to the latest order date in the dataset.  Only customers with at
-- least one order (with line items) are scored.
--
-- Tie handling — deterministic by construction.  NTILE(5) splits a run of equal
-- values across neighbouring buckets in whatever order the sorter happens to
-- visit the rows, so two customers with the same frequency could receive
-- different f_scores (and segments) from one build to the next.  Scores are
-- therefore derived from the rank of the value instead:
--
--     score = 1 + (RANK() OVER (ORDER BY value) - 1) * 5 / COUNT(*) OVER ()
--
-- RANK() gives equal values the same (minimum) rank, so equal values always
-- share a score; integer division maps that rank into the five quintile bands;
-- the lowest value scores 1 and a higher value never scores lower than a
-- smaller one.  Recency is mirrored (6 - score) so the most recent buyers
-- score 5.  Band sizes are approximately — not exactly — equal, which is the
-- price of never splitting a tie.  The segment rules are unchanged.
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
    SELECT customer_id, recency_days, frequency, monetary,
           6 - (1 + (RANK() OVER (ORDER BY recency_days) - 1) * 5 / COUNT(*) OVER ()) AS r_score,  -- lower recency = better
           1 + (RANK() OVER (ORDER BY frequency) - 1) * 5 / COUNT(*) OVER ()           AS f_score,
           1 + (RANK() OVER (ORDER BY monetary) - 1) * 5 / COUNT(*) OVER ()            AS m_score
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
ORDER BY rfm_total DESC, monetary DESC, customer_id;
