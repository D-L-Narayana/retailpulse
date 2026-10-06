"""Renderer tests: None-safe SVG charts, theme/CSP, report structure, tolerance and download links."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from retailpulse import charts, report, theme
from retailpulse.registry import QuerySpec, parse_chart

GITHUB = "https://github.com/D-L-Narayana/retailpulse"
BASELINE = [
    "01_monthly_revenue",
    "02_category_abc",
    "03_customer_rfm",
    "04_cohort_retention",
    "05_store_performance",
    "06_channel_mix",
    "07_repeat_purchase_rate",
    "08_data_quality",
]

Rows = list[dict[str, Any]]


# --------------------------------------------------------------------------- synthetic result sets
def monthly_rows() -> Rows:
    return [
        {"order_month": "2024-01", "orders": 100, "revenue": 10000.0, "margin": 4000.0, "margin_pct": 40.0,
         "mom_change": None, "mom_pct": None, "trailing_3m_avg": 10000.0},
        {"order_month": "2024-02", "orders": 120, "revenue": 12000.5, "margin": 4800.2, "margin_pct": 40.0,
         "mom_change": 2000.5, "mom_pct": 20.0, "trailing_3m_avg": 11000.25},
        {"order_month": "2024-03", "orders": 90, "revenue": 9000.0, "margin": 3600.0, "margin_pct": 40.0,
         "mom_change": -3000.5, "mom_pct": -25.0, "trailing_3m_avg": 10333.5},
    ]


def abc_rows() -> Rows:
    return [
        {"product_id": 1, "sku": "SKU-00001", "category": "Grocery", "revenue": 7000.0, "units": 50,
         "revenue_rank": 1, "cum_share_pct": 70.0, "abc_class": "A"},
        {"product_id": 2, "sku": "SKU-00002", "category": "Toys", "revenue": 2000.0, "units": 20,
         "revenue_rank": 2, "cum_share_pct": 90.0, "abc_class": "B"},
        {"product_id": 3, "sku": "<script>alert(1)</script>", "category": "Home & Garden", "revenue": 1000.0,
         "units": 10, "revenue_rank": 3, "cum_share_pct": 100.0, "abc_class": "C"},
        {"product_id": 4, "sku": "SKU-00004", "category": "Home", "revenue": 0, "units": 0,
         "revenue_rank": 4, "cum_share_pct": 100.0, "abc_class": "C"},
    ]


def rfm_rows() -> Rows:
    segments = ["Champion", "Loyal", "Lost", "Lost", "New"]
    return [
        {"customer_id": i + 1, "recency_days": 10 * i, "frequency": 5 - i, "monetary": 100.0 * (5 - i),
         "r_score": 5, "f_score": 4, "m_score": 3, "rfm_total": 12, "segment": seg}
        for i, seg in enumerate(segments)
    ]


def _cohort(month: str, size: int, offset: int, active: int, pct: float) -> dict[str, Any]:
    return {
        "cohort_month": month,
        "cohort_size": size,
        "month_offset": offset,
        "active_customers": active,
        "retention_pct": pct,
    }


def cohort_rows() -> Rows:
    # 2024-01 observable for M0..M2 (M2 is a genuine 0 %), 2024-02 for M0..M1, 2024-03 for M0 → 3 censored cells
    return [
        _cohort("2024-01", 50, 0, 50, 100.0),
        _cohort("2024-01", 50, 1, 20, 40.0),
        _cohort("2024-01", 50, 2, 0, 0.0),
        _cohort("2024-02", 40, 0, 40, 100.0),
        _cohort("2024-02", 40, 1, 10, 25.0),
        _cohort("2024-03", 30, 0, 30, 100.0),
    ]


def store_rows() -> Rows:
    return [
        {"store_id": 1, "name": "Store 001", "region": "West", "format": "flagship", "orders": 200, "revenue": 20000.0,
         "avg_basket": 100.0, "margin_pct": 41.2, "rank_in_region": 1, "region_share_pct": 100.0},
        {"store_id": 2, "name": "Store 002", "region": "South", "format": "small_format", "orders": 0, "revenue": 0,
         "avg_basket": None, "margin_pct": None, "rank_in_region": 1, "region_share_pct": None},
    ]


def channel_rows(none_share: bool = False) -> Rows:
    rows = [
        {"order_month": "2024-01", "in_store": 6000.0, "online": 3000.0, "pickup": 1000.0, "online_share_pct": 30.0},
        {"order_month": "2024-02", "in_store": 7000.0, "online": 4000.5, "pickup": 1000.0, "online_share_pct": 33.34},
        {"order_month": "2024-03", "in_store": 5000.0, "online": 3000.0, "pickup": 1000.0, "online_share_pct": 33.33},
    ]
    if none_share:
        rows[1]["online"] = None
        rows[1]["online_share_pct"] = None
    return rows


def repeat_rows() -> Rows:
    return [
        {"first_channel": "online", "customers": 100, "repeat_customers": 45, "repeat_rate_pct": 45.0,
         "avg_orders_per_customer": 1.9},
        {"first_channel": "in_store", "customers": 200, "repeat_customers": 70, "repeat_rate_pct": 35.0,
         "avg_orders_per_customer": 1.6},
        {"first_channel": "pickup", "customers": 50, "repeat_customers": 10, "repeat_rate_pct": 20.0,
         "avg_orders_per_customer": 1.3},
    ]


def dq_rows(severity: bool = False, warn_violations: int = 0, error_violations: int = 0) -> Rows:
    names = ["orders_without_items", "orders_before_signup", "negative_net_revenue", "orphan_items", "duplicate_emails"]
    if not severity:
        return [{"check_name": n, "violations": error_violations if i == 0 else 0} for i, n in enumerate(names)]
    rows = [
        {"check_name": n, "severity": "error", "violations": error_violations if i == 0 else 0,
         "description": f"{n} must be zero"}
        for i, n in enumerate(names)
    ]
    rows.append({"check_name": "customers_without_orders", "severity": "warn", "violations": warn_violations,
                 "description": "Expected > 0 for a <realistic> dataset"})
    rows.append({"check_name": "products_never_sold", "severity": "warn", "violations": 0,
                 "description": "Catalogue items with no sales"})
    return rows


def affinity_rows() -> Rows:
    return [
        {"pair_label": "SKU-00001 × SKU-00002", "sku_a": "SKU-00001", "sku_b": "SKU-00002", "category_a": "Grocery",
         "category_b": "Toys", "pair_orders": 40, "support_pct": 1.33, "confidence_pct": 20.0, "lift": 3.2},
        {"pair_label": "SKU-00003 × SKU-00004", "sku_a": "SKU-00003", "sku_b": "SKU-00004", "category_a": "Home",
         "category_b": "Home", "pair_orders": 12, "support_pct": 0.4, "confidence_pct": 8.0, "lift": 1.1},
    ]


def ltv_rows() -> Rows:
    rows: Rows = []
    for ch, base in (("in_store", 50.0), ("online", 60.0), ("pickup", 40.0)):
        for off in range(3):
            rows.append({"first_channel": ch, "month_offset": off, "eligible_customers": 100,
                         "cum_revenue_per_customer": round(base * (1 + 0.5 * off), 2)})
    return rows


def baseline_results(
    *,
    severity: bool = False,
    warn_violations: int = 0,
    error_violations: int = 0,
    include_new: bool = False,
    none_share: bool = False,
) -> dict[str, Rows]:
    results = {
        "01_monthly_revenue": monthly_rows(),
        "02_category_abc": abc_rows(),
        "03_customer_rfm": rfm_rows(),
        "04_cohort_retention": cohort_rows(),
        "05_store_performance": store_rows(),
        "06_channel_mix": channel_rows(none_share),
        "07_repeat_purchase_rate": repeat_rows(),
        "08_data_quality": dq_rows(severity, warn_violations, error_violations),
    }
    if include_new:
        results["09_basket_affinity"] = affinity_rows()
        results["10_customer_ltv"] = ltv_rows()
    return results


def sql_for(results: dict[str, Rows]) -> dict[str, str]:
    return {name: f"-- {name}\nSELECT 1 AS x FROM t WHERE a < b; -- <script>not code</script>" for name in results}


def meta_for(**overrides: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "generated_at": "2026-10-05 12:00 UTC",
        "elapsed_ms": 1234,
        "customers": 400,
        "orders": 3000,
        "line_items": 5400,
        "products": 160,
        "stores": 12,
    }
    meta.update(overrides)
    return meta


def svg_fragments(html: str) -> list[str]:
    return re.findall(r"<svg\b.*?</svg>", html, flags=re.S)


def assert_well_formed_svgs(html: str) -> list[ET.Element]:
    frags = svg_fragments(html)
    assert frags, "expected at least one <svg>"
    roots = [ET.fromstring(frag) for frag in frags]
    for root in roots:
        assert root.get("role") == "img"
        title = root.find("title")
        assert title is not None and (title.text or "").strip()
    return roots


@pytest.fixture
def results8() -> dict[str, Rows]:
    return baseline_results()


@pytest.fixture
def html8(results8: dict[str, Rows]) -> str:
    return report.build_html(results8, sql_for(results8), meta_for())


# --------------------------------------------------------------------------- charts: None-safe helpers
def test_charts_none_formatters():
    assert charts.esc(None) == "" and charts.esc("<&>") == "&lt;&amp;&gt;" and charts.esc(3) == "3"
    assert charts.fmt_cell(None) == "—"
    assert charts.fmt_cell(1234) == "1,234" and charts.fmt_cell(1234.5) == "1,234.50"
    assert charts.fmt_cell("<x>") == "&lt;x&gt;" and charts.fmt_cell(True) == "true"
    assert charts.fmt_value(12.0, "%") == "12.0%"
    assert charts.fmt_value(12345, "k") == "12.3k"
    assert charts.fmt_value(3, "") == "3" and charts.fmt_value(2.345, "") == "2.35"
    assert charts.fmt_value(1234.0, "") == "1,234"


def test_charts_none_line_chart_gaps_and_escaping():
    out = charts.line_chart(["a", "b<c", "c", "d"], [1.0, None, 3.0, 4.0], label="Revenue & gaps")
    assert out.startswith("<svg") and out.endswith("</svg>")
    assert out.count("<polyline") == 1  # the run [3, 4]; a lone observed point is a dot only
    assert out.count("<circle") == 3  # one dot per observed value, none for the gap
    assert "None" not in out and "b<c" not in out and "b&lt;c" in out
    assert "<title>Revenue &amp; gaps</title>" in out
    assert 'class="grid"' in out and 'class="ax"' in out and "series-0" in out
    [root] = assert_well_formed_svgs(out)
    assert root.get("role") == "img"


def test_charts_none_line_chart_empty_or_all_none_returns_empty():
    assert charts.line_chart([], []) == ""
    assert charts.line_chart(["a", "b"], [None, None]) == ""
    assert charts.line_chart(["a"], ["not a number"]) == ""
    assert charts.multi_line_chart(["a"], {}) == ""
    assert charts.multi_line_chart(["a"], {"s": [None]}) == ""
    assert charts.bar_chart([], []) == ""
    assert charts.bar_chart(["a"], [None]) == ""
    # ...but a single observed point is still rendered (a dot / a bar), never swallowed
    assert charts.line_chart(["a"], [1.0]).count("<circle") == 1
    assert charts.bar_chart(["a"], [1]).count('<rect class="bar') == 1


def test_charts_none_line_chart_constant_negative_and_mismatched_lengths():
    out = charts.line_chart(["a", "b"], [5, 5])
    assert "<polyline" in out and "nan" not in out and "inf" not in out
    out = charts.line_chart(["a", "b", "c"], [-10, 0, 10], unit="%")
    assert "<polyline" in out and ">-10%<" in out
    assert_well_formed_svgs(out)
    out = charts.line_chart(["a", "b"], [1, 2, 3, 4])  # extra values are ignored, never an IndexError
    assert out.count("<circle") == 2
    out = charts.line_chart([2024, None], [1, 2])  # non-string x labels are stringified / blanked
    assert ">2024<" in out and "None" not in out


def test_charts_none_multi_line_legend_and_series_classes():
    series = {"online": [1.0, 2.0, 3.0], "pickup": [None, 2.0, 2.5], "in_store": [None, None, None]}
    out = charts.multi_line_chart(["M0", "M1", "M2"], series, label="LTV", unit="")
    assert out.count('class="legend-item') == 2  # an all-None series is not plotted nor listed
    assert "series-0" in out and "series-1" in out and "series-2" not in out
    assert ">online<" in out and ">pickup<" in out and "in_store" not in out
    assert out.count("<polyline") == 2
    assert "None" not in out
    assert_well_formed_svgs(out)
    out = charts.multi_line_chart(["M0"], {"<b>": [1.0]})
    assert "<b>" not in out and "&lt;b&gt;" in out


def test_charts_none_bar_chart_values_labels_and_escaping():
    out = charts.bar_chart(["<script>alert(1)</script>", "b", "c"], [None, 2.5, 10], label="Bars", unit="%")
    assert out.count('<rect class="bar') == 2  # no bar for the None value
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert ">2.5%<" in out and ">10.0%<" in out
    assert "None" not in out
    assert_well_formed_svgs(out)
    out_k = charts.bar_chart(["A", "B"], [12345.0, 500.0], unit="k")
    assert ">12.3k<" in out_k and ">0.5k<" in out_k
    out_n = charts.bar_chart(["A"], [3], label="n")
    assert ">3<" in out_n
    out_neg = charts.bar_chart(["A", "B"], [-5, 5])
    assert out_neg.count('<rect class="bar') == 2 and 'class="ax base"' in out_neg
    assert_well_formed_svgs(out_neg)


def test_charts_none_bar_chart_many_labels_rotates_and_tooltips():
    labels = [f"SKU-{i:05d} × SKU-{i + 1:05d}" for i in range(15)]
    out = charts.bar_chart(labels, [float(i) for i in range(15)], label="Lift")
    assert "rotate(" in out
    assert out.count("<title>") >= 16  # chart title + one tooltip per bar
    assert_well_formed_svgs(out)
    short = charts.bar_chart(["a", "b"], [1, 2])
    assert "rotate(" not in short


def test_charts_none_table_caption_scope_limit_escaping():
    rows = [
        {"name<": "<i>x</i>", "value": 1.5, "count": None},
        {"name<": "y", "value": 2, "count": 3},
        {"name<": "z", "value": 3, "count": 4},
    ]
    out = charts.table(rows, limit=2, caption="Caption & co")
    assert "<caption>Caption &amp; co</caption>" in out
    assert '<th scope="col">name&lt;</th>' in out
    assert "<i>" not in out and "&lt;i&gt;x&lt;/i&gt;" in out
    assert ">1.50<" in out and ">—<" in out
    assert "Showing 2 of 3 rows." in out
    assert out.count("<tr") == 3  # header + 2 body rows
    assert charts.table([], caption="x") == '<p class="muted">No rows.</p>'
    assert "Showing" not in charts.table(rows, limit=0)
    assert charts.table(rows, limit=0).count("<tr") == 4
    assert "<caption>" not in charts.table(rows)
    flagged = charts.table(rows, row_class=lambda r: "flag" if r["count"] else "")
    assert flagged.count('<tr class="flag">') == 2


# --------------------------------------------------------------------------- heatmap: censored vs zero
def _heat() -> str:
    return charts.heatmap(
        cohort_rows(),
        row_key="cohort_month",
        col_key="month_offset",
        value_key="retention_pct",
        size_key="cohort_size",
        caption="Cohort retention",
    )


def test_heatmap_censored_cells_differ_from_zero():
    out = _heat()
    assert out.count('<td class="na" title="not yet observable">') == 3
    assert '<td class="h0" title="2024-01 → M2: 0 % (n=50)">0%</td>' in out
    assert '<td class="h10" title="2024-01 → M0: 100 % (n=50)">100%</td>' in out
    assert '<td class="h4" title="2024-01 → M1: 40 % (n=50)">40%</td>' in out
    assert '<td class="h2" title="2024-02 → M1: 25 % (n=40)">25%</td>' in out
    assert 'class="heat"' in out and "<caption>Cohort retention</caption>" in out
    assert out.count('<th scope="col">') == 5  # Cohort, Size, M0, M1, M2
    assert out.count('<th scope="row"') == 3
    assert "None" not in out and "style=" not in out


def test_heatmap_buckets_empty_and_escaping():
    assert [charts.heat_bucket(v) for v in (0, 9.9, 10, 42, 99.9, 100, 150, -5)] == [0, 0, 1, 4, 9, 10, 10, 0]
    assert "No rows" in charts.heatmap([], row_key="r", col_key="c", value_key="v", size_key="n")
    rows = [{"r": "<b>x</b>", "c": 0, "v": 50.0, "n": 1}, {"r": "<b>x</b>", "c": 1, "v": None, "n": 1}]
    out = charts.heatmap(rows, row_key="r", col_key="c", value_key="v", size_key="n")
    assert "<b>" not in out and "&lt;b&gt;x&lt;/b&gt;" in out
    assert out.count('class="na"') == 1  # a present row with a NULL value is also "not observable"
    assert '<td class="h5"' in out
    assert "None" not in out


# --------------------------------------------------------------------------- theme: tokens, dark, print
def test_theme_css_blocks_and_classes():
    css = theme.css()
    assert css == theme.css()  # deterministic: the CSP hash depends on it
    assert ":root{" in css
    assert "@media (prefers-color-scheme: dark)" in css
    assert "@media print" in css
    for i in range(11):
        assert f".heat td.h{i}{{" in css
    assert ".heat td.na{" in css
    for i in range(6):
        assert f".series-{i}" in css
    for cls in (".grid{", ".ax{", ".skip", ".legend", ".kpi", "details", "nav", "section"):
        assert cls in css
    for name in theme.TOKENS:
        assert f"--{name}:" in css
    assert "http" not in css and "@import" not in css and "url(" not in css
    assert "style=" not in css and "<" not in css


def test_theme_tokens_structure_and_no_duplicates_in_report():
    assert isinstance(theme.TOKENS, dict) and len(theme.TOKENS) >= 8
    for name, (light, dark) in theme.TOKENS.items():
        assert re.fullmatch(r"[a-z][a-z0-9-]*", name)
        assert re.fullmatch(r"#[0-9a-f]{6}", light) and re.fullmatch(r"#[0-9a-f]{6}", dark)
    assert {"ink", "muted", "line", "bg", "card", "accent", "accent-ink", "code-bg", "code-ink"} <= set(theme.TOKENS)
    assert not hasattr(report, "RED") and not hasattr(report, "GREY") and not hasattr(report, "CSS")


def test_theme_contrast_ratio_math():
    assert theme.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert theme.contrast_ratio("#ffffff", "#ffffff") == pytest.approx(1.0)
    assert theme.contrast_ratio("#777777", "#ffffff") == pytest.approx(4.48, abs=0.02)
    assert theme.contrast_ratio("#ffffff", "#777777") == theme.contrast_ratio("#777777", "#ffffff")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_theme_text_contrast_both_schemes(scheme):
    idx = 0 if scheme == "light" else 1
    tok = {k: v[idx] for k, v in theme.TOKENS.items()}
    pairs = [
        ("ink", "bg"), ("ink", "card"), ("muted", "bg"), ("muted", "card"),
        ("accent", "card"), ("accent", "bg"), ("accent-ink", "accent"), ("code-ink", "code-bg"),
    ]
    for fg, bg in pairs:
        assert fg in tok and bg in tok, (scheme, fg, bg)
        assert theme.contrast_ratio(tok[fg], tok[bg]) >= 4.5, (scheme, fg, bg)
    palette = theme.heat_palette(scheme)
    assert len(palette) == 11
    for i, (bg, fg) in enumerate(palette):
        assert theme.contrast_ratio(fg, bg) >= 4.5, (scheme, i, bg, fg)
    assert len({bg for bg, _ in palette}) == 11  # every bucket is visually distinct


def test_theme_favicon_is_inline_svg_data_uri():
    uri = theme.favicon_data_uri()
    assert uri.startswith("data:image/svg+xml,")
    assert '"' not in uri and "<" not in uri and "#" not in uri and " " not in uri
    svg = urllib.parse.unquote(uri[len("data:image/svg+xml,"):])
    root = ET.fromstring(svg)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"


# --------------------------------------------------------------------------- CSP policy and <head> meta
def test_csp_policy_strings():
    css = theme.css()
    digest = base64.b64encode(hashlib.sha256(css.encode("utf-8")).digest()).decode("ascii")
    expected = (
        f"default-src 'none'; style-src 'sha256-{digest}'; img-src 'self' data:; base-uri 'none'; form-action 'none'"
    )
    assert theme.style_hash(css) == f"sha256-{digest}"
    assert theme.csp_policy() == expected
    assert theme.csp_policy("meta") == expected
    assert theme.csp_policy("header") == expected + "; frame-ancestors 'none'"
    assert "frame-ancestors" not in theme.csp_policy("meta")
    assert theme.csp_policy("header", css_text="body{}") != theme.csp_policy("header")
    with pytest.raises(ValueError):
        theme.csp_policy("bogus")


def test_csp_html_meta_single_style_and_head_order(html8):
    styles = re.findall(r"<style>(.*?)</style>", html8, flags=re.S)
    assert len(styles) == 1 and html8.count("<style") == 1
    assert styles[0] == theme.css()
    digest = base64.b64encode(hashlib.sha256(styles[0].encode("utf-8")).digest()).decode("ascii")
    m = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">', html8)
    assert m is not None
    assert f"'sha256-{digest}'" in m.group(1)
    assert m.group(1) == theme.csp_policy("meta")
    head = html8[: html8.index("</head>")]
    assert head.index('http-equiv="Content-Security-Policy"') < head.index("<style>")
    assert '<meta charset="utf-8">' in head
    assert '<meta name="referrer" content="no-referrer">' in head
    assert '<meta name="color-scheme" content="light dark">' in head
    assert '<link rel="icon" href="data:image/svg+xml,' in head
    assert "<title>RetailPulse" in head


def test_csp_no_inline_styles_scripts_handlers_or_remote_resources(html8):
    body = re.sub(r"<pre>.*?</pre>", "", html8, flags=re.S)
    assert re.search(r"\sstyle\s*=", body) is None
    assert "<script" not in html8.lower()
    assert re.search(r"\son[a-z]+\s*=", body, flags=re.I) is None
    assert "javascript:" not in html8.lower()
    assert "<img" not in html8 and "<iframe" not in html8 and "<link rel=\"stylesheet\"" not in html8
    assert "@import" not in html8 and "url(" not in html8
    refs = re.findall(r'\s(href|src)="([^"]*)"', html8)
    assert refs
    for attr, value in refs:
        if value == GITHUB:
            continue
        assert not value.startswith(("http://", "https://", "//", "/")), (attr, value)
    assert sum(1 for _, v in refs if v == GITHUB) == 1


def test_csp_vercel_json_security_headers():
    cfg = json.loads((Path(__file__).resolve().parents[1] / "vercel.json").read_text(encoding="utf-8"))
    values = [
        h["value"]
        for entry in cfg["headers"]
        for h in entry["headers"]
        if h["key"].lower() == "content-security-policy"
    ]
    assert values == [theme.csp_policy("header")]


# --------------------------------------------------------------------------- report: sections, TOC, metadata
def test_report_sections_skeleton(html8, results8):
    assert "RetailPulse" in html8 and "<svg" in html8 and "Show SQL" in html8
    assert html8.startswith("<!doctype html>")
    assert '<html lang="en">' in html8
    assert '<a class="skip" href="#main">' in html8
    assert '<main id="main">' in html8 and html8.count("<main") == 1
    assert "<nav" in html8 and 'aria-label="Sections"' in html8
    assert '<meta name="description" content="' in html8
    assert "8 analytical queries" in html8
    for name in results8:
        assert f'id="sec-{name}"' in html8
        assert f'href="#sec-{name}"' in html8
    assert html8.count("<section") == len(results8) == html8.count("<h2")
    assert html8.count("Show SQL") == len(results8)
    assert re.findall(r'<section id="sec-([^"]+)"', html8)[-1] == "08_data_quality"
    assert "Built by" in html8 and f'href="{GITHUB}"' in html8 and ">D L Narayana<" in html8
    anchors = set(re.findall(r'href="#([^"]+)"', html8))
    ids = re.findall(r'\sid="([^"]+)"', html8)
    assert anchors <= set(ids) and len(ids) == len(set(ids))
    assert "generated 2026-10-05 12:00 UTC" in html8 and "in 1,234 ms" in html8


def test_report_sections_custom_renderers_captions_and_escaping(html8):
    assert "Revenue by ABC class" in html8
    assert "Customers per segment" in html8
    assert 'class="heat"' in html8 and 'title="not yet observable"' in html8
    assert html8.count("<caption>") >= 8
    assert '<th scope="col">' in html8 and '<th scope="row"' in html8
    roots = assert_well_formed_svgs(html8)
    assert len(roots) >= 4
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html8  # SKU label from the data is escaped
    assert "Home &amp; Garden" in html8
    assert "&lt;script&gt;not code&lt;/script&gt;" in html8  # SQL text is escaped
    assert "<details><summary>Show SQL</summary><pre>" in html8


def test_report_sections_fallback_meta_when_headers_absent(results8):
    html = report.build_html(results8, sql_for(results8), meta_for(), specs={})
    assert "<h2>Monthly revenue &amp; MoM growth</h2>" in html
    assert "<h2>Data-quality checks</h2>" in html
    names = set(BASELINE)
    assert names <= set(report.SECTION_META)
    for name, meta in report.SECTION_META.items():
        assert meta["title"] and meta["question"] and meta["technique"], name
        assert parse_chart(meta["chart"])["kind"] in {"table", "none", "line", "bar", "heatmap"}
        assert isinstance(meta["limit"], int)
    assert report.section_order(["08_data_quality", "01_monthly_revenue", "10_customer_ltv", "09_basket_affinity"]) == [
        "01_monthly_revenue", "09_basket_affinity", "10_customer_ltv", "08_data_quality",
    ]


def test_report_sections_titles_from_specs_order_and_new_queries():
    results = baseline_results(include_new=True)
    specs = {
        "09_basket_affinity": QuerySpec(
            "09_basket_affinity",
            title="Basket affinity (lift)",
            question="Which SKUs sell together?",
            technique="self-join",
            chart="bar x=pair_label y=lift",
            limit=15,
            portability=["self-join is ANSI <SQL>"],
        ),
        "10_customer_ltv": QuerySpec(
            "10_customer_ltv",
            title="Customer LTV by channel",
            chart="line x=month_offset y=cum_revenue_per_customer series=first_channel",
        ),
    }
    html = report.build_html(results, sql_for(results), meta_for(), specs=specs)
    assert "10 analytical queries" in html
    order = re.findall(r'<section id="sec-([^"]+)"', html)
    assert order == sorted(n for n in results if n != "08_data_quality") + ["08_data_quality"]
    assert "<h2>Basket affinity (lift)</h2>" in html
    assert "Which SKUs sell together?" in html and "self-join" in html
    assert "self-join is ANSI &lt;SQL&gt;" in html
    sec10 = html[html.index('id="sec-10_customer_ltv"'): html.index('id="sec-08_data_quality"')]
    assert sec10.count('class="legend-item') == 3
    assert sec10.count("<polyline") == 3
    sec09 = html[html.index('id="sec-09_basket_affinity"'): html.index('id="sec-10_customer_ltv"')]
    assert sec09.count('<rect class="bar') == len(results["09_basket_affinity"])
    assert "SKU-00001 × SKU-00002" in sec09
    assert_well_formed_svgs(html)


def test_report_sections_show_seed_version_config():
    results = baseline_results()
    config = {"customers": 6000, "products": 160, "stores": 12, "orders": 30000, "start": "2024-01-01", "months": 24}
    html = report.build_html(results, sql_for(results), meta_for(seed=42, version="0.2.0", config=config))
    assert "seed 42" in html and "v0.2.0" in html
    assert "2024-01-01" in html and "6,000" in html and "30,000" in html
    plain = report.build_html(results, sql_for(results), meta_for())
    assert re.search(r"seed \d", plain) is None and "v0." not in plain


# --------------------------------------------------------------------------- report: tolerance
def test_report_tolerance_dq_status_with_and_without_severity():
    assert report.dq_status([{"check_name": "a", "violations": 0}]) == ("PASS", 0)
    assert report.dq_status([{"check_name": "a", "violations": 2}]) == ("FAIL", 0)
    rows = [
        {"check_name": "a", "severity": "error", "violations": 0, "description": "x"},
        {"check_name": "b", "severity": "warn", "violations": 3, "description": "y"},
        {"check_name": "c", "severity": "warn", "violations": 0, "description": "z"},
    ]
    assert report.dq_status(rows) == ("PASS", 1)
    rows[0]["violations"] = 1
    assert report.dq_status(rows) == ("FAIL", 1)
    assert report.dq_status([]) == ("n/a", 0)
    assert report.dq_status([{"check_name": "a", "violations": None}]) == ("PASS", 0)
    assert report.dq_status([{"check_name": "a", "severity": None, "violations": 1}]) == ("FAIL", 0)


@pytest.mark.parametrize("severity", [False, True])
def test_report_tolerance_dq_kpi_pass_fail_and_warnings(severity):
    results = baseline_results(severity=severity, warn_violations=3)
    html = report.build_html(results, sql_for(results), meta_for())
    assert ">PASS<" in html and ">FAIL<" not in html
    assert ("1 warning" in html) is severity
    if severity:
        assert "sev-warn" in html and "Expected &gt; 0 for a &lt;realistic&gt; dataset" in html
    else:
        assert "no warnings" in html
    results = baseline_results(severity=severity, error_violations=2)
    html = report.build_html(results, sql_for(results), meta_for())
    assert ">FAIL<" in html and ">PASS<" not in html
    assert "None" not in html


def test_report_tolerance_elapsed_none_and_present():
    results = baseline_results()
    html = report.build_html(results, sql_for(results), meta_for(elapsed_ms=None))
    assert "None" not in html
    assert re.search(r"in [\d,]+ ms", html) is None
    assert "generated 2026-10-05 12:00 UTC" in html
    html2 = report.build_html(results, sql_for(results), meta_for(elapsed_ms=1234))
    assert "in 1,234 ms" in html2


def test_report_tolerance_none_values_in_series_and_cells():
    results = baseline_results(none_share=True)
    html = report.build_html(results, sql_for(results), meta_for())
    assert "None" not in html
    assert ">—<" in html  # None cells render as an em dash
    assert_well_formed_svgs(html)
    sec06 = html[html.index('id="sec-06_channel_mix"'): html.index('id="sec-07_repeat_purchase_rate"')]
    assert "<svg" in sec06 and sec06.count("<circle") == 2  # the NULL month is a gap, not a crash


def test_report_tolerance_empty_results_and_missing_queries():
    empty = {name: [] for name in BASELINE}
    html = report.build_html(empty, sql_for(empty), meta_for())
    assert "No rows" in html and "None" not in html
    assert html.count("<section") == 8 and ">n/a<" in html
    partial = {k: v for k, v in baseline_results().items() if k not in {"01_monthly_revenue", "08_data_quality"}}
    html = report.build_html(partial, sql_for(partial), meta_for())
    assert html.count("<section") == 6 and "6 analytical queries" in html
    assert "None" not in html
    only_dq = {"08_data_quality": dq_rows(severity=True)}
    html = report.build_html(only_dq, sql_for(only_dq), meta_for())
    assert html.count("<section") == 1 and ">PASS<" in html and "None" not in html


def test_report_tolerance_missing_sql_text_meta_keys_and_bad_chart_columns():
    results = baseline_results()
    sql = sql_for(results)
    del sql["05_store_performance"]
    meta = {"generated_at": "2026-10-05 12:00 UTC", "elapsed_ms": None}
    specs = {"05_store_performance": QuerySpec("05_store_performance", title="Stores", chart="line x=nope y=nada")}
    html = report.build_html(results, sql, meta, specs=specs)
    assert "SQL not available" in html
    assert "<h2>Stores</h2>" in html
    assert "None" not in html
    assert re.search(r"seed \d", html) is None
    assert html.count("<section") == 8


# --------------------------------------------------------------------------- downloads
def test_downloads_links_relative_only():
    results = baseline_results()
    exports = {name: f"csv/{name}.csv" for name in results}
    exports["02_category_abc"] = "./csv/02_category_abc.csv"
    exports["03_customer_rfm"] = "/abs/03_customer_rfm.csv"  # absolute path: never emitted
    exports["04_cohort_retention"] = "https://example.com/x.csv"  # remote: never emitted
    html = report.build_html(results, sql_for(results), meta_for(exports=exports))
    assert 'href="csv/01_monthly_revenue.csv"' in html
    assert 'href="csv/02_category_abc.csv"' in html
    assert "/abs/" not in html and "example.com" not in html
    assert html.count(">Download CSV<") == len(results) - 2
    assert 'href="data.json"' in html
    for attr, value in re.findall(r'\s(href|src)="([^"]*)"', html):
        assert value == GITHUB or not value.startswith(("http:", "https:", "//", "/")), (attr, value)


def test_downloads_absent_without_exports():
    results = baseline_results()
    html = report.build_html(results, sql_for(results), meta_for())
    assert "Download CSV" not in html
    assert 'href="data.json"' not in html
    html = report.build_html(results, sql_for(results), meta_for(exports={}))
    assert "Download CSV" not in html and 'href="data.json"' not in html


# --------------------------------------------------------------------------- synthetic-data disclosure
def header_text(html: str) -> str:
    """Visible text of the <header> element (tags stripped), so assertions are about what a reader sees."""
    m = re.search(r"<header>(.*?)</header>", html, flags=re.S)
    assert m is not None
    return re.sub(r"<[^>]+>", " ", m.group(1))


def description_meta(html: str) -> str:
    m = re.search(r'<meta name="description" content="([^"]*)">', html)
    assert m is not None
    return m.group(1)


def test_synthetic_disclosure_visible_in_header_when_meta_says_synthetic():
    results = baseline_results()
    meta = meta_for(data_source="synthetic", seed=42, version="0.2.0")
    html = report.build_html(results, sql_for(results), meta)
    text = header_text(html)
    assert "synthetic" in text.lower() and "demo" in text.lower()
    # plain visible text inside the first paragraph, not an attribute or a class name
    first_p = re.search(r"<header><h1>RetailPulse</h1><p>(.*?)</p>", html, flags=re.S)
    assert first_p is not None and "synthetic demo dataset" in first_p.group(1)
    # the existing pinned substrings and the second metadata line are intact
    assert "generated 2026-10-05 12:00 UTC" in text and "in 1,234 ms" in text
    assert "RetailPulse" in text and "seed 42" in text and "v0.2.0" in text
    assert "synthetic retail dataset" in description_meta(html)
    assert "None" not in html
    # the disclosure is text only: one stylesheet, byte-identical to theme.css(), and the CSP hash is unchanged
    assert html.count("<style") == 1 and f"<style>{theme.css()}</style>" in html
    assert f'content="{theme.csp_policy("meta")}"' in html


@pytest.mark.parametrize("extra", [{}, {"data_source": "customer"}, {"data_source": None}, {"data_source": ""}])
def test_synthetic_disclosure_absent_for_other_or_missing_data_source(extra):
    results = baseline_results()
    html = report.build_html(results, sql_for(results), meta_for(**extra))
    text = header_text(html)
    assert "synthetic" not in text.lower() and "demo" not in text.lower()
    assert "generated 2026-10-05 12:00 UTC" in text and "in 1,234 ms" in text
    desc = description_meta(html)
    assert "retail dataset" in desc and "synthetic" not in desc.lower()
    assert "None" not in html
