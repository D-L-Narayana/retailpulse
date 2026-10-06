"""Command line interface: ``python -m retailpulse <command>`` (also installed as ``retailpulse``).

Commands::

    retailpulse build  [--out DIR] [generator options] [--db PATH] [--replace] [--no-csv]
                       [--no-fail-on-dq] [--summary PATH] [--quiet]
    retailpulse check  [generator options] [--db PATH]
    retailpulse query  NAME [--db PATH] [generator options] [--format table|csv|json] [--limit N]
    retailpulse export [--out DIR] [--db PATH] [generator options] [--format csv|json]
    retailpulse docs   [--check] [--readme PATH]
    retailpulse list
    retailpulse --version

Generator options (``--seed --orders --customers --products --stores --months --start``) are shared
by every data command; options left unset take the :class:`retailpulse.generate.Config` defaults.
``check``, ``query`` and ``export`` reuse an existing ``--db`` file as-is (read-only) instead of
generating data.

Exit codes: 0 ok · 1 data-quality ``error`` violations · 2 usage or validation problem (reported
argparse-style on stderr, never as a traceback).

Reproducible builds: when ``SOURCE_DATE_EPOCH`` is set, ``meta["generated_at"]`` is derived from it
and ``meta["elapsed_ms"]`` is ``None``, so two builds of the same configuration are byte-identical
(``data.json`` is dumped with sorted keys and without NaN/Infinity).

Build metadata (``data.json`` ``meta``): the documented counts plus ``seed``, ``version``, ``config``,
``exports`` and ``data_source`` — always ``"synthetic"`` for ``build``, which only renders data it has just
generated; the renderer and the ``--summary`` Markdown disclose that visibly.

CSV output (``build`` downloads, ``export --format csv``, ``query --format csv``) is inert by default:
string cells that a spreadsheet would read as a formula (starting with ``= + - @``, also behind
leading whitespace/control characters) are prefixed with an apostrophe; numbers, ``None`` and the
header row are untouched and JSON is never altered. ``--raw-csv`` writes the cells verbatim for
machine consumers and is a usage error when the command writes no CSV. See
:mod:`retailpulse.export` for the exact policy.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, db, export, generate, report

Rows = list[dict[str, Any]]

GENERATOR_OPTIONS = ("seed", "orders", "customers", "products", "stores", "months", "start")
DQ_QUERY = "08_data_quality"
REVENUE_QUERY = "01_monthly_revenue"
# Objects every RetailPulse database carries; anything else is refused by check/query/export.
REQUIRED_OBJECTS = ("stores", "products", "customers", "orders", "order_items", "v_sales")
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M UTC"
README_MARKERS = ("<!-- queries:start -->", "<!-- queries:end -->")
COUNT_TABLES = {
    "customers": "customers",
    "orders": "orders",
    "line_items": "order_items",
    "products": "products",
    "stores": "stores",
}


class UsageError(ValueError):
    """A validation problem reported like an argparse usage error: one line on stderr, exit code 2."""


@dataclasses.dataclass
class Dataset:
    """An open connection plus the row counts shown in the dashboard header and the summary."""

    conn: sqlite3.Connection
    counts: dict[str, int]
    generated: bool  # False when an existing --db file was opened as-is


# --------------------------------------------------------------------------- reproducibility


def generated_at(source_date_epoch: str | None) -> tuple[str, bool]:
    """Return ``(timestamp, pinned)``: the UTC wall clock, or the time of ``SOURCE_DATE_EPOCH`` when set."""
    text = (source_date_epoch or "").strip()
    if not text:
        return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT), False
    stamp: datetime | None = None
    try:
        epoch = int(text)
        if epoch >= 0:
            stamp = datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        stamp = None
    if stamp is None:
        raise UsageError(
            "SOURCE_DATE_EPOCH must be a non-negative integer (seconds since 1970-01-01 UTC), "
            f"got {source_date_epoch!r}"
        )
    return stamp.strftime(TIMESTAMP_FORMAT), True


def config_dict(cfg: generate.Config) -> dict[str, Any]:
    """Every ``Config`` field as a JSON-ready value (dates become ISO strings)."""
    return {name: _jsonable(value) for name, value in dataclasses.asdict(cfg).items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, date):  # datetime is a date subclass; both serialise as ISO text
        return value.isoformat()
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


# --------------------------------------------------------------------------- data-quality gate


def _severity(row: Mapping[str, Any]) -> str:
    """Effective severity: only an explicit ``warn`` is advisory; a missing or unknown value gates the build."""
    return "warn" if str(row.get("severity") or "").strip().lower() == "warn" else "error"


def _violations(row: Mapping[str, Any]) -> int:
    return int(row.get("violations") or 0)


def dq_failures(rows: Sequence[Mapping[str, Any]]) -> tuple[Rows, Rows]:
    """Split data-quality rows that report violations into ``(errors, warnings)`` by effective severity."""
    errors = [dict(r) for r in rows if _violations(r) > 0 and _severity(r) == "error"]
    warnings = [dict(r) for r in rows if _violations(r) > 0 and _severity(r) == "warn"]
    return errors, warnings


def _md(value: Any) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def dq_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """Markdown table ``| check_name | severity | violations [| description] |`` of the data-quality rows."""
    with_description = any("description" in r for r in rows)
    header = ["check_name", "severity", "violations"] + (["description"] if with_description else [])
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for r in rows:
        cells = [_md(r.get("check_name")), _severity(r), str(_violations(r))]
        if with_description:
            cells.append(_md(r.get("description")))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def report_dq(rows: Sequence[Mapping[str, Any]], *, fail_on_dq: bool = True, quiet: bool = False) -> int:
    """Print the data-quality verdict and return the exit code (1 only for ``error`` rows with violations)."""
    errors, warnings = dq_failures(rows)
    if errors:
        total = sum(_violations(r) for r in errors)
        names = ", ".join(str(r.get("check_name")) for r in errors)
        note = "" if fail_on_dq else " (exit status left at 0 because of --no-fail-on-dq)"
        print(f"DATA QUALITY FAILED: {total} violation(s) in {len(errors)} check(s): {names}{note}", file=sys.stderr)
        return 1 if fail_on_dq else 0
    if not quiet:
        print("Data quality: PASS")
        if warnings:
            detail = ", ".join(f"{r.get('check_name')}={_violations(r)}" for r in warnings)
            print(f"Data-quality warnings ({len(warnings)} advisory check(s), never fatal): {detail}")
    return 0


def summary_markdown(
    meta: Mapping[str, Any],
    dq_rows: Sequence[Mapping[str, Any]],
    *,
    revenue: float | None = None,
) -> str:
    """Markdown build summary (KPIs + data-quality table), suitable for ``$GITHUB_STEP_SUMMARY``."""
    errors, warnings = dq_failures(dq_rows)
    elapsed = meta.get("elapsed_ms")
    kpis: list[tuple[str, str]] = [
        ("Orders", f"{int(meta.get('orders', 0)):,}"),
        ("Line items", f"{int(meta.get('line_items', 0)):,}"),
        ("Customers", f"{int(meta.get('customers', 0)):,}"),
        ("Products", f"{int(meta.get('products', 0)):,}"),
        ("Stores", f"{int(meta.get('stores', 0)):,}"),
    ]
    if revenue is not None:
        kpis.append(("Net revenue", f"${revenue:,.2f}"))
    kpis.append(("Elapsed", f"{elapsed:,} ms" if isinstance(elapsed, int) else "not recorded (SOURCE_DATE_EPOCH set)"))
    kpis.append(("Seed", str(meta.get("seed", "n/a"))))
    kpis.append(("Generated", str(meta.get("generated_at", ""))))
    if meta.get("data_source") == "synthetic":
        kpis.append(("Data source", "synthetic demo dataset"))
    if errors:
        names = ", ".join(str(r.get("check_name")) for r in errors)
        status = f"Data quality: FAIL — {sum(_violations(r) for r in errors)} violation(s) in {names}"
    else:
        status = "Data quality: PASS" + (f" ({len(warnings)} advisory warning(s))" if warnings else "")
    lines = [
        f"## RetailPulse {meta.get('version', __version__)} build",
        "",
        "| KPI | Value |",
        "|---|---|",
        *(f"| {k} | {v} |" for k, v in kpis),
        "",
        f"**{status}**",
        "",
        dq_table(dq_rows),
    ]
    return "\n".join(lines) + "\n"


def _write_summary(markdown: str, target: str | Path) -> None:
    if str(target) == "-":
        sys.stdout.write(markdown)
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:  # append, so several steps can share one summary file
        fh.write(markdown)


# --------------------------------------------------------------------------- output formats


def _cell(value: Any) -> str:
    return "" if value is None else str(value)


def text_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """Aligned plain-text table: numbers right-aligned, text left-aligned, a dashed rule under the header."""
    if not rows:
        return "(no rows)\n"
    columns = list(rows[0])
    numeric = [all(r.get(c) is None or isinstance(r.get(c), int | float) for r in rows) for c in columns]
    cells = [[_cell(r.get(c)) for c in columns] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)]

    def line(values: Sequence[str]) -> str:
        parts = zip(values, widths, numeric, strict=True)
        return "  ".join(v.rjust(w) if right else v.ljust(w) for v, w, right in parts).rstrip()

    body = [line(columns), "  ".join("-" * w for w in widths), *(line(row) for row in cells)]
    return "\n".join(body) + "\n"


def format_rows(rows: Sequence[Mapping[str, Any]], fmt: str, *, raw: bool = False) -> str:
    if fmt == "csv":
        return export.rows_to_csv(rows, raw=raw)
    if fmt == "json":
        return json.dumps([dict(r) for r in rows], indent=1, allow_nan=False) + "\n"
    if fmt == "table":
        return text_table(rows)
    raise ValueError(f"unsupported format {fmt!r}")


# --------------------------------------------------------------------------- datasets


def _counts_from_data(data: Mapping[str, Sequence[Any]]) -> dict[str, int]:
    return {key: len(data[table]) for key, table in COUNT_TABLES.items()}


def _counts_from_db(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        key: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for key, table in COUNT_TABLES.items()
    }


def _generate_into(conn: sqlite3.Connection, cfg: generate.Config) -> dict[str, int]:
    data = generate.generate(cfg)
    db.create_schema(conn)
    db.load(conn, data)
    return _counts_from_data(data)


def _open_existing(path: Path) -> sqlite3.Connection:
    """Open an existing database read-only and make sure it is a RetailPulse database."""
    try:
        conn = db.connect(path, read_only=True)
    except sqlite3.Error as exc:
        raise UsageError(f"cannot open {path}: {exc}") from exc
    try:
        present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    except sqlite3.Error as exc:
        conn.close()
        raise UsageError(f"cannot read {path}: {exc}") from exc
    missing = [name for name in REQUIRED_OBJECTS if name not in present]
    if missing:
        conn.close()
        raise UsageError(f"{path} is not a RetailPulse database (missing: {', '.join(missing)})")
    return conn


def _remove_database(path: Path) -> None:
    sidecars = (path.with_name(path.name + suffix) for suffix in ("-journal", "-wal", "-shm"))
    for candidate in (path, *sidecars):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            raise UsageError(f"cannot replace {candidate}: {exc}") from exc


def prepare_dataset(
    db_path: str | Path | None,
    cfg: generate.Config,
    *,
    reuse_existing: bool,
    replace: bool = False,
    options_given: bool = False,
) -> Dataset:
    """Generate ``cfg`` into memory or into ``db_path``, or (check/query/export) reuse an existing file.

    ``build`` passes ``reuse_existing=False``: an existing ``--db`` file is refused unless ``replace``.
    """
    path = Path(db_path) if db_path else None
    if path is not None and path.exists():
        if reuse_existing:
            if options_given:
                print(f"note: {path} exists and is used as-is; generator options are ignored", file=sys.stderr)
            conn = _open_existing(path)
            return Dataset(conn, _counts_from_db(conn), generated=False)
        if not replace:
            raise UsageError(f"database file {path} already exists; pass --replace to overwrite it or use another --db")
        _remove_database(path)
    try:
        conn = db.connect(path if path is not None else ":memory:")
    except sqlite3.Error as exc:
        raise UsageError(f"cannot create database file {path}: {exc}") from exc
    return Dataset(conn, _generate_into(conn, cfg), generated=True)


def _sql_text(names: Sequence[str]) -> dict[str, str]:
    return {name: db.load_sql(name) for name in names}


# --------------------------------------------------------------------------- build


def build(
    out: str | Path,
    seed: int | None = None,
    orders: int | None = None,
    db_path: str | Path | None = None,
    *,
    config: generate.Config | None = None,
    export_csv: bool = True,
    fail_on_dq: bool = True,
    summary: str | Path | None = None,
    quiet: bool = False,
    replace: bool = False,
    raw_csv: bool = False,
) -> int:
    """Generate data, load SQLite, run every query, render the dashboard; return the exit code.

    The first four parameters keep the original positional signature: ``seed``/``orders`` override the
    matching fields of ``config`` (default ``generate.Config()``) when given. ``summary`` is a Markdown
    file path to append to, or ``"-"`` for stdout. CSV downloads are written with inert formula-like
    cells unless ``raw_csv`` (see :mod:`retailpulse.export`); ``raw_csv`` together with
    ``export_csv=False`` is rejected because it would silently do nothing.
    """
    out = Path(out)
    if raw_csv and not export_csv:
        raise UsageError("--raw-csv has no effect together with --no-csv (no CSV files are written)")
    cfg = config if config is not None else generate.Config()
    if seed is not None:
        cfg = dataclasses.replace(cfg, seed=seed)
    if orders is not None:
        cfg = dataclasses.replace(cfg, orders=orders)
    stamp, pinned = generated_at(os.environ.get("SOURCE_DATE_EPOCH"))

    t0 = time.perf_counter()
    dataset = prepare_dataset(db_path, cfg, reuse_existing=False, replace=replace)
    try:
        results = {name: db.run_query(dataset.conn, name) for name in db.list_queries()}
    finally:
        dataset.conn.close()
    sql_text = _sql_text(list(results))
    elapsed_ms = round((time.perf_counter() - t0) * 1000)

    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(f"cannot create output directory {out}: {exc}") from exc
    exports = export.write_all(out, results, raw=raw_csv) if export_csv else {}
    meta: dict[str, Any] = {
        "generated_at": stamp,
        "elapsed_ms": None if pinned else elapsed_ms,
        **dataset.counts,
        "seed": cfg.seed,
        "version": __version__,
        # build only ever renders data it has just generated, so the dataset is always synthetic; the
        # renderer labels the page (header + description meta) from this key.
        "data_source": "synthetic",
        "config": config_dict(cfg),
        "exports": exports,
    }
    (out / "index.html").write_text(report.build_html(results, sql_text, meta), encoding="utf-8")
    payload = json.dumps({"meta": meta, "results": results}, indent=1, sort_keys=True, allow_nan=False)
    (out / "data.json").write_text(payload, encoding="utf-8")

    dq_rows = results.get(DQ_QUERY, [])
    if not quiet:
        print(
            f"RetailPulse built -> {out / 'index.html'}  "
            f"({meta['orders']:,} orders, {meta['line_items']:,} items, {elapsed_ms} ms)"
        )
    code = report_dq(dq_rows, fail_on_dq=fail_on_dq, quiet=quiet)
    if summary is not None:
        revenue = sum(float(r.get("revenue") or 0) for r in results.get(REVENUE_QUERY, []))
        _write_summary(summary_markdown(meta, dq_rows, revenue=revenue), summary)
    return code


# --------------------------------------------------------------------------- argument parsing


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an ISO date YYYY-MM-DD, got {text!r}") from exc


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {text!r}")
    return value


def _generator_options() -> argparse.ArgumentParser:
    defaults = generate.Config()
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_argument_group("generator options", "Options left unset take the generate.Config defaults.")
    for name in ("seed", "orders", "customers", "products", "stores", "months"):
        group.add_argument(f"--{name}", type=int, default=None, metavar="N", help=f"default {getattr(defaults, name)}")
    group.add_argument("--start", type=_iso_date, default=None, metavar="YYYY-MM-DD",
                       help=f"first day of the generated period (default {defaults.start.isoformat()})")
    return parent


def config_from_args(args: argparse.Namespace) -> generate.Config:
    """Build a ``Config`` from the generator options that were actually given (``ValueError`` if invalid)."""
    given = {name: value for name in GENERATOR_OPTIONS if (value := getattr(args, name, None)) is not None}
    return generate.Config(**given)


def _config(args: argparse.Namespace) -> generate.Config:
    try:
        return config_from_args(args)
    except ValueError as exc:
        raise UsageError(f"invalid generator options: {exc}") from exc


def _options_given(args: argparse.Namespace) -> bool:
    return any(getattr(args, name, None) is not None for name in GENERATOR_OPTIONS)


def _add_db_option(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument("--db", metavar="PATH", default=None, help=help_text)


RAW_CSV_HELP = (
    "write CSV cells verbatim. By default string cells that a spreadsheet would read as a formula "
    "(starting with = + - @, also behind leading whitespace or control characters) get a leading "
    "apostrophe; numbers, empty cells and the header are never changed. Raw output is for machine "
    "consumers only — do not open it in a spreadsheet unprotected. Usage error when no CSV is written."
)


def _add_raw_csv_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--raw-csv", action="store_true", help=RAW_CSV_HELP)


def build_parser() -> argparse.ArgumentParser:
    generator = _generator_options()
    parser = argparse.ArgumentParser(
        prog="retailpulse",
        description="Python + SQL retail analytics: generate data, run the SQL, render the dashboard.",
    )
    parser.add_argument("--version", action="version", version=f"retailpulse {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    p = sub.add_parser("build", parents=[generator],
                       help="generate data, run every query, write index.html + data.json (+ csv/)")
    p.add_argument("--out", type=Path, default=Path("public"), metavar="DIR", help="output directory (default public)")
    _add_db_option(p, "persist the SQLite database to PATH (refused if PATH exists unless --replace)")
    p.add_argument("--replace", action="store_true", help="delete an existing --db file before building")
    p.add_argument("--no-csv", action="store_true", help="skip the per-query CSV exports under DIR/csv/")
    _add_raw_csv_option(p)
    p.add_argument("--no-fail-on-dq", action="store_true", help="report data-quality errors but still exit 0")
    p.add_argument("--summary", metavar="PATH",
                   help="append a Markdown summary (KPIs + data-quality table) to PATH; '-' prints it to stdout")
    p.add_argument("--quiet", "-q", action="store_true", help="suppress the summary lines on stdout")

    p = sub.add_parser("check", parents=[generator],
                       help="run only the data-quality query and print its table; exit 1 on error-severity violations")
    _add_db_option(p, "use an existing database file as-is, or generate into PATH when it does not exist")

    p = sub.add_parser("query", parents=[generator], help="run one query and print it")
    p.add_argument("name", metavar="NAME", help="query name as shown by `retailpulse list`")
    _add_db_option(p, "use an existing database file as-is, or generate into PATH when it does not exist")
    p.add_argument("--format", choices=("table", "csv", "json"), default="table", help="output format (default table)")
    p.add_argument("--limit", type=_non_negative_int, default=None, metavar="N", help="print at most N rows")
    _add_raw_csv_option(p)

    p = sub.add_parser("export", parents=[generator], help="write every query result as CSV or JSON files")
    p.add_argument("--out", type=Path, default=Path("public"), metavar="DIR",
                   help="directory that receives csv/ or json/ (default public)")
    _add_db_option(p, "use an existing database file as-is, or generate into PATH when it does not exist")
    p.add_argument("--format", choices=export.FORMATS, default="csv", help="file format (default csv)")
    _add_raw_csv_option(p)

    p = sub.add_parser("docs", help="regenerate the README query table from the SQL header registry")
    p.add_argument("--check", action="store_true", help="exit 1 if the README would change; nothing is written")
    p.add_argument("--readme", type=Path, default=Path("README.md"), metavar="PATH", help="README to update")

    sub.add_parser("list", help="list query names (with titles when the registry is available)")
    return parser


# --------------------------------------------------------------------------- commands


def cmd_build(args: argparse.Namespace) -> int:
    return build(
        args.out,
        None,
        None,
        args.db,
        config=_config(args),
        export_csv=not args.no_csv,
        fail_on_dq=not args.no_fail_on_dq,
        summary=args.summary,
        quiet=args.quiet,
        replace=args.replace,
        raw_csv=args.raw_csv,
    )


def _dataset(args: argparse.Namespace) -> Dataset:
    return prepare_dataset(args.db, _config(args), reuse_existing=True, options_given=_options_given(args))


def cmd_check(args: argparse.Namespace) -> int:
    dataset = _dataset(args)
    try:
        rows = db.run_query(dataset.conn, DQ_QUERY)
    finally:
        dataset.conn.close()
    sys.stdout.write(dq_table(rows))
    return report_dq(rows)


def _require_csv_for_raw(args: argparse.Namespace) -> None:
    """``--raw-csv`` must never be a silent no-op: reject it unless the command writes CSV."""
    if args.raw_csv and args.format != "csv":
        raise UsageError(f"--raw-csv only applies to --format csv (got --format {args.format})")


def cmd_query(args: argparse.Namespace) -> int:
    _require_csv_for_raw(args)
    try:
        db.load_sql(args.name)  # validates the name before any data is generated
    except KeyError as exc:
        raise UsageError(str(exc)) from exc
    dataset = _dataset(args)
    try:
        rows = db.run_query(dataset.conn, args.name)
    finally:
        dataset.conn.close()
    if args.limit is not None:
        rows = rows[: args.limit]
    sys.stdout.write(format_rows(rows, args.format, raw=args.raw_csv))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    _require_csv_for_raw(args)
    dataset = _dataset(args)
    try:
        results = {name: db.run_query(dataset.conn, name) for name in db.list_queries()}
    finally:
        dataset.conn.close()
    written = export.write_all(args.out, results, fmt=args.format, raw=args.raw_csv)
    print(f"Exported {len(written)} queries as {args.format.upper()} -> {Path(args.out) / args.format}")
    return 0


def cmd_docs(args: argparse.Namespace) -> int:
    try:
        from . import registry
    except ImportError as exc:
        raise UsageError("`docs` needs retailpulse.registry, which could not be imported") from exc
    readme = Path(args.readme)
    if not readme.is_file():
        raise UsageError(f"README not found: {readme}")
    current = readme.read_text(encoding="utf-8")
    if any(marker not in current for marker in README_MARKERS):
        raise UsageError(f"{readme} has no {README_MARKERS[0]} / {README_MARKERS[1]} markers around the query table")
    updated = registry.sync_readme(current, registry.load_specs())
    if args.check:
        if updated != current:
            print(f"{readme}: query table is out of date; run `retailpulse docs` to regenerate it", file=sys.stderr)
            return 1
        print(f"{readme}: query table is up to date")
        return 0
    if updated != current:
        readme.write_text(updated, encoding="utf-8")
        print(f"{readme}: query table regenerated")
    else:
        print(f"{readme}: query table already up to date")
    return 0


def _titles() -> dict[str, str]:
    try:
        from . import registry

        specs = registry.load_specs()
    except (ImportError, AttributeError):
        return {}
    return {name: str(getattr(spec, "title", "") or "") for name, spec in specs.items()}


def cmd_list(args: argparse.Namespace) -> int:
    names = db.list_queries()
    titles = _titles()
    width = max((len(name) for name in names), default=0)
    for name in names:
        title = titles.get(name, "")
        print(f"{name:<{width}}  {title}" if title else name)
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "build": cmd_build,
    "check": cmd_check,
    "query": cmd_query,
    "export": cmd_export,
    "docs": cmd_docs,
    "list": cmd_list,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.cmd](args)
    except UsageError as exc:
        parser.error(str(exc))  # prints usage + message on stderr and exits with status 2


if __name__ == "__main__":
    raise SystemExit(main())
