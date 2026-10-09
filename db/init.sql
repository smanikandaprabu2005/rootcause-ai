CREATE TABLE IF NOT EXISTS items (
    id    SERIAL PRIMARY KEY,
    name  TEXT NOT NULL,
    price NUMERIC(10, 2) NOT NULL,
    stock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id         SERIAL PRIMARY KEY,
    item_id    INTEGER NOT NULL,
    qty        INTEGER NOT NULL,
    status     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payments (
    id         SERIAL PRIMARY KEY,
    order_id   INTEGER NOT NULL,
    amount     NUMERIC(10, 2) NOT NULL,
    status     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Large stock so the load generator never runs the catalogue dry mid-benchmark.
INSERT INTO items (name, price, stock)
SELECT 'Item ' || g, (5 + (g * 3) % 95)::numeric, 100000000
FROM generate_series(1, 20) AS g;
