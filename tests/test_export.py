"""Unit tests for retailpulse.export (the CSV/JSON writers behind `build` and `export`)."""
from __future__ import annotations

import csv
import io
import json

import pytest

from retailpulse import export

ROWS = [
    {"order_month": "2024-01", "orders": 12, "revenue": 1234.5, "note": None},
    {"order_month": "2024-02", "orders": 7, "revenue": 99.0, "note": "café → ok"},
]


def test_write_csv_round_trips_through_csv_module(tmp_path):
    path = tmp_path / "x.csv"
    export.write_csv(path, ROWS)
    assert path.exists()
    with path.open(encoding="utf-8", newline="") as fh:
        back = list(csv.DictReader(fh))
    assert [list(r) for r in back] == [["order_month", "orders", "revenue", "note"]] * 2
    assert back[0] == {"order_month": "2024-01", "orders": "12", "revenue": "1234.5", "note": ""}
    assert back[1]["note"] == "café → ok"


def test_write_csv_empty_rows_is_header_less_empty_file(tmp_path):
    path = tmp_path / "empty.csv"
    export.write_csv(path, [])
    assert path.exists()
    assert path.read_bytes() == b""


def test_write_csv_creates_parent_dirs_and_uses_lf(tmp_path):
    path = tmp_path / "deep" / "er" / "x.csv"
    export.write_csv(path, ROWS)
    assert path.exists()
    raw = path.read_bytes()
    assert raw.startswith(b"order_month,orders,revenue,note\n")
    assert b"\r" not in raw


def test_rows_to_csv_text_matches_file(tmp_path):
    path = tmp_path / "x.csv"
    export.write_csv(path, ROWS)
    assert export.rows_to_csv(ROWS) == path.read_text(encoding="utf-8")
    assert export.rows_to_csv([]) == ""


def test_write_all_returns_relative_posix_paths(tmp_path):
    rel = export.write_all(tmp_path, {"01_a": ROWS, "02_b": []})
    assert rel == {"01_a": "csv/01_a.csv", "02_b": "csv/02_b.csv"}
    for relpath in rel.values():
        assert (tmp_path / relpath).exists()
        assert not relpath.startswith("/") and "\\" not in relpath
    assert (tmp_path / "csv" / "02_b.csv").read_bytes() == b""


def test_write_all_json_format(tmp_path):
    rel = export.write_all(tmp_path, {"01_a": ROWS}, fmt="json")
    assert rel == {"01_a": "json/01_a.json"}
    path = tmp_path / "json" / "01_a.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == ROWS


def test_write_all_rejects_unknown_format(tmp_path):
    with pytest.raises(ValueError):
        export.write_all(tmp_path, {"01_a": ROWS}, fmt="xml")


# ----------------------------------------------------------------------------- inert-by-default CSV cells
#
# A spreadsheet that opens a CSV treats cells starting with = + - @ (also after leading whitespace,
# control or zero-width characters) as formulas. Reporting CSVs therefore prefix such *string* cells
# with a single apostrophe; numbers, None, ordinary text and the trusted header row are untouched.

FORMULA_LIKE = [
    "=1+1",
    "+1",
    "-1",
    "@SUM(1,2)",
    "\t=1+1",
    "\r=x",
    " =1",
    "​=1",  # zero-width space
    "﻿-1",  # byte-order mark
    "\x00@x",
    "\tAlice",  # a raw leading C0 control is enough (OWASP tab/CR-prefixed cells)
    "\x7f=1",
    "-",
]
ORDINARY = ["Alice", "a-b", "x=y", " Alice", "", "1-2-3", "café → ok", "2024-01", "Store 001"]
NON_STRINGS = [-1, -2.5, 0, True, None, 12, 3.0]


@pytest.mark.parametrize("text", FORMULA_LIKE)
def test_inert_predicate_marks_formula_like_text(text):
    assert export.is_formula_like(text) is True
    assert export.inert_cell(text) == "'" + text


@pytest.mark.parametrize("text", ORDINARY)
def test_inert_predicate_leaves_ordinary_text(text):
    assert export.is_formula_like(text) is False
    assert export.inert_cell(text) == text


@pytest.mark.parametrize("value", NON_STRINGS)
def test_inert_cell_leaves_non_strings_untouched(value):
    assert export.inert_cell(value) is value


def test_inert_rows_to_csv_marks_only_formula_like_string_cells():
    rows = [
        {"name": "=1+1", "region": "@SUM(1,2)", "delta": -12.5, "count": -3, "note": None, "ok": True, "p": "a-b"},
        {"name": "Alice", "region": "\t=HYPERLINK(1)", "delta": 0.5, "count": 0, "note": "x=y", "ok": False, "p": "-"},
    ]
    text = export.rows_to_csv(rows)
    assert text.endswith("\n") and "\r\n" not in text
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == ["name", "region", "delta", "count", "note", "ok", "p"]
    assert parsed[1] == ["'=1+1", "'@SUM(1,2)", "-12.5", "-3", "", "True", "a-b"]
    assert parsed[2] == ["Alice", "'\t=HYPERLINK(1)", "0.5", "0", "x=y", "False", "'-"]


def test_inert_header_names_are_never_marked():
    text = export.rows_to_csv([{"=total": "=1+1", "-delta": -1, "@at": "@x"}])
    assert text.splitlines() == ["=total,-delta,@at", "'=1+1,-1,'@x"]


def test_inert_raw_keeps_cells_verbatim():
    rows = [{"name": "=1+1", "n": -1}]
    assert export.rows_to_csv(rows, raw=True) == "name,n\n=1+1,-1\n"
    assert export.rows_to_csv(rows) == "name,n\n'=1+1,-1\n"


def test_inert_write_csv_and_write_all_propagate_raw(tmp_path):
    rows = [{"name": "@SUM(1,2)", "n": 1}]

    def first_name(path):
        with path.open(encoding="utf-8", newline="") as fh:
            return next(csv.DictReader(fh))["name"]

    export.write_csv(tmp_path / "safe.csv", rows)
    export.write_csv(tmp_path / "raw.csv", rows, raw=True)
    assert first_name(tmp_path / "safe.csv") == "'@SUM(1,2)"
    assert first_name(tmp_path / "raw.csv") == "@SUM(1,2)"
    assert b"\r" not in (tmp_path / "safe.csv").read_bytes()

    assert export.write_all(tmp_path / "a", {"q": rows}) == {"q": "csv/q.csv"}
    assert first_name(tmp_path / "a" / "csv" / "q.csv") == "'@SUM(1,2)"
    export.write_all(tmp_path / "b", {"q": rows}, raw=True)
    assert first_name(tmp_path / "b" / "csv" / "q.csv") == "@SUM(1,2)"
    # JSON is machine data and never marked, whatever `raw` says.
    export.write_all(tmp_path / "c", {"q": rows}, fmt="json")
    export.write_all(tmp_path / "d", {"q": rows}, fmt="json", raw=True)
    for folder in ("c", "d"):
        assert json.loads((tmp_path / folder / "json" / "q.json").read_text(encoding="utf-8")) == rows
