-- Simple writes: single-statement INSERT and indexed UPDATE.
--
-- Purpose in the assessment: make `inventory` write-heavy despite being tiny
-- and frequently read. It should be DISQUALIFIED on write ratio, proving the
-- scorer weights writes by call volume rather than by number of query shapes.

\set cid random(1, 12)
\set pid random(1, 20)
\set addr random(1, 14)
\set qty random(1, 5)
\set delta random(1, 3)

-- New order header. Grows the largest table.
INSERT INTO retail."order"
    (customer_id, order_status_id, billing_address_id, shipping_address_id,
     order_date, notes)
VALUES (:cid, 1, :addr, :addr, now(), 'loadgen');

-- Decrement stock. Small table, written constantly.
UPDATE retail.inventory
SET quantity_on_hand = greatest(quantity_on_hand - :delta, 0),
    updated_at = now()
WHERE product_id = :pid;

-- Advance an order's status. Indexed single-row update on a large table.
UPDATE retail."order"
SET order_status_id = 2
WHERE order_id = (SELECT max(order_id) FROM retail."order");
