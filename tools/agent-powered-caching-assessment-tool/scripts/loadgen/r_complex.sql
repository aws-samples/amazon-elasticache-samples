-- Complex reads: multi-table aggregation, GROUP BY / HAVING, window functions,
-- and a view. Deliberately expensive.
--
-- These are the most interesting candidates for the assessment. A cache does
-- not care that a query was hard to compute; it caches the RESULT. An expensive
-- aggregate whose inputs change slowly is among the best things to cache,
-- because caching avoids re-paying the computation every time.
--
-- The scorer must NOT penalise these for join complexity, which is exactly the
-- mistake the upstream migration-oriented scoring made.

\set days random(30, 365)
\set minitems random(1, 3)
\set lim random(10, 40)

-- Revenue per category over a window. Four tables, aggregate, sort.
SELECT cat.category_name,
       count(DISTINCT o.order_id) AS orders,
       sum(oi.quantity * oi.unit_price) AS revenue,
       avg(oi.quantity * oi.unit_price) AS avg_line_value
FROM retail."order" o
JOIN retail.order_item oi ON oi.order_id = o.order_id
JOIN retail.product p ON p.product_id = oi.product_id
JOIN retail.category cat ON cat.category_id = p.category_id
WHERE o.order_date > now() - make_interval(days => :days)
GROUP BY cat.category_name
ORDER BY revenue DESC NULLS LAST;

-- Top customers by spend, with HAVING. Aggregation over the largest tables.
SELECT c.customer_id, c.first_name || ' ' || c.last_name AS customer_name,
       count(DISTINCT o.order_id) AS order_count,
       sum(oi.quantity * oi.unit_price) AS lifetime_value
FROM retail.customer c
JOIN retail."order" o ON o.customer_id = c.customer_id
JOIN retail.order_item oi ON oi.order_id = o.order_id
GROUP BY c.customer_id, c.first_name, c.last_name
HAVING count(DISTINCT o.order_id) >= :minitems
ORDER BY lifetime_value DESC NULLS LAST
LIMIT :lim;

-- Window function: rank products by revenue within their category.
SELECT category_name, product_name, revenue,
       rank() OVER (PARTITION BY category_name ORDER BY revenue DESC) AS rank_in_category
FROM (
    SELECT cat.category_name, p.product_name,
           sum(oi.quantity * oi.unit_price) AS revenue
    FROM retail.order_item oi
    JOIN retail.product p ON p.product_id = oi.product_id
    JOIN retail.category cat ON cat.category_id = p.category_id
    GROUP BY cat.category_name, p.product_name
) ranked
ORDER BY category_name, rank_in_category
LIMIT :lim;

-- Daily order volume and value. Time bucketing plus aggregation.
SELECT date_trunc('day', o.order_date) AS order_day,
       count(*) AS orders,
       sum(pay.amount) AS collected
FROM retail."order" o
LEFT JOIN retail.payment pay ON pay.order_id = o.order_id
WHERE o.order_date > now() - make_interval(days => :days)
GROUP BY 1
ORDER BY 1 DESC
LIMIT :lim;

-- Orders with no successful payment. Anti-join, forces a large scan.
SELECT o.order_id, o.order_date, o.customer_id
FROM retail."order" o
WHERE NOT EXISTS (
    SELECT 1 FROM retail.payment pay
    WHERE pay.order_id = o.order_id AND pay.payment_status_id = 2
)
ORDER BY o.order_date DESC
LIMIT :lim;

-- Read through the bundled view, which itself aggregates.
SELECT * FROM retail.v_order_total
ORDER BY 1 DESC
LIMIT :lim;
