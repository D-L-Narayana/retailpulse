"""Negative tests for ``sql/08_data_quality.sql``.

The lead-owned ``tests/test_queries.py`` proves that the clean fixture reports zero ``error`` violations.
This module proves the other half of the contract: every check *detects* the corruption it is named after.

Each test copies the session fixture into a fresh in-memory database with ``Connection.backup`` (the shared
fixture is never mutated), switches foreign-key enforcement off - and, where a CHECK constraint stands in the
way, check constraints too - injects exactly one corruption and re-runs the query.  The set of ``error``
checks that fire must then equal the expected set: the targeted check plus any check that is mathematically
implied by the same corruption (a discount larger than the unit price *is* negative net revenue, so both fire).
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable

import pytest

from retailpulse import db, generate

QUERY = "08_data_quality"
COLUMNS = ["check_name", "severity", "violations", "description"]

ERROR_CHECKS = frozenset(
    {
        # the five baseline checks
        "orders_without_items",
        "orders_before_signup",
        "negative_net_revenue",
        "orphan_items",
        "duplicate_emails",
        # referential integrity beyond the FK the loader already enforces
        "orphan_order_customers",
        "orphan_order_stores",
        "orphan_item_products",
        # temporal and pricing invariants
        "malformed_order_dates",
        "malformed_signup_dates",
        "discount_exceeds_price",
        "nonpositive_quantity",
    }
)
WARN_CHECKS = frozenset(
    {"negative_gross_margin", "products_never_sold", "stores_without_orders", "customers_without_orders"}
)

# One value per failure mode: month 13, free text, a day that does not exist (SQLite's date() silently rolls
# 2024-02-30 forward to 2024-03-01, so a NULL test alone would miss it) and the wrong separator.
BAD_DATES = ("2024-13-45", "not-a-date", "2024-02-30", "2024/05/17")

Injector = Callable[[sqlite3.Connection], None]


# --------------------------------------------------------------------------- helpers


def _copy(conn: sqlite3.Connection, *, bypass_checks: bool = False) -> sqlite3.Connection:
    """Writable in-memory copy of the session database with FK enforcement off (never the fixture itself)."""
    dst = sqlite3.connect(":memory:")
    dst.row_factory = sqlite3.Row
    if conn.in_transaction:
        # A failed INSERT on the shared connection (e.g. a constraint test) leaves an implicit write transaction
        # open.  sqlite3_backup_step() answers SQLITE_BUSY for as long as the source holds a write transaction and
        # Connection.backup() retries forever, so take a read-only logical copy instead of hanging the suite.
        dst.executescript("\n".join(conn.iterdump()))
    else:
        conn.backup(dst)
    dst.execute("PRAGMA foreign_keys = OFF")
    if bypass_checks:
        dst.execute("PRAGMA ignore_check_constraints = ON")
    return dst


def _rows(conn: sqlite3.Connection) -> list[dict]:
    return db.run_query(conn, QUERY)


def _violations(rows: list[dict]) -> dict[str, int]:
    return {r["check_name"]: r["violations"] for r in rows}


def _tripped_errors(rows: list[dict]) -> set[str]:
    """Names of the ``error``-severity checks reporting at least one violation."""
    for r in rows:
        assert "severity" in r, f"severity column missing from {sorted(r)}"
    return {r["check_name"] for r in rows if r["severity"] == "error" and r["violations"] > 0}


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    return conn.execute(sql, params).fetchone()[0]


def _first_order_id(conn: sqlite3.Connection) -> int:
    return _scalar(conn, "SELECT MIN(order_id) FROM orders")


def _first_item_id(conn: sqlite3.Connection) -> int:
    return _scalar(conn, "SELECT MIN(item_id) FROM order_items")


# --------------------------------------------------------------------------- one corruption each


def inject_orders_without_items(c: sqlite3.Connection) -> None:
    # A brand-new order that copies a real order's customer, store and date so nothing else can fire.
    c.execute(
        "INSERT INTO orders (order_id, customer_id, store_id, channel, order_date) "
        "SELECT 999999, customer_id, store_id, channel, order_date FROM orders ORDER BY order_id LIMIT 1"
    )


def inject_orders_before_signup(c: sqlite3.Connection) -> None:
    c.execute("UPDATE orders SET order_date = '2000-01-01' WHERE order_id = ?", (_first_order_id(c),))


def inject_negative_quantity(c: sqlite3.Connection) -> None:
    # A line with a positive net unit price, so the negative quantity is guaranteed to flip net revenue negative.
    item_id = _scalar(c, "SELECT MIN(item_id) FROM order_items WHERE unit_price - discount > 0")
    assert item_id is not None
    c.execute("UPDATE order_items SET quantity = -2 WHERE item_id = ?", (item_id,))


def inject_orphan_items(c: sqlite3.Connection) -> None:
    c.execute(
        "INSERT INTO order_items (item_id, order_id, product_id, quantity, unit_price, discount) "
        "SELECT 999999, 999999, MIN(product_id), 1, 10.0, 0.0 FROM products"
    )


def inject_orphan_item_products(c: sqlite3.Connection) -> None:
    c.execute(
        "INSERT INTO order_items (item_id, order_id, product_id, quantity, unit_price, discount) "
        "SELECT 999999, MIN(order_id), 999999, 1, 10.0, 0.0 FROM orders"
    )


def inject_orphan_order_customers(c: sqlite3.Connection) -> None:
    c.execute("UPDATE orders SET customer_id = 999999 WHERE order_id = ?", (_first_order_id(c),))


def inject_orphan_order_stores(c: sqlite3.Connection) -> None:
    c.execute("UPDATE orders SET store_id = 999999 WHERE order_id = ?", (_first_order_id(c),))


def _drop_email_uniqueness(c: sqlite3.Connection) -> None:
    """UNIQUE is not covered by ``PRAGMA ignore_check_constraints``: rebuild customers without constraints (copy only).

    ``CREATE TABLE ... AS SELECT`` keeps every column but no PRIMARY KEY / UNIQUE / CHECK, whatever the schema version.
    """
    c.executescript(
        "CREATE TABLE customers_dup AS SELECT * FROM customers;"
        "DROP TABLE customers;"
        "ALTER TABLE customers_dup RENAME TO customers;"
    )


def inject_duplicate_emails(c: sqlite3.Connection) -> None:
    _drop_email_uniqueness(c)
    first, second = [r[0] for r in c.execute("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 2")]
    c.execute(
        "UPDATE customers SET email = (SELECT email FROM customers WHERE customer_id = ?) WHERE customer_id = ?",
        (first, second),
    )


def inject_malformed_order_dates(c: sqlite3.Connection) -> None:
    c.execute("UPDATE orders SET order_date = '2024-13-45' WHERE order_id = ?", (_first_order_id(c),))


def inject_malformed_signup_dates(c: sqlite3.Connection) -> None:
    # The customer behind the first order certainly has orders, which also proves orders_before_signup stays quiet.
    cid = _scalar(c, "SELECT customer_id FROM orders ORDER BY order_id LIMIT 1")
    c.execute("UPDATE customers SET signup_date = 'not-a-date' WHERE customer_id = ?", (cid,))


def inject_discount_exceeds_price(c: sqlite3.Connection) -> None:
    c.execute("UPDATE order_items SET discount = unit_price + 1 WHERE item_id = ?", (_first_item_id(c),))


def inject_zero_quantity(c: sqlite3.Connection) -> None:
    c.execute("UPDATE order_items SET quantity = 0 WHERE item_id = ?", (_first_item_id(c),))


# (targeted check, corruption, bypass CHECK constraints on the copy, exact set of error checks expected to fire)
CASES: list[tuple[str, Injector, bool, frozenset[str]]] = [
    ("orders_without_items", inject_orders_without_items, False, frozenset({"orders_without_items"})),
    ("orders_before_signup", inject_orders_before_signup, False, frozenset({"orders_before_signup"})),
    # Net revenue can only turn negative through a non-positive quantity or an oversized discount, so the
    # implied check necessarily fires alongside it (and vice versa for discount_exceeds_price below).
    (
        "negative_net_revenue",
        inject_negative_quantity,
        True,
        frozenset({"negative_net_revenue", "nonpositive_quantity"}),
    ),
    ("orphan_items", inject_orphan_items, False, frozenset({"orphan_items"})),
    ("duplicate_emails", inject_duplicate_emails, False, frozenset({"duplicate_emails"})),
    ("orphan_order_customers", inject_orphan_order_customers, False, frozenset({"orphan_order_customers"})),
    ("orphan_order_stores", inject_orphan_order_stores, False, frozenset({"orphan_order_stores"})),
    ("orphan_item_products", inject_orphan_item_products, False, frozenset({"orphan_item_products"})),
    ("malformed_order_dates", inject_malformed_order_dates, True, frozenset({"malformed_order_dates"})),
    ("malformed_signup_dates", inject_malformed_signup_dates, True, frozenset({"malformed_signup_dates"})),
    (
        "discount_exceeds_price",
        inject_discount_exceeds_price,
        True,
        frozenset({"discount_exceeds_price", "negative_net_revenue"}),
    ),
    ("nonpositive_quantity", inject_zero_quantity, True, frozenset({"nonpositive_quantity"})),
]


# --------------------------------------------------------------------------- contract: columns, severities, text


def test_result_columns_exact_order(conn):
    rows = _rows(conn)
    assert rows
    for r in rows:
        assert list(r) == COLUMNS, list(r)


def test_catalogue_is_complete_and_errors_come_first(conn):
    rows = _rows(conn)
    names = [r["check_name"] for r in rows]
    assert len(names) == len(set(names)), "duplicate check names"
    missing = sorted((ERROR_CHECKS | WARN_CHECKS) - set(names))
    assert not missing, f"checks missing from the catalogue: {missing}"
    for r in rows:
        assert "severity" in r, f"severity column missing from {sorted(r)}"
    severities = [r["severity"] for r in rows]
    assert severities == sorted(severities), "error rows must precede warn rows"  # 'error' < 'warn'
    assert severities.count("error") >= len(ERROR_CHECKS) and severities.count("warn") >= len(WARN_CHECKS)


def test_severity_values_are_valid(conn):
    rows = _rows(conn)
    for r in rows:
        assert "severity" in r, f"severity column missing from {sorted(r)}"
        assert r["severity"] in {"error", "warn"}, r
        if r["check_name"] in ERROR_CHECKS:
            assert r["severity"] == "error", r
        if r["check_name"] in WARN_CHECKS:
            assert r["severity"] == "warn", r
        assert isinstance(r["violations"], int) and r["violations"] >= 0, r


def test_descriptions_are_single_sentences(conn):
    for r in _rows(conn):
        assert "description" in r, f"description column missing from {sorted(r)}"
        d = r["description"]
        assert isinstance(d, str) and d and d == d.strip(), r
        assert d[0].isupper() and d.endswith(".") and d.count(".") == 1, d
        assert len(d.split()) >= 5, d


def test_clean_fixture_reports_no_errors(conn):
    rows = _rows(conn)
    assert _tripped_errors(rows) == set()
    # PLAN 3.5: the build fails only on the error rows, warn rows are informational.
    assert sum(r["violations"] for r in rows if r["severity"] == "error") == 0
    assert all(isinstance(r["violations"], int) for r in rows)


def test_copy_of_fixture_is_clean_before_injection(conn):
    c = _copy(conn, bypass_checks=True)
    assert _tripped_errors(_rows(c)) == set()


def test_copy_survives_a_leaked_write_transaction_on_the_source():
    """Connection.backup() never returns while the source holds a write transaction (SQLITE_BUSY retry loop).

    A constraint test that lets an INSERT fail on the shared connection leaves exactly that state behind, so the
    copy helper must detect it and still deliver a faithful, writable copy without blocking the suite.
    """
    src = db.connect(":memory:")
    db.create_schema(src)
    db.load(src, generate.generate(generate.Config(seed=11, customers=5, orders=20)))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            src.execute("INSERT INTO orders VALUES (999999, 999999, 1, 'online', '2024-01-01')")
        assert src.in_transaction  # the implicit BEGIN issued before the failing INSERT is still open
        c = _copy(src, bypass_checks=True)
        for table in ("stores", "products", "customers", "orders", "order_items"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == src.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0], table
        assert _tripped_errors(_rows(c)) == set()
        inject_orphan_items(c)  # the copy is writable and the source is untouched
        assert _tripped_errors(_rows(c)) == {"orphan_items"}
        assert src.execute("SELECT COUNT(*) FROM order_items WHERE item_id = 999999").fetchone()[0] == 0
    finally:
        src.rollback()


def test_runs_on_an_empty_database():
    c = db.connect(":memory:")
    db.create_schema(c)
    rows = _rows(c)
    assert [list(r) for r in rows] == [COLUMNS] * len(rows)
    assert ERROR_CHECKS | WARN_CHECKS <= {r["check_name"] for r in rows}
    assert all(r["violations"] == 0 for r in rows)


def test_sql_header_metadata_and_portability():
    text = (db.SQL_DIR / f"{QUERY}.sql").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines and lines[0].startswith("-- @"), lines[:1]
    header: dict[str, list[str]] = {}
    for line in lines:
        if not line.startswith("-- @"):
            break
        key, _, value = line[4:].partition(":")
        header.setdefault(key.strip(), []).append(value.strip())
    assert header.get("title") == ["Data-quality checks"], header
    assert header.get("chart") == ["table"], header
    assert header.get("limit") == ["30"], header
    assert header.get("technique") == ["anti-joins, invariants"], header
    assert header.get("question") and header["question"][0].endswith("?"), header
    assert len(header.get("portability", [])) >= 2, header
    # SQLite >= 3.31 floor (PLAN 2.3): nothing newer than window functions / GLOB / COALESCE.
    body = "\n".join(line for line in lines if not line.lstrip().startswith("--"))
    body = re.sub(r"'[^']*'", "''", body).upper()  # string literals (descriptions) are prose, not SQL constructs
    for forbidden in ("IIF(", "RETURNING", "->>", " STRICT", "RIGHT JOIN", "FULL JOIN"):
        assert forbidden not in body, forbidden


# --------------------------------------------------------------------------- detection: error checks


def test_every_error_check_has_an_injection_case(conn):
    rows = _rows(conn)
    for r in rows:
        assert "severity" in r, f"severity column missing from {sorted(r)}"
    error_rows = {r["check_name"] for r in rows if r["severity"] == "error"}
    assert {case[0] for case in CASES} == error_rows, "every error check needs exactly one targeted injection case"
    for target, _inject, _bypass, expected in CASES:
        assert target in expected and expected <= error_rows, target


@pytest.mark.parametrize(("target", "inject", "bypass_checks", "expected"), CASES, ids=[case[0] for case in CASES])
def test_injected_corruption_fires_exactly_the_expected_checks(conn, target, inject, bypass_checks, expected):
    c = _copy(conn, bypass_checks=bypass_checks)
    inject(c)
    rows = _rows(c)
    names = {r["check_name"] for r in rows}
    assert target in names, f"{target} is missing from the catalogue: {sorted(names)}"
    tripped = _tripped_errors(rows)
    assert target in tripped, f"{target} did not detect its own corruption: {_violations(rows)}"
    assert tripped == expected, f"unexpected set of error checks fired: {_violations(rows)}"
    assert _violations(rows)[target] >= 1


@pytest.mark.parametrize("bad", BAD_DATES)
@pytest.mark.parametrize(
    ("table", "key", "column", "check"),
    [
        ("orders", "order_id", "order_date", "malformed_order_dates"),
        ("customers", "customer_id", "signup_date", "malformed_signup_dates"),
    ],
)
def test_each_malformed_date_variant_is_flagged_exactly_once(conn, table, key, column, check, bad):
    c = _copy(conn, bypass_checks=True)
    first = _scalar(c, f"SELECT MIN({key}) FROM {table}")
    c.execute(f"UPDATE {table} SET {column} = ? WHERE {key} = ?", (bad, first))
    rows = _rows(c)
    assert check in {r["check_name"] for r in rows}, check
    assert _violations(rows)[check] == 1, _violations(rows)
    assert _tripped_errors(rows) == {check}, _violations(rows)


def test_duplicate_emails_ignores_case_and_whitespace(conn):
    # These two variants pass the binary UNIQUE index, so no schema surgery is needed on the copy.
    c = _copy(conn)
    first, second, third = [r[0] for r in c.execute("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 3")]
    base = _scalar(c, "SELECT email FROM customers WHERE customer_id = ?", (first,))
    c.execute("UPDATE customers SET email = ? WHERE customer_id = ?", (base.upper(), second))
    c.execute("UPDATE customers SET email = ? WHERE customer_id = ?", (f"  {base} ", third))
    rows = _rows(c)
    assert "duplicate_emails" in {r["check_name"] for r in rows}
    assert _violations(rows)["duplicate_emails"] == 2, _violations(rows)
    assert _tripped_errors(rows) == {"duplicate_emails"}


# --------------------------------------------------------------------------- detection: warn checks


def _assert_warn_increments(conn, check: str, corrupt: Injector) -> None:
    c = _copy(conn)
    before = _violations(_rows(c))
    assert check in before, f"{check} is missing from the catalogue: {sorted(before)}"
    corrupt(c)
    rows = _rows(c)
    assert _violations(rows)[check] == before[check] + 1, (before[check], _violations(rows)[check])
    assert {r["check_name"]: r["severity"] for r in rows}[check] == "warn"
    assert _tripped_errors(rows) == set(), "a warn-only corruption must not raise an error check"


def test_products_never_sold_counts_an_unsold_product(conn):
    def corrupt(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO products VALUES (999999, 'SKU-UNSOLD', 'Unsold item', 'Toys', 10.0, 5.0)")

    _assert_warn_increments(conn, "products_never_sold", corrupt)


def test_stores_without_orders_counts_a_new_store(conn):
    def corrupt(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO stores VALUES (999999, 'Store 999', 'West', 'standard')")

    _assert_warn_increments(conn, "stores_without_orders", corrupt)


def test_customers_without_orders_counts_a_new_customer(conn):
    def corrupt(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO customers VALUES (999999, 'nobody@example.com', 'West', '2024-06-01')")

    _assert_warn_increments(conn, "customers_without_orders", corrupt)


def test_negative_gross_margin_detects_a_sale_below_cost(conn):
    def corrupt(c: sqlite3.Connection) -> None:
        # A line currently sold at or above cost, re-priced at half its unit cost: still a valid row for every
        # CHECK constraint (price >= 0, discount <= price), but the gross margin is now negative.
        item_id = _scalar(
            c,
            "SELECT MIN(oi.item_id) FROM order_items oi JOIN products p ON p.product_id = oi.product_id "
            "WHERE p.unit_cost > 0 AND oi.unit_price - oi.discount - p.unit_cost >= 0",
        )
        assert item_id is not None
        c.execute(
            "UPDATE order_items SET discount = 0, "
            "unit_price = (SELECT unit_cost * 0.5 FROM products WHERE product_id = order_items.product_id) "
            "WHERE item_id = ?",
            (item_id,),
        )

    _assert_warn_increments(conn, "negative_gross_margin", corrupt)
