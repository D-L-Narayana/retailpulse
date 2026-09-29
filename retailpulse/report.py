"""Render query results into a self-contained static HTML dashboard.

Charts are inline SVG generated in Python — no JavaScript libraries, so the
output is a single file that can be served from any static host (e.g. Vercel).
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any, Sequence

RED = "#CC0000"
GREY = "#6b7280"


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return html.escape(str(v)) if v is not None else "—"


def table(rows: Sequence[dict[str, Any]], limit: int = 15) -> str:
    if not rows:
        return "<p class='muted'>No rows.</p>"
    cols = list(rows[0])
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(f"<td>{_fmt(r[c])}</td>" for c in cols) + "</tr>" for r in rows[:limit]
    )
    more = f"<p class='muted'>Showing {limit} of {len(rows):,} rows.</p>" if len(rows) > limit else ""
    return f"<div class='tw'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}"


def line_chart(xs: Sequence[str], ys: Sequence[float], w: int = 720, h: int = 220, label: str = "", yfmt=lambda v: f"{v/1000:,.0f}k") -> str:
    if not ys:
        return ""
    pad_l, pad_b, pad_t = 56, 34, 12
    ymax = max(ys) * 1.08 or 1
    n = len(ys)
    sx = lambda i: pad_l + i * (w - pad_l - 10) / max(n - 1, 1)
    sy = lambda v: pad_t + (h - pad_t - pad_b) * (1 - v / ymax)
    pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(ys))
    area = f"{sx(0):.1f},{sy(0):.1f} {pts} {sx(n-1):.1f},{sy(0):.1f}"
    gridlines = "".join(
        f"<line x1='{pad_l}' x2='{w-10}' y1='{sy(ymax*k/4):.1f}' y2='{sy(ymax*k/4):.1f}' class='grid'/>"
        f"<text x='{pad_l-6}' y='{sy(ymax*k/4)+4:.1f}' class='ax' text-anchor='end'>{yfmt(ymax*k/4)}</text>"
        for k in range(5)
    )
    step = max(1, n // 8)
    xlabels = "".join(
        f"<text x='{sx(i):.1f}' y='{h-10}' class='ax' text-anchor='middle'>{html.escape(xs[i])}</text>"
        for i in range(0, n, step)
    )
    return (
        f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='{html.escape(label)}'>"
        f"{gridlines}<polygon points='{area}' fill='{RED}' opacity='0.08'/>"
        f"<polyline points='{pts}' fill='none' stroke='{RED}' stroke-width='2.5'/>"
        + "".join(f"<circle cx='{sx(i):.1f}' cy='{sy(v):.1f}' r='3' fill='{RED}'/>" for i, v in enumerate(ys))
        + f"{xlabels}</svg>"
    )


def bar_chart(labels: Sequence[str], values: Sequence[float], w: int = 720, h: int = 220, label: str = "", unit: str = "") -> str:
    if not values:
        return ""
    pad_l, pad_b, pad_t = 56, 40, 12
    vmax = max(values) * 1.1 or 1
    n = len(values)
    bw = (w - pad_l - 10) / n
    sy = lambda v: pad_t + (h - pad_t - pad_b) * (1 - v / vmax)
    bars = "".join(
        f"<rect x='{pad_l + i*bw + bw*0.15:.1f}' y='{sy(v):.1f}' width='{bw*0.7:.1f}' height='{h-pad_b-sy(v):.1f}' fill='{RED}' rx='3'/>"
        f"<text x='{pad_l + i*bw + bw/2:.1f}' y='{sy(v)-5:.1f}' class='ax' text-anchor='middle'>{v:,.1f}{unit}</text>"
        f"<text x='{pad_l + i*bw + bw/2:.1f}' y='{h-14}' class='ax' text-anchor='middle'>{html.escape(str(labels[i]))}</text>"
        for i, v in enumerate(values)
    )
    return f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='{html.escape(label)}'>{bars}</svg>"


def heatmap(rows: Sequence[dict[str, Any]]) -> str:
    """Cohort retention heatmap (cohort_month x month_offset)."""
    cohorts = sorted({r["cohort_month"] for r in rows})
    offsets = sorted({r["month_offset"] for r in rows})
    lookup = {(r["cohort_month"], r["month_offset"]): r["retention_pct"] for r in rows}
    size = {r["cohort_month"]: r["cohort_size"] for r in rows}
    head = "<th>Cohort</th><th>Size</th>" + "".join(f"<th>M{o}</th>" for o in offsets)
    body = ""
    for c in cohorts:
        cells = ""
        for o in offsets:
            v = lookup.get((c, o))
            if v is None:
                cells += "<td class='na'></td>"
            else:
                alpha = min(1.0, v / 100)
                fg = "#fff" if alpha > 0.5 else "#111"
                cells += f"<td style='background:rgba(204,0,0,{alpha:.2f});color:{fg}'>{v:.0f}%</td>"
        body += f"<tr><td class='mono'>{c}</td><td>{size[c]}</td>{cells}</tr>"
    return f"<div class='tw'><table class='heat'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


CSS = """
:root{--ink:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#fafafa;--card:#fff;--red:#CC0000}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--red);color:#fff;padding:28px 24px}header h1{margin:0;font-size:26px}header p{margin:4px 0 0;opacity:.9}
main{max-width:1100px;margin:0 auto;padding:24px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:24px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi .v{font-size:24px;font-weight:700}.kpi .l{color:var(--muted);font-size:13px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:20px}
h2{margin:0 0 4px;font-size:18px}.sub{color:var(--muted);margin:0 0 12px;font-size:14px}
svg{width:100%;height:auto;display:block}.grid{stroke:var(--line)}.ax{font-size:11px;fill:var(--muted)}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:600}
.heat td{text-align:center;min-width:44px}.heat td.na{background:#f3f4f6}.mono{font-family:ui-monospace,monospace}
details summary{cursor:pointer;color:var(--red);font-weight:600;margin-top:10px}pre{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;overflow:auto;font-size:12px}
.muted{color:var(--muted);font-size:13px}footer{color:var(--muted);text-align:center;padding:24px;font-size:13px}
a{color:var(--red)}
"""


def build_html(results: dict[str, list[dict[str, Any]]], sql_text: dict[str, str], meta: dict[str, Any]) -> str:
    monthly = results["01_monthly_revenue"]
    abc = results["02_category_abc"]
    rfm = results["03_customer_rfm"]
    cohort = results["04_cohort_retention"]
    stores = results["05_store_performance"]
    channel = results["06_channel_mix"]
    repeat = results["07_repeat_purchase_rate"]
    dq = results["08_data_quality"]

    total_rev = sum(r["revenue"] for r in monthly)
    total_orders = sum(r["orders"] for r in monthly)
    margin_pct = sum(r["margin"] for r in monthly) / total_rev * 100 if total_rev else 0
    seg_counts: dict[str, int] = {}
    for r in rfm:
        seg_counts[r["segment"]] = seg_counts.get(r["segment"], 0) + 1
    abc_counts = {k: sum(1 for r in abc if r["abc_class"] == k) for k in "ABC"}
    abc_rev = {k: sum(r["revenue"] for r in abc if r["abc_class"] == k) for k in "ABC"}
    dq_ok = all(r["violations"] == 0 for r in dq)

    def sec(title: str, sub: str, body: str, key: str) -> str:
        return (
            f"<section><h2>{title}</h2><p class='sub'>{sub}</p>{body}"
            f"<details><summary>Show SQL</summary><pre>{html.escape(sql_text[key])}</pre></details></section>"
        )

    kpis = "".join(
        f"<div class='kpi'><div class='v'>{v}</div><div class='l'>{l}</div></div>"
        for v, l in [
            (f"${total_rev/1e6:,.2f}M", "Net revenue"),
            (f"{total_orders:,}", "Orders"),
            (f"{margin_pct:.1f}%", "Gross margin"),
            (f"{meta['customers']:,}", "Customers"),
            (f"{meta['line_items']:,}", "Line items"),
            ("PASS" if dq_ok else "FAIL", "Data-quality checks"),
        ]
    )

    seg_order = ["Champion", "Loyal", "Regular", "New", "At Risk", "Lost"]
    segs = [s for s in seg_order if s in seg_counts]

    parts = [
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>RetailPulse — SQL Analytics Dashboard</title><style>{CSS}</style></head><body>",
        "<header><h1>RetailPulse</h1><p>Python + SQL retail analytics · 8 analytical queries over a normalised 5-table schema · "
        f"generated {meta['generated_at']} in {meta['elapsed_ms']} ms</p></header><main>",
        f"<div class='kpis'>{kpis}</div>",
        sec("Monthly revenue & MoM growth", "Window functions: LAG for month-over-month change, trailing 3-month average.",
            line_chart([r["order_month"][2:] for r in monthly], [r["revenue"] for r in monthly], label="Monthly revenue") + table(monthly, 24),
            "01_monthly_revenue"),
        sec("ABC product classification", "Cumulative revenue share via running SUM() OVER — A ≈ top 70 %, B next 20 %, C tail 10 %.",
            bar_chart(["A ("+str(abc_counts['A'])+" SKUs)", "B ("+str(abc_counts['B'])+" SKUs)", "C ("+str(abc_counts['C'])+" SKUs)"],
                      [abc_rev[k]/1000 for k in "ABC"], label="Revenue by ABC class", unit="k") + table(abc, 10),
            "02_category_abc"),
        sec("Customer RFM segmentation", "NTILE(5) quintiles for recency, frequency, monetary → six actionable segments.",
            bar_chart(segs, [seg_counts[s] for s in segs], label="Customers per segment") + table(rfm, 10),
            "03_customer_rfm"),
        sec("Cohort retention", "Monthly acquisition cohorts; % of each cohort that purchased again N months later.",
            heatmap(cohort), "04_cohort_retention"),
        sec("Store performance", "DENSE_RANK within region and share of regional revenue.", table(stores, 12), "05_store_performance"),
        sec("Channel mix", "Conditional aggregation pivot of revenue by channel per month.",
            line_chart([r["order_month"][2:] for r in channel], [r["online_share_pct"] for r in channel], label="Online share of revenue (%)", yfmt=lambda v: f"{v:.0f}%") + table(channel, 24),
            "06_channel_mix"),
        sec("Repeat purchase rate by acquisition channel", "ROW_NUMBER to isolate the first order, then repeat-rate per channel.",
            bar_chart([r["first_channel"] for r in repeat], [r["repeat_rate_pct"] for r in repeat], label="Repeat rate", unit="%") + table(repeat),
            "07_repeat_purchase_rate"),
        sec("Data-quality checks", "Every check must return 0 violations; the CLI exits non-zero otherwise.", table(dq), "08_data_quality"),
        "</main><footer>Built by <a href='https://github.com/D-L-Narayana/retailpulse'>D L Narayana</a> · Python 3 stdlib + SQLite · no runtime dependencies</footer></body></html>",
    ]
    return "".join(parts)
