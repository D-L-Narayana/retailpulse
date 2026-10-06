"""Schema, loader and SQL-access tests for ``retailpulse.db``.

The shared ``conn`` fixture (one session-scoped database handed out per test by
``tests/conftest.py``) is used by every other test module and is never mutated here:
writable scenarios run on a ``Connection.backup()`` copy or on a temporary file database
under ``tmp_path``.
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from pathlib import Path

import pytest

from retailpulse import db

TABLES = ("stores", "products", "customers", "orders", "order_items")
HOT_TABLES = frozenset({"orders", "order_items"})
# Aliases used inside the v_sales view body; they appear verbatim in EXPLAIN output when the view is flattened.
VIEW_ALIASES = {"oi": "order_items", "o": "orders", "p": "products"}
V_SALES_BASE_COLUMNS = [
    "item_id", "order_id", "customer_id", "store_id", "channel", "order_date", "order_month",
    "product_id", "category", "quantity", "net_revenue", "gross_margin",
]
V_SALES_ADDED_COLUMNS = ["unit_price", "discount", "unit_cost", "sku"]
MALFORMED_DATES = ["2024-1-05", "20240105", "2024/01/05", "05-01-2024", "2024-01-05T00:00", "2024-01-0X", ""]


def test_all_tables_loaded(conn, data):
    for t in ("stores", "products", "customers", "orders", "order_items"):
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert n == len(data[t])


def test_fk_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO orders VALUES (999999, 999999, 1, 'online', '2024-01-01')")


def test_check_constraints(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO order_items VALUES (999999, 1, 1, 0, 10, 0)")  # quantity > 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO orders VALUES (999998, 1, 1, 'carrier_pigeon', '2024-01-01')")


def test_view_revenue_matches_manual(conn):
    v = conn.execute("SELECT ROUND(SUM(net_revenue),2) FROM v_sales").fetchone()[0]
    m = conn.execute("SELECT ROUND(SUM(quantity*(unit_price-discount)),2) FROM order_items").fetchone()[0]
    assert v == m


def test_indexes_used_for_customer_lookup(conn):
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM orders WHERE customer_id = 5").fetchall()
    assert any("idx_orders_customer" in row[3] for row in plan)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _raised(fn, *args, **kwargs):
    """Call ``fn``; return the exception it raised, or None when it returned normally."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        return exc
    return None


def _writable_copy(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Copy the shared fixture into a fresh in-memory database that a test may mutate.

    ``Connection.backup()`` needs a read snapshot of the source. A failed INSERT (see the constraint
    tests above) leaves Python's implicit transaction open on the shared connection, and backing up an
    in-memory database from a connection that holds an open write transaction blocks forever, so any
    such leftover is rolled back first - nothing was ever committed through it.
    """
    if conn.in_transaction:
        conn.rollback()
    dst = sqlite3.connect(":memory:")
    conn.backup(dst)
    dst.row_factory = sqlite3.Row
    dst.execute("PRAGMA foreign_keys = ON")
    return dst


def _assert_integrity_error(c: sqlite3.Connection, sql: str, params: tuple, label: str) -> None:
    exc = _raised(c.execute, sql, params)
    assert isinstance(exc, sqlite3.IntegrityError), f"{label}: expected IntegrityError, got {exc!r}"


def _count(c: sqlite3.Connection, table: str) -> int:
    return int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _pragmas(c: sqlite3.Connection) -> tuple[int, str]:
    return (c.execute("PRAGMA synchronous").fetchone()[0], c.execute("PRAGMA journal_mode").fetchone()[0].lower())


# -- EXPLAIN QUERY PLAN analysis ---------------------------------------------------------
#
# Plan rows are (id, parent, detail). Loop nodes read ``SCAN <src>`` / ``SEARCH <src> USING ...``;
# SQLite < 3.36 prints ``SCAN TABLE orders AS o`` and newer releases print the alias only (``SCAN o``),
# so sources are resolved through the FROM/JOIN aliases of the statement (plus the view's own aliases).

_LOOP = re.compile(r"^(SCAN|SEARCH)\s+(?:TABLE\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
_SOURCE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*)"
    r"(?:\s+(?:AS\s+)?(?!(?:ON|USING|JOIN|LEFT|INNER|CROSS|NATURAL|WHERE|GROUP|ORDER|LIMIT|UNION|HAVING|WINDOW)\b)"
    r"([A-Za-z_]\w*))?",
    re.IGNORECASE,
)


def _strip_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _alias_map(sql: str) -> dict[str, str]:
    """Map every FROM/JOIN alias in ``sql`` to its source; statement aliases win over view-internal ones."""
    amap: dict[str, str] = {}
    for source, alias in _SOURCE.findall(_strip_comments(sql)):
        if alias:
            amap.setdefault(alias, source)
    for alias, table in VIEW_ALIASES.items():
        amap.setdefault(alias, table)
    return amap


_INDEX_HINTS = (
    ("idx_orders_", "orders"),
    ("sqlite_autoindex_orders_", "orders"),
    ("idx_items_", "order_items"),
    ("sqlite_autoindex_order_items_", "order_items"),
)


def _base_table(detail: str, amap: dict[str, str]) -> str | None:
    """Base table read by a SCAN/SEARCH node, or None for subqueries, CTEs and constant rows."""
    m = _LOOP.match(detail)
    if not m:
        return None
    for hint, table in _INDEX_HINTS:  # an index name identifies the table even when aliases collide
        if hint in detail:
            return table
    for name in (m.group(3), m.group(2)):
        if name and amap.get(name, name) in TABLES:
            return amap.get(name, name)
    return None


def _plan(c: sqlite3.Connection, sql: str) -> list[tuple[int, int, str]]:
    return [(r[0], r[1], r[3]) for r in c.execute("EXPLAIN QUERY PLAN " + sql).fetchall()]


def _tree(rows: list[tuple[int, int, str]]) -> str:
    children: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for r in rows:
        children[r[1]].append(r)
    lines: list[str] = []

    def walk(parent: int, depth: int) -> None:
        for node in children.get(parent, []):
            lines.append("  " * depth + node[2])
            walk(node[0], depth + 1)

    walk(0, 0)
    return "\n".join(lines)


def _nested_hot_scans(rows: list[tuple[int, int, str]], amap: dict[str, str]) -> list[str]:
    """Full scans of orders/order_items that run once per row of an enclosing loop.

    A SCAN of a hot table is flagged when an earlier loop under the same parent reads a base table
    (so the scan repeats per outer row) or when it sits under a CORRELATED subquery. Scans that only
    follow single-row/CTE sources are not flagged because the plan text carries no row estimates.
    """
    by_id = {r[0]: r for r in rows}
    children: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for r in rows:
        children[r[1]].append(r)

    def correlated(node: tuple[int, int, str]) -> bool:
        parent = node[1]
        while parent in by_id:
            if by_id[parent][2].upper().startswith("CORRELATED"):
                return True
            parent = by_id[parent][1]
        return False

    flagged: list[str] = []
    for siblings in children.values():
        loops = [r for r in siblings if _LOOP.match(r[2])]
        for i, node in enumerate(loops):
            if not node[2].upper().startswith("SCAN") or _base_table(node[2], amap) not in HOT_TABLES:
                continue
            if any(_base_table(prev[2], amap) for prev in loops[:i]) or correlated(node):
                flagged.append(node[2])
    return flagged


# --------------------------------------------------------------------------------------
# load_sql / run_query validation
# --------------------------------------------------------------------------------------


BAD_QUERY_NAMES = [
    "nope",
    "../x",
    "../sql/01_monthly_revenue",
    str(db.SQL_DIR / "01_monthly_revenue"),  # absolute path to a real query file
    "01_monthly_revenue.sql",
    "",
]


@pytest.mark.parametrize("bad", BAD_QUERY_NAMES)
def test_load_sql_rejects_bad_names(bad):
    exc = _raised(db.load_sql, bad)
    assert isinstance(exc, KeyError), f"load_sql({bad!r}) should raise KeyError, got {exc!r}"
    for valid in db.list_queries():
        assert valid in str(exc)  # the message lists every valid name


def test_load_sql_returns_query_text_for_valid_names():
    names = db.list_queries()
    assert names == sorted(names) and names
    for name in names:
        text = db.load_sql(name)
        assert text == (db.SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")
        assert "select" in text.lower()


@pytest.mark.parametrize("bad", [str(db.SQL_DIR / "01_monthly_revenue"), "../sql/01_monthly_revenue", "nope"])
def test_run_query_rejects_paths_and_unknown_names(conn, bad):
    exc = _raised(db.run_query, conn, bad)
    assert isinstance(exc, KeyError), f"run_query({bad!r}) should raise KeyError, got {exc!r}"


def test_run_query_still_returns_dict_rows(conn):
    for name in db.list_queries():
        rows = db.run_query(conn, name)
        assert isinstance(rows, list)
        assert all(isinstance(r, dict) and all(isinstance(k, str) for k in r) for r in rows)
    assert db.run_query(conn, "08_data_quality", params=None)


# --------------------------------------------------------------------------------------
# schema: ISO date CHECKs, user_version, v_sales additive columns
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad_date", MALFORMED_DATES)
def test_date_check_rejects_malformed_order_date(conn, bad_date):
    c = _writable_copy(conn)
    _assert_integrity_error(
        c, "INSERT INTO orders VALUES (999999, 1, 1, 'online', ?)", (bad_date,), f"order_date={bad_date!r}"
    )
    assert _count(c, "orders") == _count(conn, "orders")


@pytest.mark.parametrize("bad_date", MALFORMED_DATES)
def test_date_check_rejects_malformed_signup_date(conn, bad_date):
    c = _writable_copy(conn)
    _assert_integrity_error(
        c,
        "INSERT INTO customers VALUES (999999, 'date-check@example.com', 'West', ?)",
        (bad_date,),
        f"signup_date={bad_date!r}",
    )
    assert _count(c, "customers") == _count(conn, "customers")


def test_date_check_accepts_iso_dates(conn):
    c = _writable_copy(conn)
    c.execute("INSERT INTO customers VALUES (999999, 'iso-check@example.com', 'West', '2024-01-05')")
    c.execute("INSERT INTO orders VALUES (999999, 999999, 1, 'online', '2024-01-05')")
    assert _count(c, "orders") == _count(conn, "orders") + 1
    assert _count(c, "customers") == _count(conn, "customers") + 1


def test_user_version_is_2(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    fresh = db.connect(":memory:")
    db.create_schema(fresh)
    assert fresh.execute("PRAGMA user_version").fetchone()[0] == 2


def test_v_sales_exposes_additive_columns_at_the_end(conn):
    cols = [d[0] for d in conn.execute("SELECT * FROM v_sales LIMIT 1").description]
    assert cols[: len(V_SALES_BASE_COLUMNS)] == V_SALES_BASE_COLUMNS  # existing columns keep name and position
    assert cols[len(V_SALES_BASE_COLUMNS):] == V_SALES_ADDED_COLUMNS


def test_v_sales_new_columns_match_source_tables_and_arithmetic(conn):
    cols = {d[0] for d in conn.execute("SELECT * FROM v_sales LIMIT 1").description}
    assert set(V_SALES_ADDED_COLUMNS) <= cols
    mismatched = conn.execute(
        """
        SELECT COUNT(*)
        FROM v_sales v
        JOIN order_items oi ON oi.item_id = v.item_id
        JOIN products p ON p.product_id = oi.product_id
        WHERE v.unit_price != oi.unit_price OR v.discount != oi.discount
           OR v.unit_cost != p.unit_cost OR v.sku != p.sku
        """
    ).fetchone()[0]
    assert mismatched == 0
    off = conn.execute(
        """
        SELECT COUNT(*) FROM v_sales
        WHERE ABS(net_revenue - quantity * (unit_price - discount)) > 1e-9
           OR ABS(gross_margin - quantity * (unit_price - discount - unit_cost)) > 1e-9
        """
    ).fetchone()[0]
    assert off == 0
    assert _count(conn, "v_sales") == _count(conn, "order_items")


# --------------------------------------------------------------------------------------
# EXPLAIN QUERY PLAN: hot joins must be index/PK driven
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("where", "indexes"),
    [
        ("orders WHERE customer_id = 5", ("idx_orders_customer",)),
        ("orders WHERE customer_id = 5 AND order_date >= '2024-06-01'", ("idx_orders_customer",)),
        ("orders WHERE store_id = 1", ("idx_orders_store",)),
        ("orders WHERE order_date BETWEEN '2024-06-01' AND '2024-06-30'", ("idx_orders_date",)),
        ("order_items WHERE order_id = 1", ("idx_items_order", "sqlite_autoindex_order_items_1")),
        ("order_items WHERE product_id = 1", ("idx_items_product",)),
    ],
)
def test_explain_point_lookups_use_an_index(conn, where, indexes):
    rows = _plan(conn, f"SELECT * FROM {where}")
    details = [r[2] for r in rows]
    assert any(d.upper().startswith("SEARCH") and any(ix in d for ix in indexes) for d in details), details


def test_explain_v_sales_join_is_driven_by_one_scan(conn):
    rows = _plan(conn, "SELECT * FROM v_sales")
    loops = [r[2] for r in rows if _LOOP.match(r[2])]
    assert len(loops) == 3, _tree(rows)
    assert sum(d.upper().startswith("SCAN") for d in loops) == 1, _tree(rows)
    assert all(d.upper().startswith("SEARCH") for d in loops if not d.upper().startswith("SCAN")), _tree(rows)


@pytest.mark.parametrize("name", db.list_queries())
def test_explain_no_nested_scan_of_orders_or_items(conn, name):
    sql = (db.SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")
    rows = _plan(conn, sql)
    assert rows
    flagged = _nested_hot_scans(rows, _alias_map(sql))
    assert not flagged, f"{name}: nested full scans {flagged}\n{_tree(rows)}"


def test_explain_detector_flags_nested_scan_when_index_is_missing(conn):
    sql = (
        "SELECT c.customer_id, COUNT(o.order_id) FROM customers c "
        "LEFT JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id"
    )
    healthy = _plan(conn, sql)
    assert not _nested_hot_scans(healthy, _alias_map(sql)), _tree(healthy)
    c = _writable_copy(conn)
    c.execute("PRAGMA automatic_index = OFF")
    c.execute("DROP INDEX idx_orders_customer")
    degraded = _plan(c, sql)
    assert _nested_hot_scans(degraded, _alias_map(sql)), _tree(degraded)


# --------------------------------------------------------------------------------------
# loader: transactional, tuned pragmas restored, ANALYZE
# --------------------------------------------------------------------------------------


def _with_orphan_item(data: dict[str, list[tuple]]) -> dict[str, list[tuple]]:
    """Copy of the data dict whose last order_items row references an order that does not exist."""
    corrupt = dict(data)
    orphan_order = max(o[0] for o in data["orders"]) + 1_000_000
    next_item = max(i[0] for i in data["order_items"]) + 1
    corrupt["order_items"] = [*data["order_items"], (next_item, orphan_order, 1, 1, 9.99, 0.0)]
    return corrupt


def test_load_is_transactional_on_fk_violation(data, tmp_path):
    corrupt = _with_orphan_item(data)
    path = tmp_path / "partial.db"
    c = db.connect(path)
    db.create_schema(c)
    before = _pragmas(c)
    exc = _raised(db.load, c, corrupt)
    assert isinstance(exc, sqlite3.IntegrityError), repr(exc)
    assert not c.in_transaction
    assert [_count(c, t) for t in TABLES] == [0] * len(TABLES)
    assert _pragmas(c) == before  # settings restored even when the load fails
    c.close()
    fresh = sqlite3.connect(path)
    assert [_count(fresh, t) for t in TABLES] == [0] * len(TABLES)
    fresh.close()


def test_load_uses_fast_pragmas_and_restores_them(data, tmp_path):
    seen: list[tuple[str, str]] = []

    def authorizer(action, arg1, arg2, _db_name, _trigger):
        if action == sqlite3.SQLITE_PRAGMA:
            seen.append(((arg1 or "").lower(), (arg2 or "").lower()))
        return sqlite3.SQLITE_OK

    c = db.connect(tmp_path / "tuned.db")
    db.create_schema(c)
    before = _pragmas(c)
    c.set_authorizer(authorizer)
    db.load(c, data)
    assert ("synchronous", "off") in seen, seen
    assert ("journal_mode", "memory") in seen, seen
    assert _pragmas(c) == before
    assert not c.in_transaction
    assert [_count(c, t) for t in TABLES] == [len(data[t]) for t in TABLES]
    assert c.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone()[0] > 0  # ANALYZE ran
    assert not (tmp_path / "tuned.db-journal").exists()
    c.close()


def test_load_inside_caller_transaction_leaves_commit_to_the_caller(data):
    c = db.connect(":memory:")
    db.create_schema(c)
    c.execute("BEGIN")
    c.execute("CREATE TABLE caller_marker (id INTEGER PRIMARY KEY)")
    db.load(c, data)
    assert c.in_transaction, "load() must not COMMIT a transaction it did not open"
    assert [_count(c, t) for t in TABLES] == [len(data[t]) for t in TABLES]  # rows are visible to the caller
    c.rollback()
    # The caller decided to roll back: its own work and every loaded row disappear together.
    assert [_count(c, t) for t in TABLES] == [0] * len(TABLES)
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'caller_marker'").fetchone()[0] == 0
    c.close()


def test_failing_load_inside_caller_transaction_keeps_the_callers_work(data):
    c = db.connect(":memory:")
    db.create_schema(c)
    c.execute("BEGIN")
    c.execute("INSERT INTO stores VALUES (999999, 'Caller store', 'West', 'standard')")
    exc = _raised(db.load, c, _with_orphan_item(data))
    assert isinstance(exc, sqlite3.IntegrityError), repr(exc)
    assert c.in_transaction, "load() must not ROLLBACK a transaction it did not open"
    assert _count(c, "stores") == 1  # the caller's row survives; load's own partial rows were undone
    assert [_count(c, t) for t in TABLES[1:]] == [0] * (len(TABLES) - 1)
    c.commit()
    assert _count(c, "stores") == 1
    c.close()


# --------------------------------------------------------------------------------------
# connect(read_only=True)
# --------------------------------------------------------------------------------------


def test_read_only_connection_refuses_writes(conn, tmp_path):
    path = tmp_path / "odd name #1 %20.db"
    file_conn = sqlite3.connect(path)
    conn.backup(file_conn)
    file_conn.close()
    ro = db.connect(path, read_only=True)
    try:
        assert _count(ro, "orders") == _count(conn, "orders")
        assert isinstance(ro.execute("SELECT * FROM stores LIMIT 1").fetchone(), sqlite3.Row)
        assert ro.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        exc = _raised(ro.execute, "INSERT INTO stores VALUES (999999, 'ro', 'West', 'standard')")
        assert isinstance(exc, sqlite3.OperationalError), f"write on read-only connection: {exc!r}"
        exc = _raised(ro.execute, "DELETE FROM order_items")
        assert isinstance(exc, sqlite3.OperationalError), f"delete on read-only connection: {exc!r}"
        assert _count(ro, "order_items") == _count(conn, "order_items")
    finally:
        ro.close()


def test_read_only_memory_database_is_rejected():
    exc = _raised(db.connect, ":memory:", read_only=True)
    assert isinstance(exc, ValueError), repr(exc)


def test_read_only_missing_file_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    exc = _raised(db.connect, missing, read_only=True)
    assert isinstance(exc, sqlite3.OperationalError), repr(exc)
    assert not missing.exists()


def test_default_connect_still_writable(tmp_path):
    c = db.connect(Path(tmp_path) / "rw.db")
    db.create_schema(c)
    c.execute("INSERT INTO stores VALUES (1, 'Store 001', 'West', 'standard')")
    c.commit()
    assert _count(c, "stores") == 1
    c.close()
