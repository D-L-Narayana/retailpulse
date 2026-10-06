"""Invariant tests for the customer-lifecycle queries: 03 RFM, 04 cohort retention, 07 repeat rate.

Purpose-built databases are tiny in-memory SQLite connections created here with hand-computable
expectations; the session-scoped ``conn``/``data`` fixtures are only ever read.
"""
from __future__ import annotations

import re
import sqlite3
import statistics
from collections.abc import Callable, Sequence
from datetime import date

import pytest

from retailpulse import db

LIFECYCLE_QUERIES = ("03_customer_rfm", "04_cohort_retention", "07_repeat_purchase_rate")
CHANNELS = {"in_store", "online", "pickup"}
STORE = (1, "Store 001", "West", "standard")
PRODUCT = (1, "SKU-00001", "Grocery Item 1", "Grocery", 10.0, 5.0)

# 16 customers with heavily tied frequencies (and therefore tied monetary and recency values).
FREQ_PATTERN = (1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 5, 5)


def _month_index(ym: str) -> int:
    return int(ym[:4]) * 12 + int(ym[5:7])


def _customer(cid: int, email: str | None = None, signup: str = "2024-01-01") -> tuple:
    return (cid, email or f"customer{cid}@example.com", "West", signup)


def _build(customers: Sequence[tuple], orders: Sequence[tuple]) -> sqlite3.Connection:
    """One store, one product, the given customers/orders and exactly one 10.00 line per order."""
    conn = db.connect(":memory:")
    db.create_schema(conn)
    items = [(i, o[0], PRODUCT[0], 1, PRODUCT[4], 0.0) for i, o in enumerate(orders, start=1)]
    db.load(
        conn,
        {"stores": [STORE], "products": [PRODUCT], "customers": list(customers), "orders": list(orders),
         "order_items": items},
    )
    return conn


def _tied_dataset(id_of: Callable[[int], int]) -> tuple[list[tuple], list[tuple]]:
    """Customer k (labelled ``id_of(k)``) places FREQ_PATTERN[k] orders on the 15th of months 1..n."""
    customers, orders = [], []
    oid = 0
    for k, freq in enumerate(FREQ_PATTERN):
        customers.append(_customer(id_of(k), f"tied{k}@example.com"))
        for j in range(freq):
            oid += 1
            orders.append((oid, id_of(k), STORE[0], "online", f"2024-{j + 1:02d}-15"))
    return customers, orders


def _rfm_by_email(conn: sqlite3.Connection) -> dict[str, tuple]:
    emails = {r[0]: r[1] for r in conn.execute("SELECT customer_id, email FROM customers").fetchall()}
    return {
        emails[r["customer_id"]]: (r["r_score"], r["f_score"], r["m_score"], r["rfm_total"], r["segment"])
        for r in db.run_query(conn, "03_customer_rfm")
    }


# --------------------------------------------------------------------------- 03 customer RFM


def test_rfm_identical_databases_give_identical_rows():
    a = _build(*_tied_dataset(lambda k: k + 1))
    b = _build(*_tied_dataset(lambda k: k + 1))
    assert db.run_query(a, "03_customer_rfm") == db.run_query(b, "03_customer_rfm")


def test_rfm_scores_do_not_depend_on_customer_id_labels():
    """Same customers, same orders, only the customer_id labels permuted: scores must be identical.

    NTILE() splits a tie group across quintiles in whatever order the sorter happens to see the
    rows, so relabelling ids used to move customers between scores and segments.
    """
    straight = _rfm_by_email(_build(*_tied_dataset(lambda k: k + 1)))
    reversed_ids = _rfm_by_email(_build(*_tied_dataset(lambda k: len(FREQ_PATTERN) - k)))
    assert straight == reversed_ids


def test_rfm_tied_values_share_a_score_on_tied_dataset():
    conn = _build(*_tied_dataset(lambda k: k + 1))
    rows = db.run_query(conn, "03_customer_rfm")
    assert len(rows) == len(FREQ_PATTERN)
    f_by_freq: dict[int, set[int]] = {}
    for r in rows:
        f_by_freq.setdefault(r["frequency"], set()).add(r["f_score"])
        assert r["monetary"] == r["frequency"] * PRODUCT[4]
        assert r["f_score"] == r["m_score"]  # monetary is a fixed multiple of frequency here
    # Five evenly spread frequency values (1..5) land exactly on the five scores, ties intact.
    assert f_by_freq == {1: {1}, 2: {2}, 3: {3}, 4: {4}, 5: {5}}
    assert {"Champion", "Lost"} <= {r["segment"] for r in rows}


def test_rfm_equal_values_get_equal_scores_on_fixture(conn):
    rows = db.run_query(conn, "03_customer_rfm")
    assert rows
    for value_col, score_col in (("recency_days", "r_score"), ("frequency", "f_score"), ("monetary", "m_score")):
        by_value: dict[float, set[int]] = {}
        for r in rows:
            by_value.setdefault(r[value_col], set()).add(r[score_col])
        split = {v: sorted(s) for v, s in by_value.items() if len(s) > 1}
        assert not split, f"{value_col} ties split across {score_col}: {split}"


def test_rfm_scores_are_integers_in_range_and_monotone(conn):
    rows = db.run_query(conn, "03_customer_rfm")
    for r in rows:
        for col in ("r_score", "f_score", "m_score"):
            assert isinstance(r[col], int) and 1 <= r[col] <= 5
        assert r["rfm_total"] == r["r_score"] + r["f_score"] + r["m_score"]
    by_freq = sorted(rows, key=lambda r: r["frequency"])
    assert all(a["f_score"] <= b["f_score"] for a, b in zip(by_freq, by_freq[1:], strict=False))
    by_money = sorted(rows, key=lambda r: r["monetary"])
    assert all(a["m_score"] <= b["m_score"] for a, b in zip(by_money, by_money[1:], strict=False))
    by_recency = sorted(rows, key=lambda r: r["recency_days"])
    assert all(a["r_score"] >= b["r_score"] for a, b in zip(by_recency, by_recency[1:], strict=False))
    # By construction: the least frequent / lowest-spending group always scores 1 and the most
    # recently active group always scores 5 (true for NTILE as well as for rank-based quintiles).
    assert min(r["f_score"] for r in rows) == 1
    assert min(r["m_score"] for r in rows) == 1
    assert max(r["r_score"] for r in rows) == 5


def test_rfm_segment_coverage_on_fixture(conn):
    rows = db.run_query(conn, "03_customer_rfm")
    segments = {r["segment"] for r in rows}
    assert {"Champion", "Lost"} <= segments
    assert segments <= {"Champion", "Loyal", "New", "At Risk", "Lost", "Regular"}
    assert len(rows) == conn.execute("SELECT COUNT(DISTINCT customer_id) FROM orders").fetchone()[0]


def test_rfm_query_is_repeatable_and_ordered_on_fixture(conn):
    first = db.run_query(conn, "03_customer_rfm")
    second = db.run_query(conn, "03_customer_rfm")
    assert first == second
    keys = [(-r["rfm_total"], -r["monetary"], r["customer_id"]) for r in first]
    assert keys == sorted(keys)


# --------------------------------------------------------------------------- 04 cohort retention


def _observable_cells(conn: sqlite3.Connection) -> set[tuple[str, int]]:
    max_month = conn.execute("SELECT substr(MAX(order_date), 1, 7) FROM orders").fetchone()[0]
    cohorts = {
        r[0][:7] for r in conn.execute("SELECT MIN(order_date) FROM orders GROUP BY customer_id").fetchall()
    }
    max_idx = _month_index(max_month)
    return {(c, n) for c in cohorts for n in range(12) if _month_index(c) + n <= max_idx}


def test_cohort_grid_has_a_row_for_every_observable_cell(conn):
    rows = db.run_query(conn, "04_cohort_retention")
    cells = [(r["cohort_month"], r["month_offset"]) for r in rows]
    assert len(cells) == len(set(cells))  # no duplicate cells
    expected = _observable_cells(conn)
    assert set(cells) == expected
    assert len(rows) == len(expected)
    # The plan's formulation: rows == sum over cohorts of the number of observable offsets.
    per_cohort: dict[str, int] = {}
    for c, _ in expected:
        per_cohort[c] = per_cohort.get(c, 0) + 1
    assert len(rows) == sum(per_cohort.values())


def test_cohort_censored_cells_are_absent_and_offsets_capped(conn):
    rows = db.run_query(conn, "04_cohort_retention")
    max_idx = _month_index(conn.execute("SELECT substr(MAX(order_date), 1, 7) FROM orders").fetchone()[0])
    for r in rows:
        assert 0 <= r["month_offset"] <= 11
        assert _month_index(r["cohort_month"]) + r["month_offset"] <= max_idx


def test_cohort_month_zero_bounds_and_sizes(conn):
    rows = db.run_query(conn, "04_cohort_retention")
    assert rows
    size_by_cohort: dict[str, set[int]] = {}
    for r in rows:
        assert 0 <= r["retention_pct"] <= 100
        assert 0 <= r["active_customers"] <= r["cohort_size"]
        # one-decimal rounding of the exact ratio (tolerance: SQLite rounds halves away from zero)
        assert abs(r["retention_pct"] - r["active_customers"] * 100.0 / r["cohort_size"]) <= 0.05 + 1e-9
        size_by_cohort.setdefault(r["cohort_month"], set()).add(r["cohort_size"])
        if r["month_offset"] == 0:
            assert r["active_customers"] == r["cohort_size"] and r["retention_pct"] == 100.0
    assert all(len(s) == 1 for s in size_by_cohort.values())
    expected_sizes = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT cohort_month, COUNT(*) FROM (SELECT substr(MIN(order_date), 1, 7) AS cohort_month "
            "FROM orders GROUP BY customer_id) GROUP BY cohort_month"
        ).fetchall()
    }
    assert {c: next(iter(s)) for c, s in size_by_cohort.items()} == expected_sizes


def test_cohort_zero_activity_rows_are_emitted_with_zero_pct():
    customers = [_customer(1), _customer(2), _customer(3)]
    orders = [
        (1, 1, 1, "online", "2024-01-15"),  # customer 1 never returns
        (2, 2, 1, "online", "2024-01-20"),
        (3, 2, 1, "pickup", "2024-04-10"),  # customer 2 returns in month offset 3
        (4, 3, 1, "in_store", "2024-03-05"),  # cohort 2024-03, observable offsets 0..1 only
    ]
    rows = db.run_query(_build(customers, orders), "04_cohort_retention")
    got = [
        (r["cohort_month"], r["cohort_size"], r["month_offset"], r["active_customers"], r["retention_pct"])
        for r in rows
    ]
    assert got == [
        ("2024-01", 2, 0, 2, 100.0),
        ("2024-01", 2, 1, 0, 0.0),
        ("2024-01", 2, 2, 0, 0.0),
        ("2024-01", 2, 3, 1, 50.0),
        ("2024-03", 1, 0, 1, 100.0),
        ("2024-03", 1, 1, 0, 0.0),
    ]
    zero_rows = [r for r in rows if r["active_customers"] == 0]
    assert zero_rows and all(r["retention_pct"] == 0.0 for r in zero_rows)
    assert ("2024-03", 2) not in {(r["cohort_month"], r["month_offset"]) for r in rows}  # right-censored


def test_cohort_grid_stops_at_offset_eleven():
    orders = [(1, 1, 1, "online", "2024-01-10"), (2, 1, 1, "online", "2025-06-10")]  # offset 17
    rows = db.run_query(_build([_customer(1)], orders), "04_cohort_retention")
    assert [r["month_offset"] for r in rows] == list(range(12))
    assert [r["retention_pct"] for r in rows] == [100.0] + [0.0] * 11
    assert all(r["cohort_month"] == "2024-01" and r["cohort_size"] == 1 for r in rows)


# --------------------------------------------------------------------------- 07 repeat purchase rate


def _python_repeat_stats(conn: sqlite3.Connection) -> dict[str, dict[str, float | None]]:
    """Independent re-computation of the 07 metrics from raw orders (same first-order rule, unrounded)."""
    per_customer: dict[int, list[tuple[str, int, str]]] = {}
    for row in conn.execute("SELECT order_id, customer_id, channel, order_date FROM orders").fetchall():
        oid, cid, channel, d = row[0], row[1], row[2], row[3]
        per_customer.setdefault(cid, []).append((d, oid, channel))
    stats: dict[str, dict] = {}
    gaps: dict[str, list[int]] = {}
    for history in per_customer.values():
        history.sort()
        first_channel = history[0][2]
        s = stats.setdefault(first_channel, {"customers": 0, "repeat_customers": 0, "orders": 0})
        s["customers"] += 1
        s["orders"] += len(history)
        if len(history) >= 2:
            s["repeat_customers"] += 1
            gap = (date.fromisoformat(history[1][0]) - date.fromisoformat(history[0][0])).days
            gaps.setdefault(first_channel, []).append(gap)
    return {
        ch: {
            "customers": s["customers"],
            "repeat_customers": s["repeat_customers"],
            "repeat_rate_pct": s["repeat_customers"] * 100.0 / s["customers"],
            "avg_orders_per_customer": s["orders"] / s["customers"],
            "median_days_to_second_order": statistics.median(gaps[ch]) if ch in gaps else None,
        }
        for ch, s in stats.items()
    }


def test_repeat_rate_customers_sum_to_distinct_customers(conn):
    rows = db.run_query(conn, "07_repeat_purchase_rate")
    assert rows
    assert {r["first_channel"] for r in rows} <= CHANNELS
    assert len(rows) == len({r["first_channel"] for r in rows})
    assert sum(r["customers"] for r in rows) == conn.execute(
        "SELECT COUNT(DISTINCT customer_id) FROM orders"
    ).fetchone()[0]
    for r in rows:
        assert 0 <= r["repeat_customers"] <= r["customers"]
        assert 0 <= r["repeat_rate_pct"] <= 100
        assert r["avg_orders_per_customer"] >= 1
    rates = [r["repeat_rate_pct"] for r in rows]
    assert rates == sorted(rates, reverse=True)


def test_repeat_rate_matches_independent_recomputation(conn):
    rows = db.run_query(conn, "07_repeat_purchase_rate")
    assert all("median_days_to_second_order" in r for r in rows)
    expected = _python_repeat_stats(conn)
    assert {r["first_channel"] for r in rows} == set(expected)
    for r in rows:
        e = expected[r["first_channel"]]
        for col in ("customers", "repeat_customers"):
            assert r[col] == e[col], (r["first_channel"], col)
        for col in ("repeat_rate_pct", "avg_orders_per_customer"):  # ROUND(..., 2) of the exact ratio
            assert abs(r[col] - e[col]) <= 0.005 + 1e-9, (r["first_channel"], col, r[col], e[col])
        median = r["median_days_to_second_order"]
        if e["median_days_to_second_order"] is None:
            assert median is None
        else:
            assert median is not None and abs(median - e["median_days_to_second_order"]) < 1e-9


def test_repeat_rate_median_days_hand_computed():
    customers = [_customer(i) for i in range(1, 9)]
    orders = [
        (1, 1, 1, "online", "2024-01-01"), (2, 1, 1, "online", "2024-01-11"),  # gap 10
        (3, 2, 1, "online", "2024-01-01"), (4, 2, 1, "in_store", "2024-01-21"),  # gap 20, 2nd order in store
        (5, 3, 1, "online", "2024-01-01"), (6, 3, 1, "online", "2024-04-10"),  # gap 100
        (7, 4, 1, "online", "2024-02-02"),  # one-time online buyer
        (8, 5, 1, "pickup", "2024-02-01"), (9, 5, 1, "pickup", "2024-02-11"),  # gap 10
        (10, 6, 1, "pickup", "2024-02-01"), (11, 6, 1, "pickup", "2024-03-02"),  # gap 30
        (12, 7, 1, "in_store", "2024-03-01"),  # one-time in-store buyer
        (13, 8, 1, "pickup", "2024-03-05"), (14, 8, 1, "online", "2024-03-05"),  # same day: order_id breaks tie
    ]
    rows = db.run_query(_build(customers, orders), "07_repeat_purchase_rate")
    assert all("median_days_to_second_order" in r for r in rows)
    got = {
        r["first_channel"]: (
            r["customers"], r["repeat_customers"], r["repeat_rate_pct"], r["avg_orders_per_customer"],
            r["median_days_to_second_order"],
        )
        for r in rows
    }
    assert got == {
        "pickup": (3, 3, 100.0, 2.0, 10.0),  # gaps 0, 10, 30 -> median 10
        "online": (4, 3, 75.0, 1.75, 20.0),  # gaps 10, 20, 100 -> median 20
        "in_store": (1, 0, 0.0, 1.0, None),  # no repeaters -> NULL median
    }
    assert [r["first_channel"] for r in rows] == ["pickup", "online", "in_store"]


def test_repeat_rate_even_count_median_averages_middle_pair():
    customers = [_customer(1), _customer(2)]
    orders = [
        (1, 1, 1, "online", "2024-01-01"), (2, 1, 1, "online", "2024-01-11"),  # gap 10
        (3, 2, 1, "online", "2024-01-01"), (4, 2, 1, "online", "2024-02-01"),  # gap 31
    ]
    rows = db.run_query(_build(customers, orders), "07_repeat_purchase_rate")
    assert all("median_days_to_second_order" in r for r in rows)
    assert [(r["first_channel"], r["median_days_to_second_order"]) for r in rows] == [("online", 20.5)]


# --------------------------------------------------------------------------- edge cases & headers


@pytest.mark.parametrize("name", LIFECYCLE_QUERIES)
def test_empty_database_returns_empty_list(name):
    conn = db.connect(":memory:")
    db.create_schema(conn)
    assert db.run_query(conn, name) == []


def test_single_order_database():
    conn = _build([_customer(1)], [(1, 1, 1, "pickup", "2024-05-05")])
    rfm = db.run_query(conn, "03_customer_rfm")
    assert len(rfm) == 1 and rfm[0]["frequency"] == 1 and rfm[0]["recency_days"] == 0
    assert all(1 <= rfm[0][c] <= 5 for c in ("r_score", "f_score", "m_score"))
    cohort = db.run_query(conn, "04_cohort_retention")
    assert [(r["cohort_month"], r["month_offset"], r["retention_pct"]) for r in cohort] == [("2024-05", 0, 100.0)]
    repeat = db.run_query(conn, "07_repeat_purchase_rate")
    assert [(r["first_channel"], r["customers"], r["repeat_customers"], r["repeat_rate_pct"]) for r in repeat] == [
        ("pickup", 1, 0, 0.0)
    ]
    assert "median_days_to_second_order" in repeat[0]
    assert repeat[0]["median_days_to_second_order"] is None


HEADER_LINE = re.compile(r"^-- @([a-z_]+): (.+)$")
EXPECTED_HEADERS = {
    "03_customer_rfm": ("table", "10"),
    "04_cohort_retention": ("heatmap row=cohort_month col=month_offset value=retention_pct size=cohort_size", "24"),
    "07_repeat_purchase_rate": ("bar x=first_channel y=repeat_rate_pct unit=%", "15"),
}


def _header(name: str) -> dict[str, list[str]]:
    """First contiguous block of `-- @key: value` lines at the very top of the file."""
    keys: dict[str, list[str]] = {}
    for line in (db.SQL_DIR / f"{name}.sql").read_text(encoding="utf-8").splitlines():
        m = HEADER_LINE.match(line)
        if not m:
            break
        keys.setdefault(m.group(1), []).append(m.group(2).strip())
    return keys


@pytest.mark.parametrize("name", LIFECYCLE_QUERIES)
def test_header_block_present(name):
    header = _header(name)
    assert {"title", "question", "technique", "chart", "limit", "portability"} <= set(header), header
    for key in ("title", "question", "technique", "chart", "limit"):
        assert len(header[key]) == 1 and header[key][0]
    chart, limit = EXPECTED_HEADERS[name]
    assert header["chart"] == [chart]
    assert header["limit"] == [limit]
    # every note maps a SQLite construct to its warehouse equivalent ("sqlite → postgres / bigquery")
    assert header["portability"] and all(("→" in note or "->" in note) for note in header["portability"])
