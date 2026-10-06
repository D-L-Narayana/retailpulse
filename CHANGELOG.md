# Changelog

All notable changes to RetailPulse are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses semantic versioning.

## [0.2.0] — 2026-10-06

### Added
- **Two new analytical queries.** `09_basket_affinity.sql` (market-basket pairs with support, confidence
  and lift via an `order_items` self-join; minimum support scales with the order count) and
  `10_customer_ltv.sql` (12-month cumulative revenue per customer by acquisition channel, restricted to
  cohorts observable for all twelve months so every curve is computed over the same customers).
- **Query metadata registry.** Each `.sql` file carries a `-- @title / @question / @technique / @chart /
  @limit / @portability` header; `retailpulse/registry.py` parses it to drive dashboard sections and the
  README query table (`retailpulse docs`, with `docs --check` as a drift gate).
- **CLI subcommands.** `build`, `check` (data-quality only), `query NAME --format table|csv|json`,
  `export`, `docs`, `list`, `--version`; every `Config` field is exposed as a flag (`--customers`,
  `--products`, `--stores`, `--months`, `--start`); `--db` refuses to overwrite an existing file unless
  `--replace`; `--summary PATH` writes a Markdown summary suitable for `$GITHUB_STEP_SUMMARY`.
- **CSV exports.** `build` writes `public/csv/<query>.csv` for every query and the dashboard links to
  them and to `data.json` (`--no-csv` to disable).
- **Reproducible builds.** Setting `SOURCE_DATE_EPOCH` makes `index.html` and `data.json`
  byte-identical across runs for the same configuration.
- **Data-quality severities.** `08_data_quality.sql` reports `check_name, severity, violations,
  description`: twelve `error` checks (orphans in every direction, orders before signup, malformed ISO
  dates, discounts above price, non-positive quantities, negative net revenue, duplicate e-mails) and four
  `warn` checks (negative gross margin, products never sold, stores and customers without orders). The
  build fails only on `error` violations (`--no-fail-on-dq` to override).
- **Content-Security-Policy-ready dashboard.** The page carries a `<meta http-equiv="Content-Security-Policy">`
  whose `style-src` is the SHA-256 hash of its single stylesheet, uses no inline `style=` attributes, no
  JavaScript and no external resources; `vercel.json` sends the matching production policy plus
  `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options` and `Permissions-Policy` headers.
- **Dashboard quality.** Dark mode (`prefers-color-scheme`), print styles, table of contents, skip link,
  section anchors, table captions, titled SVGs, heatmap tooltips, and distinct rendering for "0 % retained"
  versus "not yet observable" cohort cells.
- **Visible synthetic-data disclosure.** The dashboard header states "synthetic demo dataset" in rendered
  text (previously only the invisible `<meta name="description">` said so), the `--summary` Markdown
  carries a `Data source | synthetic demo dataset` row, and `data.json` carries
  `meta.data_source = "synthetic"`; `retailpulse build` always sets it because it always generates its data.
  The renderer labels only metadata that declares the data synthetic, so library callers rendering other
  data are never mislabelled.
- **Schema hardening.** ISO-date `CHECK` constraints on order and signup dates, `PRAGMA user_version = 2`,
  additive `v_sales` columns (`unit_price`, `discount`, `unit_cost`, `sku`), validated SQL lookup
  (`db.load_sql` raises `UnknownQueryError` listing the valid names), read-only connections
  (`db.connect(path, read_only=True)`), and an atomic, tuned bulk loader that relaxes durability pragmas
  only for the duration of the load and restores them afterwards (200 000 orders into a file database:
  ~18 s → ~3.3 s).
- **Generator realism.** Exact order counts for any configuration, O(n) seasonality-weighted sampling
  (30 000 orders in ~0.15 s, 200 000 in ~1 s), guaranteed regional store coverage, store traffic weighted by
  format, planted product-affinity pairs (`Config.affinity_pairs`, `Config.affinity_boost`,
  `generate.affinity_pairs()`), home-channel loyalty with channel-dependent lifetime and purchase
  frequency, a Q4 uplift for Toys and Electronics, and `Config` validation that raises `ValueError`.
- **Packaging and CI.** `retailpulse` console script, dynamic version, PEP 639 licence metadata,
  `py.typed`, ruff/mypy/coverage configuration, a CI matrix for Python 3.10–3.14, an installed-package
  smoke job, least-privilege workflow permissions, and Dependabot.

### Changed
- `01_monthly_revenue` gains `yoy_pct` (year-over-year, `NULL` until a full year of history exists).
- `02_category_abc` ranks with `ROW_NUMBER` (ties broken by `product_id`, so classes are stable across
  SQLite builds) and lists every SKU; unsold SKUs appear as class `C` with zero revenue.
- `05_store_performance` lists every store (zero-order stores show `0` orders and a `0.0` regional share)
  and uses a warehouse-portable `GROUP BY`.
- `06_channel_mix` columns are never `NULL`; `in_store_share_pct` and `pickup_share_pct` are added.
- `03_customer_rfm` derives quintile scores from `RANK()` so equal values always receive equal scores.
- `04_cohort_retention` returns the full observable grid (explicit zero rows; right-censored cells absent).
- `07_repeat_purchase_rate` adds `median_days_to_second_order`.
- `10_customer_ltv` aggregates revenue in a single pass before customer attribution (≈ 0.1 s at the
  default size).
- Order ids are assigned chronologically; `db.load()` inside a caller-held transaction uses a
  `SAVEPOINT` and leaves commit/rollback to the caller.
- Dashboard colours live in a single token table (`retailpulse/theme.py`) rendered as CSS custom
  properties.

### Fixed
- Rendering crashed on datasets where a month had no online revenue (`max()` over `None`).
- `--db` on an existing database raised a raw `OperationalError`.
- `data.json` could emit non-strict JSON (`NaN`) and its key order was unstable.
- The generator could silently return fewer orders than requested and leave regions without stores.
- The test fixture could leave an implicit write transaction open after a deliberately failing
  `INSERT`, which made a later `Connection.backup()` block indefinitely; the shared connection is now
  rolled back before and after every test.

### Security
- **CSV formula injection.** Reporting CSVs (`build` downloads, `export --format csv`, `query --format csv`)
  now neutralise formula-like text cells (`= + - @`, including after leading whitespace/control/zero-width
  characters) with a leading apostrophe. Numeric values, empty cells, headers, JSON output and the database
  are unchanged; the default sample build is byte-identical. New `--raw-csv` flag (and
  `export.rows_to_csv/write_csv/write_all(raw=True)`, `cli.build(raw_csv=True)`) restores verbatim cells for
  machine consumers and is a usage error when no CSV is written. Verified with `csv`-module parsing and CLI
  tests on a synthetic database; not tested against a spreadsheet application.

## [0.1.0]

- Initial release: deterministic synthetic retail dataset, SQLite schema with constraints and indexes,
  eight analytical SQL queries, single-file HTML dashboard with inline SVG charts, GitHub Pages / Vercel
  static deployment.
