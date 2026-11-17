-- Home page widget: an expensive aggregate with few distinct parameters.
\set cid random(1, 6)
SELECT p.product_id, p.name, sum(i.qty) AS sold
FROM shop.order_item i JOIN shop.orders o USING (order_id) JOIN shop.product p USING (product_id)
WHERE p.category_id = :cid AND o.ordered_at > now() - interval '30 days'
GROUP BY p.product_id, p.name ORDER BY sold DESC LIMIT 10;
