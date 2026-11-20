-- ECDL sample tables for PostgreSQL 13+ / Aurora PostgreSQL.
-- Synthetic data only. Run it in a TEST database, in the schema the link
-- reads (public by default), as a user that can create tables there:
--   psql "host=<writer endpoint> dbname=<database> user=<admin user> sslmode=verify-full sslrootcert=ecdl_link/rds-ca-bundle.pem" \
--        -v ON_ERROR_STOP=1 -f samples/postgresql.sql
-- It creates three tables, all prefixed sample_, and drops nothing.
-- Running it again refuses (the tables already exist): delete them first with
-- samples/cleanup-postgresql.sql.

CREATE TABLE sample_customers (
  customer_id integer PRIMARY KEY,
  name        varchar(80)  NOT NULL,
  tier        varchar(10)  NOT NULL,
  created_at  timestamp(3) NOT NULL
);

CREATE TABLE sample_products (
  product_id  integer PRIMARY KEY,
  name        varchar(80)   NOT NULL,
  price       numeric(10,2) NOT NULL,
  status      varchar(10)   NOT NULL,   -- 'active' or 'retired', for a filter
  in_stock    boolean       NOT NULL,
  updated_at  timestamp(3)  NOT NULL
);

CREATE TABLE sample_orders (
  order_id    integer PRIMARY KEY,
  customer_id integer       NOT NULL,   -- parent: sample_customers, for denormalize
  product_id  integer       NOT NULL,
  quantity    integer       NOT NULL,
  total       numeric(12,2) NOT NULL,
  note        varchar(200)  NULL,
  ordered_at  timestamp(3)  NOT NULL
);
CREATE INDEX sample_orders_customer ON sample_orders (customer_id);

INSERT INTO sample_customers (customer_id, name, tier, created_at)
SELECT i, 'Customer ' || i, (ARRAY['gold', 'silver', 'bronze'])[1 + i % 3],
       timestamp '2025-01-01' + i * interval '1 hour'
FROM generate_series(1, 200) AS i;

INSERT INTO sample_products (product_id, name, price, status, in_stock, updated_at)
SELECT i, 'Product ' || i, round(1 + (i * 7919 % 50000) / 100.0, 2),
       CASE WHEN i % 10 = 0 THEN 'retired' ELSE 'active' END, i % 4 <> 0,
       timestamp '2025-01-01' + i * interval '1 minute'
FROM generate_series(1, 500) AS i;

INSERT INTO sample_orders (order_id, customer_id, product_id, quantity, total, note, ordered_at)
SELECT i, 1 + i % 200, 1 + i % 500, 1 + i % 5,
       round((1 + i % 5) * (1 + (i * 31 % 9000) / 100.0), 2),
       CASE WHEN i % 7 = 0 THEN NULL ELSE 'order ' || i END,
       timestamp '2025-02-01' + i * interval '1 minute'
FROM generate_series(1, 5000) AS i;
