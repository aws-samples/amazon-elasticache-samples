-- Navigation: a tiny table read constantly.
\set cid random(1, 24)
SELECT category_id, name, slug FROM shop.category ORDER BY name;
SELECT product_id, name, unit_price FROM shop.product
WHERE category_id = :cid AND is_active ORDER BY product_id LIMIT 20;
