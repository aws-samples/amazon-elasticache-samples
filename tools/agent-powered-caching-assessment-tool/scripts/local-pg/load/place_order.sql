-- Checkout: insert an order and its item, decrement stock.
\set cust random(1, 50000)
\set pid random_zipfian(1, 300000, 1.07)
\set qty random(1, 3)
BEGIN;
INSERT INTO shop.orders (customer_id, total) VALUES (:cust, 0) RETURNING order_id \gset
INSERT INTO shop.order_item (order_id, product_id, qty, price)
  SELECT :order_id, product_id, :qty, unit_price FROM shop.product WHERE product_id = :pid;
UPDATE shop.orders SET total = (SELECT sum(qty * price) FROM shop.order_item WHERE order_id = :order_id)
  WHERE order_id = :order_id;
UPDATE shop.inventory SET on_hand = on_hand - :qty WHERE product_id = :pid;
UPDATE shop.customer SET loyalty_points = loyalty_points + 1 WHERE customer_id = :cust;
COMMIT;
