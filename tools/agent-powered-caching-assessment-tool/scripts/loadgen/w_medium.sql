-- Medium writes: a multi-statement transaction with dependent inserts.
--
-- Models a realistic checkout: create the order, add line items, record
-- payment, decrement stock. All inside one transaction so the work is
-- committed atomically and lock behaviour is realistic.

\set cid random(1, 12)
\set addr random(1, 14)
\set p1 random(1, 20)
\set p2 random(1, 20)
\set q1 random(1, 4)
\set q2 random(1, 4)
\set amt random(20, 900)

BEGIN;

-- Order header, capturing the generated id for the dependent statements.
INSERT INTO retail."order"
    (customer_id, order_status_id, billing_address_id, shipping_address_id,
     order_date, notes)
VALUES (:cid, 1, :addr, :addr, now(), 'loadgen-txn')
RETURNING order_id \gset neworder_

-- Two line items priced from the catalog.
--
-- ON CONFLICT is required, not optional: order_item has a unique constraint on
-- (order_id, product_id), and :p1 and :p2 are independent random draws that
-- collide roughly 1 in 20 times. Without this the transaction aborts and
-- pgbench stops the whole run. Merging the quantity is also the realistic
-- behaviour for adding the same product to an order twice.
INSERT INTO retail.order_item (order_id, product_id, quantity, unit_price)
SELECT :neworder_order_id, p.product_id, :q1, p.unit_price
FROM retail.product p WHERE p.product_id = :p1
ON CONFLICT (order_id, product_id)
DO UPDATE SET quantity = retail.order_item.quantity + EXCLUDED.quantity;

INSERT INTO retail.order_item (order_id, product_id, quantity, unit_price)
SELECT :neworder_order_id, p.product_id, :q2, p.unit_price
FROM retail.product p WHERE p.product_id = :p2
ON CONFLICT (order_id, product_id)
DO UPDATE SET quantity = retail.order_item.quantity + EXCLUDED.quantity;

-- Payment record.
INSERT INTO retail.payment
    (order_id, payment_method_id, payment_status_id, amount, paid_at)
VALUES (:neworder_order_id, 1, 2, :amt, now());

-- Stock decrement for both products. Contends on the same small table across
-- concurrent clients, which is what produces lock waits worth measuring.
UPDATE retail.inventory
SET quantity_on_hand = greatest(quantity_on_hand - :q1, 0), updated_at = now()
WHERE product_id = :p1;

UPDATE retail.inventory
SET quantity_on_hand = greatest(quantity_on_hand - :q2, 0), updated_at = now()
WHERE product_id = :p2;

COMMIT;
