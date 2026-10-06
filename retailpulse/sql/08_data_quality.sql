-- @title: Data-quality checks
-- @question: Does the loaded dataset satisfy every referential, temporal and pricing invariant the analytics rely on?
-- @technique: anti-joins, invariants
-- @chart: table
-- @limit: 30
-- @portability: LEFT JOIN ... WHERE right.key IS NULL anti-joins run on every engine (NOT EXISTS is equivalent)
-- @portability: GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' → regex `~ '^\d{4}-\d{2}-\d{2}$'` (Postgres) / REGEXP_CONTAINS(x, r'^\d{4}-\d{2}-\d{2}$') (BigQuery)
-- @portability: date(x) = x (rejects 2024-02-30, which SQLite would otherwise roll forward) → `x::date::text = x` (Postgres) / `SAFE.PARSE_DATE('%F', x) IS NOT NULL` (BigQuery)
-- @portability: lower(trim(email)) and the UNION ALL of single-row COUNT(*) aggregates are standard SQL
--
-- Result columns: check_name, severity, violations, description.
--   severity = 'error' → the build fails when violations > 0 (sum of error rows must be 0);
--   severity = 'warn'  → informational: expected to be > 0 on real data (e.g. registered customers who
--                        never ordered) and never fails the build.
-- Each row is one invariant; a single corrupt record is counted once by the check that owns the
-- invariant (so a malformed date is reported as malformed, not as "before signup"), except where one
-- fact implies another (a discount larger than the price is also negative net revenue).
-- The query takes no parameters and returns all 16 rows (violations = 0) on an empty database.
WITH iso_orders AS (
    SELECT order_id, customer_id, order_date,
           CASE WHEN order_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                 AND date(order_date) = order_date THEN 1 ELSE 0 END AS is_iso
    FROM orders
),
iso_customers AS (
    SELECT customer_id, signup_date,
           CASE WHEN signup_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                 AND date(signup_date) = signup_date THEN 1 ELSE 0 END AS is_iso
    FROM customers
)
-- ---------------------------------------------------------------- error: completeness
SELECT 'orders_without_items' AS check_name,
       'error'                AS severity,
       COUNT(*)               AS violations,
       'Every order must carry at least one line item.' AS description
FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.order_id
WHERE oi.item_id IS NULL
UNION ALL
-- ---------------------------------------------------------------- error: referential integrity
SELECT 'orphan_items', 'error', COUNT(*),
       'Every order item must reference an existing order.'
FROM order_items oi LEFT JOIN orders o ON o.order_id = oi.order_id
WHERE o.order_id IS NULL
UNION ALL
SELECT 'orphan_item_products', 'error', COUNT(*),
       'Every order item must reference an existing product.'
FROM order_items oi LEFT JOIN products p ON p.product_id = oi.product_id
WHERE p.product_id IS NULL
UNION ALL
SELECT 'orphan_order_customers', 'error', COUNT(*),
       'Every order must reference an existing customer.'
FROM orders o LEFT JOIN customers c ON c.customer_id = o.customer_id
WHERE c.customer_id IS NULL
UNION ALL
SELECT 'orphan_order_stores', 'error', COUNT(*),
       'Every order must reference an existing store.'
FROM orders o LEFT JOIN stores s ON s.store_id = o.store_id
WHERE s.store_id IS NULL
UNION ALL
-- ---------------------------------------------------------------- error: temporal invariants
SELECT 'malformed_order_dates', 'error', COUNT(*),
       'Order dates must be real calendar dates in ISO YYYY-MM-DD form.'
FROM iso_orders
WHERE is_iso = 0
UNION ALL
SELECT 'malformed_signup_dates', 'error', COUNT(*),
       'Customer signup dates must be real calendar dates in ISO YYYY-MM-DD form.'
FROM iso_customers
WHERE is_iso = 0
UNION ALL
SELECT 'orders_before_signup', 'error', COUNT(*),
       'An order cannot be dated before the signup date of its customer.'
FROM iso_orders o JOIN iso_customers c ON c.customer_id = o.customer_id
WHERE o.is_iso = 1 AND c.is_iso = 1 AND o.order_date < c.signup_date
UNION ALL
-- ---------------------------------------------------------------- error: pricing invariants
SELECT 'nonpositive_quantity', 'error', COUNT(*),
       'Every line item must have a quantity of at least one.'
FROM order_items
WHERE quantity <= 0
UNION ALL
SELECT 'discount_exceeds_price', 'error', COUNT(*),
       'A line discount can never be larger than the unit price it applies to.'
FROM order_items
WHERE discount > unit_price
UNION ALL
SELECT 'negative_net_revenue', 'error', COUNT(*),
       'Net revenue per line item (quantity times unit price minus discount) must not be negative.'
FROM v_sales
WHERE net_revenue < 0
UNION ALL
-- ---------------------------------------------------------------- error: uniqueness
SELECT 'duplicate_emails', 'error', COUNT(*) - COUNT(DISTINCT lower(trim(email))),
       'Customer email addresses must be unique after trimming whitespace and ignoring case.'
FROM customers
UNION ALL
-- ---------------------------------------------------------------- warn: commercially suspicious, never fatal
SELECT 'negative_gross_margin', 'warn', COUNT(*),
       'Line items sold below the unit cost of the product carry a negative gross margin.'
FROM order_items oi JOIN products p ON p.product_id = oi.product_id
WHERE oi.unit_price - oi.discount < p.unit_cost
UNION ALL
SELECT 'products_never_sold', 'warn', COUNT(*),
       'Catalogue products that never appear on any order item are dead stock candidates.'
FROM products p LEFT JOIN order_items oi ON oi.product_id = p.product_id
WHERE oi.item_id IS NULL
UNION ALL
SELECT 'stores_without_orders', 'warn', COUNT(*),
       'Stores that have not recorded a single order deserve a second look.'
FROM stores s LEFT JOIN orders o ON o.store_id = s.store_id
WHERE o.order_id IS NULL
UNION ALL
SELECT 'customers_without_orders', 'warn', COUNT(*),
       'Registered customers who have never placed an order are expected but worth tracking.'
FROM customers c LEFT JOIN orders o ON o.customer_id = c.customer_id
WHERE o.order_id IS NULL
ORDER BY severity, check_name;
