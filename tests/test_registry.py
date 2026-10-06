"""Tests for retailpulse.registry: SQL header metadata, the @chart grammar and README sync."""
from __future__ import annotations

import pytest

from retailpulse import db, registry
from retailpulse.registry import QuerySpec, parse_chart, parse_header

HEADER = """-- @title: Monthly revenue & MoM growth
-- @question: How is net revenue trending month over month, and what margin does it carry?
-- @technique: LAG, trailing-window AVG
-- @chart: line x=order_month y=revenue
-- @limit: 24
-- @portability: substr(order_date,1,7) → to_char(order_date,'YYYY-MM') (Postgres) / FORMAT_DATE (BigQuery)
-- @portability: ROWS BETWEEN 2 PRECEDING AND CURRENT ROW is ANSI and supported everywhere
-- @owner: W03
-- Plain comment lines inside the block are ignored.
WITH monthly AS (SELECT 1 AS x)
-- @title: NOT THE TITLE (appears after the first non-comment line)
SELECT * FROM monthly;
"""


# --------------------------------------------------------------------------- QuerySpec / parse_header
def test_registry_parse_queryspec_defaults():
    spec = QuerySpec("x")
    assert (spec.name, spec.title, spec.question, spec.technique) == ("x", "", "", "")
    assert (spec.chart, spec.limit) == ("table", 15)
    assert spec.portability == [] and spec.extra == {}
    assert QuerySpec("x").portability is not QuerySpec("x").portability  # no shared mutable defaults


def test_registry_parse_full_header():
    spec = parse_header(HEADER, "01_monthly_revenue")
    assert isinstance(spec, QuerySpec)
    assert spec.name == "01_monthly_revenue"
    assert spec.title == "Monthly revenue & MoM growth"
    assert spec.question.startswith("How is net revenue trending")
    assert spec.technique == "LAG, trailing-window AVG"
    assert spec.chart == "line x=order_month y=revenue"
    assert spec.limit == 24
    assert len(spec.portability) == 2
    assert spec.portability[0].startswith("substr(order_date,1,7)")
    assert spec.portability[1].startswith("ROWS BETWEEN")
    assert spec.extra == {"owner": "W03"}


def test_registry_parse_stops_at_first_non_comment_line():
    text = "-- @title: First\nSELECT 1;\n-- @title: Second\n-- @chart: bar x=a y=b\n"
    spec = parse_header(text, "x")
    assert spec.title == "First"
    assert spec.chart == "table"


def test_registry_parse_fallbacks_without_header():
    spec = parse_header("-- just a description\nSELECT 1;", "01_monthly_revenue")
    assert spec.title == "Monthly revenue"
    assert spec.question == "" and spec.technique == ""
    assert spec.chart == "table"
    assert spec.limit == 15
    assert spec.portability == [] and spec.extra == {}
    assert parse_header("", "").title == ""


def test_registry_parse_tolerates_blank_lines_case_and_bad_values():
    text = "\n-- @Title:   Spaced title  \n\n-- @LIMIT: abc\n-- @chart:  bar x=a y=b \n-- @empty:\nSELECT 1;"
    spec = parse_header(text, "02_category_abc")
    assert spec.title == "Spaced title"
    assert spec.limit == 15  # unparsable limit falls back to the default
    assert spec.chart == "bar x=a y=b"
    assert spec.extra == {"empty": ""}
    assert parse_header("-- @limit: 0\nSELECT 1;", "x").limit == 0  # 0 means "all rows"
    assert parse_header("-- @limit: -3\nSELECT 1;", "x").limit == 15


def test_registry_parse_header_fields_keeps_every_value_in_order():
    fields = registry.header_fields(HEADER)
    assert "title" in fields and "portability" in fields, fields
    assert fields["title"] == ["Monthly revenue & MoM growth"]
    assert len(fields["portability"]) == 2
    assert "x" not in fields  # nothing after the first non-comment line is parsed
    assert registry.header_fields("SELECT 1") == {}


@pytest.mark.parametrize(
    ("stem", "title"),
    [
        ("01_monthly_revenue", "Monthly revenue"),
        ("09_basket_affinity", "Basket affinity"),
        ("10_customer_ltv", "Customer LTV"),
        ("03_customer_rfm", "Customer RFM"),
        ("02_category_abc", "Category ABC"),
        ("weird", "Weird"),
        ("", ""),
    ],
)
def test_registry_parse_humanise(stem, title):
    assert registry.humanise(stem) == title


# --------------------------------------------------------------------------- @chart grammar
@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("table", {"kind": "table"}),
        ("none", {"kind": "none"}),
        ("", {"kind": "table"}),
        (None, {"kind": "table"}),
        ("line x=order_month y=revenue", {"kind": "line", "x": "order_month", "y": "revenue"}),
        (
            "line x=month_offset y=cum_revenue_per_customer series=first_channel",
            {"kind": "line", "x": "month_offset", "y": "cum_revenue_per_customer", "series": "first_channel"},
        ),
        (
            "line x=order_month y=online_share_pct unit=%",
            {"kind": "line", "x": "order_month", "y": "online_share_pct", "unit": "%"},
        ),
        ("bar x=pair_label y=lift", {"kind": "bar", "x": "pair_label", "y": "lift"}),
        (
            "bar x=first_channel y=repeat_rate_pct unit=%",
            {"kind": "bar", "x": "first_channel", "y": "repeat_rate_pct", "unit": "%"},
        ),
        ("bar x=revenue_class y=revenue unit=k", {"kind": "bar", "x": "revenue_class", "y": "revenue", "unit": "k"}),
        ("  BAR   X=a   Y=b  ", {"kind": "bar", "x": "a", "y": "b"}),
        (
            "heatmap row=cohort_month col=month_offset value=retention_pct size=cohort_size",
            {
                "kind": "heatmap",
                "row": "cohort_month",
                "col": "month_offset",
                "value": "retention_pct",
                "size": "cohort_size",
            },
        ),
        ("pie x=a y=b", {"kind": "table"}),  # unknown kind
        ("line x=a", {"kind": "table"}),  # missing required y=
        ("heatmap row=a col=b", {"kind": "table"}),  # missing value=/size=
        ("line x=a y=b bogus", {"kind": "line", "x": "a", "y": "b"}),  # stray token ignored
        ("line x=a y=b color=red", {"kind": "line", "x": "a", "y": "b"}),  # unknown parameter dropped
        ("bar x=a y=b series=c", {"kind": "bar", "x": "a", "y": "b"}),  # series only applies to line
    ],
)
def test_registry_chart_grammar(spec, expected):
    assert parse_chart(spec) == expected


# --------------------------------------------------------------------------- load_specs
def test_registry_load_specs_matches_db_queries():
    specs = registry.load_specs()
    assert list(specs) == db.list_queries()
    for name, spec in specs.items():
        assert spec.name == name
        assert spec.title.strip(), name
        assert parse_chart(spec.chart)["kind"] in {"table", "none", "line", "bar", "heatmap"}
        assert isinstance(spec.limit, int) and spec.limit >= 0
        assert all(isinstance(note, str) and note for note in spec.portability)


def test_registry_load_specs_from_directory(tmp_path):
    with_header = "-- @title: Has header\n-- @chart: bar x=a y=b\nSELECT 1;"
    (tmp_path / "02_with_header.sql").write_text(with_header, encoding="utf-8")
    (tmp_path / "01_no_header.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    specs = registry.load_specs(tmp_path)
    assert list(specs) == ["01_no_header", "02_with_header"]
    assert specs["01_no_header"].title == "No header"
    assert specs["01_no_header"].chart == "table"
    assert specs["02_with_header"].title == "Has header"
    assert specs["02_with_header"].chart == "bar x=a y=b"
    assert registry.load_specs(tmp_path / "missing") == {}


# --------------------------------------------------------------------------- README table / sync
def _specs() -> dict[str, QuerySpec]:
    return {
        "01_monthly_revenue": QuerySpec(
            "01_monthly_revenue",
            title="Monthly revenue",
            technique="`LAG`, trailing `AVG`",
            question="How is revenue trending | by month?",
        ),
        "02_category_abc": QuerySpec("02_category_abc", title="ABC"),
    }


README = """# RetailPulse

Intro paragraph.

## SQL queries

<!-- queries:start -->
| File | Technique | Question answered |
|---|---|---|
| `old.sql` | stale | stale |
<!-- queries:end -->

## Schema
"""


def test_registry_readme_table_markdown():
    md = registry.readme_table(_specs())
    lines = md.splitlines()
    assert len(lines) == 4, md
    assert lines[0] == "| File | Technique | Question answered |"
    assert lines[1] == "|---|---|---|"
    assert lines[2] == "| `01_monthly_revenue.sql` | `LAG`, trailing `AVG` | How is revenue trending \\| by month? |"
    assert lines[3] == "| `02_category_abc.sql` | — | — |"
    assert len(lines) == 4
    assert not md.endswith("\n")
    # an iterable of specs is accepted too and is always emitted in name order
    assert registry.readme_table(reversed(list(_specs().values()))) == md
    assert registry.readme_table({}).splitlines() == lines[:2]


def test_registry_readme_table_flattens_newlines():
    spec = QuerySpec("x", title="t", technique="a\nb", question="multi\nline")
    assert registry.readme_table([spec]).splitlines()[2] == "| `x.sql` | a b | multi line |"


def test_registry_readme_sync_replaces_block_and_is_idempotent():
    out = registry.sync_readme(README, _specs())
    assert "`old.sql`" not in out
    assert out.startswith("# RetailPulse\n\nIntro paragraph.\n\n## SQL queries\n\n<!-- queries:start -->\n")
    assert out.endswith("<!-- queries:end -->\n\n## Schema\n")
    assert "\n" + registry.readme_table(_specs()) + "\n" in out
    assert registry.sync_readme(out, _specs()) == out
    assert registry.readme_in_sync(out, _specs()) is True
    assert registry.readme_in_sync(README, _specs()) is False


def test_registry_readme_sync_without_markers_is_unchanged():
    text = "# No markers here\n"
    assert registry.sync_readme(text, _specs()) == text
    half = "<!-- queries:start -->\nno end marker\n"
    assert registry.sync_readme(half, _specs()) == half
    reversed_markers = "<!-- queries:end -->\nx\n<!-- queries:start -->\n"
    assert registry.sync_readme(reversed_markers, _specs()) == reversed_markers
    assert registry.readme_in_sync(text, _specs()) is True
