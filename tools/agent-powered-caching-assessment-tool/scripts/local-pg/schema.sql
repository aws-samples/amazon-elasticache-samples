-- The shop database for scripts/local-pg. Runs once, when the data volume is new.
-- Shaped so the tables score differently:
--   shop.product     wide and larger than shared_buffers, read by key all the time, rarely
--                    written: should be a strong object-caching candidate
--   shop.category    tiny and always in memory: likely "already well served"
--   shop.customer    read by key, sometimes updated
--   shop.inventory   updated on almost every order: writes should rule it out
--   shop.orders, shop.order_item  growing, read for a customer's recent orders
--   top sellers      an expensive aggregate repeated with few distinct parameters: a
--                    query-result caching candidate
CREATE DATABASE shop;
\connect shop
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE SCHEMA shop;

CREATE TABLE shop.category (
  category_id int PRIMARY KEY,
  name text NOT NULL,
  slug text NOT NULL
);
INSERT INTO shop.category
SELECT g, 'Category ' || g, 'category-' || g FROM generate_series(1, 24) g;

CREATE TABLE shop.product (
  product_id bigint PRIMARY KEY,
  category_id int NOT NULL REFERENCES shop.category,
  sku text NOT NULL UNIQUE,
  name text NOT NULL,
  description text NOT NULL,
  unit_price numeric(10,2) NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO shop.product
SELECT g, 1 + (g % 24), 'SKU-' || lpad(g::text, 8, '0'), 'Product ' || g,
       repeat(md5(g::text), 10), round((5 + random() * 495)::numeric, 2), true, now()
FROM generate_series(1, 300000) g;
CREATE INDEX ON shop.product (category_id);

CREATE TABLE shop.customer (
  customer_id bigint PRIMARY KEY,
  email text NOT NULL UNIQUE,
  full_name text NOT NULL,
  loyalty_points int NOT NULL DEFAULT 0
);
INSERT INTO shop.customer
SELECT g, 'customer' || g || '@example.com', 'Customer ' || g, 0
FROM generate_series(1, 50000) g;

CREATE TABLE shop.inventory (
  product_id bigint PRIMARY KEY REFERENCES shop.product,
  on_hand int NOT NULL
);
INSERT INTO shop.inventory SELECT product_id, 1000 FROM shop.product;

CREATE TABLE shop.orders (
  order_id bigserial PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES shop.customer,
  ordered_at timestamptz NOT NULL DEFAULT now(),
  total numeric(12,2) NOT NULL
);
CREATE INDEX ON shop.orders (customer_id, ordered_at DESC);
CREATE INDEX ON shop.orders (ordered_at);

CREATE TABLE shop.order_item (
  order_id bigint NOT NULL REFERENCES shop.orders,
  product_id bigint NOT NULL REFERENCES shop.product,
  qty int NOT NULL,
  price numeric(10,2) NOT NULL,
  PRIMARY KEY (order_id, product_id)
);
CREATE INDEX ON shop.order_item (product_id);

-- Some history so the reads have something to find.
INSERT INTO shop.orders (customer_id, ordered_at, total)
SELECT 1 + (random() * 49999)::int, now() - random() * interval '30 days', 0
FROM generate_series(1, 60000);
INSERT INTO shop.order_item
SELECT o.order_id, 1 + (random() * 299999)::int, 1 + (random() * 3)::int, 20
FROM shop.orders o ON CONFLICT DO NOTHING;

ANALYZE;
