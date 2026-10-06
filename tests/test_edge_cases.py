"""Edge-configuration tests: tiny builds through the CLI and through the library.

Contracts exercised (consumer side):
* ``build`` exits 0 without a traceback and writes ``index.html`` + ``data.json`` even for 0 orders;
* every query in ``db.list_queries()`` returns a list on an empty, a 0-order and a 1-order database;
* ``report.build_html`` renders empty result lists, ``None`` values and ``elapsed_ms=None`` without
  raising and without leaking the literal text ``None`` into the page;
* ``len(orders) == cfg.orders`` holds exactly whenever ``customers >= 1``;
* CLI exit codes: 0 ok, 1 data-quality errors, 2 usage/validation -- never a raw traceback.

Every dataset here is tiny (<= 300 orders); the expensive CLI builds are shared through module fixtures.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import retailpulse
from retailpulse import cli, db, generate, report

EMPTY_DATA: dict[str, list[tuple]] = {"stores": [], "products": [], "customers": [], "orders": [], "order_items": []}
ONE_ORDER_DATA: dict[str, list[tuple]] = {
    "stores": [(1, "Store 001", "West", "standard")],
    "products": [(1, "SKU-00001", "Grocery Item 1", "Grocery", 10.0, 5.0)],
    "customers": [(1, "customer1@example.com", "West", "2024-01-01")],
    "orders": [(1, 1, 1, "online", "2024-01-15")],
    "order_items": [(1, 1, 1, 2, 10.0, 1.0)],  # net revenue 2 * (10 - 1) = 18.0, margin 2 * (10 - 1 - 5) = 8.0
}
EDGE_CONFIGS: dict[str, dict[str, int]] = {
    "customers_1": {"customers": 1, "orders": 40},
    "stores_1": {"stores": 1, "customers": 50, "orders": 200},
    "months_1": {"months": 1, "customers": 50, "orders": 200},
    "products_8": {"products": 8, "customers": 50, "orders": 200},
}
TINY_ORDER_COUNTS = [0, 1, 10]

_STRIP_RAW_TEXT = re.compile(r"<(pre|style|script|code)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_LITERAL_JUNK = re.compile(r"\b(?:None|nan|NaN)\b")


# --------------------------------------------------------------------------- helpers


def _run_cli(argv: list[str]) -> int:
    """Run the CLI in-process; argparse's SystemExit becomes a plain exit status, other exceptions propagate."""
    try:
        return cli.main(argv)
    except SystemExit as exc:  # --help / --version / usage errors
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1


def _fresh_conn(data: dict[str, list[tuple]]) -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.create_schema(conn)
    db.load(conn, data)
    return conn


def _run_all(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    return {name: db.run_query(conn, name) for name in db.list_queries()}


def _sql_text(results: dict[str, Any]) -> dict[str, str]:
    return {name: (db.SQL_DIR / f"{name}.sql").read_text(encoding="utf-8") for name in results}


def _meta(data: dict[str, list[tuple]], **overrides: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "generated_at": "2024-01-01 00:00 UTC",
        "elapsed_ms": 1,
        "customers": len(data["customers"]),
        "orders": len(data["orders"]),
        "line_items": len(data["order_items"]),
        "products": len(data["products"]),
        "stores": len(data["stores"]),
    }
    meta.update(overrides)
    return meta


def _render(results: dict[str, list[dict[str, Any]]], meta: dict[str, Any]) -> str:
    html = report.build_html(results, _sql_text(results), meta)
    assert isinstance(html, str)
    assert "<html" in html.lower() and "</html>" in html.lower()
    return html


def _assert_no_literal_none(html: str) -> None:
    visible = _STRIP_RAW_TEXT.sub(" ", html)
    hit = _LITERAL_JUNK.search(visible)
    assert hit is None, f"literal {hit.group(0)!r} rendered near {visible[max(0, hit.start() - 60):hit.end() + 20]!r}"


def _error_rows(dq_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Data-quality rows that gate the build: a missing `severity` column means 'error' (PLAN §3.5)."""
    return [r for r in dq_rows if r.get("severity", "error") == "error"]


def _distinct_customers(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(DISTINCT customer_id) FROM orders").fetchone()[0]


# --------------------------------------------------------------------------- CLI: tiny builds


@dataclass
class TinyBuild:
    n: int
    out: Path
    rc: int
    stdout: str
    stderr: str
    html: str
    doc: dict[str, Any]


@pytest.fixture(scope="module", params=TINY_ORDER_COUNTS, ids=[f"orders{n}" for n in TINY_ORDER_COUNTS])
def tiny(request, tmp_path_factory) -> TinyBuild:
    """One CLI build per tiny order count, shared by the read-only assertions below."""
    n = request.param
    out = tmp_path_factory.mktemp(f"orders-{n}") / "public"
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = _run_cli(["build", "--out", str(out), "--orders", str(n), "--seed", "42"])
    html = (out / "index.html").read_text(encoding="utf-8") if (out / "index.html").is_file() else ""
    doc = json.loads((out / "data.json").read_text(encoding="utf-8")) if (out / "data.json").is_file() else {}
    return TinyBuild(n, out, rc, stdout.getvalue(), stderr.getvalue(), html, doc)


def test_cli_tiny_build_writes_both_artefacts_without_a_traceback(tiny):
    assert "Traceback" not in tiny.stderr and "Traceback" not in tiny.stdout
    assert tiny.rc in (0, 1), tiny.stderr  # 0 ok / 1 data-quality gate -- never a crash or usage error
    assert (tiny.out / "index.html").is_file() and (tiny.out / "data.json").is_file()
    assert "RetailPulse built" in tiny.stdout


def test_cli_tiny_build_exit_code_follows_the_severity_contract(tiny):
    """§3.5: exit 1 only when an error-severity check reports violations; warn rows never fail the build."""
    dq = tiny.doc["results"]["08_data_quality"]
    expected = 1 if any(r["violations"] for r in _error_rows(dq)) else 0
    assert tiny.rc == expected, tiny.stderr
    if expected == 0:
        assert "Data quality: PASS" in tiny.stdout
        assert "DATA QUALITY FAILED" not in tiny.stderr


def test_cli_tiny_build_html_has_no_literal_none(tiny):
    assert "RetailPulse" in tiny.html and "</html>" in tiny.html
    _assert_no_literal_none(tiny.html)


def test_cli_tiny_build_data_json_covers_every_query(tiny):
    results = tiny.doc["results"]
    assert set(results) == set(db.list_queries())
    assert all(isinstance(rows, list) for rows in results.values())
    dq = results["08_data_quality"]
    assert dq and all(r["violations"] == 0 for r in _error_rows(dq))


def test_cli_tiny_build_delivers_the_requested_order_count(tiny):
    meta, results = tiny.doc["meta"], tiny.doc["results"]
    assert meta["orders"] == tiny.n
    assert meta["line_items"] >= tiny.n
    assert sum(r["orders"] for r in results["01_monthly_revenue"]) == tiny.n
    assert len(results["03_customer_rfm"]) <= tiny.n
    assert sum(r["customers"] for r in results["07_repeat_purchase_rate"]) <= tiny.n


# --------------------------------------------------------------------------- CLI: exit codes


def test_cli_usage_errors_exit_2_without_traceback(tmp_path):
    assert _run_cli([]) == 2
    assert _run_cli(["bogus-subcommand"]) == 2
    assert _run_cli(["build", "--out", str(tmp_path), "--orders", "ten"]) == 2
    assert not (tmp_path / "index.html").exists()


def test_cli_negative_orders_is_a_usage_error(tmp_path, capsys):
    out = tmp_path / "public"
    rc = _run_cli(["build", "--out", str(out), "--orders", "-1"])
    captured = capsys.readouterr()
    assert rc == 2, captured.out + captured.err
    assert not (out / "index.html").exists()


def test_cli_existing_db_requires_replace(tmp_path, capsys):
    db_file = tmp_path / "retail.db"
    rc = _run_cli(["build", "--out", str(tmp_path / "a"), "--orders", "25", "--db", str(db_file)])
    assert rc in (0, 1) and db_file.is_file()  # the first build persists the database (gate verdict tested above)
    capsys.readouterr()

    rc = _run_cli(["build", "--out", str(tmp_path / "b"), "--orders", "25", "--db", str(db_file)])
    message = "".join(capsys.readouterr()).lower()
    assert rc == 2, message
    assert "exist" in message or "replace" in message
    assert "traceback" not in message
    assert not (tmp_path / "b" / "index.html").exists()

    rc = _run_cli(["build", "--out", str(tmp_path / "b"), "--orders", "30", "--db", str(db_file), "--replace"])
    assert rc == 0
    doc = json.loads((tmp_path / "b" / "data.json").read_text(encoding="utf-8"))
    conn = sqlite3.connect(str(db_file))
    try:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == doc["meta"]["orders"] == 30
    finally:
        conn.close()


def test_cli_version_flag(capsys):
    rc = _run_cli(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert retailpulse.__version__ in out


# --------------------------------------------------------------------------- library: edge configs


@pytest.fixture(scope="module", params=sorted(EDGE_CONFIGS))
def edge(request):
    cfg = generate.Config(seed=42, **EDGE_CONFIGS[request.param])
    data = generate.generate(cfg)
    conn = _fresh_conn(data)
    return request.param, cfg, data, conn, _run_all(conn)


def test_library_edge_config_runs_every_query_and_renders(edge):
    _name, _cfg, data, _conn, results = edge
    assert set(results) == set(db.list_queries())
    assert all(isinstance(rows, list) for rows in results.values())
    failing = [r["check_name"] for r in _error_rows(results["08_data_quality"]) if r["violations"] != 0]
    assert failing == []
    html = _render(results, _meta(data))
    assert "RetailPulse" in html
    _assert_no_literal_none(html)


def test_library_edge_config_delivers_the_requested_order_count(edge):
    _name, cfg, data, _conn, _results = edge
    assert len(data["orders"]) == cfg.orders
    assert len({o[0] for o in data["orders"]}) == cfg.orders
    assert {i[1] for i in data["order_items"]} == {o[0] for o in data["orders"]}


def test_library_edge_config_result_shapes(edge):
    name, cfg, data, conn, results = edge
    assert len(data["stores"]) == cfg.stores
    assert len(data["products"]) == cfg.products
    assert len(data["customers"]) == cfg.customers
    active = _distinct_customers(conn)
    assert len(results["03_customer_rfm"]) == active
    assert sum(r["customers"] for r in results["07_repeat_purchase_rate"]) == active

    if name == "stores_1":
        rows = results["05_store_performance"]
        assert len(rows) == 1
        assert rows[0]["rank_in_region"] == 1 and rows[0]["region_share_pct"] == 100.0
        assert {o[2] for o in data["orders"]} == {1}
    elif name == "products_8":
        rows = results["02_category_abc"]
        assert len(rows) == cfg.products  # every SKU is listed, including ones that never sold (§3.2)
        assert rows[-1]["cum_share_pct"] == 100.0
    elif name == "months_1":
        assert len(results["01_monthly_revenue"]) <= 2  # a 30-day horizon spans at most two calendar months
        assert all(r["month_offset"] <= 1 for r in results["04_cohort_retention"])
    elif name == "customers_1":
        assert active == 1
        assert {o[1] for o in data["orders"]} == {1}


# --------------------------------------------------------------------------- library: empty / tiny databases


def test_every_query_returns_a_list_on_an_empty_database():
    conn = _fresh_conn(EMPTY_DATA)
    results = _run_all(conn)
    assert set(results) == set(db.list_queries())
    for name, rows in results.items():
        assert isinstance(rows, list), name
    for name in ("01_monthly_revenue", "02_category_abc", "03_customer_rfm", "04_cohort_retention",
                 "05_store_performance", "06_channel_mix", "07_repeat_purchase_rate"):
        assert results[name] == [], name
    assert results.get("09_basket_affinity", []) == []
    assert results.get("10_customer_ltv", []) == []
    dq = results["08_data_quality"]
    assert dq and all(r["violations"] == 0 for r in _error_rows(dq))
    _assert_no_literal_none(_render(results, _meta(EMPTY_DATA)))


def test_zero_orders_from_the_generator_keep_dimension_rows():
    cfg = generate.Config(seed=5, customers=30, orders=0)
    data = generate.generate(cfg)
    assert data["orders"] == [] and data["order_items"] == []
    assert len(data["customers"]) == cfg.customers and len(data["products"]) == cfg.products
    results = _run_all(_fresh_conn(data))
    abc = results["02_category_abc"]
    assert len(abc) == cfg.products
    assert all(r["revenue"] == 0 and r["units"] == 0 and r["abc_class"] == "C" for r in abc)
    stores = results["05_store_performance"]
    assert len(stores) == cfg.stores
    assert all(r["orders"] == 0 and r["revenue"] == 0 and r["region_share_pct"] == 0 for r in stores)
    assert results["06_channel_mix"] == [] and results["03_customer_rfm"] == []
    _assert_no_literal_none(_render(results, _meta(data)))


def test_every_query_returns_a_list_on_a_hand_built_one_order_database():
    conn = _fresh_conn(ONE_ORDER_DATA)
    results = _run_all(conn)
    assert all(isinstance(rows, list) for rows in results.values())

    monthly = results["01_monthly_revenue"]
    assert len(monthly) == 1
    assert monthly[0]["order_month"] == "2024-01" and monthly[0]["orders"] == 1
    assert monthly[0]["revenue"] == 18.0 and monthly[0]["margin"] == 8.0
    assert monthly[0]["mom_change"] is None

    rfm = results["03_customer_rfm"]
    assert len(rfm) == 1 and rfm[0]["frequency"] == 1 and rfm[0]["monetary"] == 18.0
    assert all(1 <= rfm[0][k] <= 5 for k in ("r_score", "f_score", "m_score"))

    cohort = results["04_cohort_retention"]
    assert [(r["cohort_month"], r["month_offset"], r["retention_pct"]) for r in cohort] == [("2024-01", 0, 100.0)]

    stores = results["05_store_performance"]
    assert len(stores) == 1 and stores[0]["orders"] == 1 and stores[0]["region_share_pct"] == 100.0

    channel = results["06_channel_mix"]
    assert len(channel) == 1 and channel[0]["online"] == 18.0 and channel[0]["online_share_pct"] == 100.0

    repeat = results["07_repeat_purchase_rate"]
    assert len(repeat) == 1 and repeat[0]["first_channel"] == "online"
    assert repeat[0]["customers"] == 1 and repeat[0]["repeat_customers"] == 0 and repeat[0]["repeat_rate_pct"] == 0.0

    assert results.get("09_basket_affinity", []) == []  # a pair needs at least two products in one basket
    assert all(r["violations"] == 0 for r in _error_rows(results["08_data_quality"]))
    _assert_no_literal_none(_render(results, _meta(ONE_ORDER_DATA)))


def test_single_generated_order_runs_the_whole_pipeline():
    cfg = generate.Config(seed=2, orders=1)
    data = generate.generate(cfg)
    assert len(data["orders"]) == 1
    conn = _fresh_conn(data)
    results = _run_all(conn)
    assert len(results["01_monthly_revenue"]) == 1 and results["01_monthly_revenue"][0]["orders"] == 1
    assert len(results["03_customer_rfm"]) == 1
    assert [r["retention_pct"] for r in results["04_cohort_retention"]] == [100.0]
    _assert_no_literal_none(_render(results, _meta(data)))


# --------------------------------------------------------------------------- renderer tolerance


@pytest.fixture(scope="module")
def small_results():
    data = generate.generate(generate.Config(seed=3, customers=60, orders=300))
    return data, _run_all(_fresh_conn(data))


def test_build_html_renders_empty_result_lists():
    results: dict[str, list[dict[str, Any]]] = {name: [] for name in db.list_queries()}
    html = _render(results, _meta(EMPTY_DATA))
    assert "RetailPulse" in html
    _assert_no_literal_none(html)


def test_build_html_tolerates_missing_new_queries_and_a_severity_column(small_results):
    data, base = small_results
    results = dict(base)
    results.pop("09_basket_affinity", None)
    results.pop("10_customer_ltv", None)
    dq = [dict(r) for r in results["08_data_quality"]]
    for row in dq:
        row.setdefault("severity", "error")
        row.setdefault("description", "check description")
    dq.insert(0, {"check_name": "synthetic_warn_check", "severity": "warn", "violations": 7,
                  "description": "a warning row with violations must render without failing anything"})
    results["08_data_quality"] = dq
    html = _render(results, _meta(data))
    assert "synthetic_warn_check" in html
    _assert_no_literal_none(html)


def test_build_html_tolerates_none_in_a_charted_series(small_results):
    data, base = small_results
    results = dict(base)
    channel = [dict(r) for r in results["06_channel_mix"]]
    assert len(channel) >= 2
    channel[0]["online_share_pct"] = None  # a month without online revenue under the pre-COALESCE SQL
    channel[-1]["online_share_pct"] = None
    results["06_channel_mix"] = channel
    _assert_no_literal_none(_render(results, _meta(data)))


def test_build_html_tolerates_elapsed_ms_none(small_results):
    data, results = small_results
    _assert_no_literal_none(_render(results, _meta(data, elapsed_ms=None)))
