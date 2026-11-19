-- Product page: a skewed key lookup on a table larger than shared_buffers.
\set pid random_zipfian(1, 300000, 1.07)
SELECT product_id, sku, name, description, unit_price, is_active
FROM shop.product WHERE product_id = :pid;
