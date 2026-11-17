-- Medium reads: small joins, range filters, ORDER BY with LIMIT.
-- These return bounded result sets and touch several tables, so they are
-- plausible cache-aside candidates: expensive enough to be worth caching,
-- small enough to cache cleanly.

\set oid random(1, 15000)
\set cid random(1, 12)
\set days random(1, 365)
\set lim random(10, 50)

-- Two-table join, one row out. Classic cache-aside shape.
SELECT o.order_id, o.order_date, os.status_code, c.first_name || ' ' || c.last_name AS customer_name
FROM retail."order" o
JOIN retail.order_status os ON os.order_status_id = o.order_status_id
JOIN retail.customer c ON c.customer_id = o.customer_id
WHERE o.order_id = :oid;

-- Order detail with product names. Bounded by a single order.
SELECT oi.order_item_id, p.product_name, p.sku, oi.quantity, oi.unit_price,
       (oi.quantity * oi.unit_price) AS line_total
FROM retail.order_item oi
JOIN retail.product p ON p.product_id = oi.product_id
WHERE oi.order_id = :oid
ORDER BY oi.order_item_id;

-- Recent orders for one customer. Range filter plus sort plus limit.
SELECT o.order_id, o.order_date, o.order_status_id
FROM retail."order" o
WHERE o.customer_id = :cid
  AND o.order_date > now() - make_interval(days => :days)
ORDER BY o.order_date DESC
LIMIT :lim;

-- Payment history joined to status. Indexed FK plus small join.
SELECT pay.payment_id, pay.amount, pay.paid_at, ps.status_code, pm.method_code
FROM retail.payment pay
JOIN retail.payment_status ps ON ps.payment_status_id = pay.payment_status_id
JOIN retail.payment_method pm ON pm.payment_method_id = pay.payment_method_id
WHERE pay.order_id = :oid;

-- Catalog browse: filtered, sorted, limited. Small table but real work.
SELECT p.product_id, p.product_name, p.unit_price, cat.category_name
FROM retail.product p
JOIN retail.category cat ON cat.category_id = p.category_id
WHERE p.is_active = true
ORDER BY p.unit_price DESC
LIMIT :lim;

-- Inventory check against a threshold. Small table, frequent read.
SELECT i.product_id, p.product_name, i.quantity_on_hand, i.reorder_threshold
FROM retail.inventory i
JOIN retail.product p ON p.product_id = i.product_id
WHERE i.quantity_on_hand < i.reorder_threshold * 3;
