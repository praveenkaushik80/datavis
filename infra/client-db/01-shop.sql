-- Sample client database for the demo ("shop"). Replace with your own database in real use.
CREATE SCHEMA IF NOT EXISTS sales;

CREATE TABLE sales.customers (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  email       TEXT,
  country     CHAR(2) NOT NULL,
  segment     TEXT NOT NULL CHECK (segment IN ('consumer', 'business')),
  created_at  DATE NOT NULL
);
COMMENT ON TABLE sales.customers IS 'Registered customers';

CREATE TABLE sales.products (
  id         SERIAL PRIMARY KEY,
  sku        TEXT UNIQUE NOT NULL,
  name       TEXT NOT NULL,
  category   TEXT NOT NULL,
  list_price NUMERIC(10,2) NOT NULL
);

CREATE TABLE sales.orders (
  id           SERIAL PRIMARY KEY,
  customer_id  INT NOT NULL REFERENCES sales.customers(id),
  status       TEXT NOT NULL CHECK (status IN ('paid', 'refunded', 'cancelled')),
  ordered_at   DATE NOT NULL,
  channel      TEXT NOT NULL
);

CREATE TABLE sales.order_items (
  id          SERIAL PRIMARY KEY,
  order_id    INT NOT NULL REFERENCES sales.orders(id),
  product_id  INT NOT NULL REFERENCES sales.products(id),
  quantity    INT NOT NULL,
  unit_price  NUMERIC(10,2) NOT NULL
);

INSERT INTO sales.customers (name, email, country, segment, created_at)
SELECT 'Customer ' || g, 'customer' || g || '@example.com',
       (ARRAY['IN','GB','DE','US','FR'])[1 + g % 5], (ARRAY['consumer','business'])[1 + g % 2],
       DATE '2025-01-01' + (g % 365)
FROM generate_series(1, 200) g;

INSERT INTO sales.products (sku, name, category, list_price)
SELECT 'SKU-' || g, 'Product ' || g, (ARRAY['hardware','software','services'])[1 + g % 3], 10 + (g * 7) % 490
FROM generate_series(1, 40) g;

INSERT INTO sales.orders (customer_id, status, ordered_at, channel)
SELECT 1 + (g * 13) % 200, (ARRAY['paid','paid','paid','refunded','cancelled'])[1 + g % 5],
       DATE '2026-01-01' + (g % 260), (ARRAY['web','store','partner'])[1 + g % 3]
FROM generate_series(1, 1500) g;

INSERT INTO sales.order_items (order_id, product_id, quantity, unit_price)
SELECT 1 + (g % 1500), 1 + (g * 7) % 40, 1 + g % 4, 10 + (g * 11) % 490
FROM generate_series(1, 4000) g;

-- Read-only account used by DataFusion (connection check, catalogue, MCP queries).
CREATE ROLE datafusion_ro LOGIN PASSWORD 'readonly-demo-password';
GRANT CONNECT ON DATABASE shop TO datafusion_ro;
GRANT USAGE ON SCHEMA sales TO datafusion_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA sales TO datafusion_ro;
ALTER ROLE datafusion_ro SET default_transaction_read_only = on;
