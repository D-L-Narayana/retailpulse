from retailpulse import db


def test_every_query_runs(conn):
    names = db.list_queries()
    assert len(names) == 8
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
    by_region = {}
    for r in rows:
        by_region[r["region"]] = by_region.get(r["region"], 0) + r["region_share_pct"]
    for total in by_region.values():
        assert abs(total - 100) < 0.1
    assert all(r["rank_in_region"] >= 1 for r in rows)


def test_channel_mix_shares(conn):
    rows = db.run_query(conn, "06_channel_mix")
    assert all(0 <= (r["online_share_pct"] or 0) <= 100 for r in rows)


def test_repeat_rate_bounds(conn):
    rows = db.run_query(conn, "07_repeat_purchase_rate")
    assert {r["first_channel"] for r in rows} <= {"in_store", "online", "pickup"}
    assert all(0 <= r["repeat_rate_pct"] <= 100 for r in rows)
    assert sum(r["customers"] for r in rows) == conn.execute("SELECT COUNT(DISTINCT customer_id) FROM orders").fetchone()[0]


def test_data_quality_all_zero(conn):
    rows = db.run_query(conn, "08_data_quality")
    assert len(rows) == 5
    assert all(r["violations"] == 0 for r in rows)
