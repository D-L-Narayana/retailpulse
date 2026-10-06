"""Deterministic synthetic retail dataset generator.

Produces customers, products, stores, orders and order_items with realistic
seasonality (holiday peaks), channel mix, category price bands and customer
churn so that downstream SQL (cohorts, RFM, ABC) has meaningful structure.

Planted structure (all of it deterministic for a given :class:`Config`):

* **Exact order counts.** ``len(orders) == cfg.orders`` always holds. Orders are
  drawn customer-first: a customer is picked with probability proportional to the
  seasonality mass of their active window (times a per-customer intensity), then
  an order date is drawn inside that window, so no order precedes the signup date
  or outlives the customer and nothing is rejected. Order ids are chronological.
* **Region coverage.** With at least ``len(REGIONS)`` stores every region gets a
  store (round-robin, then shuffled). Within a region, store traffic is weighted
  by format (flagship > standard > small_format).
* **Home-channel loyalty.** Each customer has a latent home channel (55/30/15 %
  in_store/online/pickup). ~70 % of their orders use it; the rest follow the
  market mix. Mean lifetime and purchase frequency depend on the home channel
  (``MEAN_LIFETIME_DAYS``, ``HOME_CHANNEL_INTENSITY``: online customers stay
  longest and buy most often, pickup customers least), so the acquisition
  channel predicts repeat purchasing and online-acquired customers lead.
* **Product affinity.** :func:`affinity_pairs` plants anchor/partner pairs among
  the most popular products; whenever an anchor is in a basket its partner is
  attached with probability ``cfg.affinity_boost``.
* **Category seasonality.** Toys and Electronics are over-represented in Q4 baskets.
"""
from __future__ import annotations

import itertools
import math
import random
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta

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
CHANNEL_WEIGHTS = (0.55, 0.30, 0.15)  # market mix; also the prior for a customer's home channel
REGIONS = ("West", "South", "Midwest", "Northeast")
STORE_FORMATS = ("standard", "flagship", "small_format")

HOME_CHANNEL_LOYALTY = 0.70  # share of a customer's orders placed through their home channel
MEAN_LIFETIME_DAYS = {"in_store": 230.0, "online": 330.0, "pickup": 170.0}  # active window, exponential
HOME_CHANNEL_INTENSITY = {"in_store": 1.0, "online": 1.25, "pickup": 0.85}  # relative order frequency while active
STORE_FORMAT_TRAFFIC = {"standard": 2, "flagship": 4, "small_format": 1}  # relative order volume per store
Q4_CATEGORY_UPLIFT = {"Toys": 1.8, "Electronics": 1.4}  # basket-share multiplier in October-December
POPULAR_POOL = 40  # affinity pairs are drawn from this many most-popular products
DISCOUNT_RATES = (0.0, 0.0, 0.0, 0.05, 0.10, 0.20)

# Cumulative channel weights without the final 1.0: bisect_right() then yields 0, 1 or 2 for any r in [0, 1),
# so the last channel absorbs the remainder and float rounding can never produce an out-of-range index.
_CHANNEL_CUM = tuple(itertools.accumulate(CHANNEL_WEIGHTS))[:-1]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Config:
    """Generator settings. Every field has a default; invalid values raise ``ValueError``."""

    seed: int = 42
    customers: int = 6000
    products: int = 160
    stores: int = 12
    orders: int = 30_000
    start: date = date(2024, 1, 1)
    months: int = 24
    affinity_pairs: int = 8
    affinity_boost: float = 0.35

    def __post_init__(self) -> None:
        minimums = {"customers": 1, "products": 1, "stores": 1, "months": 1, "orders": 0, "affinity_pairs": 0}
        for name, minimum in minimums.items():
            value = getattr(self, name)
            if not _is_int(value) or value < minimum:
                raise ValueError(f"Config.{name} must be an integer >= {minimum}, got {value!r}")
        if not _is_int(self.seed):
            raise ValueError(f"Config.seed must be an integer, got {self.seed!r}")
        boost = self.affinity_boost
        if isinstance(boost, bool) or not isinstance(boost, int | float) or not 0.0 <= boost <= 1.0:
            raise ValueError(f"Config.affinity_boost must be a number between 0 and 1, got {boost!r}")
        if not isinstance(self.start, date) or isinstance(self.start, datetime):
            raise ValueError(f"Config.start must be a datetime.date, got {self.start!r}")


def _seasonality(d: date) -> float:
    """Multiplicative demand factor: Q4 peak, summer dip, weekend bump."""
    yearly = 1.0 + 0.35 * math.cos((d.timetuple().tm_yday - 340) / 365.25 * 2 * math.pi)
    weekend = 1.25 if d.weekday() >= 5 else 1.0
    return yearly * weekend


def _product_weights(cfg: Config) -> list[float]:
    """Zipf-like popularity weight per product (index = product_id - 1), so ABC analysis is non-trivial.

    Uses its own RNG stream derived from ``cfg.seed`` so :func:`affinity_pairs` can
    reproduce the ranking without running the whole generator.
    """
    weights = [1.0 / (rank + 3) ** 0.9 for rank in range(cfg.products)]
    random.Random(f"popularity:{cfg.seed}").shuffle(weights)
    return weights


def affinity_pairs(cfg: Config) -> list[tuple[int, int]]:
    """Planted ``(anchor, partner)`` product-id pairs, deterministic for ``cfg.seed``.

    ``cfg.affinity_pairs`` pairs are drawn from the ``POPULAR_POOL`` most popular
    products; pairs never share a product while the pool allows it, and fewer pairs
    are returned only when the catalogue cannot supply that many distinct pairs.
    """
    weights = _product_weights(cfg)
    pool = sorted(range(1, cfg.products + 1), key=lambda pid: -weights[pid - 1])[:POPULAR_POOL]
    wanted = min(cfg.affinity_pairs, len(pool) * (len(pool) - 1) // 2)
    rng = random.Random(f"affinity:{cfg.seed}")
    rng.shuffle(pool)
    pairs = [(pool[2 * i], pool[2 * i + 1]) for i in range(min(wanted, len(pool) // 2))]
    seen = {frozenset(p) for p in pairs}
    while len(pairs) < wanted:  # more pairs than disjoint slots: allow shared products, never duplicate pairs
        a, b = rng.sample(pool, 2)
        if frozenset((a, b)) not in seen:
            seen.add(frozenset((a, b)))
            pairs.append((a, b))
    return pairs


def generate(cfg: Config | None = None) -> dict[str, list[tuple]]:
    """Return ``{"stores", "products", "customers", "orders", "order_items"}`` as lists of tuples."""
    if cfg is None:
        cfg = Config()
    rng = random.Random(cfg.seed)

    # Calendar: day index k <-> cfg.start + k days for k in [0, horizon); cumulative seasonality weights.
    horizon = int(cfg.months * 30.44)
    days = [cfg.start + timedelta(days=k) for k in range(horizon)]
    day_iso = [d.isoformat() for d in days]
    day_q4 = [d.month >= 10 for d in days]
    cum_w = list(itertools.accumulate(_seasonality(d) for d in days))

    # Stores: round-robin regions (then shuffled) guarantee coverage when there are enough stores.
    if cfg.stores >= len(REGIONS):
        regions = [REGIONS[i % len(REGIONS)] for i in range(cfg.stores)]
        rng.shuffle(regions)
    else:
        regions = [rng.choice(REGIONS) for _ in range(cfg.stores)]
    stores: list[tuple] = [
        (i + 1, f"Store {i + 1:03d}", regions[i], rng.choice(STORE_FORMATS)) for i in range(cfg.stores)
    ]
    # Per-region pick lists: store ids repeated by format traffic; a region without stores uses the whole chain.
    all_picks = [sid for sid, _name, _region, fmt in stores for _ in range(STORE_FORMAT_TRAFFIC[fmt])]
    picks_by_region = {r: [sid for sid in all_picks if regions[sid - 1] == r] or all_picks for r in REGIONS}

    products: list[tuple] = []
    cats = list(CATEGORIES)
    for i in range(cfg.products):
        cat = cats[i % len(cats)]
        lo, hi = CATEGORIES[cat]
        price = round(math.exp(rng.uniform(math.log(lo), math.log(hi))), 2)
        cost = round(price * rng.uniform(0.45, 0.75), 2)
        products.append((i + 1, f"SKU-{i + 1:05d}", f"{cat} Item {i + 1}", cat, price, cost))
    prices = [p[4] for p in products]
    weights = _product_weights(cfg)
    cum_pw = list(itertools.accumulate(weights))
    q4_weights = [w * Q4_CATEGORY_UPLIFT.get(p[3], 1.0) for w, p in zip(weights, products, strict=True)]
    cum_pw_q4 = list(itertools.accumulate(q4_weights))
    last_product = cfg.products - 1
    partners: dict[int, tuple[int, ...]] = {}
    for anchor, partner in affinity_pairs(cfg):
        partners[anchor] = partners.get(anchor, ()) + (partner,)
    boost = cfg.affinity_boost

    # Customers: signup, latent home channel, lifetime (depends on home channel) and buying intensity.
    customers: list[tuple] = []
    windows: list[tuple[int, int]] = []  # inclusive [signup day, last active day] as day indices
    homes: list[str] = []
    cust_region: list[str] = []
    masses: list[float] = []  # expected order share = intensity x seasonality mass of the active window
    for i in range(cfg.customers):
        lo_day = min(int(rng.triangular(0, horizon * 0.85, horizon * 0.15)), horizon - 1)
        home = CHANNELS[bisect_right(_CHANNEL_CUM, rng.random())]
        lifetime = rng.expovariate(1.0 / MEAN_LIFETIME_DAYS[home])
        intensity = HOME_CHANNEL_INTENSITY[home] * rng.lognormvariate(0.0, 0.4)  # heavy vs light buyers
        region = rng.choice(REGIONS)
        hi_day = min(lo_day + int(lifetime), horizon - 1)
        customers.append((i + 1, f"customer{i + 1}@example.com", region, day_iso[lo_day]))
        windows.append((lo_day, hi_day))
        homes.append(home)
        cust_region.append(region)
        masses.append(intensity * (cum_w[hi_day] - (cum_w[lo_day - 1] if lo_day else 0.0)))

    # Orders: pick the customer, then a seasonality-weighted day inside their window (slice of cum_w + bisect).
    cust_cum = list(itertools.accumulate(masses))
    draws: list[tuple[int, int]] = []
    for ci in rng.choices(range(cfg.customers), cum_weights=cust_cum, k=cfg.orders):
        lo_day, hi_day = windows[ci]
        base = cum_w[lo_day - 1] if lo_day else 0.0
        k = bisect_right(cum_w, base + rng.random() * (cum_w[hi_day] - base), lo_day, hi_day + 1)
        draws.append((k if k <= hi_day else hi_day, ci))
    draws.sort(key=lambda t: t[0])  # chronological order ids; stable sort keeps the draw order within a day

    orders: list[tuple] = []
    items: list[tuple] = []
    item_id = 0
    for order_id, (k, ci) in enumerate(draws, 1):
        if rng.random() < HOME_CHANNEL_LOYALTY:
            channel = homes[ci]
        else:
            channel = CHANNELS[bisect_right(_CHANNEL_CUM, rng.random())]
        store_id = rng.choice(picks_by_region[cust_region[ci]])
        orders.append((order_id, ci + 1, store_id, channel, day_iso[k]))

        cw = cum_pw_q4 if day_q4[k] else cum_pw
        total = cw[-1]
        n_lines = max(1, int(rng.lognormvariate(0.6, 0.6)))
        basket: list[int] = []
        for _ in range(n_lines):  # same weighted draw as random.choices(), without the per-call overhead
            pid = bisect_right(cw, rng.random() * total, 0, last_product) + 1
            if pid not in basket:
                basket.append(pid)
        if partners:
            for pid in tuple(basket):
                for partner in partners.get(pid, ()):
                    if partner not in basket and rng.random() < boost:
                        basket.append(partner)
        for pid in basket:
            item_id += 1
            qty = 1 if rng.random() < 0.7 else rng.randint(2, 4)
            price = prices[pid - 1]
            items.append((item_id, order_id, pid, qty, price, round(rng.choice(DISCOUNT_RATES) * price, 2)))

    return {
        "stores": stores,
        "products": products,
        "customers": customers,
        "orders": orders,
        "order_items": items,
    }
