"""Seed sweep: the analytical invariants must hold for every seed, not only for the shared conftest fixture.

Six seeds at a small configuration (120 customers, 900 orders) re-run the invariant battery:
data-quality errors are zero, cohort month 0 is 100 %, ABC classes are monotone, regional shares sum to
100, RFM scores stay within 1..5, repeat-rate customers add up to the distinct buyers and the monthly
revenue series sums to the ``v_sales`` total.
"""
from __future__ import annotations

from collections import defaultdict

import pytest

from retailpulse import db, generate

SEEDS = [1, 2, 3, 4, 5, 6]
CHANNELS = {"in_store", "online", "pickup"}
SEGMENTS = {"Champion", "Loyal", "Regular", "New", "At Risk", "Lost"}


@pytest.fixture(scope="module", params=SEEDS, ids=[f"seed{s}" for s in SEEDS])
def sweep(request):
    cfg = generate.Config(seed=request.param, customers=120, orders=900)
    data = generate.generate(cfg)
    conn = db.connect(":memory:")
    db.create_schema(conn)
    db.load(conn, data)
    results = {name: db.run_query(conn, name) for name in db.list_queries()}
    return cfg, data, conn, results


def _distinct_buyers(conn) -> int:
    return conn.execute("SELECT COUNT(DISTINCT customer_id) FROM orders").fetchone()[0]


def test_generator_counts_match_config(sweep):
    cfg, data, _conn, _results = sweep
    assert len(data["customers"]) == cfg.customers
    assert len(data["stores"]) == cfg.stores
    assert len(data["products"]) == cfg.products
    assert len(data["orders"]) == cfg.orders
    assert len({o[0] for o in data["orders"]}) == cfg.orders
    assert {i[1] for i in data["order_items"]} == {o[0] for o in data["orders"]}


def test_data_quality_error_rows_are_zero(sweep):
    _cfg, _data, _conn, results = sweep
    rows = results["08_data_quality"]
    assert rows
    for r in rows:
        assert r.get("severity", "error") in {"error", "warn"}, r
        assert isinstance(r["violations"], int) and r["violations"] >= 0, r
    failing = [r["check_name"] for r in rows if r.get("severity", "error") == "error" and r["violations"] != 0]
    assert failing == []


def test_cohort_month_zero_is_100_percent(sweep):
    _cfg, _data, conn, results = sweep
    rows = results["04_cohort_retention"]
    assert rows
    m0 = [r for r in rows if r["month_offset"] == 0]
    assert m0 and all(r["retention_pct"] == 100.0 for r in m0)
    assert all(0 <= r["retention_pct"] <= 100 for r in rows)
    assert all(0 <= r["month_offset"] <= 11 for r in rows)
    assert all(0 <= r["active_customers"] <= r["cohort_size"] for r in rows)
    assert len({r["cohort_month"] for r in m0}) == len(m0)
    assert sum(r["cohort_size"] for r in m0) == _distinct_buyers(conn)


def test_abc_classes_are_monotone(sweep):
    _cfg, _data, _conn, results = sweep
    rows = results["02_category_abc"]
    assert rows
    order = {"A": 0, "B": 1, "C": 2}
    classes = [order[r["abc_class"]] for r in rows]
    assert classes == sorted(classes)
    assert "A" in {r["abc_class"] for r in rows}
    shares = [r["cum_share_pct"] for r in rows]
    assert shares == sorted(shares)
    assert shares[-1] == 100.0
    revenues = [r["revenue"] for r in rows]
    assert revenues == sorted(revenues, reverse=True)
    ranks = [r["revenue_rank"] for r in rows]
    assert ranks[0] == 1 and ranks == sorted(ranks)


def test_store_region_shares_sum_to_100(sweep):
    cfg, _data, _conn, results = sweep
    rows = results["05_store_performance"]
    assert len(rows) == cfg.stores
    by_region: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_region[r["region"]].append(r)
    for region, stores in by_region.items():
        share = sum(r["region_share_pct"] or 0 for r in stores)
        if sum(r["revenue"] for r in stores) > 0:
            assert abs(share - 100) < 0.1, (region, share)
        else:
            assert share == 0, (region, share)
        ranks = sorted(r["rank_in_region"] for r in stores)
        assert ranks[0] == 1 and ranks[-1] <= len(stores), (region, ranks)


def test_rfm_scores_in_range(sweep):
    _cfg, _data, conn, results = sweep
    rows = results["03_customer_rfm"]
    assert rows
    assert len(rows) == _distinct_buyers(conn)
    assert len({r["customer_id"] for r in rows}) == len(rows)
    for r in rows:
        assert 1 <= r["r_score"] <= 5 and 1 <= r["f_score"] <= 5 and 1 <= r["m_score"] <= 5, r
        assert r["rfm_total"] == r["r_score"] + r["f_score"] + r["m_score"]
        assert r["segment"] in SEGMENTS, r
        assert r["recency_days"] >= 0 and r["frequency"] >= 1 and r["monetary"] >= 0


def test_repeat_customers_sum_to_distinct_buyers(sweep):
    _cfg, data, conn, results = sweep
    rows = results["07_repeat_purchase_rate"]
    assert rows
    assert {r["first_channel"] for r in rows} <= CHANNELS
    assert sum(r["customers"] for r in rows) == _distinct_buyers(conn)
    for r in rows:
        assert 0 <= r["repeat_customers"] <= r["customers"]
        assert abs(r["repeat_rate_pct"] - 100.0 * r["repeat_customers"] / r["customers"]) < 0.01
        assert r["avg_orders_per_customer"] >= 1
    reconstructed = sum(r["customers"] * r["avg_orders_per_customer"] for r in rows)
    assert abs(reconstructed - len(data["orders"])) <= 1.0  # avg is rounded to 2 dp


def test_monthly_revenue_sums_to_sales_total(sweep):
    _cfg, data, conn, results = sweep
    rows = results["01_monthly_revenue"]
    assert rows
    total = sum(r["revenue"] for r in rows)
    expected = conn.execute("SELECT SUM(net_revenue) FROM v_sales").fetchone()[0]
    assert abs(total - expected) <= 1
    assert sum(r["orders"] for r in rows) == len(data["orders"])
    months = [r["order_month"] for r in rows]
    assert months == sorted(months) and len(set(months)) == len(months)
    assert rows[0]["mom_change"] is None and rows[0]["mom_pct"] is None
    assert all(r["margin"] <= r["revenue"] for r in rows)


def test_channel_mix_is_bounded_and_consistent_with_monthly_revenue(sweep):
    _cfg, _data, _conn, results = sweep
    rows = results["06_channel_mix"]
    monthly = {r["order_month"]: r["revenue"] for r in results["01_monthly_revenue"]}
    assert [r["order_month"] for r in rows] == sorted(monthly)
    for r in rows:
        parts = [r["in_store"], r["online"], r["pickup"]]
        assert all(v is not None and v >= 0 for v in parts), r
        assert 0 <= r["online_share_pct"] <= 100, r
        assert abs(sum(parts) - monthly[r["order_month"]]) <= 0.05, r


def test_new_analytics_keep_their_structural_invariants(sweep):
    _cfg, _data, _conn, results = sweep
    affinity = results.get("09_basket_affinity")
    if affinity is not None:
        for r in affinity:
            assert r["sku_a"] < r["sku_b"], r
            assert r["pair_orders"] >= 1
            assert 0 < r["support_pct"] <= 100 and 0 < r["confidence_pct"] <= 100, r
            assert r["lift"] >= 0, r
        lifts = [r["lift"] for r in affinity]
        assert lifts == sorted(lifts, reverse=True)
    ltv = results.get("10_customer_ltv")
    if ltv is not None:
        per_channel: dict[str, list[dict]] = defaultdict(list)
        for r in ltv:
            per_channel[r["first_channel"]].append(r)
        assert set(per_channel) <= CHANNELS
        for channel, curve in per_channel.items():
            assert [r["month_offset"] for r in curve] == list(range(12)), channel
            assert len({r["eligible_customers"] for r in curve}) == 1 and curve[0]["eligible_customers"] > 0
            values = [r["cum_revenue_per_customer"] for r in curve]
            assert all(v >= 0 for v in values)
            assert values == sorted(values), channel
