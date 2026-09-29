"""Deterministic synthetic retail dataset generator.

Produces customers, products, stores, orders and order_items with realistic
seasonality (holiday peaks), channel mix, category price bands and customer
churn so that downstream SQL (cohorts, RFM, ABC) has meaningful structure.
"""
from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass
from datetime import date, timedelta

CATEGORIES = {
    "Grocery": (2.0, 15.0),
    "Household": (4.0, 40.0),
    "Apparel": (10.0, 90.0),
    "Electronics": (25.0, 600.0),
    "Beauty": (5.0, 60.0),
    "Toys": (8.0, 80.0),
    "Home": (15.0, 250.0),
    "Sports": (12.0, 180.0),
}
CHANNELS = ("in_store", "online", "pickup")
REGIONS = ("West", "South", "Midwest", "Northeast")


@dataclass(frozen=True)
class Config:
    seed: int = 42
    customers: int = 6000
    products: int = 160
    stores: int = 12
    orders: int = 30_000
    start: date = date(2024, 1, 1)
    months: int = 24


def _seasonality(d: date) -> float:
    """Multiplicative demand factor: Q4 peak, summer dip, weekend bump."""
    yearly = 1.0 + 0.35 * math.cos((d.timetuple().tm_yday - 340) / 365.25 * 2 * math.pi)
    weekend = 1.25 if d.weekday() >= 5 else 1.0
    return yearly * weekend


def generate(cfg: Config = Config()) -> dict[str, list[tuple]]:
    rng = random.Random(cfg.seed)
    end = cfg.start + timedelta(days=int(cfg.months * 30.44))
    horizon = (end - cfg.start).days

    stores = [
        (i + 1, f"Store {i + 1:03d}", rng.choice(REGIONS), rng.choice(["standard", "flagship", "small_format"]))
        for i in range(cfg.stores)
    ]

    products = []
    cats = list(CATEGORIES)
    for i in range(cfg.products):
        cat = cats[i % len(cats)]
        lo, hi = CATEGORIES[cat]
        price = round(math.exp(rng.uniform(math.log(lo), math.log(hi))), 2)
        cost = round(price * rng.uniform(0.45, 0.75), 2)
        products.append((i + 1, f"SKU-{i + 1:05d}", f"{cat} Item {i + 1}", cat, price, cost))

    # Product popularity follows a Zipf-like curve so ABC analysis is non-trivial.
    weights = [1.0 / (rank + 3) ** 0.9 for rank in range(cfg.products)]
    rng.shuffle(weights)

    customers = []
    for i in range(cfg.customers):
        signup_offset = int(rng.triangular(0, horizon * 0.85, horizon * 0.15))
        signup = cfg.start + timedelta(days=signup_offset)
        churn_after = rng.expovariate(1 / 240)  # mean lifetime ~8 months
        customers.append((i + 1, f"customer{i + 1}@example.com", rng.choice(REGIONS), signup.isoformat(), churn_after))

    # Build a day-weight table once, then sample order dates.
    days = [cfg.start + timedelta(days=k) for k in range(horizon)]
    day_w = [_seasonality(d) for d in days]
    cum_w = list(itertools.accumulate(day_w))

    orders, items = [], []
    order_id = item_id = 0
    attempts = 0
    while len(orders) < cfg.orders and attempts < cfg.orders * 5:
        attempts += 1
        cust = rng.choice(customers)
        cid, _, region, signup_iso, churn_after = cust
        signup = date.fromisoformat(signup_iso)
        d = rng.choices(days, cum_weights=cum_w)[0]
        if d < signup or (d - signup).days > churn_after:
            continue
        order_id += 1
        channel = rng.choices(CHANNELS, weights=(0.55, 0.30, 0.15))[0]
        store = rng.choice([s for s in stores if s[2] == region] or stores)
        orders.append((order_id, cid, store[0], channel, d.isoformat()))
        n_lines = max(1, int(rng.lognormvariate(0.6, 0.6)))
        chosen = rng.choices(products, weights=weights, k=n_lines)
        seen = set()
        for p in chosen:
            if p[0] in seen:
                continue
            seen.add(p[0])
            item_id += 1
            qty = 1 if rng.random() < 0.7 else rng.randint(2, 4)
            discount = round(rng.choice([0, 0, 0, 0.05, 0.10, 0.20]) * p[4], 2)
            items.append((item_id, order_id, p[0], qty, p[4], discount))

    return {
        "stores": stores,
        "products": products,
        "customers": [(c[0], c[1], c[2], c[3]) for c in customers],
        "orders": orders,
        "order_items": items,
    }
