from retailpulse import db

BASELINE_QUERIES = {
    "01_monthly_revenue", "02_category_abc", "03_customer_rfm", "04_cohort_retention",
    "05_store_performance", "06_channel_mix", "07_repeat_purchase_rate", "08_data_quality",
}


def test_every_query_runs(conn):
    names = db.list_queries()
    assert names == sorted(names) and len(names) == len(set(names))
    assert BASELINE_QUERIES <= set(names)  # queries are only ever added, never removed
    assert {"09_basket_affinity", "10_customer_ltv"} <= set(names)
    assert len(names) >= 10
    for n in names:
        assert isinstance(db.run_query(conn, n), list)


def test_monthly_revenue_sums_to_total(conn):
    rows = db.run_query(conn, "01_monthly_revenue")
    total = round(sum(r["revenue"] for r in rows), 0)
    expected = round(conn.execute("SELECT SUM(net_revenue) FROM v_sales").fetchone()[0], 0)
    assert abs(total - expected) <= 1
    assert rows[0]["mom_change"] is None  # first month has no prior


def test_abc_classes_are_monotonic(conn):
    rows = db.run_query(conn, "02_category_abc")
    order = {"A": 0, "B": 1, "C": 2}
    classes = [order[r["abc_class"]] for r in rows]
    assert classes == sorted(classes)
    assert rows[-1]["cum_share_pct"] == 100.0
    assert "A" in {r["abc_class"] for r in rows}


def test_rfm_scores_in_range(conn):
    rows = db.run_query(conn, "03_customer_rfm")
    assert rows
    for r in rows:
        assert 1 <= r["r_score"] <= 5 and 1 <= r["f_score"] <= 5 and 1 <= r["m_score"] <= 5
        assert r["rfm_total"] == r["r_score"] + r["f_score"] + r["m_score"]
    assert {"Champion", "Lost"} <= {r["segment"] for r in rows}


def test_cohort_month_zero_is_100_percent(conn):
    rows = db.run_query(conn, "04_cohort_retention")
    m0 = [r for r in rows if r["month_offset"] == 0]
    assert m0 and all(r["retention_pct"] == 100.0 for r in m0)
    assert all(0 <= r["retention_pct"] <= 100 for r in rows)


def test_store_region_share_sums_to_100(conn):
    rows = db.run_query(conn, "05_store_performance")
    assert len(rows) == conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]  # zero-order stores included
    share_by_region: dict[str, float] = {}
    revenue_by_region: dict[str, float] = {}
    for r in rows:
        share_by_region[r["region"]] = share_by_region.get(r["region"], 0.0) + r["region_share_pct"]
        revenue_by_region[r["region"]] = revenue_by_region.get(r["region"], 0.0) + r["revenue"]
    for region, total in share_by_region.items():
        if revenue_by_region[region] > 0:
            assert abs(total - 100) < 0.1, region
        else:
            assert total == 0.0, region  # a region without sales has nothing to share out
    assert all(r["rank_in_region"] >= 1 for r in rows)


def test_channel_mix_shares(conn):
    rows = db.run_query(conn, "06_channel_mix")
    assert rows
    for r in rows:
        assert r["online_share_pct"] is not None and 0 <= r["online_share_pct"] <= 100
        revenue = r["in_store"] + r["online"] + r["pickup"]
        if revenue > 0:
            assert abs(r["in_store_share_pct"] + r["online_share_pct"] + r["pickup_share_pct"] - 100) < 0.05


def test_repeat_rate_bounds(conn):
    rows = db.run_query(conn, "07_repeat_purchase_rate")
    assert {r["first_channel"] for r in rows} <= {"in_store", "online", "pickup"}
    assert all(0 <= r["repeat_rate_pct"] <= 100 for r in rows)
    distinct_customers = conn.execute("SELECT COUNT(DISTINCT customer_id) FROM orders").fetchone()[0]
    assert sum(r["customers"] for r in rows) == distinct_customers


BASELINE_DQ_CHECKS = {
    "orders_without_items",
    "orders_before_signup",
    "negative_net_revenue",
    "orphan_items",
    "duplicate_emails",
}


def test_data_quality_errors_all_zero(conn):
    rows = db.run_query(conn, "08_data_quality")
    assert BASELINE_DQ_CHECKS <= {r["check_name"] for r in rows}
    assert [list(r) for r in rows] == [["check_name", "severity", "violations", "description"]] * len(rows)
    assert {r["severity"] for r in rows} == {"error", "warn"}
    errors = [r for r in rows if r["severity"] == "error"]
    assert len(errors) == 12
    failing = [r["check_name"] for r in errors if r["violations"] != 0]
    assert not failing, failing  # the build gate fails only on error-severity rows (PLAN §3.5)
