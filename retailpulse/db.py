"""SQLite schema, loading and query execution helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SQL_DIR = Path(__file__).parent / "sql"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE stores (
    store_id   INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    region     TEXT NOT NULL,
    format     TEXT NOT NULL CHECK (format IN ('standard','flagship','small_format'))
);

CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    sku        TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL,
    list_price REAL NOT NULL CHECK (list_price > 0),
    unit_cost  REAL NOT NULL CHECK (unit_cost >= 0 AND unit_cost <= list_price)
);

CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    region      TEXT NOT NULL,
    signup_date TEXT NOT NULL
);

CREATE TABLE orders (
    order_id    INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    store_id    INTEGER NOT NULL REFERENCES stores(store_id),
    channel     TEXT NOT NULL CHECK (channel IN ('in_store','online','pickup')),
    order_date  TEXT NOT NULL
);

CREATE TABLE order_items (
    item_id    INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    quantity   INTEGER NOT NULL CHECK (quantity > 0),
    unit_price REAL NOT NULL CHECK (unit_price >= 0),
    discount   REAL NOT NULL DEFAULT 0 CHECK (discount >= 0 AND discount <= unit_price),
    UNIQUE (order_id, product_id)
);

CREATE INDEX idx_orders_customer ON orders(customer_id, order_date);
CREATE INDEX idx_orders_date     ON orders(order_date);
CREATE INDEX idx_orders_store    ON orders(store_id);
CREATE INDEX idx_items_order     ON order_items(order_id);
CREATE INDEX idx_items_product   ON order_items(product_id);

-- Line-level revenue view keeps arithmetic in exactly one place.
CREATE VIEW v_sales AS
SELECT oi.item_id, o.order_id, o.customer_id, o.store_id, o.channel,
       o.order_date, substr(o.order_date, 1, 7) AS order_month,
       oi.product_id, p.category, oi.quantity,
       oi.quantity * (oi.unit_price - oi.discount)          AS net_revenue,
       oi.quantity * (oi.unit_price - oi.discount - p.unit_cost) AS gross_margin
FROM order_items oi
JOIN orders   o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id;
"""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def load(conn: sqlite3.Connection, data: dict[str, list[tuple]]) -> None:
    with conn:
        conn.executemany("INSERT INTO stores VALUES (?,?,?,?)", data["stores"])
        conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", data["products"])
        conn.executemany("INSERT INTO customers VALUES (?,?,?,?)", data["customers"])
        conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", data["orders"])
        conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?)", data["order_items"])
    conn.execute("ANALYZE")


def list_queries() -> list[str]:
    return sorted(p.stem for p in SQL_DIR.glob("*.sql"))


def run_query(conn: sqlite3.Connection, name: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    sql = (SQL_DIR / f"{name}.sql").read_text()
    cur = conn.execute(sql, params or {})
    return [dict(r) for r in cur.fetchall()]
