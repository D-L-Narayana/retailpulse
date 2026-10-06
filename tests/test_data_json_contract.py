"""``data.json`` contract: shape, strict JSON, ``meta`` keys and reproducible builds.

``data.json`` is ``{"meta": {...}, "results": {query_name: rows}}``.  Rows of one query share one key
set, values are JSON scalars (no NaN/Infinity), ``meta`` carries the documented keys, and two builds
with ``SOURCE_DATE_EPOCH`` set are byte-identical.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from retailpulse import cli, db, generate

REQUESTED_ORDERS = 1200
SEED = 11
META_KEYS = {"generated_at", "elapsed_ms", "customers", "orders", "line_items", "products", "stores"}
COUNT_KEYS = ("customers", "orders", "line_items", "products", "stores")
CONFIG_KEYS = {"customers", "products", "stores", "orders", "start", "months"}
GENERATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$")

def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant {token!r} is not valid JSON")


def _load_strict(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_constant)


class _Object(list):
    """Marker type so ordered JSON objects can be told apart from arrays."""


@dataclass
class Build:
    out: Path
    text: str
    doc: dict[str, Any]
    rc: int


@pytest.fixture(scope="module")
def build(tmp_path_factory) -> Build:
    """One small CLI build; the exit code is recorded and asserted separately from the artefact contract."""
    out = tmp_path_factory.mktemp("public")
    rc = cli.main(["build", "--out", str(out), "--orders", str(REQUESTED_ORDERS), "--seed", str(SEED)])
    assert (out / "data.json").is_file(), rc
    text = (out / "data.json").read_text(encoding="utf-8")
    return Build(out, text, _load_strict(text), rc)


def _error_rows(dq_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in dq_rows if r.get("severity", "error") == "error"]


def test_json_is_strict(build):
    assert build.text.lstrip().startswith("{")
    assert build.text.rstrip().endswith("}")
    assert _load_strict(build.text) == json.loads(build.text)


def test_build_exit_code_follows_the_severity_contract(build):
    dq = build.doc["results"]["08_data_quality"]
    expected = 1 if any(r["violations"] for r in _error_rows(dq)) else 0
    assert build.rc == expected


def test_top_level_shape(build):
    assert set(build.doc) == {"meta", "results"}
    assert isinstance(build.doc["meta"], dict)
    assert isinstance(build.doc["results"], dict)


def test_every_query_is_present_with_list_results(build):
    results = build.doc["results"]
    assert set(results) == set(db.list_queries())
    assert list(results) == db.list_queries()  # name order is the sorted query list
    assert all(isinstance(rows, list) for rows in results.values())


def test_rows_share_identical_key_sets_and_scalar_values(build):
    for name, rows in build.doc["results"].items():
        assert len({tuple(row) for row in rows}) <= 1, name
        for row in rows:
            assert isinstance(row, dict), name
            for key, value in row.items():
                assert isinstance(key, str) and key, (name, key)
                assert value is None or isinstance(value, (int, float, str)), (name, key, value)
                assert not isinstance(value, bool), (name, key)
                if isinstance(value, float):
                    assert math.isfinite(value), (name, key, value)


def test_meta_keys_and_types(build):
    meta = build.doc["meta"]
    assert META_KEYS <= set(meta)
    assert GENERATED_AT.match(meta["generated_at"]), meta["generated_at"]
    elapsed = meta["elapsed_ms"]
    assert elapsed is None or (type(elapsed) is int and elapsed >= 0)
    for key in COUNT_KEYS:
        assert type(meta[key]) is int and meta[key] >= 0, key


def test_meta_counts_match_request_and_results(build):
    meta, results = build.doc["meta"], build.doc["results"]
    defaults = generate.Config()
    assert meta["orders"] == REQUESTED_ORDERS
    assert meta["customers"] == defaults.customers
    assert meta["products"] == defaults.products
    assert meta["stores"] == defaults.stores
    assert meta["line_items"] >= meta["orders"]
    assert sum(r["orders"] for r in results["01_monthly_revenue"]) == meta["orders"]
    assert len(results["02_category_abc"]) == meta["products"]
    assert len(results["05_store_performance"]) == meta["stores"]
    assert len(results["03_customer_rfm"]) <= meta["customers"]


def test_optional_meta_extensions_are_well_typed(build):
    meta, results = build.doc["meta"], build.doc["results"]
    if "seed" in meta:
        assert meta["seed"] == SEED
    if "version" in meta:
        assert isinstance(meta["version"], str) and meta["version"]
    if "config" in meta:
        config = meta["config"]
        assert isinstance(config, dict) and CONFIG_KEYS <= set(config)
        assert config["orders"] == REQUESTED_ORDERS
        assert date.fromisoformat(config["start"])
    if "exports" in meta:
        exports = meta["exports"]
        assert isinstance(exports, dict)
        assert set(exports) - set(results) <= {"data.json"}  # per-query CSVs plus the optional data.json link
        for name, rel in exports.items():
            assert isinstance(rel, str) and rel, name
            assert not rel.startswith("/") and "://" not in rel and ".." not in rel, rel
            assert (build.out / rel).is_file(), rel


def test_data_quality_rows_contract(build):
    rows = build.doc["results"]["08_data_quality"]
    assert rows
    for r in rows:
        assert isinstance(r["check_name"], str) and r["check_name"]
        assert type(r["violations"]) is int and r["violations"] >= 0
        if "severity" in r:
            assert r["severity"] in {"error", "warn"}, r
        if "description" in r:
            assert isinstance(r["description"], str) and r["description"].strip(), r
    assert [r["check_name"] for r in _error_rows(rows) if r["violations"]] == []


def _build_artefacts(out: Path) -> None:
    """Small build whose artefacts are the subject; the gate verdict is covered by the severity-contract test."""
    rc = cli.main(["build", "--out", str(out), "--orders", "800", "--seed", "5"])
    assert rc in (0, 1), rc
    assert (out / "index.html").is_file() and (out / "data.json").is_file()


def _assert_same_bytes(a: Path, b: Path) -> None:
    """Byte equality with a short, targeted message (a plain ``==`` on large artefacts makes pytest's
    assertion rewriting spend a very long time computing a difflib diff of the two blobs)."""
    da, db_ = a.read_bytes(), b.read_bytes()
    if hashlib.sha256(da).hexdigest() == hashlib.sha256(db_).hexdigest():
        return
    pairs = zip(da, db_, strict=False)  # unequal lengths: the first missing byte is the divergence point
    offset = next((i for i, (x, y) in enumerate(pairs) if x != y), min(len(da), len(db_)))
    lo, hi = max(0, offset - 60), offset + 60
    raise AssertionError(
        f"{a.name} differs between the two builds at byte {offset}: {da[lo:hi]!r} != {db_[lo:hi]!r}"
    )


def test_source_date_epoch_makes_builds_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    a, b = tmp_path / "a", tmp_path / "b"
    _build_artefacts(a)
    _build_artefacts(b)
    _assert_same_bytes(a / "data.json", b / "data.json")
    _assert_same_bytes(a / "index.html", b / "index.html")


def test_source_date_epoch_pins_timestamp_and_elapsed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    out = tmp_path / "out"
    _build_artefacts(out)
    meta = _load_strict((out / "data.json").read_text(encoding="utf-8"))["meta"]
    assert meta["generated_at"] == "1970-01-01 00:00 UTC"
    assert meta["elapsed_ms"] is None


def test_json_object_keys_are_sorted(build):
    def check(node: Any, path: str) -> None:
        if isinstance(node, _Object):
            keys = [k for k, _ in node]
            assert keys == sorted(keys), path
            for key, value in node:
                check(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                check(value, f"{path}[{i}]")

    check(json.loads(build.text, object_pairs_hook=_Object), "$")
