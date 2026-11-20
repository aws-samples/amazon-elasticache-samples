-- ECDL sample tables for MySQL 8.0 / Aurora MySQL 3.
-- Synthetic data only. Run it in a TEST database, as a user that can create
-- tables there, for example:
--   mysql --ssl-mode=VERIFY_IDENTITY --ssl-ca=ecdl_link/rds-ca-bundle.pem \
--         -h <writer endpoint> -u <admin user> -p <database> < samples/mysql.sql
-- It creates three tables, all prefixed sample_, and drops nothing.
-- Running it again refuses (the tables already exist): delete them first with
-- samples/cleanup-mysql.sql.

CREATE TABLE sample_customers (
  customer_id INT PRIMARY KEY,
  name        VARCHAR(80)  NOT NULL,
  tier        VARCHAR(10)  NOT NULL,
  created_at  DATETIME(3)  NOT NULL
);

CREATE TABLE sample_products (
  product_id  INT PRIMARY KEY,
  name        VARCHAR(80)   NOT NULL,
  price       DECIMAL(10,2) NOT NULL,
  status      VARCHAR(10)   NOT NULL,   -- 'active' or 'retired', for a filter
  in_stock    TINYINT(1)    NOT NULL,
  updated_at  DATETIME(3)   NOT NULL
);

CREATE TABLE sample_orders (
  order_id    INT PRIMARY KEY,
  customer_id INT           NOT NULL,   -- parent: sample_customers, for denormalize
  product_id  INT           NOT NULL,
  quantity    INT           NOT NULL,
  total       DECIMAL(12,2) NOT NULL,
  note        VARCHAR(200)  NULL,
  ordered_at  DATETIME(3)   NOT NULL,
  KEY idx_customer (customer_id)
);

SET SESSION cte_max_recursion_depth = 10000;

INSERT INTO sample_customers (customer_id, name, tier, created_at)
WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 200)
SELECT i, CONCAT('Customer ', i), ELT(1 + i % 3, 'gold', 'silver', 'bronze'),
       TIMESTAMP('2025-01-01') + INTERVAL i HOUR
FROM n;

INSERT INTO sample_products (product_id, name, price, status, in_stock, updated_at)
WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 500)
SELECT i, CONCAT('Product ', i), ROUND(1 + (i * 7919 % 50000) / 100, 2),
       IF(i % 10 = 0, 'retired', 'active'), i % 4 <> 0,
       TIMESTAMP('2025-01-01') + INTERVAL i MINUTE
FROM n;

INSERT INTO sample_orders (customer_id, order_id, product_id, quantity, total, note, ordered_at)
WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5000)
SELECT 1 + i % 200, i, 1 + i % 500, 1 + i % 5, ROUND((1 + i % 5) * (1 + (i * 31 % 9000) / 100), 2),
       IF(i % 7 = 0, NULL, CONCAT('order ', i)),
       TIMESTAMP('2025-02-01') + INTERVAL i MINUTE
FROM n;
