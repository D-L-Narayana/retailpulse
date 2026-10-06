"""Performance budget: the default build stays within 10 s end-to-end and 200 000 orders generate in 20 s.

Both tests always run; the ``slow`` marker is informational and must never be used to deselect them.
The 200 000-order test is generate-only by design: loading or rendering a dataset of that size is
outside both the budget and the memory envelope of CI runners.
"""
from __future__ import annotations

import time

import pytest

from retailpulse import db, generate, report

DEFAULT_BUDGET_S = 10.0
LARGE_ORDERS = 200_000
LARGE_BUDGET_S = 20.0


def test_default_config_end_to_end_within_10s():
    timings: dict[str, float] = {}
    t_start = time.perf_counter()

    cfg = generate.Config()
    data = generate.generate(cfg)
    timings["generate"] = time.perf_counter() - t_start

    t = time.perf_counter()
    conn = db.connect(":memory:")
    db.create_schema(conn)
    db.load(conn, data)
    timings["load"] = time.perf_counter() - t

    per_query: dict[str, float] = {}
    results = {}
    for name in db.list_queries():
        t = time.perf_counter()
        results[name] = db.run_query(conn, name)
        per_query[name] = time.perf_counter() - t
    timings["queries"] = sum(per_query.values())

    t = time.perf_counter()
    sql_text = {name: (db.SQL_DIR / f"{name}.sql").read_text(encoding="utf-8") for name in results}
    meta = {
        "generated_at": "2024-01-01 00:00 UTC",
        "elapsed_ms": 0,
        "customers": len(data["customers"]),
        "orders": len(data["orders"]),
        "line_items": len(data["order_items"]),
        "products": len(data["products"]),
        "stores": len(data["stores"]),
    }
    html = report.build_html(results, sql_text, meta)
    timings["render"] = time.perf_counter() - t
    total = time.perf_counter() - t_start

    print(f"\nperf default-config total={total:.2f}s " + " ".join(f"{k}={v:.2f}s" for k, v in timings.items()))
    slowest = sorted(per_query.items(), key=lambda kv: kv[1], reverse=True)
    print("perf per-query " + " ".join(f"{k}={v:.2f}s" for k, v in slowest))

    assert len(data["orders"]) == cfg.orders
    assert html and "</html>" in html
    assert total <= DEFAULT_BUDGET_S, (total, timings, slowest)


@pytest.mark.slow
def test_generate_200k_orders_within_20s_generate_only():
    cfg = generate.Config(orders=LARGE_ORDERS)
    t0 = time.perf_counter()
    data = generate.generate(cfg)
    elapsed = time.perf_counter() - t0
    n_items = len(data["order_items"])
    print(f"\nperf generate {LARGE_ORDERS:,} orders: {elapsed:.2f}s ({n_items:,} line items)")

    assert len(data["orders"]) == LARGE_ORDERS  # exactness contract: len(orders) == cfg.orders
    assert len({o[0] for o in data["orders"]}) == LARGE_ORDERS
    assert n_items >= LARGE_ORDERS
    assert elapsed <= LARGE_BUDGET_S, elapsed
