"""SQLite schema, loading and query execution helpers.

Schema version 2 (``PRAGMA user_version``): five tables with foreign keys and CHECK
constraints (ISO ``YYYY-MM-DD`` dates are enforced with ``GLOB``), five supporting
indexes and the ``v_sales`` view, which keeps the revenue and margin arithmetic in
exactly one place. Everything here is stdlib-only and stays within SQLite 3.31
features so the same files work with the interpreter's bundled library.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SQL_DIR = Path(__file__).parent / "sql"

# ``GLOB`` is case-sensitive and anchored, so the whole value must be exactly ten characters in
# YYYY-MM-DD shape. Portability: ``~ '^\d{4}-\d{2}-\d{2}$'`` (Postgres) / ``REGEXP_CONTAINS`` (BigQuery).
ISO_DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"

SCHEMA = f"""
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
    signup_date TEXT NOT NULL CHECK (signup_date GLOB '{ISO_DATE_GLOB}')
);

CREATE TABLE orders (
    order_id    INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    store_id    INTEGER NOT NULL REFERENCES stores(store_id),
    channel     TEXT NOT NULL CHECK (channel IN ('in_store','online','pickup')),
    order_date  TEXT NOT NULL CHECK (order_date GLOB '{ISO_DATE_GLOB}')
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
-- Columns are only ever appended (unit_price, discount, unit_cost, sku arrived in schema version 2),
-- so positional consumers of the original twelve columns keep working.
CREATE VIEW v_sales AS
SELECT oi.item_id, o.order_id, o.customer_id, o.store_id, o.channel,
       o.order_date, substr(o.order_date, 1, 7) AS order_month,
       oi.product_id, p.category, oi.quantity,
       oi.quantity * (oi.unit_price - oi.discount)          AS net_revenue,
       oi.quantity * (oi.unit_price - oi.discount - p.unit_cost) AS gross_margin,
       oi.unit_price, oi.discount, p.unit_cost, p.sku
FROM order_items oi
JOIN orders   o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id;

PRAGMA user_version = 2;
"""

# Insert statements in dependency order; the tuple arities (4/6/4/5/6) are the generator's contract.
_INSERTS: tuple[tuple[str, str], ...] = (
    ("stores", "INSERT INTO stores VALUES (?,?,?,?)"),
    ("products", "INSERT INTO products VALUES (?,?,?,?,?,?)"),
    ("customers", "INSERT INTO customers VALUES (?,?,?,?)"),
    ("orders", "INSERT INTO orders VALUES (?,?,?,?,?)"),
    ("order_items", "INSERT INTO order_items VALUES (?,?,?,?,?,?)"),
)
_JOURNAL_MODES = frozenset({"delete", "truncate", "persist", "memory", "wal", "off"})


class UnknownQueryError(KeyError):
    """Raised for a query name that is not one of :func:`list_queries`; ``str()`` lists the valid names."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


def connect(path: str | Path = ":memory:", *, read_only: bool = False) -> sqlite3.Connection:
    """Open ``path`` with ``sqlite3.Row`` rows and foreign keys enforced.

    ``read_only=True`` opens an existing file through the ``file:...?mode=ro`` URI, so writes raise
    ``sqlite3.OperationalError`` and a missing file is reported instead of being created. In-memory
    databases cannot be read-only (there would be nothing to read), so ``":memory:"`` raises ``ValueError``.
    """
    target = str(path)
    if read_only:
        if target in ("", ":memory:") or target.startswith("file::memory:"):
            raise ValueError("read_only=True needs a database file; ':memory:' cannot be opened read-only")
        # Path.as_uri() percent-encodes spaces, '#', '%' and '?' and yields file:///C:/... on Windows.
        conn = sqlite3.connect(Path(target).resolve().as_uri() + "?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _pragma(conn: sqlite3.Connection, statement: str) -> Any:
    """Run a PRAGMA and return its single value (``None`` when it produces no row), fully consuming the cursor."""
    rows = conn.execute(f"PRAGMA {statement}").fetchall()
    return rows[0][0] if rows else None


def _insert_rows(conn: sqlite3.Connection, data: dict[str, list[tuple]]) -> None:
    for table, sql in _INSERTS:
        conn.executemany(sql, data[table])


def load(conn: sqlite3.Connection, data: dict[str, list[tuple]]) -> None:
    """Bulk-load the generator's data dict atomically, then refresh planner statistics.

    Outside a transaction (the normal case) all rows go in one transaction opened and
    committed here: a failing row (FK or CHECK violation) rolls the whole batch back, so
    the tables are either completely loaded or untouched. While the rows stream in, the
    durability pragmas are relaxed (``synchronous=OFF``, ``journal_mode=MEMORY`` - a crash
    mid-load could only lose regenerable synthetic data) and the previous values are
    restored afterwards, even when the load fails.

    Inside a caller's transaction (``conn.in_transaction`` is true) the rows join it under
    a SAVEPOINT and the caller decides: ``load()`` never issues COMMIT or ROLLBACK on the
    caller's behalf, a failing batch is undone back to the savepoint (the caller's own
    work survives) and the error is re-raised. Pragmas are left alone because
    ``journal_mode`` cannot change inside a transaction. ``ANALYZE`` runs last either way.
    """
    if conn.in_transaction:
        conn.execute("SAVEPOINT retailpulse_load")
        try:
            _insert_rows(conn, data)
        except BaseException:
            conn.execute("ROLLBACK TO retailpulse_load")
            conn.execute("RELEASE retailpulse_load")
            raise
        conn.execute("RELEASE retailpulse_load")
        conn.execute("ANALYZE")
        return

    synchronous = int(_pragma(conn, "synchronous"))
    journal_mode = str(_pragma(conn, "journal_mode")).lower()
    _pragma(conn, "synchronous = OFF")
    _pragma(conn, "journal_mode = MEMORY")
    try:
        conn.execute("BEGIN")
        try:
            _insert_rows(conn, data)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        conn.execute("ANALYZE")
    finally:
        _pragma(conn, f"synchronous = {synchronous}")
        if journal_mode in _JOURNAL_MODES:
            _pragma(conn, f"journal_mode = {journal_mode}")


def list_queries() -> list[str]:
    return sorted(p.stem for p in SQL_DIR.glob("*.sql"))


def load_sql(name: str) -> str:
    """Return the text of ``sql/<name>.sql``; ``name`` must be exactly one of :func:`list_queries`.

    Paths (``../x``, absolute paths), file suffixes and unknown stems raise :class:`UnknownQueryError`
    (a ``KeyError``) whose message lists the valid names, so callers can safely pass user input.
    """
    names = list_queries()
    if name not in names:
        raise UnknownQueryError(f"unknown query {name!r}; valid names: {', '.join(names)}")
    return (SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")


def run_query(conn: sqlite3.Connection, name: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Execute the named query and return its rows as dicts (works with any ``row_factory``)."""
    cur = conn.execute(load_sql(name), params or {})
    if cur.description is None:
        return []
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
