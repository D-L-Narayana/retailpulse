# RetailPulse — Python + SQL Retail Analytics Dashboard

[![CI](https://github.com/D-L-Narayana/retailpulse/actions/workflows/ci.yml/badge.svg)](https://github.com/D-L-Narayana/retailpulse/actions)
**Live dashboard:** https://d-l-narayana.github.io/retailpulse/

RetailPulse is a small, dependency-free analytics pipeline: it generates a
realistic retail transaction dataset, loads it into a normalised SQLite schema,
runs ten analytical SQL queries (window functions, CTEs, cohort logic, RFM,
ABC classification, market-basket affinity, lifetime value) and renders the
results into a single static HTML dashboard with inline SVG charts, CSV
exports and a machine-readable `data.json`. The whole build runs in a few
seconds and is byte-for-byte reproducible.

```
generate.py ──► SQLite (5 tables, FKs, CHECKs, indexes, view) ──► sql/*.sql (+ metadata headers)
                                                                      │
          registry.py (reads the headers) ──► report.py ──► public/index.html
                                                      └──► public/data.json + public/csv/*.csv
```

## Why it exists

Retail analytics questions ("which SKUs drive 70 % of revenue?", "do customers
acquired online come back as often as in-store ones?", "how does each monthly
cohort retain?", "which products are bought together?") are best answered in
SQL. This project keeps every metric definition in a `.sql` file that is
visible on the dashboard itself, so the numbers are auditable and the queries
are reusable against any warehouse.

## SQL queries

Each query file starts with a small metadata header (`-- @title`, `-- @question`,
`-- @technique`, `-- @chart`, `-- @limit`, `-- @portability`). The dashboard
sections, the CSV exports and the table below are generated from those headers —
adding a metric means adding one `.sql` file. Regenerate this table with
`python -m retailpulse docs`; CI fails if it drifts (`docs --check`).

<!-- queries:start -->
| File | Technique | Question answered |
|---|---|---|
| `01_monthly_revenue.sql` | LAG (1 and 12 rows back), trailing-window AVG, NULL-safe percentages | How is net revenue trending month over month and year over year, and what margin does it carry? |
| `02_category_abc.sql` | LEFT JOIN from products, ROW_NUMBER and running SUM() OVER with a deterministic tie-break | Which SKUs make up the top 70 % (A), next 20 % (B) and last 10 % (C) of revenue, and which never sold at all? |
| `03_customer_rfm.sql` | Rank-based quintiles (RANK() + COUNT(*) OVER) so equal values get equal scores, CASE segmentation | Which customers are champions, loyal, new, at risk or lost, judged by how recently, how often and how much they buy? |
| `04_cohort_retention.sql` | Recursive CTE (offsets 0-11) cross-joined with cohorts for a full observable grid, LEFT JOIN to activity, right-censoring at the latest order month | Of the customers acquired in a given month, what share bought again 1 to 11 months later? |
| `05_store_performance.sql` | LEFT JOIN from stores, DENSE_RANK within region with a deterministic tie-break, share via SUM() OVER (PARTITION BY) | Which stores lead their region on revenue, how large are their baskets and margins, and which stores took no orders at all? |
| `06_channel_mix.sql` | Conditional-aggregation pivot (SUM(CASE WHEN ...)), COALESCE so a channel with no sales reads 0 | How is monthly revenue split between in-store, online and pickup, and is the online share growing? |
| `07_repeat_purchase_rate.sql` | ROW_NUMBER() to isolate the first order, LEAD() for the second, median via ROW_NUMBER/COUNT window over per-customer gaps | Do customers acquired online, in store or via pickup come back for a second order, and how quickly? |
| `08_data_quality.sql` | anti-joins, invariants | Does the loaded dataset satisfy every referential, temporal and pricing invariant the analytics rely on? |
| `09_basket_affinity.sql` | order_items self-join (market-basket analysis), support / confidence / lift, relative minimum-support floor | Which product pairs land in the same basket far more often than chance predicts, and how strongly does buying one predict the other? |
| `10_customer_ltv.sql` | ROW_NUMBER first-order attribution, single-pass revenue aggregation, recursive CTE offset spine, cumulative SUM OVER, right-censoring filter | Do customers acquired online come back and spend more over their first twelve months than customers acquired in store or via pickup? |
<!-- queries:end -->

Every file also carries `@portability` notes listing the SQLite-specific
constructs it uses (`substr()` month bucketing, `julianday()`, recursive CTEs)
and their PostgreSQL / BigQuery equivalents. The queries need SQLite ≥ 3.31
(window functions), which ships with every supported Python.

## Schema

`stores`, `products`, `customers`, `orders`, `order_items` — third normal form,
foreign keys enforced (`PRAGMA foreign_keys = ON`), `CHECK` constraints on
prices/quantities/enums/ISO dates, composite indexes on hot join columns, and a
`v_sales` view so revenue arithmetic lives in exactly one place.

## Run it

```bash
pip install -e .[dev]                      # or just run the module from a checkout
retailpulse build --out public             # index.html + data.json + csv/, data-quality gate
retailpulse build --orders 200000 --db retail.db --out public   # bigger dataset, keep the DB
retailpulse check                          # data-quality checks only (exit 1 on errors)
retailpulse query 03_customer_rfm --format csv --limit 20       # one query to stdout
retailpulse export --out exports --format csv                   # every query as CSV
retailpulse list                           # query names and titles
retailpulse docs --check                   # README query table in sync?
python -m pytest                           # test suite with coverage floor
```

`python -m retailpulse build --out public` keeps working exactly as before.
Every generator parameter is a flag: `--seed`, `--orders`, `--customers`,
`--products`, `--stores`, `--months`, `--start YYYY-MM-DD`. `--db PATH` refuses
to overwrite an existing database unless you pass `--replace`. `--summary PATH`
writes a Markdown build summary (KPIs plus the data-quality table) — point it at
`$GITHUB_STEP_SUMMARY` in CI. `--no-csv` skips the CSV exports.

No third-party dependencies at runtime (Python ≥ 3.10 standard library only).

### CSV cells are inert by default

Spreadsheet applications treat a cell that starts with `=`, `+`, `-` or `@` —
also when that character hides behind leading whitespace, control or zero-width
characters — as a formula. Because `--db` reuses whatever an existing database
contains, every reporting CSV (`build` downloads under `csv/`,
`export --format csv`, `query --format csv`) prefixes such *text* cells with a
single apostrophe (`'=1+1`). Numbers (a real `-12.5` stays `-12.5`), empty
cells, ordinary text and the header row are written unchanged, and JSON output /
`data.json` are never altered. Pass `--raw-csv` for lossless machine output; raw
files are for programs, not for opening in a spreadsheet unprotected, and the
flag is rejected when a command writes no CSV (`--format json`, the default
`table` format of `query`, or `build --no-csv`). The behaviour is verified by
parsing the output with Python's `csv` module and through the CLI; no
spreadsheet application was tested.

### Reproducible builds

Set `SOURCE_DATE_EPOCH` and two builds with the same configuration produce
byte-identical `index.html` and `data.json` (the timestamp comes from the epoch
and the elapsed-time figure is omitted).

### Data quality

`08_data_quality.sql` reports `check_name, severity, violations, description`
for sixteen invariants. The twelve `error` checks must be zero or the build
exits non-zero, so it can gate a CI pipeline:

`orders_without_items`, `orphan_items`, `orphan_item_products`,
`orphan_order_customers`, `orphan_order_stores`, `orders_before_signup`,
`malformed_order_dates`, `malformed_signup_dates`, `nonpositive_quantity`,
`discount_exceeds_price`, `negative_net_revenue`, `duplicate_emails`.

The four `warn` checks are informational and expected to be non-zero on
realistic data: `customers_without_orders`, `products_never_sold`,
`stores_without_orders`, `negative_gross_margin`. `--no-fail-on-dq` turns the
gate off. The test suite injects each corruption into a copy of the dataset and
proves that exactly the matching check fires.

## Dashboard

* **Zero JavaScript, strict CSP.** Charts are SVG strings produced in Python;
  the page carries a `Content-Security-Policy` meta tag whose `style-src` is the
  SHA-256 hash of its single stylesheet, has no inline `style=` attributes and
  loads no external resources. `vercel.json` sends the same policy plus
  `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options` and
  `Permissions-Policy` headers.
* **Readable anywhere.** Dark mode via `prefers-color-scheme`, print styles,
  a table of contents, skip link, table captions, titled SVGs and tooltips on
  the cohort heatmap (which distinguishes "0 % retained" from "not yet
  observable").
* **Downloadable.** Every section links to its CSV; `data.json` carries all
  results plus build metadata (seed, version, configuration, data source).
* **Synthetic data, disclosed.** Every build renders data generated by
  `retailpulse.generate` — there is no real customer data anywhere. `build`
  records `"data_source": "synthetic"` in `data.json`'s `meta`; the dashboard
  header reads "synthetic demo dataset" next to the generation timestamp, the
  page description says "synthetic retail dataset", and the `--summary`
  Markdown carries a `Data source | synthetic demo dataset` row, so the
  revenue and customer figures are never mistaken for a real business. Pages
  rendered from metadata without that key are not labelled, so library users
  rendering their own results are never mislabelled.

## Design notes

* **Deterministic data.** `Config.seed` makes every run reproducible; tests
  assert identical output for identical seeds and that exactly `Config.orders`
  orders are produced for any configuration (30 000 orders generate in about
  0.15 s, 200 000 in about a second).
* **Realistic structure.** Zipf-like product popularity (so ABC is meaningful),
  Q4 seasonality + weekend uplift with a Toys/Electronics holiday boost,
  exponential customer lifetimes (so retention decays), every region guaranteed
  a store with traffic weighted by store format, planted product-affinity pairs
  (`Config.affinity_pairs`, `Config.affinity_boost`) and a latent home channel
  per customer with channel-dependent lifetime and purchase frequency — so the
  README's own question has an answer in the data: at the default seed,
  customers acquired online repeat at roughly 87 % against 81 % in-store and
  78 % pickup.
* **Deterministic analytics.** Every window function has a full tie-break, so
  ABC classes and RFM scores are stable across SQLite builds; zero-sales SKUs
  and zero-order stores appear in their league tables; channel-mix columns are
  never `NULL`.
* **Testing.** Query invariants (cohort month 0 = 100 %, regional shares sum to
  100, ABC classes monotonic, RFM scores in 1..5), negative data-quality tests
  that inject each corruption and prove the matching check fires, schema
  constraint tests, `EXPLAIN QUERY PLAN` tests, renderer/CSP tests, HTML
  well-formedness, a seed sweep, reproducibility and performance budgets.

## Development

```bash
pip install -e .[dev]
ruff check .
mypy
python -m pytest --cov --cov-fail-under=90
```

CI runs the same steps on Python 3.10–3.14, builds the dashboard, and installs
the package non-editably to prove the SQL files ship with it.

## Deploy

* **GitHub Pages** — `.github/workflows/deploy.yml` tests, builds and publishes
  `public/` on every push to `main`. Pages cannot send custom headers, so the
  CSP is enforced through the page's `<meta http-equiv>` tag there.
* **Vercel** — `vercel.json` runs the build command, serves `public/` and adds
  the response security headers.

## License

MIT
