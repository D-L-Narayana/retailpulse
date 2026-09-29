from datetime import date
from retailpulse import generate


def test_deterministic_for_same_seed():
    a = generate.generate(generate.Config(seed=1, customers=50, orders=300))
    b = generate.generate(generate.Config(seed=1, customers=50, orders=300))
    assert a == b


def test_different_seed_differs():
    a = generate.generate(generate.Config(seed=1, customers=50, orders=300))
    b = generate.generate(generate.Config(seed=2, customers=50, orders=300))
    assert a["orders"] != b["orders"]


def test_row_counts_and_keys(data):
    assert len(data["orders"]) == 3000
    assert len(data["customers"]) == 400
    order_ids = {o[0] for o in data["orders"]}
    assert len(order_ids) == 3000
    assert all(i[1] in order_ids for i in data["order_items"])
    assert len(data["order_items"]) >= len(data["orders"])  # >= 1 line per order


def test_no_orders_before_signup(data):
    signup = {c[0]: date.fromisoformat(c[3]) for c in data["customers"]}
    assert all(date.fromisoformat(o[4]) >= signup[o[1]] for o in data["orders"])


def test_seasonality_peaks_in_q4():
    assert generate._seasonality(date(2024, 12, 6)) > generate._seasonality(date(2024, 6, 7))


def test_costs_below_prices(data):
    assert all(p[5] <= p[4] for p in data["products"])
