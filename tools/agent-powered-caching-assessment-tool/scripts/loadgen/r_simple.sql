-- Simple reads: indexed point lookups.
-- Expected to be fast and served from shared_buffers. These are the queries
-- that should mostly be EXCLUDED as "already well served", which is the
-- behaviour worth proving.

\set oid random(1, 15000)
\set pid random(1, 20)
\set cid random(1, 12)

-- Primary key lookup on a large table
SELECT order_id, customer_id, order_status_id, order_date
FROM retail."order"
WHERE order_id = :oid;

-- Primary key lookup on a small hot reference table
SELECT product_id, sku, product_name, unit_price, is_active
FROM retail.product
WHERE product_id = :pid;

-- Small table scan, cheap, runs constantly. Tests that the seq-scan
-- size gate correctly refuses to credit this as pressure.
SELECT category_id, category_name
FROM retail.category;

-- Foreign key lookup returning a small bounded set
SELECT order_item_id, product_id, quantity, unit_price
FROM retail.order_item
WHERE order_id = :oid;

-- Narrow indexed lookup on a reference table
SELECT customer_id, email, first_name, last_name
FROM retail.customer
WHERE customer_id = :cid;
