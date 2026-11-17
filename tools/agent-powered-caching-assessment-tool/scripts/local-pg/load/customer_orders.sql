-- Account page: the customer and their recent orders.
\set cust random(1, 50000)
SELECT customer_id, email, full_name, loyalty_points FROM shop.customer WHERE customer_id = :cust;
SELECT o.order_id, o.ordered_at, o.total, count(*) AS items
FROM shop.orders o JOIN shop.order_item i USING (order_id)
WHERE o.customer_id = :cust
GROUP BY o.order_id ORDER BY o.ordered_at DESC LIMIT 10;
