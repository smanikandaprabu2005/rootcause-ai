-- MySQL 8 schema for the demo shop. Runs once, on first start, via /docker-entrypoint-initdb.d.
CREATE TABLE IF NOT EXISTS items (
    id    INT AUTO_INCREMENT PRIMARY KEY,
    name  VARCHAR(100)  NOT NULL,
    price DECIMAL(10, 2) NOT NULL,
    stock INT           NOT NULL
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS orders (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    item_id    INT         NOT NULL,
    qty        INT         NOT NULL,
    status     VARCHAR(20) NOT NULL,
    created_at TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX ix_orders_status (status)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS payments (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    order_id   INT            NOT NULL,
    amount     DECIMAL(10, 2) NOT NULL,
    status     VARCHAR(20)    NOT NULL,
    created_at TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX ix_payments_order (order_id)
) ENGINE = InnoDB;

-- Large stock so the load generator never runs the catalogue dry mid-benchmark.
INSERT INTO items (name, price, stock) VALUES
    ('Item 1', 8.00, 100000000),
    ('Item 2', 11.00, 100000000),
    ('Item 3', 14.00, 100000000),
    ('Item 4', 17.00, 100000000),
    ('Item 5', 20.00, 100000000),
    ('Item 6', 23.00, 100000000),
    ('Item 7', 26.00, 100000000),
    ('Item 8', 29.00, 100000000),
    ('Item 9', 32.00, 100000000),
    ('Item 10', 35.00, 100000000),
    ('Item 11', 38.00, 100000000),
    ('Item 12', 41.00, 100000000),
    ('Item 13', 44.00, 100000000),
    ('Item 14', 47.00, 100000000),
    ('Item 15', 50.00, 100000000),
    ('Item 16', 53.00, 100000000),
    ('Item 17', 56.00, 100000000),
    ('Item 18', 59.00, 100000000),
    ('Item 19', 62.00, 100000000),
    ('Item 20', 65.00, 100000000);
