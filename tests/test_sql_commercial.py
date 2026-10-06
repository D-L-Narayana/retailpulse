"""Behavioural tests for the commercial SQL queries: 01 monthly revenue, 02 ABC,
05 store performance and 06 channel mix.

Small purpose-built in-memory databases pin down tie-breaks, zero-activity
entities and NULL-safety; the shared session fixture checks aggregate
invariants and the header-metadata contract of each query file.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence

import pytest

from retailpulse import db

MISSING = object()  # sentinel: a missing column fails an assertion instead of raising KeyError

MONTHLY, ABC, STORES, CHANNEL = "01_monthly_revenue", "02_category_abc", "05_store_performance", "06_channel_mix"
COMMERCIAL_QUERIES = (MONTHLY, ABC, STORES, CHANNEL)

# Existing columns keep their names and positions; additions are appended (dashboard / data.json contract).
CONTRACT_COLUMNS = {
    MONTHLY: ["order_month", "orders", "revenue", "margin", "margin_pct", "mom_change", "mom_pct", "trailing_3m_avg",
              "yoy_pct"],
    ABC: ["product_id", "sku", "category", "revenue", "units", "revenue_rank", "cum_share_pct", "abc_class"],
    STORES: ["store_id", "name", "region", "format", "orders", "revenue", "avg_basket", "margin_pct",
             "rank_in_region", "region_share_pct"],
    CHANNEL: ["order_month", "in_store", "online", "pickup", "online_share_pct", "in_store_share_pct",
              "pickup_share_pct"],
}
EXPECTED_LIMITS = {MONTHLY: "24", ABC: "10", STORES: "12", CHANNEL: "24"}


class TinyDB:
    """Hand-built dataset on the real schema (foreign keys and CHECK constraints stay enforced)."""

    def __init__(self) -> None:
        self.conn: sqlite3.Connection = db.connect(":memory:")
        db.create_schema(self.conn)
        self._next_item = 1

    def store(self, store_id: int, region: str = "West", fmt: str = "standard") -> TinyDB:
        self.conn.execute("INSERT INTO stores VALUES (?,?,?,?)", (store_id, f"Store {store_id:03d}", region, fmt))
        return self

    def product(self, product_id: int, category: str = "Grocery", price: float = 10.0, cost: float = 6.0) -> TinyDB:
        row = (product_id, f"SKU-{product_id:05d}", f"{category} Item {product_id}", category, price, cost)
        self.conn.execute("INSERT INTO products VALUES (?,?,?,?,?,?)", row)
        return self

    def customer(self, customer_id: int, region: str = "West", signup: str = "2024-01-01") -> TinyDB:
        row = (customer_id, f"customer{customer_id}@example.com", region, signup)
        self.conn.execute("INSERT INTO customers VALUES (?,?,?,?)", row)
        return self

    def order(
        self,
        order_id: int,
        customer_id: int,
        store_id: int,
        channel: str,
        order_date: str,
        items: Sequence[tuple[int, int, float, float]],
    ) -> TinyDB:
        """`items` are `(product_id, quantity, unit_price, discount)` tuples."""
        self.conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?)", (order_id, customer_id, store_id, channel, order_date)
        )
        for product_id, quantity, unit_price, discount in items:
            self.conn.execute(
                "INSERT INTO order_items VALUES (?,?,?,?,?,?)",
                (self._next_item, order_id, product_id, quantity, unit_price, discount),
            )
            self._next_item += 1
        return self

    def run(self, name: str) -> list[dict]:
        return db.run_query(self.conn, name)


# ---- scenario builders ------------------------------------------------------------------------


def abc_with_unsold_sku() -> TinyDB:
    """Products 1 and 2 sell (30.00 and 10.00); product 3 never sells."""
    t = TinyDB().store(1).customer(1)
    for pid in (1, 2, 3):
        t.product(pid)
    return t.order(1, 1, 1, "in_store", "2024-03-05", [(1, 3, 10.0, 0.0), (2, 1, 10.0, 0.0)])


def abc_with_tied_revenues() -> TinyDB:
    """Four products with identical revenue (20.00 each), inserted out of product_id order."""
    t = TinyDB().store(1).customer(1)
    for pid in (1, 2, 3, 4):
        t.product(pid)
    for order_id, pid in enumerate((3, 1, 4, 2), start=1):
        t.order(order_id, 1, 1, "online", f"2024-02-{order_id:02d}", [(pid, 2, 10.0, 0.0)])
    return t


def stores_with_idle_store_and_idle_region() -> TinyDB:
    """West: store 1 sells 30.00 over two orders, store 2 never sells. South: store 3 never sells."""
    t = TinyDB().store(1, "West").store(2, "West").store(3, "South").product(1).customer(1)
    t.order(1, 1, 1, "in_store", "2024-01-10", [(1, 2, 10.0, 0.0)])
    t.order(2, 1, 1, "in_store", "2024-01-20", [(1, 1, 10.0, 0.0)])
    return t


def stores_with_tied_revenue() -> TinyDB:
    """Two West stores with identical revenue; store 2's order is inserted first."""
    t = TinyDB().store(1, "West").store(2, "West").product(1).customer(1)
    t.order(1, 1, 2, "in_store", "2024-01-10", [(1, 1, 10.0, 0.0)])
    t.order(2, 1, 1, "in_store", "2024-01-11", [(1, 1, 10.0, 0.0)])
    return t


def channel_mix_with_offline_month() -> TinyDB:
    """2024-01 has in-store revenue only; 2024-02 has 10 / 30 / 10 across in_store / online / pickup."""
    t = TinyDB().store(1).product(1).customer(1)
    t.order(1, 1, 1, "in_store", "2024-01-15", [(1, 1, 10.0, 0.0)])
    t.order(2, 1, 1, "in_store", "2024-02-01", [(1, 1, 10.0, 0.0)])
    t.order(3, 1, 1, "online", "2024-02-02", [(1, 3, 10.0, 0.0)])
    t.order(4, 1, 1, "pickup", "2024-02-03", [(1, 1, 10.0, 0.0)])
    return t


def channel_mix_with_zero_net_revenue_month() -> TinyDB:
    """A single fully discounted line: the month exists but its net revenue is 0."""
    return TinyDB().store(1).product(1).customer(1).order(1, 1, 1, "online", "2024-01-15", [(1, 1, 10.0, 10.0)])


def monthly_series(months: Sequence[str]) -> TinyDB:
    """One order per listed month; the k-th month sells quantity k at 10.00, so revenue is 10·k."""
    t = TinyDB().store(1).product(1).customer(1)
    for k, ym in enumerate(months, start=1):
        t.order(k, 1, 1, "in_store", f"{ym}-15", [(1, k, 10.0, 0.0)])
    return t


def single_order() -> TinyDB:
    """One online order with one line: 2 × (10.00 − 1.00) = 18.00 net, 2 × (10 − 1 − 6) = 6.00 margin."""
    return TinyDB().store(1).product(1).customer(1).order(1, 1, 1, "online", "2024-05-10", [(1, 2, 10.0, 1.0)])


CONTIGUOUS_14 = [f"2024-{m:02d}" for m in range(1, 13)] + ["2025-01", "2025-02"]
GAPPED_14 = [f"2024-{m:02d}" for m in range(1, 12)] + ["2025-01", "2025-02", "2025-03"]  # 2024-12 is missing


# ---- 01 monthly revenue -----------------------------------------------------------------------


def test_yoy_pct_is_null_for_first_12_months_then_hand_computed():
    rows = monthly_series(CONTIGUOUS_14).run(MONTHLY)
    assert len(rows) == 14
    assert "yoy_pct" in rows[0]
    assert [r["yoy_pct"] for r in rows[:12]] == [None] * 12
    # 2025-01: 130.00 vs 10.00 a year earlier → +1200 %; 2025-02: 140.00 vs 20.00 → +600 %
    assert (rows[12]["order_month"], rows[12]["yoy_pct"]) == ("2025-01", 1200.0)
    assert (rows[13]["order_month"], rows[13]["yoy_pct"]) == ("2025-02", 600.0)
    # pre-existing columns keep their semantics
    assert rows[0]["mom_change"] is None and rows[0]["mom_pct"] is None
    assert (rows[1]["mom_change"], rows[1]["mom_pct"]) == (10.0, 100.0)
    assert rows[2]["trailing_3m_avg"] == 20.0


def test_yoy_pct_is_null_when_the_row_12_back_is_not_the_same_calendar_month():
    rows = monthly_series(GAPPED_14).run(MONTHLY)
    assert len(rows) == 14
    assert "yoy_pct" in rows[0]
    # with 2024-12 absent, "12 rows back" is never the same calendar month: NULL rather than a wrong number
    assert [r["yoy_pct"] for r in rows] == [None] * 14


def test_yoy_pct_on_fixture_matches_hand_computation(conn):
    rows = db.run_query(conn, MONTHLY)
    assert "yoy_pct" in rows[0]
    assert len(rows) >= 13, "fixture must span at least 13 months for a year-over-year check"
    cur = conn.execute("SELECT order_month, ROUND(SUM(net_revenue), 2) FROM v_sales GROUP BY order_month")
    monthly = {month: revenue for month, revenue in cur.fetchall()}
    for i, r in enumerate(rows):
        year, month = r["order_month"].split("-")
        year_ago = f"{int(year) - 1}-{month}"
        if i < 12 or rows[i - 12]["order_month"] != year_ago:
            assert r["yoy_pct"] is None, r
        else:
            prev = monthly[year_ago]
            expected = (monthly[r["order_month"]] - prev) * 100.0 / prev
            assert r["yoy_pct"] == pytest.approx(expected, abs=0.011), r
    assert sum(r["yoy_pct"] is not None for r in rows) >= 1


# ---- 02 ABC classification --------------------------------------------------------------------


def test_abc_includes_zero_sales_sku_as_class_c():
    rows = abc_with_unsold_sku().run(ABC)
    assert len(rows) == 3  # every SKU appears, sold or not
    assert [r["product_id"] for r in rows] == [1, 2, 3]
    assert [r["revenue_rank"] for r in rows] == [1, 2, 3]
    assert [r["cum_share_pct"] for r in rows] == [75.0, 100.0, 100.0]
    assert [r["abc_class"] for r in rows] == ["B", "C", "C"]
    idle = rows[-1]
    assert idle["revenue"] == 0.0 and idle["units"] == 0


def test_abc_tied_revenues_break_ties_by_product_id_deterministically():
    first, second = abc_with_tied_revenues().run(ABC), abc_with_tied_revenues().run(ABC)
    assert [r["revenue_rank"] for r in first] == [1, 2, 3, 4]  # unique ranks although all revenues tie
    assert [r["product_id"] for r in first] == [1, 2, 3, 4]
    assert [r["cum_share_pct"] for r in first] == [25.0, 50.0, 75.0, 100.0]
    assert [r["abc_class"] for r in first] == ["A", "A", "B", "C"]
    assert first == second


def test_abc_with_no_sales_at_all_keeps_every_sku_with_null_share():
    rows = TinyDB().product(1).product(2).run(ABC)
    assert len(rows) == 2
    assert [r["revenue_rank"] for r in rows] == [1, 2]
    assert all(r["revenue"] == 0.0 and r["units"] == 0 for r in rows)
    assert all(r["cum_share_pct"] is None and r["abc_class"] == "C" for r in rows)


def test_abc_fixture_covers_every_product_monotonically(conn):
    rows = db.run_query(conn, ABC)
    assert len(rows) == conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    assert [r["revenue_rank"] for r in rows] == list(range(1, len(rows) + 1))
    shares = [r["cum_share_pct"] for r in rows]
    assert shares == sorted(shares) and shares[-1] == 100.0
    assert all(r["revenue"] >= 0 and r["units"] >= 0 for r in rows)


# ---- 05 store performance ---------------------------------------------------------------------


def test_store_without_orders_appears_with_zero_share_and_integer_rank():
    rows = stores_with_idle_store_and_idle_region().run(STORES)
    assert len(rows) == 3  # every store appears, with or without orders
    by_id = {r["store_id"]: r for r in rows}
    busy, idle, lonely = by_id[1], by_id[2], by_id[3]
    assert (busy["orders"], busy["revenue"], busy["avg_basket"], busy["margin_pct"]) == (2, 30.0, 15.0, 40.0)
    assert (busy["rank_in_region"], busy["region_share_pct"]) == (1, 100.0)
    assert (idle["orders"], idle["revenue"]) == (0, 0.0)
    assert idle["avg_basket"] is None and idle["margin_pct"] is None
    assert isinstance(idle["rank_in_region"], int) and idle["rank_in_region"] == 2
    assert isinstance(idle["region_share_pct"], float) and idle["region_share_pct"] == 0.0
    # a region whose only store never sold anything still gets numeric values
    assert (lonely["rank_in_region"], lonely["region_share_pct"]) == (1, 0.0)
    assert [r["store_id"] for r in rows] == [3, 1, 2]  # ORDER BY region, rank_in_region


def test_store_ties_rank_by_store_id_deterministically():
    first, second = stores_with_tied_revenue().run(STORES), stores_with_tied_revenue().run(STORES)
    assert [(r["store_id"], r["rank_in_region"]) for r in first] == [(1, 1), (2, 2)]
    assert [r["region_share_pct"] for r in first] == [50.0, 50.0]
    assert first == second


def test_store_fixture_lists_every_store_with_numeric_shares(conn):
    rows = db.run_query(conn, STORES)
    assert len(rows) == conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
    ids = [r["store_id"] for r in rows]
    assert len(ids) == len(set(ids))
    assert all(isinstance(r["rank_in_region"], int) and r["rank_in_region"] >= 1 for r in rows)
    assert all(isinstance(r["region_share_pct"], int | float) for r in rows)
    # Shares sum to 100 within every region that has revenue; a region without any revenue is all 0.0.
    # Ranks run 1..n per region because the tie-break (store_id) makes the ordering total.
    by_region: dict[str, list[dict]] = {}
    for r in rows:
        by_region.setdefault(r["region"], []).append(r)
    for region_rows in by_region.values():
        shares = [r["region_share_pct"] for r in region_rows]
        if sum(r["revenue"] for r in region_rows) > 0:
            assert sum(shares) == pytest.approx(100.0, abs=0.1), region_rows
        else:
            assert shares == [0.0] * len(shares), region_rows
        assert [r["rank_in_region"] for r in region_rows] == list(range(1, len(region_rows) + 1))


# ---- 06 channel mix ---------------------------------------------------------------------------


def test_channel_mix_month_without_online_revenue_reports_zero_not_null():
    rows = channel_mix_with_offline_month().run(CHANNEL)
    assert [r["order_month"] for r in rows] == ["2024-01", "2024-02"]
    offline, mixed = rows
    assert (offline["in_store"], offline["online"], offline["pickup"]) == (10.0, 0, 0)
    assert offline["online_share_pct"] == 0.0
    assert offline.get("in_store_share_pct", MISSING) == 100.0
    assert offline.get("pickup_share_pct", MISSING) == 0.0
    assert (mixed["in_store"], mixed["online"], mixed["pickup"]) == (10.0, 30.0, 10.0)
    assert (mixed["online_share_pct"], mixed["in_store_share_pct"], mixed["pickup_share_pct"]) == (60.0, 20.0, 20.0)


def test_channel_mix_zero_net_revenue_month_has_zero_shares():
    rows = channel_mix_with_zero_net_revenue_month().run(CHANNEL)
    assert len(rows) == 1
    row = rows[0]
    assert (row["in_store"], row["online"], row["pickup"]) == (0, 0, 0)
    assert row["online_share_pct"] == 0.0
    assert row.get("in_store_share_pct", MISSING) == 0.0 and row.get("pickup_share_pct", MISSING) == 0.0


def test_channel_mix_fixture_has_no_nulls_and_shares_sum_to_100(conn):
    rows = db.run_query(conn, CHANNEL)
    assert rows
    for r in rows:
        assert all(v is not None for v in r.values()), r
        shares = (r["online_share_pct"], r.get("in_store_share_pct", MISSING), r.get("pickup_share_pct", MISSING))
        assert all(isinstance(s, int | float) and 0 <= s <= 100 for s in shares), r
        if r["in_store"] + r["online"] + r["pickup"] > 0:
            assert sum(shares) == pytest.approx(100.0, abs=0.05), r
        else:
            assert shares == (0.0, 0.0, 0.0), r


# ---- cross-cutting: empty DB, single order, column contract, header metadata, SQLite 3.31 floor ----


@pytest.mark.parametrize("name", COMMERCIAL_QUERIES)
def test_queries_return_empty_list_on_empty_database(name):
    assert TinyDB().run(name) == []


def test_queries_run_on_a_single_order():
    t = single_order()
    monthly, abc, stores, channel = (t.run(n) for n in COMMERCIAL_QUERIES)
    assert len(monthly) == len(abc) == len(stores) == len(channel) == 1
    m = monthly[0]
    assert (m["orders"], m["revenue"], m["margin"], m["margin_pct"]) == (1, 18.0, 6.0, 33.33)
    assert m["mom_change"] is None and m["mom_pct"] is None and m["trailing_3m_avg"] == 18.0
    assert m.get("yoy_pct", MISSING) is None
    a = abc[0]
    assert (a["revenue_rank"], a["revenue"], a["units"], a["cum_share_pct"]) == (1, 18.0, 2, 100.0)
    s = stores[0]
    assert (s["orders"], s["revenue"], s["avg_basket"], s["margin_pct"]) == (1, 18.0, 18.0, 33.33)
    assert (s["rank_in_region"], s["region_share_pct"]) == (1, 100.0)
    c = channel[0]
    assert (c["in_store"], c["online"], c["pickup"], c["online_share_pct"]) == (0, 18.0, 0, 100.0)


@pytest.mark.parametrize("name", COMMERCIAL_QUERIES)
def test_result_columns_keep_contract_order_with_additions_appended(name, conn):
    rows = db.run_query(conn, name)
    assert rows
    assert list(rows[0]) == CONTRACT_COLUMNS[name]


HEADER_LINE = re.compile(r"^-- @([a-z_]+):\s*(.*?)\s*$")
CHART_GRAMMAR = re.compile(
    r"^(table|none"
    r"|line x=\w+ y=\w+( series=\w+)?( unit=%)?"
    r"|bar x=\w+ y=\w+( unit=(%|k))?"
    r"|heatmap row=\w+ col=\w+ value=\w+ size=\w+)$"
)


def _header(text: str) -> dict[str, list[str]]:
    """First contiguous block of `-- @key: value` lines at the top of a query file (registry header format)."""
    meta: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = HEADER_LINE.match(line)
        if not m:
            break
        meta.setdefault(m.group(1), []).append(m.group(2))
    return meta


@pytest.mark.parametrize("name", COMMERCIAL_QUERIES)
def test_header_block_is_present_and_parseable(name):
    text = (db.SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")
    assert text.startswith("-- @title:"), f"{name}.sql must start with the @ header block"
    meta = _header(text)
    assert {"title", "question", "technique", "chart", "limit", "portability"} <= set(meta), meta
    assert all(meta[k][0] for k in ("title", "question", "technique"))
    assert meta["limit"] == [EXPECTED_LIMITS[name]]
    assert CHART_GRAMMAR.match(meta["chart"][0]), meta["chart"]
    assert meta["portability"] and all(note for note in meta["portability"])
    assert any("Postgres" in note or "BigQuery" in note for note in meta["portability"])


# SQLite 3.31 (bundled with common Python 3.10 builds) is the supported floor: IIF arrived in 3.32 and
# RIGHT/FULL JOIN in 3.39; RETURNING, ->> and STRICT tables are newer still and not portable.
FORBIDDEN_SQL = re.compile(
    r"\bIIF\s*\(|\bRIGHT\s+(OUTER\s+)?JOIN\b|\bFULL\s+(OUTER\s+)?JOIN\b|\bRETURNING\b|->>|\bSTRICT\b"
)
PARAM_MARKER = re.compile(r"[:@$][A-Za-z_]\w*|\?")  # the CLI runs query files with no bindings


@pytest.mark.parametrize("name", COMMERCIAL_QUERIES)
def test_sql_stays_within_sqlite_331_and_takes_no_parameters(name):
    text = (db.SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")
    body = "\n".join(line.split("--", 1)[0] for line in text.splitlines())  # drop comments
    assert FORBIDDEN_SQL.search(body.upper()) is None, FORBIDDEN_SQL.search(body.upper())
    assert PARAM_MARKER.search(body) is None, PARAM_MARKER.search(body)
