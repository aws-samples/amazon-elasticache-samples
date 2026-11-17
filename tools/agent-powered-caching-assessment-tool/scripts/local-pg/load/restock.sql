-- Back office: restock, and a rare product edit.
\set pid random(1, 300000)
UPDATE shop.inventory SET on_hand = on_hand + 50 WHERE product_id = :pid;
UPDATE shop.product SET unit_price = unit_price WHERE product_id = :pid AND random() < 0.02;
