import sqlite3
import pytest
from retailpulse import db


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
