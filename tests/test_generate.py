"""Generator contract tests: shapes, invariants, exact counts, planted signals and performance."""
import inspect
import time
from collections import Counter, defaultdict
from datetime import date

import pytest

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


# --------------------------------------------------------------------------- helpers / fixtures


@pytest.fixture(scope="module")
def default_data():
    """One default-config dataset (30 000 orders) shared by the planted-signal tests."""
    return generate.generate(generate.Config())


def _repeat_rate_by_first_channel(orders):
    """Mirror of sql/07: acquisition channel = channel of the first order (order_date, order_id)."""
    first: dict[int, tuple[tuple[str, int], str]] = {}
    n_orders: Counter[int] = Counter()
    for oid, cid, _sid, channel, d in orders:
        n_orders[cid] += 1
        key = (d, oid)
        if cid not in first or key < first[cid][0]:
            first[cid] = (key, channel)
    totals: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    for cid, (_key, channel) in first.items():
        totals[channel][0] += 1
        totals[channel][1] += 1 if n_orders[cid] >= 2 else 0
    return {channel: 100.0 * rep / tot for channel, (tot, rep) in totals.items()}


# --------------------------------------------------------------------------- shapes and invariants


def test_tuple_shapes_match_loader_contract(data):
    arities = {"stores": 4, "products": 6, "customers": 4, "orders": 5, "order_items": 6}
    assert set(data) == set(arities)
    for key, n in arities.items():
        assert data[key], key
        assert all(len(row) == n for row in data[key]), key


def test_order_and_item_invariants(data):
    store_ids = {s[0] for s in data["stores"]}
    customer_ids = {c[0] for c in data["customers"]}
    product_ids = {p[0] for p in data["products"]}
    assert [o[0] for o in data["orders"]] == list(range(1, len(data["orders"]) + 1))
    for _oid, cid, sid, channel, d in data["orders"]:
        assert cid in customer_ids
        assert sid in store_ids
        assert channel in generate.CHANNELS
        assert date.fromisoformat(d).isoformat() == d  # strict YYYY-MM-DD
    seen = set()
    orders_with_items = set()
    assert [i[0] for i in data["order_items"]] == list(range(1, len(data["order_items"]) + 1))
    for _item_id, oid, pid, qty, price, discount in data["order_items"]:
        assert pid in product_ids
        assert qty > 0
        assert 0 <= discount <= price
        assert (oid, pid) not in seen, (oid, pid)
        seen.add((oid, pid))
        orders_with_items.add(oid)
    assert orders_with_items == {o[0] for o in data["orders"]}  # every order has >= 1 line


def test_order_dates_within_horizon(data):
    cfg = generate.Config(seed=7, customers=400, orders=3000)
    dates = [date.fromisoformat(o[4]) for o in data["orders"]]
    assert min(dates) >= cfg.start
    assert (max(dates) - cfg.start).days < int(cfg.months * 30.44)


# --------------------------------------------------------------------------- exact order counts


@pytest.mark.parametrize("n", [0, 1, 7, 3000])
def test_exact_order_count(n):
    data = generate.generate(generate.Config(seed=3, customers=400, orders=n))
    assert len(data["orders"]) == n
    assert len({o[0] for o in data["orders"]}) == n
    if n == 0:
        assert data["order_items"] == []
    else:
        assert {i[1] for i in data["order_items"]} == {o[0] for o in data["orders"]}


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_exact_order_count_single_customer(seed):
    """One customer with a short lifetime: the old rejection sampler fell short of the request."""
    data = generate.generate(generate.Config(seed=seed, customers=1, orders=500))
    assert len(data["orders"]) == 500
    signup = date.fromisoformat(data["customers"][0][3])
    assert all(date.fromisoformat(o[4]) >= signup for o in data["orders"])


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_exact_order_count_tiny_horizon(seed):
    data = generate.generate(generate.Config(seed=seed, customers=3, orders=200, months=1))
    assert len(data["orders"]) == 200
    signup = {c[0]: date.fromisoformat(c[3]) for c in data["customers"]}
    assert all(date.fromisoformat(o[4]) >= signup[o[1]] for o in data["orders"])


# --------------------------------------------------------------------------- region coverage


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
@pytest.mark.parametrize("stores", [4, 5, 12])
def test_every_region_has_a_store(seed, stores):
    data = generate.generate(generate.Config(seed=seed, customers=10, orders=0, stores=stores))
    assert len(data["stores"]) == stores
    assert {s[2] for s in data["stores"]} == set(generate.REGIONS)


def test_fewer_stores_than_regions_still_builds():
    data = generate.generate(generate.Config(seed=1, customers=20, orders=50, stores=1))
    assert len(data["stores"]) == 1
    assert len(data["orders"]) == 50
    assert {o[2] for o in data["orders"]} == {1}  # every order falls back to the only store


# --------------------------------------------------------------------------- planted signals


def test_affinity_pairs_are_deterministic_and_well_formed():
    cfg = generate.Config()
    pairs = generate.affinity_pairs(cfg)
    assert pairs == generate.affinity_pairs(generate.Config())
    assert len(pairs) == cfg.affinity_pairs == 8
    flat = [pid for pair in pairs for pid in pair]
    assert all(1 <= pid <= cfg.products for pid in flat)
    assert all(a != b for a, b in pairs)
    assert len({frozenset(p) for p in pairs}) == len(pairs)  # distinct pairs
    assert len(set(flat)) == len(flat)  # at default size, pairs do not share products
    assert generate.affinity_pairs(generate.Config(seed=1)) != generate.affinity_pairs(generate.Config(seed=2))
    assert generate.affinity_pairs(generate.Config(affinity_pairs=0)) == []
    assert len(generate.affinity_pairs(generate.Config(affinity_pairs=3))) == 3


def test_planted_affinity_pairs_have_lift_above_2(default_data):
    cfg = generate.Config()
    pairs = generate.affinity_pairs(cfg)
    assert len(pairs) == cfg.affinity_pairs == 8
    baskets: defaultdict[int, set[int]] = defaultdict(set)
    for _item_id, oid, pid, _qty, _price, _discount in default_data["order_items"]:
        baskets[oid].add(pid)
    n = len(default_data["orders"])
    count: Counter[int] = Counter()
    for basket in baskets.values():
        count.update(basket)
    for a, b in pairs:
        both = sum(1 for basket in baskets.values() if a in basket and b in basket)
        p_a, p_b = count[a] / n, count[b] / n
        assert p_a >= 0.005 and p_b >= 0.005, (a, b)  # drawn from the popular end of the catalogue
        lift = (both / n) / (p_a * p_b)
        assert lift > 2, (a, b, both, lift)


def test_repeat_rate_differs_by_acquisition_channel(default_data):
    rates = _repeat_rate_by_first_channel(default_data["orders"])
    assert set(rates) == set(generate.CHANNELS)
    assert all(0 <= r <= 100 for r in rates.values())
    assert max(rates.values()) - min(rates.values()) >= 3.0, rates
    # The README's headline question: online-acquired customers come back most, by a clear margin.
    runner_up = max(rate for channel, rate in rates.items() if channel != "online")
    assert rates["online"] - runner_up >= 2.0, rates


def test_customers_concentrate_on_a_home_channel(default_data):
    per_customer: defaultdict[int, Counter[str]] = defaultdict(Counter)
    for _oid, cid, _sid, channel, _d in default_data["orders"]:
        per_customer[cid][channel] += 1
    shares = [c.most_common(1)[0][1] / sum(c.values()) for c in per_customer.values() if sum(c.values()) >= 6]
    assert len(shares) >= 200
    assert sum(shares) / len(shares) >= 0.70, sum(shares) / len(shares)


# --------------------------------------------------------------------------- config validation


@pytest.mark.parametrize(
    "kwargs",
    [
        {"customers": 0},
        {"products": 0},
        {"stores": 0},
        {"orders": -1},
        {"months": 0},
        {"affinity_pairs": -1},
        {"affinity_boost": -0.1},
        {"affinity_boost": 1.5},
        {"customers": 2.5},
        {"orders": "30"},
        {"start": "2024-01-01"},
    ],
)
def test_config_rejects_invalid_values(kwargs):
    with pytest.raises(ValueError):
        generate.Config(**kwargs)


def test_config_accepts_boundary_values():
    assert generate.Config(orders=0).orders == 0
    assert generate.Config(affinity_pairs=0, affinity_boost=0.0).affinity_boost == 0.0
    assert generate.Config(affinity_boost=1).affinity_boost == 1
    assert generate.Config(customers=1, products=1, stores=1, months=1).months == 1
    assert generate.Config().affinity_pairs == 8 and generate.Config().affinity_boost == 0.35


def test_generate_default_argument_is_none():
    assert inspect.signature(generate.generate).parameters["cfg"].default is None


def test_generate_no_pairs_and_single_product():
    data = generate.generate(generate.Config(seed=2, customers=5, products=1, orders=20, affinity_pairs=0))
    assert len(data["orders"]) == 20
    assert all(i[2] == 1 for i in data["order_items"])
    assert len(data["order_items"]) == 20  # one product -> exactly one line per order


# --------------------------------------------------------------------------- performance


def test_generate_default_config_under_3s():
    t0 = time.perf_counter()
    data = generate.generate()
    elapsed = time.perf_counter() - t0
    assert len(data["orders"]) == 30_000
    assert elapsed < 3.0, elapsed
