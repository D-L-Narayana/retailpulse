"""CLI tests: subcommands, exit codes, reproducible builds, the data-quality gate, exports and summaries.

Builds use tiny configs (300 orders / 50 customers) so the whole module stays well under the 5 s budget.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import sys
import types
from html import unescape

import pytest

import retailpulse
from retailpulse import cli, db, export, generate
from retailpulse.cli import main

SMALL = ["--orders", "300", "--customers", "50", "--seed", "5"]
DQ_HEADER = "| check_name | severity | violations |"


def run(argv: list[str]) -> int:
    """Invoke the CLI the way a shell would: an argparse ``SystemExit`` becomes its exit code."""
    try:
        return main(argv)
    except SystemExit as e:  # argparse usage/validation errors (2) and --version (0)
        code = e.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON constant {name!r}")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One small build shared by read-only assertions (meta contract, CSV exports, summary file)."""
    root = tmp_path_factory.mktemp("built")
    out, summary = root / "public", root / "summary.md"
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("SOURCE_DATE_EPOCH", raising=False)
        rc = run(["build", "--out", str(out), *SMALL, "--summary", str(summary)])
    assert rc == 0
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    return types.SimpleNamespace(out=out, summary=summary, data=data, meta=data["meta"])


def _read_meta(out) -> dict:
    return json.loads((out / "data.json").read_text(encoding="utf-8"))["meta"]


# ----------------------------------------------------------------------------- baseline smoke test


def test_build_writes_dashboard(tmp_path):
    rc = main(["build", "--out", str(tmp_path), "--orders", "1500", "--seed", "3"])
    assert rc == 0
    html = (tmp_path / "index.html").read_text()
    assert "<svg" in html and "RetailPulse" in html and "Show SQL" in html
    data = json.loads((tmp_path / "data.json").read_text())
    assert data["meta"]["orders"] == 1500
    # Every SQL file on disk is a query: the expected set is derived, never hard-coded.
    assert set(data["results"]) == set(db.list_queries())
    assert len(data["results"]) >= 8


# ----------------------------------------------------------------------------- generator flags


def test_genflags_reach_config(tmp_path):
    out = tmp_path / "o"
    rc = run(["build", "--out", str(out), "--orders", "200", "--customers", "40", "--products", "20",
              "--stores", "4", "--months", "6", "--start", "2023-03-01", "--seed", "9", "--quiet"])
    assert rc == 0
    meta = _read_meta(out)
    assert meta["customers"] == 40
    assert meta["products"] == 20
    assert meta["stores"] == 4
    assert meta["seed"] == 9
    config = meta.get("config", {})
    assert config.get("months") == 6
    assert config.get("start") == "2023-03-01"
    assert config.get("customers") == 40
    assert config.get("orders") == 200


@pytest.mark.parametrize(
    "flags",
    [["--start", "2023-13-01"], ["--start", "yesterday"], ["--customers", "0"], ["--orders", "-1"], ["--months", "0"]],
)
def test_genflags_invalid_values_exit_2(tmp_path, capsys, flags):
    assert run(["build", "--out", str(tmp_path / "o"), *flags]) == 2
    assert "error" in capsys.readouterr().err


# ----------------------------------------------------------------------------- --db exists / --replace


def test_dbpath_existing_file_exits_2(tmp_path, capsys):
    dbp = tmp_path / "existing.db"
    dbp.write_bytes(b"")
    assert run(["build", "--out", str(tmp_path / "o"), "--db", str(dbp), *SMALL]) == 2
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "--replace" in err


def test_dbpath_replace_overwrites_and_persists(tmp_path):
    dbp = tmp_path / "retail.db"
    out = tmp_path / "o"
    assert run(["build", "--out", str(out), "--db", str(dbp), *SMALL, "--quiet"]) == 0
    assert dbp.exists()
    # A second build onto the same file must be refused (no raw OperationalError traceback) ...
    assert run(["build", "--out", str(out), "--db", str(dbp), *SMALL, "--quiet"]) == 2
    # ... unless --replace is given, which starts from a fresh file.
    assert run(["build", "--out", str(out), "--db", str(dbp), "--orders", "120", "--customers", "50", "--replace",
                "--quiet"]) == 0
    conn = sqlite3.connect(dbp)
    try:
        n_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        conn.close()
    assert n_orders == _read_meta(out)["orders"]


# ----------------------------------------------------------------------------- reproducible builds


def test_repro_source_date_epoch_pins_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    out = tmp_path / "o"
    assert run(["build", "--out", str(out), *SMALL, "--quiet"]) == 0
    meta = _read_meta(out)
    assert meta["generated_at"] == "1970-01-01 00:00 UTC"
    assert meta["elapsed_ms"] is None


def test_repro_two_builds_are_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    a, b = tmp_path / "a", tmp_path / "b"
    assert run(["build", "--out", str(a), *SMALL, "--quiet"]) == 0
    assert run(["build", "--out", str(b), *SMALL, "--quiet"]) == 0
    assert _read_meta(a)["elapsed_ms"] is None  # timing must not leak into the artefacts
    for name in ("index.html", "data.json", "csv/01_monthly_revenue.csv", "csv/08_data_quality.csv"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_repro_without_epoch_records_wall_clock(tmp_path, monkeypatch):
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    out = tmp_path / "o"
    assert run(["build", "--out", str(out), *SMALL, "--quiet"]) == 0
    meta = _read_meta(out)
    assert isinstance(meta["elapsed_ms"], int)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", meta["generated_at"])


def test_repro_invalid_epoch_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "yesterday")
    assert run(["build", "--out", str(tmp_path / "o"), *SMALL]) == 2
    assert "SOURCE_DATE_EPOCH" in capsys.readouterr().err


# ----------------------------------------------------------------------------- CSV exports


def test_csv_exports_written_for_every_query(built):
    for name in db.list_queries():
        assert (built.out / "csv" / f"{name}.csv").exists(), name
    with (built.out / "csv" / "03_customer_rfm.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    expected = built.data["results"]["03_customer_rfm"]
    assert len(rows) == len(expected)
    assert rows and set(rows[0]) == set(expected[0])  # CSV keeps SQL column order; data.json sorts keys


def test_csv_exports_are_listed_in_meta(built):
    # Contract: exports maps query name -> relative POSIX path of its CSV (keys are a subset of results;
    # the renderer links data.json itself whenever exports is non-empty).
    exports = built.meta["exports"]
    assert set(exports) == set(db.list_queries())
    for name in db.list_queries():
        assert exports[name] == f"csv/{name}.csv"
        assert (built.out / exports[name]).is_file()
    assert all(not v.startswith("/") and "\\" not in v and ".." not in v for v in exports.values())


def test_csv_no_csv_flag_skips_exports(tmp_path):
    out = tmp_path / "o"
    assert run(["build", "--out", str(out), *SMALL, "--no-csv", "--quiet"]) == 0
    assert not (out / "csv").exists()
    assert _read_meta(out)["exports"] == {}


# ----------------------------------------------------------------------------- data-quality severity gate


def _patch_dq(monkeypatch, dq_rows):
    """Make the 08 query return synthetic rows; every other query runs for real."""
    real = cli.db.run_query

    def fake(conn, name, params=None):
        if name == "08_data_quality":
            return [dict(r) for r in dq_rows]
        return real(conn, name, params)

    monkeypatch.setattr(cli.db, "run_query", fake)


def test_dq_warn_rows_do_not_fail_build(tmp_path, monkeypatch, capsys):
    _patch_dq(monkeypatch, [
        {"check_name": "customers_without_orders", "severity": "warn", "violations": 7},
        {"check_name": "orphan_items", "severity": "error", "violations": 0},
    ])
    assert run(["build", "--out", str(tmp_path / "o"), *SMALL]) == 0
    out, err = capsys.readouterr()
    assert "Data quality: PASS" in out
    assert "customers_without_orders=7" in out  # warnings are reported (stdout), never fatal
    assert "FAILED" not in err


def test_dq_error_rows_fail_build_and_name_checks(tmp_path, monkeypatch, capsys):
    _patch_dq(monkeypatch, [
        {"check_name": "orphan_items", "severity": "error", "violations": 2},
        {"check_name": "duplicate_emails", "severity": "error", "violations": 1},
        {"check_name": "customers_without_orders", "severity": "warn", "violations": 7},
    ])
    assert run(["build", "--out", str(tmp_path / "o"), *SMALL]) == 1
    out, err = capsys.readouterr()
    assert "DATA QUALITY FAILED" in err
    assert "orphan_items" in err and "duplicate_emails" in err
    assert "customers_without_orders" not in err  # warnings are not failures
    assert "Data quality: PASS" not in out


def test_dq_no_fail_on_dq_turns_exit_1_into_0(tmp_path, monkeypatch, capsys):
    _patch_dq(monkeypatch, [{"check_name": "orphan_items", "severity": "error", "violations": 2}])
    assert run(["build", "--out", str(tmp_path / "o"), *SMALL, "--no-fail-on-dq"]) == 0
    assert "DATA QUALITY FAILED" in capsys.readouterr().err  # still reported


def test_dq_missing_severity_column_counts_as_error(tmp_path, monkeypatch):
    _patch_dq(monkeypatch, [{"check_name": "orphan_items", "violations": 1}])
    assert run(["build", "--out", str(tmp_path / "o"), *SMALL]) == 1


def test_dq_failures_helper_splits_by_severity():
    rows = [
        {"check_name": "a", "severity": "error", "violations": 0},
        {"check_name": "b", "severity": "error", "violations": 3},
        {"check_name": "c", "severity": "warn", "violations": 5},
        {"check_name": "d", "violations": 1},  # missing severity ⇒ error
        {"check_name": "e", "severity": "warn", "violations": 0},
    ]
    errors, warns = cli.dq_failures(rows)
    assert [r["check_name"] for r in errors] == ["b", "d"]
    assert [r["check_name"] for r in warns] == ["c"]


# ----------------------------------------------------------------------------- check


def test_check_clean_dataset_prints_table_and_passes(capsys):
    assert run(["check", *SMALL]) == 0
    out = capsys.readouterr().out
    assert DQ_HEADER in out
    assert "orphan_items" in out
    assert "Data quality: PASS" in out


def test_check_detects_injected_violation(tmp_path, capsys):
    dbp = tmp_path / "retail.db"
    assert run(["build", "--out", str(tmp_path / "o"), "--db", str(dbp), *SMALL, "--quiet"]) == 0
    assert run(["check", "--db", str(dbp)]) == 0  # opens the file; no regeneration
    capsys.readouterr()
    conn = sqlite3.connect(dbp)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO order_items VALUES (999999, 999999, 1, 1, 10.0, 0)")  # orphan line item
        conn.commit()
    finally:
        conn.close()
    rc = run(["check", "--db", str(dbp)])
    out, err = capsys.readouterr()
    assert rc == 1
    assert re.search(r"^\| orphan_items \| error \| 1 \|", out, re.MULTILINE)
    assert "DATA QUALITY FAILED" in err and "orphan_items" in err


def test_check_db_path_is_created_when_missing(tmp_path):
    dbp = tmp_path / "new.db"
    assert run(["check", "--db", str(dbp), *SMALL]) == 0
    assert dbp.exists()
    conn = sqlite3.connect(dbp)
    try:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] > 0
    finally:
        conn.close()


def test_check_rejects_non_retailpulse_db(tmp_path, capsys):
    bogus = tmp_path / "bogus.db"
    bogus.write_bytes(b"")  # an empty file is a valid, table-less SQLite database
    assert run(["check", "--db", str(bogus)]) == 2
    assert "not a RetailPulse database" in capsys.readouterr().err


# ----------------------------------------------------------------------------- query


def test_query_csv_format(capsys):
    assert run(["query", "03_customer_rfm", *SMALL, "--format", "csv", "--limit", "3"]) == 0
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert len(rows) == 3
    assert "customer_id" in rows[0] and "segment" in rows[0]


def test_query_json_format(capsys):
    assert run(["query", "03_customer_rfm", *SMALL, "--format", "json", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert out.lstrip().startswith("[")
    rows = json.loads(out)
    assert len(rows) == 3
    assert "customer_id" in rows[0] and isinstance(rows[0]["rfm_total"], int)


def test_query_table_format_is_default(capsys):
    assert run(["query", "07_repeat_purchase_rate", *SMALL]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines and "first_channel" in lines[0] and "repeat_rate_pct" in lines[0]
    assert set(lines[1]) <= {"-", " "}  # separator under the header
    assert len(lines) >= 3


@pytest.mark.parametrize("name", ["99_nope", "../08_data_quality", "01_MONTHLY_REVENUE"])
def test_query_unknown_name_exits_2_and_lists_valid_names(capsys, name):
    assert run(["query", name, *SMALL]) == 2
    err = capsys.readouterr().err
    assert name in err
    assert all(valid in err for valid in db.list_queries())


def test_query_reads_existing_db_without_generating(tmp_path, capsys):
    dbp = tmp_path / "r.db"
    assert run(["build", "--out", str(tmp_path / "o"), "--db", str(dbp), *SMALL, "--quiet"]) == 0
    capsys.readouterr()
    assert run(["query", "01_monthly_revenue", "--db", str(dbp), "--format", "json"]) == 0
    out = capsys.readouterr().out
    assert out.lstrip().startswith("[")
    rows = json.loads(out)
    data = json.loads((tmp_path / "o" / "data.json").read_text(encoding="utf-8"))
    assert [r["order_month"] for r in rows] == [r["order_month"] for r in data["results"]["01_monthly_revenue"]]


# ----------------------------------------------------------------------------- export


def test_export_cmd_writes_csv_per_query(tmp_path):
    out = tmp_path / "exp"
    assert run(["export", "--out", str(out), *SMALL]) == 0
    for name in db.list_queries():
        assert (out / "csv" / f"{name}.csv").exists(), name
    with (out / "csv" / "03_customer_rfm.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows and "customer_id" in rows[0]


def test_export_cmd_json_format(tmp_path):
    out = tmp_path / "exp"
    assert run(["export", "--out", str(out), *SMALL, "--format", "json"]) == 0
    path = out / "json" / "03_customer_rfm.json"
    assert path.exists()
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(rows, list) and rows and "customer_id" in rows[0]


# ----------------------------------------------------------------------------- docs

README_STALE = "# Title\n\n<!-- queries:start -->\nold table\n<!-- queries:end -->\n\nrest\n"
FAKE_TABLE = "| File | Technique | Question answered |\n|---|---|---|\n| `01_x.sql` | t | q |\n"


def _fake_registry(monkeypatch, titles=None):
    """Stand-in for retailpulse.registry exposing the PLAN §3.3 API used by `docs` and `list`."""
    mod = types.ModuleType("retailpulse.registry")
    specs = {n: types.SimpleNamespace(title=t) for n, t in (titles or {"01_x": "X"}).items()}
    mod.load_specs = lambda: specs  # type: ignore[attr-defined]

    def sync_readme(text, specs):
        start, end = "<!-- queries:start -->", "<!-- queries:end -->"
        i, j = text.index(start) + len(start), text.index(end)
        return text[:i] + "\n" + FAKE_TABLE + text[j:]

    mod.sync_readme = sync_readme  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "retailpulse.registry", mod)
    monkeypatch.setattr(retailpulse, "registry", mod, raising=False)
    return mod


def test_docs_rewrites_readme_table(tmp_path, monkeypatch):
    _fake_registry(monkeypatch)
    readme = tmp_path / "README.md"
    readme.write_text(README_STALE, encoding="utf-8")
    assert run(["docs", "--readme", str(readme)]) == 0
    text = readme.read_text(encoding="utf-8")
    assert "old table" not in text
    assert "| `01_x.sql` | t | q |" in text
    assert text.startswith("# Title") and text.endswith("rest\n")


def test_docs_check_exits_1_when_stale_and_0_when_current(tmp_path, monkeypatch):
    _fake_registry(monkeypatch)
    readme = tmp_path / "README.md"
    readme.write_text(README_STALE, encoding="utf-8")
    assert run(["docs", "--check", "--readme", str(readme)]) == 1
    assert readme.read_text(encoding="utf-8") == README_STALE  # --check never writes
    assert run(["docs", "--readme", str(readme)]) == 0
    assert run(["docs", "--check", "--readme", str(readme)]) == 0


def test_docs_without_registry_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "retailpulse.registry", None)  # makes `from . import registry` fail
    monkeypatch.delattr(retailpulse, "registry", raising=False)
    readme = tmp_path / "README.md"
    readme.write_text(README_STALE, encoding="utf-8")
    assert run(["docs", "--check", "--readme", str(readme)]) == 2
    assert "registry" in capsys.readouterr().err


def test_docs_missing_readme_exits_2(tmp_path, monkeypatch, capsys):
    _fake_registry(monkeypatch)
    assert run(["docs", "--readme", str(tmp_path / "nope.md")]) == 2
    assert "nope.md" in capsys.readouterr().err


def test_docs_check_against_real_registry(tmp_path):
    from retailpulse import registry  # the real module; its behaviour is owned (and tested) elsewhere

    specs = registry.load_specs()
    readme = tmp_path / "README.md"
    readme.write_text("# RetailPulse\n\n<!-- queries:start -->\n<!-- queries:end -->\n\n## Schema\n", encoding="utf-8")
    assert run(["docs", "--readme", str(readme)]) == 0
    assert run(["docs", "--check", "--readme", str(readme)]) == 0  # regenerating is idempotent
    text = readme.read_text(encoding="utf-8")
    assert text.startswith("# RetailPulse\n") and text.endswith("## Schema\n")
    assert all(f"`{name}.sql`" in text for name in specs)


def test_docs_readme_without_markers_exits_2(tmp_path, monkeypatch, capsys):
    _fake_registry(monkeypatch)
    readme = tmp_path / "README.md"
    readme.write_text("# No markers\n", encoding="utf-8")
    assert run(["docs", "--readme", str(readme)]) == 2
    assert "queries:start" in capsys.readouterr().err


# ----------------------------------------------------------------------------- list / --version


def test_list_prints_every_query_name(capsys):
    assert run(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == db.list_queries()


def test_list_shows_titles_when_registry_present(monkeypatch, capsys):
    names = db.list_queries()
    _fake_registry(monkeypatch, titles={n: f"Title for {n}" for n in names})
    assert run(["list"]) == 0
    out = capsys.readouterr().out
    assert f"Title for {names[0]}" in out
    assert [line.split()[0] for line in out.splitlines()] == names


def test_version_flag(capsys):
    assert run(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"retailpulse {retailpulse.__version__}"


# ----------------------------------------------------------------------------- --summary


def test_summary_file_has_kpis_and_dq_table(built):
    assert built.summary.exists()
    text = built.summary.read_text(encoding="utf-8")
    assert DQ_HEADER in text
    assert "orphan_items" in text
    assert f"{built.meta['orders']:,}" in text
    assert f"{built.meta['line_items']:,}" in text
    assert "Elapsed" in text
    assert "Data quality: PASS" in text


def test_summary_dash_prints_to_stdout(tmp_path, capsys):
    assert run(["build", "--out", str(tmp_path / "o"), *SMALL, "--summary", "-"]) == 0
    out = capsys.readouterr().out
    assert DQ_HEADER in out
    assert "RetailPulse built -> " in out  # the two standard lines are still printed


def test_summary_appends_so_step_summaries_compose(tmp_path):
    target = tmp_path / "step.md"
    target.write_text("# earlier step\n", encoding="utf-8")
    assert run(["build", "--out", str(tmp_path / "o"), *SMALL, "--summary", str(target), "--quiet"]) == 0
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# earlier step\n")
    assert DQ_HEADER in text


# ----------------------------------------------------------------------------- --quiet, meta contract, data.json


def test_quiet_suppresses_summary_lines(tmp_path, capsys):
    assert run(["build", "--out", str(tmp_path / "o"), *SMALL, "--quiet"]) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""


def test_meta_contract_keys(built):
    m = built.meta
    required = {"generated_at", "elapsed_ms", "customers", "orders", "line_items", "products", "stores",
                "seed", "version", "config", "exports", "data_source"}
    assert required <= set(m)
    assert m["data_source"] == "synthetic"  # build always renders freshly generated data
    assert m["version"] == retailpulse.__version__
    assert m["seed"] == 5
    assert m["config"]["seed"] == 5 and m["config"]["customers"] == 50 and m["config"]["start"] == "2024-01-01"
    assert m["customers"] == 50


def test_meta_data_json_is_sorted_strict_json(built):
    raw = (built.out / "data.json").read_text(encoding="utf-8")
    parsed = json.loads(raw, parse_constant=_reject_constant)
    assert list(parsed) == ["meta", "results"]
    assert raw == json.dumps(parsed, indent=1, sort_keys=True, allow_nan=False)


def test_build_function_keeps_positional_signature(tmp_path, capsys):
    rc = cli.build(tmp_path / "o", 3, 300, None)
    assert rc == 0
    assert (tmp_path / "o" / "index.html").exists()
    assert "RetailPulse built -> " in capsys.readouterr().out


# ----------------------------------------------------------------------------- inert CSV cells (existing-DB workflow)
#
# `--db` deliberately reuses whatever an existing database contains, so a store called "=1+1" must not
# reach a spreadsheet as a live formula. Every reporting CSV path marks such string cells with a leading
# apostrophe; JSON output and the database itself are never touched; --raw-csv restores verbatim text.

POISON_NAME, POISON_REGION = "=1+1", "@SUM(1,2)"
STORE_QUERY = "05_store_performance"


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _csv_rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def _as_csv_text(value) -> str:
    return "" if value is None else str(value)


def _poisoned_db(tmp_path):
    """A real persisted build whose store 1 is renamed to formula-like text (the reviewer's workflow)."""
    dbp = tmp_path / "synthetic.db"
    assert run(["build", "--out", str(tmp_path / "o"), "--db", str(dbp), *SMALL, "--quiet"]) == 0
    conn = sqlite3.connect(dbp)
    try:
        conn.execute("UPDATE stores SET name = ?, region = ? WHERE store_id = 1", (POISON_NAME, POISON_REGION))
        conn.commit()
    finally:
        conn.close()
    return dbp


def test_inert_query_csv_from_existing_db_neutralises_formula_cells(tmp_path, capsys):
    dbp = _poisoned_db(tmp_path)
    digest = _sha256(dbp)
    capsys.readouterr()

    assert run(["query", STORE_QUERY, "--db", str(dbp), "--format", "json"]) == 0
    json_row = next(r for r in json.loads(capsys.readouterr().out) if r["store_id"] == 1)
    assert json_row["name"] == POISON_NAME and json_row["region"] == POISON_REGION  # JSON is verbatim

    assert run(["query", STORE_QUERY, "--db", str(dbp), "--format", "csv"]) == 0
    csv_rows = _csv_rows(capsys.readouterr().out)
    row = next(r for r in csv_rows if r["store_id"] == "1")
    assert row["name"] == "'" + POISON_NAME
    assert row["region"] == "'" + POISON_REGION
    for col in ("orders", "revenue", "avg_basket", "margin_pct", "rank_in_region", "region_share_pct"):
        assert row[col] == _as_csv_text(json_row[col])  # numeric columns are untouched
    assert all(not r["name"].startswith("'") for r in csv_rows if r["store_id"] != "1")  # ordinary names too

    assert run(["query", STORE_QUERY, "--db", str(dbp), "--format", "csv", "--raw-csv"]) == 0
    raw_row = next(r for r in _csv_rows(capsys.readouterr().out) if r["store_id"] == "1")
    assert raw_row["name"] == POISON_NAME and raw_row["region"] == POISON_REGION
    assert _sha256(dbp) == digest  # read-only access: the database file is byte-identical


def test_inert_export_csv_from_existing_db_and_raw_flag_rules(tmp_path, capsys):
    dbp = _poisoned_db(tmp_path)
    digest = _sha256(dbp)

    def store_row(path):
        with path.open(encoding="utf-8", newline="") as fh:
            return next(r for r in csv.DictReader(fh) if r["store_id"] == "1")

    out = tmp_path / "exp"
    assert run(["export", "--db", str(dbp), "--format", "csv", "--out", str(out)]) == 0
    row = store_row(out / "csv" / f"{STORE_QUERY}.csv")
    assert row["name"] == "'" + POISON_NAME and row["region"] == "'" + POISON_REGION

    assert run(["export", "--db", str(dbp), "--format", "json", "--out", str(out)]) == 0
    json_rows = json.loads((out / "json" / f"{STORE_QUERY}.json").read_text(encoding="utf-8"))
    assert next(r for r in json_rows if r["store_id"] == 1)["name"] == POISON_NAME

    raw_out = tmp_path / "raw"
    assert run(["export", "--db", str(dbp), "--format", "csv", "--raw-csv", "--out", str(raw_out)]) == 0
    assert store_row(raw_out / "csv" / f"{STORE_QUERY}.csv")["name"] == POISON_NAME

    # --raw-csv can never be a silent no-op: it is a usage error wherever no CSV is written.
    capsys.readouterr()
    assert run(["export", "--db", str(dbp), "--format", "json", "--raw-csv", "--out", str(tmp_path / "x")]) == 2
    assert "--raw-csv" in capsys.readouterr().err
    assert run(["query", STORE_QUERY, "--db", str(dbp), "--format", "json", "--raw-csv"]) == 2
    assert "--raw-csv" in capsys.readouterr().err
    assert run(["query", STORE_QUERY, "--db", str(dbp), "--raw-csv"]) == 2  # default --format is table
    assert run(["build", "--out", str(tmp_path / "y"), *SMALL, "--no-csv", "--raw-csv"]) == 2
    assert "--raw-csv" in capsys.readouterr().err
    assert _sha256(dbp) == digest


def test_inert_negative_numbers_in_real_query_output_stay_numeric(tmp_path, capsys):
    dbp = _poisoned_db(tmp_path)
    capsys.readouterr()
    assert run(["query", "01_monthly_revenue", "--db", str(dbp), "--format", "json"]) == 0
    json_rows = json.loads(capsys.readouterr().out)
    negatives = [
        (i, col, value)
        for i, r in enumerate(json_rows)
        for col, value in r.items()
        if isinstance(value, int | float) and not isinstance(value, bool) and value < 0
    ]
    assert negatives  # month-over-month revenue drops make this query carry genuinely negative numbers
    assert run(["query", "01_monthly_revenue", "--db", str(dbp), "--format", "csv"]) == 0
    csv_rows = _csv_rows(capsys.readouterr().out)
    for i, col, value in negatives:
        assert csv_rows[i][col] == str(value)
        assert not csv_rows[i][col].startswith("'")


def test_inert_build_csv_matches_writer_and_raw_flag_reaches_write_all(tmp_path, monkeypatch):
    dbp, out = tmp_path / "b.db", tmp_path / "o"
    assert run(["build", "--out", str(out), "--db", str(dbp), *SMALL, "--quiet"]) == 0
    conn = db.connect(dbp, read_only=True)
    try:
        rows = db.run_query(conn, STORE_QUERY)
    finally:
        conn.close()
    text = (out / "csv" / f"{STORE_QUERY}.csv").read_text(encoding="utf-8")
    assert text == export.rows_to_csv(rows)  # the download is exactly the inert writer's output ...
    assert text == export.rows_to_csv(rows, raw=True)  # ... and sample data has no formula-like cells

    seen: list[dict] = []
    real = cli.export.write_all

    def spy(out_dir, results, fmt="csv", **kwargs):
        seen.append(kwargs)
        return real(out_dir, results, fmt, **kwargs)

    monkeypatch.setattr(cli.export, "write_all", spy)
    assert run(["build", "--out", str(tmp_path / "p"), *SMALL, "--quiet"]) == 0
    assert run(["build", "--out", str(tmp_path / "q"), *SMALL, "--quiet", "--raw-csv"]) == 0
    small = generate.Config(seed=5, customers=50, orders=300)
    assert cli.build(tmp_path / "r", None, None, None, config=small, quiet=True, raw_csv=True) == 0
    assert [k.get("raw") for k in seen] == [False, True, True]


# ----------------------------------------------------------------------------- synthetic-data disclosure
#
# `build` always renders freshly generated data (even with --db it persists what it just generated), so
# its metadata declares `data_source: synthetic` and the renderer labels the page visibly (header) and in
# the description meta. query/export emit result rows only and must never carry that key.

HEADER_FIRST_P = re.compile(r"<header><h1>RetailPulse</h1><p>(.*?)</p>", re.S)
DESCRIPTION_META = re.compile(r"<meta\s+name=([\"'])description\1\s+content=([\"'])(.*?)\2", re.S)
DESCRIPTION_META_REVERSED = re.compile(r"<meta\s+content=([\"'])(.*?)\1\s+name=([\"'])description\3", re.S)


def _visible_header_text(html: str) -> str:
    header = re.search(r"<header>(.*?)</header>", html, re.S)
    assert header is not None, "no <header> in index.html"
    return unescape(re.sub(r"<[^>]+>", " ", header.group(1)))


def _description_meta(html: str) -> str:
    match = DESCRIPTION_META.search(html)
    if match is not None:
        return unescape(match.group(3))
    reversed_match = DESCRIPTION_META_REVERSED.search(html)
    assert reversed_match is not None, "description meta missing"
    return unescape(reversed_match.group(2))


def test_synthetic_disclosure_in_built_header_and_data_json(tmp_path):
    out = tmp_path / "o"
    assert run(["build", "--out", str(out), *SMALL, "--quiet"]) == 0
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "synthetic demo dataset" in _visible_header_text(html)
    first_p = HEADER_FIRST_P.search(html)
    assert first_p is not None and "synthetic demo dataset" in first_p.group(1)
    assert "synthetic retail dataset" in _description_meta(html)
    assert _read_meta(out)["data_source"] == "synthetic"


def test_synthetic_summary_row(tmp_path, capsys):
    assert run(["build", "--out", str(tmp_path / "o"), *SMALL, "--summary", "-", "--quiet"]) == 0
    assert "| Data source | synthetic demo dataset |" in capsys.readouterr().out


def test_synthetic_key_absent_from_row_outputs(tmp_path, capsys):
    # query/export emit result rows, never build metadata: the disclosure key must not leak into them.
    assert run(["query", STORE_QUERY, *SMALL, "--format", "json"]) == 0
    assert "data_source" not in capsys.readouterr().out
    out = tmp_path / "exp"
    assert run(["export", "--out", str(out), *SMALL, "--format", "json"]) == 0
    assert "data_source" not in (out / "json" / f"{STORE_QUERY}.json").read_text(encoding="utf-8")
