"""Render query results into a self-contained static HTML dashboard.

Sections are driven by the SQL header metadata (:mod:`retailpulse.registry`): one ``<section>`` per query in
``results``, in name order with the data-quality section last, each with its question, technique, chart (per
``@chart``), result table, CSV link and the SQL itself.  Charts are inline SVG generated in Python — no
JavaScript — so the output is a single file served from any static host under a strict Content-Security-Policy
(one hashed ``<style>`` element, no inline ``style`` attributes, no external resources).
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from . import charts, registry, theme
from .charts import esc
from .registry import QuerySpec, parse_chart

Rows = list[dict[str, Any]]

DQ_QUERY = "08_data_quality"
GITHUB_URL = "https://github.com/D-L-Narayana/retailpulse"
SEGMENT_ORDER = ("Champion", "Loyal", "Regular", "New", "At Risk", "Lost")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

# Fallback metadata used when a query file carries no `-- @key:` header (or no registry spec is supplied).
SECTION_META: dict[str, dict[str, Any]] = {
    "01_monthly_revenue": {
        "title": "Monthly revenue & MoM growth",
        "question": "How is net revenue trending month over month, and what margin does it carry?",
        "technique": "LAG for month-over-month change, trailing 3-month AVG window",
        "chart": "line x=order_month y=revenue",
        "limit": 24,
    },
    "02_category_abc": {
        "title": "ABC product classification",
        "question": "Which SKUs make up the top 70 % (A), next 20 % (B) and last 10 % (C) of revenue?",
        "technique": "Running SUM() OVER for cumulative revenue share, ranked products",
        "chart": "table",
        "limit": 10,
    },
    "03_customer_rfm": {
        "title": "Customer RFM segmentation",
        "question": "Which customers are champions, loyal, new, at risk or lost?",
        "technique": "Quintile scores for recency, frequency and monetary value, CASE segmentation",
        "chart": "table",
        "limit": 10,
    },
    "04_cohort_retention": {
        "title": "Cohort retention",
        "question": "Of the customers acquired in a given month, what share bought again N months later?",
        "technique": "Monthly acquisition cohorts joined to later activity by month offset",
        "chart": "heatmap row=cohort_month col=month_offset value=retention_pct size=cohort_size",
        "limit": 24,
    },
    "05_store_performance": {
        "title": "Store performance league table",
        "question": "Which stores lead their region on revenue, basket size and margin?",
        "technique": "DENSE_RANK within region, regional share via SUM() OVER (PARTITION BY)",
        "chart": "table",
        "limit": 12,
    },
    "06_channel_mix": {
        "title": "Channel mix by month",
        "question": "How is monthly revenue split between in-store, online and pickup?",
        "technique": "Conditional-aggregation pivot of revenue by channel",
        "chart": "line x=order_month y=online_share_pct unit=%",
        "limit": 24,
    },
    "07_repeat_purchase_rate": {
        "title": "Repeat purchase rate by acquisition channel",
        "question": "Do customers acquired online, in store or via pickup come back for a second order?",
        "technique": "ROW_NUMBER to isolate the first order, repeat rate per channel",
        "chart": "bar x=first_channel y=repeat_rate_pct unit=%",
        "limit": 15,
    },
    "08_data_quality": {
        "title": "Data-quality checks",
        "question": "Does the loaded dataset satisfy every referential, temporal and pricing invariant?",
        "technique": "Anti-joins and invariant counts; error rows fail the build, warn rows are informational",
        "chart": "table",
        "limit": 30,
    },
    "09_basket_affinity": {
        "title": "Basket affinity",
        "question": "Which product pairs are bought together more often than chance predicts?",
        "technique": "order_items self-join; support, confidence and lift per pair",
        "chart": "bar x=pair_label y=lift",
        "limit": 15,
    },
    "10_customer_ltv": {
        "title": "Customer lifetime value by acquisition channel",
        "question": "Do online-acquired customers spend more over their first year than in-store or pickup ones?",
        "technique": "ROW_NUMBER first-order attribution, cumulative SUM() OVER per month offset",
        "chart": "line x=month_offset y=cum_revenue_per_customer series=first_channel",
        "limit": 36,
    },
}


# --------------------------------------------------------------------------- small helpers
def _f(value: Any) -> float:
    """Numeric value or 0.0 (``None``, strings and bools do not count)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _int_text(value: Any) -> str:
    if isinstance(value, bool):
        return esc(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.0f}"
    return esc(value)


def _money(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 1e6:
        return f"${value / 1e6:,.2f}M"
    if magnitude >= 1e3:
        return f"${value / 1e3:,.1f}k"
    return f"${value:,.0f}"


def _attr(spec: Any, key: str, default: Any = None) -> Any:
    """Attribute of a QuerySpec-like object (or key of a mapping), ``default`` when missing or ``None``."""
    value = spec.get(key) if isinstance(spec, Mapping) else getattr(spec, key, None)
    return default if value is None else value


def _label(column: str) -> str:
    return column.replace("_", " ").strip().capitalize()


def relative_href(value: Any) -> str | None:
    """A relative URL safe to emit under the ``/retailpulse/`` base path, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    href = value.strip().replace("\\", "/")
    while href.startswith("./"):
        href = href[2:]
    if not href or href.startswith("/") or _SCHEME.match(href) or ".." in href.split("/"):
        return None
    return href


def section_order(names: Sequence[str]) -> list[str]:
    """Name order, except the data-quality section always comes last."""
    ordered = sorted(n for n in names if n != DQ_QUERY)
    if DQ_QUERY in names:
        ordered.append(DQ_QUERY)
    return ordered


def dq_status(rows: Sequence[Mapping[str, Any]]) -> tuple[str, int]:
    """``("PASS" | "FAIL" | "n/a", warnings)``: FAIL when any error-severity row has violations.

    A missing or NULL ``severity`` counts as ``error`` (PLAN §3.5); ``warnings`` is the number of ``warn`` rows
    with at least one violation.
    """
    if not rows:
        return ("n/a", 0)
    errors = 0.0
    warnings = 0
    for row in rows:
        count = _f(row.get("violations"))
        severity = str(row.get("severity") or "error").lower()
        if severity == "warn":
            warnings += count > 0
        else:
            errors += count
    return ("FAIL" if errors > 0 else "PASS", warnings)


# --------------------------------------------------------------------------- specs
def _fallback_spec(name: str) -> QuerySpec:
    meta = SECTION_META.get(name)
    if meta is None:
        return QuerySpec(name, title=registry.humanise(name) or name)
    return QuerySpec(
        name,
        title=str(meta["title"]),
        question=str(meta["question"]),
        technique=str(meta["technique"]),
        chart=str(meta["chart"]),
        limit=int(meta["limit"]),
    )


def _resolve_specs(names: Sequence[str], specs: Mapping[str, Any] | None) -> dict[str, Any]:
    if specs is None:
        try:
            specs = registry.load_specs()
        except OSError:
            specs = {}
    resolved: dict[str, Any] = {}
    for name in names:
        spec = specs.get(name)
        resolved[name] = spec if spec is not None else _fallback_spec(name)
    return resolved


def _title(spec: Any, name: str) -> str:
    title = str(_attr(spec, "title", "")).strip()
    return title or registry.humanise(name) or name


# --------------------------------------------------------------------------- custom renderers
def _caption(name: str, rows: Rows) -> str:
    return f"{name}.sql — {len(rows):,} rows"


def _render_abc(name: str, rows: Rows, limit: int, title: str) -> str:
    counts = {k: 0 for k in "ABC"}
    revenue = {k: 0.0 for k in "ABC"}
    for row in rows:
        cls = row.get("abc_class")
        if cls in counts:
            counts[cls] += 1
            revenue[cls] += _f(row.get("revenue"))
    chart = ""
    if rows:
        labels = [f"{k} ({counts[k]:,} SKUs)" for k in "ABC"]
        chart = charts.bar_chart(labels, [revenue[k] for k in "ABC"], label="Revenue by ABC class", unit="k")
    return chart + charts.table(rows, limit, _caption(name, rows))


def _render_rfm(name: str, rows: Rows, limit: int, title: str) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        segment = row.get("segment")
        if segment is not None:
            counts[str(segment)] = counts.get(str(segment), 0) + 1
    segments = [s for s in SEGMENT_ORDER if s in counts] + sorted(s for s in counts if s not in SEGMENT_ORDER)
    chart = charts.bar_chart(segments, [counts[s] for s in segments], label="Customers per segment")
    return chart + charts.table(rows, limit, _caption(name, rows))


def _render_cohort(name: str, rows: Rows, limit: int, title: str) -> str:
    return charts.heatmap(
        rows,
        row_key="cohort_month",
        col_key="month_offset",
        value_key="retention_pct",
        size_key="cohort_size",
        caption="Share of each monthly cohort active N months after acquisition; striped cells are not yet observable",
    )


def _dq_row_class(row: Mapping[str, Any]) -> str:
    severity = str(row.get("severity") or "error").lower()
    return f"sev-{severity}" + (" hit" if _f(row.get("violations")) > 0 else "")


def _render_dq(name: str, rows: Rows, limit: int, title: str) -> str:
    # Every check is shown regardless of @limit: the table *is* the audit trail.
    return charts.table(rows, 0, _caption(name, rows), row_class=_dq_row_class, table_class="dq")


_CUSTOM = {
    "02_category_abc": _render_abc,
    "03_customer_rfm": _render_rfm,
    "04_cohort_retention": _render_cohort,
    DQ_QUERY: _render_dq,
}


# --------------------------------------------------------------------------- generic @chart renderers
def _line_from_rows(rows: Rows, chart: Mapping[str, str]) -> str:
    x, y, unit = chart["x"], chart["y"], chart.get("unit", "")
    series_key = chart.get("series")
    label = f"{_label(y)} by {_label(x)}"
    if not series_key:
        return charts.line_chart([r.get(x) for r in rows], [r.get(y) for r in rows], label=label, unit=unit)
    xs: list[Any] = []
    per_series: dict[str, dict[Any, Any]] = {}
    for row in rows:
        xv = row.get(x)
        if xv not in xs:
            xs.append(xv)
        raw = row.get(series_key)
        per_series.setdefault("—" if raw is None else str(raw), {})[xv] = row.get(y)
    try:
        xs.sort()
    except TypeError:
        pass  # mixed types: keep first-seen order
    data = {name: [values.get(xv) for xv in xs] for name, values in per_series.items()}
    return charts.multi_line_chart(xs, data, label=label, unit=unit)


def _bar_from_rows(rows: Rows, chart: Mapping[str, str], limit: int) -> str:
    x, y, unit = chart["x"], chart["y"], chart.get("unit", "")
    shown = (rows[:limit] if limit > 0 else rows)[:40]
    label = f"{_label(y)} by {_label(x)}"
    return charts.bar_chart([r.get(x) for r in shown], [r.get(y) for r in shown], label=label, unit=unit)


def _render_generic(name: str, rows: Rows, chart: Mapping[str, str], limit: int, title: str) -> str:
    kind = chart["kind"]
    if kind == "none":
        return f'<p class="muted">{len(rows):,} rows — no visualisation configured for this query.</p>'
    if kind == "heatmap":
        prefix = "M" if chart["col"] == "month_offset" else ""
        return charts.heatmap(
            rows,
            row_key=chart["row"],
            col_key=chart["col"],
            value_key=chart["value"],
            size_key=chart["size"],
            caption=title,
            col_prefix=prefix,
        )
    viz = ""
    if rows and kind == "line":
        viz = _line_from_rows(rows, chart)
    elif rows and kind == "bar":
        viz = _bar_from_rows(rows, chart, limit)
    return viz + charts.table(rows, limit, _caption(name, rows))


# --------------------------------------------------------------------------- page parts
def _section(name: str, rows: Rows, spec: Any, sql: str | None, csv_href: str | None) -> str:
    title = _title(spec, name)
    question = str(_attr(spec, "question", ""))
    technique = str(_attr(spec, "technique", ""))
    limit = _attr(spec, "limit", registry.DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        limit = registry.DEFAULT_LIMIT
    chart = parse_chart(str(_attr(spec, "chart", registry.DEFAULT_CHART)))
    notes = [str(n) for n in (_attr(spec, "portability", []) or []) if n]

    custom = _CUSTOM.get(name)
    body = custom(name, rows, limit, title) if custom else _render_generic(name, rows, chart, limit, title)

    files = [f'<span class="mono">{esc(name)}.sql</span>']
    if csv_href:
        files.append(f'<a href="{esc(csv_href)}" download>Download CSV</a>')
    port = ""
    if notes:
        items = "".join(f"<li>{esc(n)}</li>" for n in notes)
        port = f'<p class="muted">Portability notes</p><ul class="port">{items}</ul>'
    sql_html = esc(sql) if sql else "SQL not available"
    return (
        f'<section id="sec-{esc(name)}"><h2>{esc(title)}</h2>'
        + (f'<p class="sub">{esc(question)}</p>' if question else "")
        + (f'<p class="tech muted">{esc(technique)}</p>' if technique else "")
        + f'<p class="dl">{" · ".join(files)}</p>'
        + body
        + f"<details><summary>Show SQL</summary><pre>{sql_html}</pre>{port}</details></section>"
    )


def _kpis(results: Mapping[str, Rows], meta: Mapping[str, Any]) -> str:
    items: list[tuple[str, str, str]] = []
    monthly = results.get("01_monthly_revenue")
    if monthly is not None:
        total_rev = sum(_f(r.get("revenue")) for r in monthly)
        margin = sum(_f(r.get("margin")) for r in monthly)
        orders = meta.get("orders")
        if not isinstance(orders, int) or isinstance(orders, bool):
            orders = round(sum(_f(r.get("orders")) for r in monthly))
        items.append((_money(total_rev), "Net revenue", ""))
        items.append((f"{orders:,}", "Orders", ""))
        items.append((f"{margin / total_rev * 100:.1f}%" if total_rev else "—", "Gross margin", ""))
    for key, label in (("customers", "Customers"), ("line_items", "Line items")):
        value = meta.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            items.append((f"{value:,}", label, ""))
    if DQ_QUERY in results:
        status, warnings = dq_status(results.get(DQ_QUERY) or [])
        if status == "n/a":
            label = "Data quality"
        else:
            suffix = "no warnings" if warnings == 0 else f"{warnings} warning" + ("" if warnings == 1 else "s")
            label = f"Data quality · {suffix}"
        items.append((status, label, {"PASS": "ok", "FAIL": "fail"}.get(status, "")))
    cards = "".join(
        f'<div class="kpi{" " + cls if cls else ""}">'
        f'<div class="v">{esc(value)}</div><div class="l">{esc(label)}</div></div>'
        for value, label, cls in items
    )
    return f'<div class="kpis">{cards}</div>'


def is_synthetic_source(meta: Mapping[str, Any]) -> bool:
    """``True`` only when the build metadata declares the data synthetic (``meta["data_source"] == "synthetic"``).

    The CLI's ``build`` always generates its dataset and sets this key; library callers rendering their own
    results may omit it, and their data is then never labelled as synthetic.
    """
    return str(meta.get("data_source") or "").strip().lower() == "synthetic"


def _header(meta: Mapping[str, Any], n_queries: int, data_href: str | None) -> str:
    noun = "query" if n_queries == 1 else "queries"
    line = f"Python + SQL retail analytics · {n_queries} analytical {noun} over a normalised 5-table schema"
    if is_synthetic_source(meta):
        line += " · synthetic demo dataset"
    generated = meta.get("generated_at")
    if generated:
        line += f" · generated {esc(generated)}"
    elapsed = meta.get("elapsed_ms")
    if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
        line += f" in {elapsed:,.0f} ms"
    bits: list[str] = []
    if meta.get("version"):
        bits.append(f"v{esc(meta['version'])}")
    if meta.get("seed") is not None:
        bits.append(f"seed {esc(meta['seed'])}")
    config = meta.get("config")
    if isinstance(config, Mapping):
        for key in ("customers", "products", "stores", "orders"):
            if config.get(key) is not None:
                bits.append(f"{_int_text(config[key])} {key}")
        if config.get("months") is not None:
            span = f"{_int_text(config['months'])} months"
            if config.get("start"):
                span += f" from {esc(config['start'])}"
            bits.append(span)
    if data_href:
        bits.append(f'<a href="{esc(data_href)}">data.json</a>')
    meta_line = f'<p class="meta">{" · ".join(bits)}</p>' if bits else ""
    return f"<header><h1>RetailPulse</h1><p>{line}</p>{meta_line}</header>"


def _toc(order: Sequence[str], titles: Mapping[str, str]) -> str:
    items = "".join(f'<li><a href="#sec-{esc(n)}">{esc(titles[n])}</a></li>' for n in order)
    return f'<nav aria-label="Sections"><ul>{items}</ul></nav>'


def _exports(meta: Mapping[str, Any]) -> tuple[dict[str, str], str | None]:
    """``(csv link per query, data.json link)`` from ``meta["exports"]`` — relative URLs only."""
    raw = meta.get("exports")
    if not isinstance(raw, Mapping) or not raw:
        return {}, None
    links = {str(k): href for k, v in raw.items() if (href := relative_href(v)) is not None}
    data_href = links.pop("data.json", None) or links.pop("data", None) or "data.json"
    return links, data_href


# --------------------------------------------------------------------------- entry point
def build_html(
    results: dict[str, list[dict[str, Any]]],
    sql_text: dict[str, str],
    meta: dict[str, Any],
    specs: Mapping[str, Any] | None = None,
) -> str:
    """Render the dashboard.

    ``results`` maps query names to rows, ``sql_text`` maps names to SQL source, ``meta`` carries the build
    facts (PLAN §3.4).  ``specs`` (optional) overrides the registry metadata, e.g. for tests; by default the
    ``-- @key:`` headers of the SQL files are used, with :data:`SECTION_META` as the fallback.
    """
    order = section_order(list(results))
    spec_map = _resolve_specs(order, specs)
    titles = {name: _title(spec_map[name], name) for name in order}
    csv_links, data_href = _exports(meta)
    css = theme.css()
    n_queries = len(results)
    dataset = "a synthetic retail dataset" if is_synthetic_source(meta) else "a retail dataset"
    description = (
        f"RetailPulse — Python + SQL retail analytics dashboard: {n_queries} analytical SQL "
        f"{'query' if n_queries == 1 else 'queries'} over {dataset}, rendered as one static page "
        "with inline SVG charts and zero JavaScript."
    )
    head = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{theme.csp_policy("meta", css)}">'
        '<meta name="referrer" content="no-referrer">'
        '<meta name="color-scheme" content="light dark">'
        f'<meta name="description" content="{esc(description)}">'
        "<title>RetailPulse — SQL analytics dashboard</title>"
        f'<link rel="icon" href="{theme.favicon_data_uri()}">'
        f"<style>{css}</style></head><body>"
    )
    sections = [
        _section(name, results.get(name) or [], spec_map[name], sql_text.get(name), csv_links.get(name))
        for name in order
    ]
    parts = [
        head,
        '<a class="skip" href="#main">Skip to content</a>',
        _header(meta, n_queries, data_href),
        '<main id="main">',
        _toc(order, titles),
        _kpis(results, meta),
        *sections,
        "</main>",
        f'<footer>Built by <a href="{GITHUB_URL}">D L Narayana</a> · Python 3 stdlib + SQLite · '
        "no runtime dependencies · zero JavaScript</footer></body></html>",
    ]
    return "".join(parts)
