"""Structural tests for ``09_basket_affinity`` and ``10_customer_ltv``.

Every assertion here is about *structure* and self-consistency: the column contract, deterministic
ordering, metric bounds, cross-checks against independent SQL / Python computations, and exact
values on hand-built micro databases.  Nothing asserts the magnitude of the signals the generator
plants (affinity pairs, channel loyalty) - those belong to the generator's own tests.
"""
import itertools
import re
import sqlite3
import time
from collections import Counter, defaultdict

import pytest

from retailpulse import db, generate

AFFINITY = "09_basket_affinity"
LTV = "10_customer_ltv"
CHANNELS = {"in_store", "online", "pickup"}
AFFINITY_COLUMNS = [
    "pair_label", "sku_a", "sku_b", "category_a", "category_b",
    "pair_orders", "support_pct", "confidence_pct", "lift",
]
LTV_COLUMNS = ["first_channel", "month_offset", "eligible_customers", "cum_revenue_per_customer"]

_ORDERS_WITH_SKU = """
SELECT COUNT(DISTINCT oi.order_id)
FROM order_items oi JOIN products p ON p.product_id = oi.product_id
WHERE p.sku = ?
"""
_ORDERS_WITH_BOTH_SKUS = """
SELECT COUNT(*) FROM orders o
WHERE EXISTS (SELECT 1 FROM order_items oi JOIN products p ON p.product_id = oi.product_id
              WHERE oi.order_id = o.order_id AND p.sku = ?)
  AND EXISTS (SELECT 1 FROM order_items oi JOIN products p ON p.product_id = oi.product_id
              WHERE oi.order_id = o.order_id AND p.sku = ?)
"""
# Independent formulation of "acquisition channel of the first order" (correlated subquery + LIMIT,
# not ROW_NUMBER) and of the 12-offset observability rule.
_ELIGIBLE_BY_CHANNEL = """
WITH firsts AS (
    SELECT o.customer_id, o.channel,
           CAST(substr(o.order_date, 1, 4) AS INTEGER) * 12
             + CAST(substr(o.order_date, 6, 2) AS INTEGER) AS cohort_idx
    FROM orders o
    WHERE o.order_id = (SELECT o2.order_id FROM orders o2 WHERE o2.customer_id = o.customer_id
                        ORDER BY o2.order_date, o2.order_id LIMIT 1)
)
SELECT channel, COUNT(*) AS n
FROM firsts
WHERE cohort_idx + 11 <= (SELECT MAX(CAST(substr(order_date, 1, 4) AS INTEGER) * 12
                                     + CAST(substr(order_date, 6, 2) AS INTEGER)) FROM orders)
GROUP BY channel
"""


# ----------------------------------------------------------------------------- helpers


def _month_index(iso_date: str) -> int:
    return int(iso_date[:4]) * 12 + int(iso_date[5:7])


def _fresh_db() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.create_schema(conn)
    return conn


def _affinity_sort_key(row: dict) -> tuple:
    return (-row["lift"], -row["pair_orders"], row["sku_a"], row["sku_b"])


def _expected_affinity(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Pure-Python re-implementation of the affinity contract, keyed by (sku_a, sku_b)."""
    total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    products = {pid: (sku, cat) for pid, sku, cat in conn.execute("SELECT product_id, sku, category FROM products")}
    baskets: dict[int, set[int]] = defaultdict(set)
    for order_id, product_id in conn.execute("SELECT order_id, product_id FROM order_items"):
        baskets[order_id].add(product_id)
    orders_with: Counter = Counter()
    pair_orders: Counter = Counter()
    for pids in baskets.values():
        orders_with.update(pids)
        for a, b in itertools.combinations(sorted(pids, key=lambda p: products[p][0]), 2):
            pair_orders[(a, b)] += 1
    threshold = max(3, total_orders // 1000)
    expected = {}
    for (a, b), n in pair_orders.items():
        if n < threshold:
            continue
        expected[(products[a][0], products[b][0])] = {
            "category_a": products[a][1],
            "category_b": products[b][1],
            "pair_orders": n,
            "support_pct": 100.0 * n / total_orders,
            "confidence_pct": 100.0 * n / orders_with[a],
            "lift": n * total_orders / (orders_with[a] * orders_with[b]),
        }
    return expected


def _expected_ltv(conn: sqlite3.Connection) -> dict[tuple[str, int], tuple[int, float]]:
    """Pure-Python re-implementation of the LTV contract: (channel, offset) -> (eligible, cum/customer)."""
    orders = conn.execute(
        "SELECT order_id, customer_id, channel, order_date FROM orders ORDER BY order_date, order_id"
    ).fetchall()
    if not orders:
        return {}
    first: dict[int, tuple[str, int]] = {}
    for _order_id, customer_id, channel, order_date in orders:
        first.setdefault(customer_id, (channel, _month_index(order_date)))
    max_idx = max(_month_index(o[3]) for o in orders)
    eligible = {cid: fc for cid, fc in first.items() if fc[1] + 11 <= max_idx}
    sizes = Counter(channel for channel, _ in eligible.values())
    revenue: dict[tuple[str, int], float] = defaultdict(float)
    lines = conn.execute("SELECT customer_id, order_date, net_revenue FROM v_sales")
    for customer_id, order_date, net_revenue in lines:
        if customer_id in eligible:
            channel, cohort_idx = eligible[customer_id]
            offset = _month_index(order_date) - cohort_idx
            if 0 <= offset <= 11:
                revenue[(channel, offset)] += net_revenue
    expected = {}
    for channel, n in sizes.items():
        cum = 0.0
        for offset in range(12):
            cum += revenue.get((channel, offset), 0.0)
            expected[(channel, offset)] = (n, cum / n)
    return expected


# EXPLAIN QUERY PLAN lines that loop over or probe a relation, in both the >= 3.36 wording ("SCAN oi",
# "SEARCH o USING INDEX ...", "SCAN (subquery-3)") and the older one ("SCAN TABLE orders AS o"), plus
# "BLOOM FILTER ON f (rn=?)".  Group 1/2 = table and alias, group 3 = bloom-filter relation.
_PLAN_ACCESS = re.compile(r"^(?:SCAN|SEARCH)\s+(?:TABLE\s+)?(\S+)(?:\s+AS\s+(\w+))?|^BLOOM FILTER ON\s+(\S+)")
_SQL_KEYWORDS = {"ON", "USING", "JOIN", "LEFT", "INNER", "CROSS", "WHERE", "GROUP", "ORDER"}


def _plan_access_names(detail: str) -> set[str]:
    """Relation names (table, alias or subquery label) that one plan line scans, searches or filters."""
    m = _PLAN_ACCESS.match(detail)
    return {g for g in m.groups() if g} if m else set()


def _view_relations(conn: sqlite3.Connection, view: str) -> dict[str, set[str]]:
    """``{table: {table, alias}}`` for every table referenced in a view definition (read from sqlite_master)."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = ?", (view,)).fetchone()[0]
    out: dict[str, set[str]] = {}
    for table, alias in re.findall(r"\b(?:FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?", sql, flags=re.IGNORECASE):
        names = out.setdefault(table, {table})
        if alias and alias.upper() not in _SQL_KEYWORDS:
            names.add(alias)
    return out


def _seed_store_and_customer(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO stores VALUES (1, 'Store 001', 'West', 'standard')")
    conn.execute("INSERT INTO customers VALUES (1, 'customer1@example.com', 'West', '2023-12-01')")


def _single_order_db() -> sqlite3.Connection:
    conn = _fresh_db()
    with conn:
        _seed_store_and_customer(conn)
        conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", [
            (1, "SKU-00001", "Grocery Item 1", "Grocery", 10.0, 5.0),
            (2, "SKU-00002", "Toys Item 2", "Toys", 20.0, 10.0),
        ])
        conn.execute("INSERT INTO orders VALUES (1, 1, 1, 'online', '2024-01-15')")
        conn.execute("INSERT INTO order_items VALUES (1, 1, 1, 1, 10.0, 0.0)")
        conn.execute("INSERT INTO order_items VALUES (2, 1, 2, 2, 20.0, 1.0)")
    return conn


# ----------------------------------------------------------------------------- micro fixtures


@pytest.fixture(scope="module")
def affinity_mini() -> sqlite3.Connection:
    """Ten orders over three products; SKU order deliberately disagrees with product_id order.

    baskets: 4x {1,2}, 2x {1}, 1x {2}, 3x {1,3}  ->  orders_with: p1=9, p2=5, p3=3; total=10; threshold=max(3, 0)=3
    """
    conn = _fresh_db()
    with conn:
        _seed_store_and_customer(conn)
        conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", [
            (1, "SKU-00002", "Grocery Item 1", "Grocery", 10.0, 5.0),
            (2, "SKU-00001", "Toys Item 2", "Toys", 20.0, 10.0),
            (3, "SKU-00003", "Home Item 3", "Home", 30.0, 15.0),
        ])
        baskets = {
            1: (1, 2), 2: (1, 2), 3: (1, 2), 4: (1, 2), 5: (1,),
            6: (1,), 7: (2,), 8: (1, 3), 9: (1, 3), 10: (1, 3),
        }
        item_id = 0
        for order_id, pids in baskets.items():
            order = (order_id, 1, 1, "in_store", f"2024-01-{order_id:02d}")
            conn.execute("INSERT INTO orders VALUES (?,?,?,?,?)", order)
            for pid in pids:
                item_id += 1
                conn.execute("INSERT INTO order_items VALUES (?,?,?,?,?,?)", (item_id, order_id, pid, 1, 10.0, 0.0))
    return conn


@pytest.fixture(scope="module")
def affinity_large_mini() -> sqlite3.Connection:
    """4 000 orders so the relative floor (total_orders / 1000 = 4) overrides the absolute floor of 3."""
    conn = _fresh_db()
    with conn:
        _seed_store_and_customer(conn)
        conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", [
            (1, "SKU-00001", "Grocery Item 1", "Grocery", 10.0, 5.0),
            (2, "SKU-00002", "Toys Item 2", "Toys", 20.0, 10.0),
            (3, "SKU-00003", "Home Item 3", "Home", 30.0, 15.0),
        ])
        orders, items = [], []
        item_id = 0
        for order_id in range(1, 4001):
            orders.append((order_id, 1, 1, "online", "2024-01-01"))
            partners = (1, 2) if order_id <= 3 else (1, 3) if order_id <= 7 else (1,)
            for pid in partners:
                item_id += 1
                items.append((item_id, order_id, pid, 1, 10.0, 0.0))
        conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)
        conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?)", items)
    return conn


@pytest.fixture(scope="module")
def ltv_mini() -> sqlite3.Connection:
    """Hand-built acquisition cohorts; the last order month is 2025-01, so cohorts <= 2024-02 are eligible.

    c1 online  2024-01 (100), +50 at offset 3, +999 at offset 12 (outside the window, in_store channel)
    c2 online  2024-01 (20), never returns
    c3 pickup  2024-03 (10)  -> cohort 2024-03 + 11 = 2025-02 > 2025-01 -> NOT eligible
    c4 in_store 2024-02 (30) -> cohort 2024-02 + 11 = 2025-01 -> eligible (boundary)
    c5 two orders on 2024-01-05: order 20 pickup (5) and order 21 online (7) -> lower order_id wins -> pickup
    """
    conn = _fresh_db()
    with conn:
        conn.execute("INSERT INTO stores VALUES (1, 'Store 001', 'West', 'standard')")
        conn.execute("INSERT INTO products VALUES (1, 'SKU-00001', 'Home Item 1', 'Home', 1.0, 0.5)")
        conn.executemany("INSERT INTO customers VALUES (?,?,?,?)", [
            (cid, f"customer{cid}@example.com", "West", "2023-12-01") for cid in range(1, 6)
        ])
        orders = [
            (1, 1, "online", "2024-01-15", 100),
            (2, 1, "online", "2024-04-10", 50),
            (3, 1, "in_store", "2025-01-05", 999),
            (4, 2, "online", "2024-01-20", 20),
            (5, 3, "pickup", "2024-03-01", 10),
            (6, 4, "in_store", "2024-02-10", 30),
            (20, 5, "pickup", "2024-01-05", 5),
            (21, 5, "online", "2024-01-05", 7),
        ]
        for order_id, cid, channel, day, qty in orders:
            conn.execute("INSERT INTO orders VALUES (?,?,?,?,?)", (order_id, cid, 1, channel, day))
            conn.execute("INSERT INTO order_items VALUES (?,?,?,?,?,?)", (order_id, order_id, 1, qty, 1.0, 0.0))
    return conn


# ----------------------------------------------------------------------------- 09_basket_affinity


class TestAffinity:
    def test_rows_and_column_contract_on_fixture(self, conn):
        rows = db.run_query(conn, AFFINITY)
        assert rows, "the 3 000-order fixture must yield at least one qualifying product pair"
        for r in rows:
            assert list(r) == AFFINITY_COLUMNS

    def test_sku_orientation_and_label(self, conn):
        rows = db.run_query(conn, AFFINITY)
        assert rows
        for r in rows:
            assert r["sku_a"] < r["sku_b"]
            assert r["pair_label"] == f"{r['sku_a']} + {r['sku_b']}"
            assert r["category_a"] and r["category_b"]

    def test_metric_bounds(self, conn):
        rows = db.run_query(conn, AFFINITY)
        assert rows
        total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        for r in rows:
            assert r["pair_orders"] >= max(3, total_orders // 1000)
            assert 0 < r["support_pct"] <= r["confidence_pct"] <= 100
            assert r["lift"] >= 0

    def test_sorted_by_contract_key(self, conn):
        rows = db.run_query(conn, AFFINITY)
        assert rows
        keys = [_affinity_sort_key(r) for r in rows]
        assert keys == sorted(keys)
        assert len({(r["sku_a"], r["sku_b"]) for r in rows}) == len(rows)  # one row per pair

    def test_top_row_matches_direct_sql_counts(self, conn):
        rows = db.run_query(conn, AFFINITY)
        assert rows
        top = rows[0]
        both = conn.execute(_ORDERS_WITH_BOTH_SKUS, (top["sku_a"], top["sku_b"])).fetchone()[0]
        with_a = conn.execute(_ORDERS_WITH_SKU, (top["sku_a"],)).fetchone()[0]
        with_b = conn.execute(_ORDERS_WITH_SKU, (top["sku_b"],)).fetchone()[0]
        total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        assert top["pair_orders"] == both
        assert both <= min(with_a, with_b)
        assert top["support_pct"] == pytest.approx(100.0 * both / total_orders, abs=0.006)
        assert top["confidence_pct"] == pytest.approx(100.0 * both / with_a, abs=0.006)
        assert top["lift"] == pytest.approx(both * total_orders / (with_a * with_b), abs=0.0006)

    def test_matches_independent_python_computation(self, conn):
        rows = db.run_query(conn, AFFINITY)
        expected = _expected_affinity(conn)
        assert expected, "sanity: the independent computation must find qualifying pairs too"
        assert {(r["sku_a"], r["sku_b"]) for r in rows} == set(expected)
        for r in rows:
            e = expected[(r["sku_a"], r["sku_b"])]
            assert (r["category_a"], r["category_b"]) == (e["category_a"], e["category_b"])
            assert r["pair_orders"] == e["pair_orders"]
            assert r["support_pct"] == pytest.approx(e["support_pct"], abs=0.006)
            assert r["confidence_pct"] == pytest.approx(e["confidence_pct"], abs=0.006)
            assert r["lift"] == pytest.approx(e["lift"], abs=0.0006)

    def test_hand_computed_metrics(self, affinity_mini):
        rows = db.run_query(affinity_mini, AFFINITY)
        assert [(r["sku_a"], r["sku_b"]) for r in rows] == [("SKU-00002", "SKU-00003"), ("SKU-00001", "SKU-00002")]
        top, second = rows
        assert top["pair_label"] == "SKU-00002 + SKU-00003"
        assert (top["category_a"], top["category_b"]) == ("Grocery", "Home")
        assert top["pair_orders"] == 3
        assert top["support_pct"] == pytest.approx(30.0, abs=0.006)
        assert top["confidence_pct"] == pytest.approx(100.0 * 3 / 9, abs=0.006)
        assert top["lift"] == pytest.approx(3 * 10 / (9 * 3), abs=0.0006)
        assert second["pair_label"] == "SKU-00001 + SKU-00002"
        assert (second["category_a"], second["category_b"]) == ("Toys", "Grocery")
        assert second["pair_orders"] == 4
        assert second["support_pct"] == pytest.approx(40.0, abs=0.006)
        assert second["confidence_pct"] == pytest.approx(80.0, abs=0.006)
        assert second["lift"] == pytest.approx(4 * 10 / (5 * 9), abs=0.0006)

    def test_minimum_support_scales_with_total_orders(self, affinity_large_mini):
        rows = db.run_query(affinity_large_mini, AFFINITY)
        assert [(r["sku_a"], r["sku_b"], r["pair_orders"]) for r in rows] == [("SKU-00001", "SKU-00003", 4)]

    def test_empty_and_single_order_databases_return_no_rows(self):
        assert db.run_query(_fresh_db(), AFFINITY) == []
        assert db.run_query(_single_order_db(), AFFINITY) == []


# ----------------------------------------------------------------------------- 10_customer_ltv


class TestLtv:
    def test_rows_and_column_contract_on_fixture(self, conn):
        rows = db.run_query(conn, LTV)
        assert rows, "the 24-month fixture must contain cohorts observable for all 12 offsets"
        for r in rows:
            assert list(r) == LTV_COLUMNS

    def test_channels_and_offsets(self, conn):
        rows = db.run_query(conn, LTV)
        assert rows
        offsets_by_channel: dict[str, list[int]] = defaultdict(list)
        for r in rows:
            offsets_by_channel[r["first_channel"]].append(r["month_offset"])
        assert set(offsets_by_channel) <= CHANNELS
        for channel, offsets in offsets_by_channel.items():
            assert offsets == list(range(12)), channel

    def test_curve_non_decreasing_and_non_negative(self, conn):
        rows = db.run_query(conn, LTV)
        assert rows
        last: dict[str, float] = {}
        for r in rows:
            value = r["cum_revenue_per_customer"]
            assert value is not None and value >= 0
            assert value >= last.get(r["first_channel"], 0.0)
            last[r["first_channel"]] = value

    def test_eligible_customers_constant_and_match_direct_count(self, conn):
        rows = db.run_query(conn, LTV)
        assert rows
        direct = {channel: n for channel, n in conn.execute(_ELIGIBLE_BY_CHANNEL)}
        sizes: dict[str, set[int]] = defaultdict(set)
        for r in rows:
            sizes[r["first_channel"]].add(r["eligible_customers"])
        assert set(sizes) == set(direct)
        for channel, values in sizes.items():
            assert values == {direct[channel]}, channel
            assert direct[channel] >= 1

    def test_sorted_by_channel_then_offset(self, conn):
        rows = db.run_query(conn, LTV)
        keys = [(r["first_channel"], r["month_offset"]) for r in rows]
        assert keys == sorted(keys) and len(keys) == len(set(keys))

    def test_matches_independent_python_computation(self, conn):
        rows = db.run_query(conn, LTV)
        expected = _expected_ltv(conn)
        assert expected
        assert {(r["first_channel"], r["month_offset"]) for r in rows} == set(expected)
        for r in rows:
            n, cum = expected[(r["first_channel"], r["month_offset"])]
            assert r["eligible_customers"] == n
            assert r["cum_revenue_per_customer"] == pytest.approx(cum, abs=0.011)

    def test_hand_computed_curves(self, ltv_mini):
        rows = db.run_query(ltv_mini, LTV)
        expected_keys = [(c, o) for c in ("in_store", "online", "pickup") for o in range(12)]
        assert [(r["first_channel"], r["month_offset"]) for r in rows] == expected_keys
        by = {(r["first_channel"], r["month_offset"]): r for r in rows}
        assert {r["eligible_customers"] for r in rows if r["first_channel"] == "in_store"} == {1}
        assert {r["eligible_customers"] for r in rows if r["first_channel"] == "online"} == {2}
        assert {r["eligible_customers"] for r in rows if r["first_channel"] == "pickup"} == {1}
        assert all(by[("in_store", o)]["cum_revenue_per_customer"] == 30.0 for o in range(12))
        assert all(by[("online", o)]["cum_revenue_per_customer"] == 60.0 for o in range(3))
        assert all(by[("online", o)]["cum_revenue_per_customer"] == 85.0 for o in range(3, 12))
        assert all(by[("pickup", o)]["cum_revenue_per_customer"] == 12.0 for o in range(12))

    def test_empty_and_single_order_databases_return_no_rows(self):
        assert db.run_query(_fresh_db(), LTV) == []
        assert db.run_query(_single_order_db(), LTV) == []

    def test_runs_within_time_budget_on_fixture(self, conn):
        # A formulation that lets SQLite enter v_sales through products and re-probe first orders for
        # every product row took ~0.2 s per run on this 3 000-order fixture and 4-6 s at the default
        # 30 000 orders; aggregating revenue in one pass takes ~10 ms.  min() of three runs filters noise.
        timings = []
        for _ in range(3):
            t0 = time.perf_counter()
            rows = db.run_query(conn, LTV)
            timings.append(time.perf_counter() - t0)
        assert rows
        assert min(timings) <= 0.5, f"{LTV} took {min(timings):.3f}s on the 3 000-order fixture (budget 0.5 s)"

    def test_plan_aggregates_revenue_in_one_pass_over_v_sales(self, conn):
        # Line-level revenue must be aggregated by a loop nest that touches only the v_sales base tables
        # (order_items / orders / products).  If customer attribution (first orders, cohort, horizon) is
        # joined *inside* that nest, SQLite may enter v_sales through products and re-probe the first-order
        # CTE for every product x customer - the shape that took seconds at the default 30 000 orders.
        sql = (db.SQL_DIR / f"{LTV}.sql").read_text(encoding="utf-8")
        plan = [(row[0], row[1], row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        text = "\n".join(detail for _, _, detail in plan)
        relations = _view_relations(conn, "v_sales")
        fact_relations = {"v_sales"}.union(*relations.values())
        item_nodes = [(node_id, parent) for node_id, parent, detail in plan
                      if _plan_access_names(detail) & relations["order_items"]]
        assert len(item_nodes) == 1, f"order_items must be traversed exactly once:\n{text}"
        _, parent = item_nodes[0]
        siblings = [_plan_access_names(detail) for _, p, detail in plan if p == parent]
        foreign = [names for names in siblings if names and not names <= fact_relations]
        assert not foreign, f"relations {foreign} are joined inside the revenue loop nest:\n{text}"


# ----------------------------------------------------------------------------- discovery + headers


class TestRegistration:
    def test_both_queries_are_discovered(self):
        names = db.list_queries()
        assert AFFINITY in names and LTV in names

    @pytest.mark.parametrize(
        ("name", "chart", "limit"),
        [
            (AFFINITY, "bar x=pair_label y=lift", "15"),
            (LTV, "line x=month_offset y=cum_revenue_per_customer series=first_channel", "36"),
        ],
    )
    def test_header_metadata_block(self, name, chart, limit):
        text = (db.SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")
        assert text.startswith("-- @title:")
        header: dict[str, list[str]] = defaultdict(list)
        for line in text.splitlines():
            if not line.startswith("--"):
                break
            body = line[2:].strip()
            if body.startswith("@") and ":" in body:
                key, _, value = body[1:].partition(":")
                header[key.strip()].append(value.strip())
        assert header["title"] and header["question"] and header["technique"]
        assert header["chart"] == [chart]
        assert header["limit"] == [limit]
        assert len(header["portability"]) >= 2 and all(header["portability"])

    @pytest.mark.parametrize(
        "cfg",
        [generate.Config(orders=200), generate.Config(seed=11, customers=60, products=40, stores=4, orders=200)],
        ids=["default-config-200-orders", "tiny-config-200-orders"],
    )
    def test_both_queries_run_on_small_generated_configs(self, cfg):
        conn = _fresh_db()
        db.load(conn, generate.generate(cfg))
        for name in (AFFINITY, LTV):
            rows = db.run_query(conn, name)
            assert isinstance(rows, list)
            for r in rows:
                assert list(r) == (AFFINITY_COLUMNS if name == AFFINITY else LTV_COLUMNS)
