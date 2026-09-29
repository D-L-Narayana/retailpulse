# RetailPulse — Python + SQL Retail Analytics Dashboard

[![CI](https://github.com/D-L-Narayana/retailpulse/actions/workflows/ci.yml/badge.svg)](https://github.com/D-L-Narayana/retailpulse/actions)
**Live dashboard:** https://retailpulse-dln.vercel.app

RetailPulse is a small, dependency-free analytics pipeline: it generates a
realistic retail transaction dataset, loads it into a normalised SQLite schema,
runs eight analytical SQL queries (window functions, CTEs, cohort logic, RFM,
ABC classification) and renders the results into a single static HTML dashboard
with inline SVG charts. The whole build runs in about one second.

```
generate.py  ──►  SQLite (5 tables, FKs, CHECKs, indexes, view)  ──►  sql/*.sql  ──►  report.py  ──►  public/index.html
```

## Why it exists

Retail analytics questions ("which SKUs drive 70 % of revenue?", "do customers
acquired online come back as often as in-store ones?", "how does each monthly
cohort retain?") are best answered in SQL. This project keeps every metric
definition in a `.sql` file that is visible on the dashboard itself, so the
numbers are auditable and the queries are reusable against any warehouse.

## SQL queries

| File | Technique | Question answered |
|---|---|---|
| `01_monthly_revenue.sql` | `LAG`, trailing-window `AVG` | Monthly revenue, margin, MoM growth |
| `02_category_abc.sql` | running `SUM() OVER`, `RANK` | ABC classification of SKUs (Pareto) |
| `03_customer_rfm.sql` | `NTILE(5)` quintiles | Recency/Frequency/Monetary segments |
| `04_cohort_retention.sql` | self-join cohort matrix | Monthly cohort retention heatmap |
| `05_store_performance.sql` | `DENSE_RANK PARTITION BY` | Store league table by region |
| `06_channel_mix.sql` | conditional-aggregation pivot | In-store / online / pickup share |
| `07_repeat_purchase_rate.sql` | `ROW_NUMBER` first-order isolation | Repeat rate by acquisition channel |
| `08_data_quality.sql` | anti-joins, invariants | Orphans, negatives, date violations |

## Schema

`stores`, `products`, `customers`, `orders`, `order_items` — third normal form,
foreign keys enforced (`PRAGMA foreign_keys = ON`), `CHECK` constraints on
prices/quantities/enums, composite indexes on hot join columns, and a `v_sales`
view so revenue arithmetic lives in exactly one place.

## Run it

```bash
python -m retailpulse build --out public          # ~1 s, writes index.html + data.json
python -m retailpulse build --orders 200000 --db retail.db   # bigger dataset, keep the DB
python -m pytest --cov=retailpulse                 # 21 tests, 96 % coverage
```

No third-party dependencies at runtime (Python ≥ 3.10 standard library only).
The CLI exits non-zero if any data-quality check reports a violation, so it can
gate a CI pipeline.

## Design notes

* **Deterministic data.** `Config.seed` makes every run reproducible; tests
  assert identical output for identical seeds.
* **Realistic structure.** Zipf-like product popularity (so ABC is meaningful),
  Q4 seasonality + weekend uplift, exponential customer lifetimes (so retention
  decays), regional store assignment.
* **Zero-JS dashboard.** Charts are SVG strings produced in Python; the output
  is one HTML file that Vercel serves as a static asset.
* **Testing.** Query invariants are tested (cohort month 0 = 100 %, regional
  shares sum to 100, ABC classes monotonic, RFM scores in 1..5), plus schema
  constraint tests and an `EXPLAIN QUERY PLAN` test proving the index is used.

## Deploy

`vercel.json` runs the build command on Vercel and serves `public/`.

## License

MIT
