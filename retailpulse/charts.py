"""Inline SVG charts and HTML tables for the dashboard: stdlib only, None-safe and CSP-friendly.

Every chart returns ``""`` when it has nothing numeric to show and never raises on ``None`` (or non-numeric)
values: gaps in a series simply break the line.  All data-derived strings are escaped.  Colours come from CSS
classes (``.series-N``, ``.grid``, ``.ax``) rather than inline attributes so the theme's tokens and dark mode
apply, and no element carries a ``style`` attribute (the page is served under a strict Content-Security-Policy).
Each ``<svg>`` has ``role="img"``, an ``aria-label`` and a ``<title>`` child and is well-formed XML.
"""
from __future__ import annotations

import html
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

SERIES_CLASSES = 6
HEAT_LEVELS = 11
MAX_X_LABELS = 8
CHAR_W = 6.5  # approximate glyph width of the 11 px axis font, used to decide label thinning/rotation

Formatter = Callable[[float], str]


# --------------------------------------------------------------------------- formatting helpers
def esc(value: Any) -> str:
    """HTML/XML-escape any value (``None`` -> ``""``)."""
    return "" if value is None else html.escape(str(value), quote=True)


def _num(value: Any) -> float | None:
    """Finite number or ``None`` (bools, strings, NaN and infinities are not plottable)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _is_integral(v: float) -> bool:
    return abs(v - round(v)) < 1e-9 * max(1.0, abs(v))


def _trim(text: str) -> str:
    return text.rstrip("0").rstrip(".") if "." in text else text


def fmt_cell(value: Any) -> str:
    """Table cell text: thousands separators, two decimals for floats, an em dash for ``None``."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}" if math.isfinite(value) else "—"
    return esc(value)


def fmt_value(value: float, unit: str = "") -> str:
    """Compact value label: ``12.0%``, ``12.3k`` / ``1.2M`` (unit ``k``), ``1,234`` or ``2.35`` otherwise."""
    v = float(value)
    if v == 0:
        v = 0.0  # never print "-0"
    if unit == "%":
        return f"{v:.1f}%"
    if unit == "k":
        if abs(v) >= 1e6:
            return f"{v / 1e6:,.1f}M"
        return f"{v / 1e3:,.1f}k"
    text = f"{v:,.0f}" if _is_integral(v) else f"{v:,.2f}"
    return text + unit


def _axis_label(v: float, unit: str) -> str:
    """Axis tick text: integral ticks lose their decimals; large magnitudes abbreviate to k / M."""
    if v == 0:
        v = 0.0
    if unit == "%":
        return (f"{v:.0f}" if _is_integral(v) else f"{v:.1f}") + "%"
    if unit == "k" or abs(v) >= 1e4:
        if abs(v) >= 1e6:
            return _trim(f"{v / 1e6:.1f}") + "M"
        if v == 0:
            return "0"
        return _trim(f"{v / 1e3:.1f}") + "k"
    text = f"{v:,.0f}" if _is_integral(v) else _trim(f"{v:,.2f}")
    return text + unit


def heat_bucket(value: float) -> int:
    """Heatmap intensity bucket 0..10 for a percentage (0-9 -> 0, 10-19 -> 1, ..., 100+ -> 10)."""
    return min(HEAT_LEVELS - 1, max(0, int(float(value) // 10)))


# --------------------------------------------------------------------------- geometry helpers
def _ticks(lo: float, hi: float) -> list[float]:
    """Round axis ticks (1/2/2.5/5 steps) covering ``[lo, hi]``; always at least two ticks."""
    if not hi > lo:
        hi = lo + 1.0
    raw = (hi - lo) / 4
    magnitude = 10.0 ** math.floor(math.log10(raw))
    step = magnitude * next(n for n in (1.0, 2.0, 2.5, 5.0, 10.0) if raw / magnitude <= n)
    start = math.floor(lo / step + 1e-9) * step
    end = math.ceil(hi / step - 1e-9) * step
    count = max(1, int(round((end - start) / step)))
    return [0.0 if abs(start + i * step) < step * 1e-9 else start + i * step for i in range(count + 1)]


def _range(values: Sequence[float]) -> tuple[list[float], float, float]:
    ticks = _ticks(min(0.0, min(values)), max(0.0, max(values)))
    return ticks, ticks[0], ticks[-1]


def _open(w: float, h: float, label: str) -> str:
    return f'<svg viewBox="0 0 {w:g} {h:g}" role="img" aria-label="{esc(label)}"><title>{esc(label)}</title>'


def _grid(
    ticks: Sequence[float], sy: Callable[[float], float], x1: float, x2: float, unit: str, yfmt: Formatter | None
) -> str:
    parts = []
    for t in ticks:
        y = sy(t)
        label = yfmt(t) if yfmt is not None else _axis_label(t, unit)
        parts.append(f'<line class="grid" x1="{x1:g}" x2="{x2:g}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="ax" x="{x1 - 6:g}" y="{y + 4:.1f}" text-anchor="end">{esc(label)}</text>')
    return "".join(parts)


def _label_indices(labels: Sequence[Any], plot_w: float) -> list[int]:
    """Which x labels to draw: all of them when they fit, otherwise up to eight including both ends."""
    n = len(labels)
    if n == 0:
        return []
    longest = max((len(str(x)) for x in labels if x is not None), default=1)
    if n * (longest * CHAR_W + 8) <= plot_w:
        return list(range(n))
    k = min(n, MAX_X_LABELS)
    if k < 2:
        return [0]
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def _runs(values: Sequence[float | None]) -> list[list[tuple[int, float]]]:
    """Split a series into runs of consecutive observed points (gaps at ``None``)."""
    runs: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for i, v in enumerate(values):
        if v is None:
            if current:
                runs.append(current)
                current = []
        else:
            current.append((i, v))
    if current:
        runs.append(current)
    return runs


def _legend(names: Sequence[str], x0: float, x_max: float) -> tuple[str, int]:
    """Horizontal legend rows; returns the markup and the number of rows used."""
    items: list[str] = []
    x, row = x0, 0
    for k, name in enumerate(names):
        width = 12 + 6 + max(1, len(name)) * 7 + 18
        if x + width > x_max and x > x0:
            row += 1
            x = x0
        y = 4 + row * 18
        items.append(
            f'<g class="legend-item series-{k % SERIES_CLASSES}" transform="translate({x:g},{y:g})">'
            f'<rect class="swatch" width="12" height="12" rx="2"/><text x="18" y="10">{esc(name)}</text></g>'
        )
        x += width
    return "".join(items), row + 1


# --------------------------------------------------------------------------- tables
def table(
    rows: Sequence[Mapping[str, Any]],
    limit: int = 15,
    caption: str = "",
    *,
    row_class: Callable[[Mapping[str, Any]], str] | None = None,
    table_class: str = "",
) -> str:
    """HTML table with ``<caption>`` and ``scope="col"`` headers; ``limit <= 0`` shows every row."""
    if not rows:
        return '<p class="muted">No rows.</p>'
    shown = list(rows) if limit <= 0 else list(rows[:limit])
    cols: list[str] = []
    for r in shown:
        for c in r:
            if c not in cols:
                cols.append(c)
    head = "".join(f'<th scope="col">{esc(c)}</th>' for c in cols)
    body = []
    for r in shown:
        cls = row_class(r) if row_class is not None else ""
        open_tag = f'<tr class="{esc(cls)}">' if cls else "<tr>"
        body.append(open_tag + "".join(f"<td>{fmt_cell(r.get(c))}</td>" for c in cols) + "</tr>")
    more = f'<p class="muted">Showing {len(shown):,} of {len(rows):,} rows.</p>' if len(shown) < len(rows) else ""
    cap = f"<caption>{esc(caption)}</caption>" if caption else ""
    cls_attr = f' class="{esc(table_class)}"' if table_class else ""
    return (
        f'<div class="tw"><table{cls_attr}>{cap}<thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>{more}'
    )


# --------------------------------------------------------------------------- line charts
def _plot_lines(
    xs: Sequence[Any],
    plotted: Sequence[tuple[str, list[float | None]]],
    *,
    w: int,
    h: int,
    label: str,
    unit: str,
    yfmt: Formatter | None,
    legend: bool,
    area: bool,
) -> str:
    n = len(xs)
    observed = [v for _, vals in plotted for v in vals if v is not None]
    if n == 0 or not observed:
        return ""
    ticks, lo, hi = _range(observed)
    pad_l, pad_r, pad_b = 56, 28, 34
    legend_svg, legend_rows = ("", 0)
    if legend:
        legend_svg, legend_rows = _legend([name for name, _ in plotted], pad_l, w - pad_r)
    pad_t = 14 + legend_rows * 18
    height = h + legend_rows * 18
    plot_w, plot_h = w - pad_l - pad_r, height - pad_t - pad_b

    def sx(i: int) -> float:
        return pad_l + i * plot_w / max(n - 1, 1)

    def sy(v: float) -> float:
        return pad_t + plot_h * (1 - (v - lo) / (hi - lo))

    base_y = sy(min(hi, max(lo, 0.0)))
    parts = [_open(w, height, label), _grid(ticks, sy, pad_l, w - pad_r, unit, yfmt)]
    for k, (name, vals) in enumerate(plotted):
        cls = f"series-{k % SERIES_CLASSES}"
        for run in _runs(vals):
            pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in run)
            if len(run) >= 2:
                if area:
                    first_x, last_x = sx(run[0][0]), sx(run[-1][0])
                    corners = f"{first_x:.1f},{base_y:.1f} {pts} {last_x:.1f},{base_y:.1f}"
                    parts.append(f'<polygon class="area {cls}" points="{corners}"/>')
                parts.append(f'<polyline class="line {cls}" points="{pts}"/>')
        for i, v in enumerate(vals):
            if v is not None:
                who = f"{esc(name)}, " if legend else ""
                parts.append(
                    f'<circle class="dot {cls}" cx="{sx(i):.1f}" cy="{sy(v):.1f}" r="3">'
                    f"<title>{who}{esc(xs[i])}: {esc(fmt_value(v, unit))}</title></circle>"
                )
    label_y = height - 10
    parts.extend(
        f'<text class="ax" x="{sx(i):.1f}" y="{label_y:g}" text-anchor="middle">{esc(xs[i])}</text>'
        for i in _label_indices(xs, plot_w)
    )
    parts.append(legend_svg)
    parts.append("</svg>")
    return "".join(parts)


def _series_values(values: Sequence[Any], n: int) -> list[float | None]:
    return [_num(values[i]) if i < len(values) else None for i in range(n)]


def line_chart(
    xs: Sequence[Any],
    ys: Sequence[Any],
    *,
    w: int = 720,
    h: int = 220,
    label: str = "",
    unit: str = "",
    yfmt: Formatter | None = None,
) -> str:
    """Single-series line chart; ``None``/non-numeric points become gaps; ``""`` when nothing is plottable."""
    values = _series_values(ys, len(xs))
    return _plot_lines(
        xs, [("", values)], w=w, h=h, label=label or "Line chart", unit=unit, yfmt=yfmt, legend=False, area=True
    )


def multi_line_chart(
    xs: Sequence[Any],
    series: Mapping[str, Sequence[float | None]],
    *,
    w: int = 720,
    h: int = 240,
    label: str = "",
    unit: str = "",
    yfmt: Formatter | None = None,
) -> str:
    """Several series on one axis with a legend; series without any observed value are left out entirely."""
    n = len(xs)
    plotted: list[tuple[str, list[float | None]]] = []
    for name, values in series.items():
        nums = _series_values(values, n)
        if any(v is not None for v in nums):
            plotted.append((str(name), nums))
    if not plotted:
        return ""
    return _plot_lines(
        xs, plotted, w=w, h=h, label=label or "Line chart", unit=unit, yfmt=yfmt, legend=True, area=False
    )


# --------------------------------------------------------------------------- bar chart
def bar_chart(
    labels: Sequence[Any],
    values: Sequence[Any],
    *,
    w: int = 720,
    h: int = 220,
    label: str = "",
    unit: str = "",
    yfmt: Formatter | None = None,
) -> str:
    """Vertical bars with value labels, grid, zero baseline and per-bar tooltips; ``""`` when nothing is numeric."""
    n = len(labels)
    nums = _series_values(values, n)
    observed = [v for v in nums if v is not None]
    if n == 0 or not observed:
        return ""
    ticks, lo, hi = _range(observed)
    pad_l, pad_r, pad_t, pad_b = 56, 12, 22, 40
    plot_w = w - pad_l - pad_r
    bw = plot_w / n
    longest = max((len(str(x)) for x in labels if x is not None), default=1)
    rotate = longest * CHAR_W > bw * 0.95
    height = h
    if rotate:
        extra = min(120, int(longest * CHAR_W * 0.6))
        pad_b += extra
        height += extra
    plot_h = height - pad_t - pad_b

    def sy(v: float) -> float:
        return pad_t + plot_h * (1 - (v - lo) / (hi - lo))

    base_y = sy(min(hi, max(lo, 0.0)))
    parts = [_open(w, height, label or "Bar chart"), _grid(ticks, sy, pad_l, w - pad_r, unit, yfmt)]
    parts.append(f'<line class="ax base" x1="{pad_l:g}" x2="{w - pad_r:g}" y1="{base_y:.1f}" y2="{base_y:.1f}"/>')
    for i, (raw_label, v) in enumerate(zip(labels, nums, strict=True)):
        text = esc(raw_label)
        cx = pad_l + i * bw + bw / 2
        if v is not None:
            top, bottom = sy(max(v, 0.0)), sy(min(v, 0.0))
            shown = esc(fmt_value(v, unit))
            parts.append(
                f'<rect class="bar series-0" x="{pad_l + i * bw + bw * 0.15:.1f}" y="{top:.1f}" '
                f'width="{bw * 0.7:.1f}" height="{bottom - top:.1f}" rx="3"><title>{text}: {shown}</title></rect>'
            )
            ty = top - 5 if v >= 0 else bottom + 12
            parts.append(f'<text class="ax" x="{cx:.1f}" y="{ty:.1f}" text-anchor="middle">{shown}</text>')
        if rotate:
            ly = height - pad_b + 14
            parts.append(
                f'<text class="ax xl" x="{cx:.1f}" y="{ly:g}" text-anchor="end" '
                f'transform="rotate(-35 {cx:.1f} {ly:g})">{text}</text>'
            )
        else:
            parts.append(f'<text class="ax" x="{cx:.1f}" y="{height - 14:g}" text-anchor="middle">{text}</text>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- heatmap
def _sort_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, value)
    return (1, str(value))


def _column_label(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def heatmap(
    rows: Sequence[Mapping[str, Any]],
    *,
    row_key: str,
    col_key: str,
    value_key: str,
    size_key: str,
    caption: str = "",
    col_prefix: str = "M",
    unit: str = "%",
) -> str:
    """Matrix of percentage cells with intensity classes ``h0``..``h10``.

    A cell that has a row with a numeric value renders that value (a genuine ``0 %`` gets class ``h0``);
    a cell with no row — or a ``NULL`` value — is right-censored and renders as
    ``<td class="na" title="not yet observable">`` so the two cases are visually and semantically distinct.
    """
    cells: dict[tuple[Any, Any], float | None] = {}
    sizes: dict[Any, Any] = {}
    for r in rows:
        rk, ck = r.get(row_key), r.get(col_key)
        if rk is None or ck is None:
            continue
        cells[(rk, ck)] = _num(r.get(value_key))
        sizes.setdefault(rk, r.get(size_key))
    if not cells:
        return '<p class="muted">No rows.</p>'
    row_keys = sorted({rk for rk, _ in cells}, key=_sort_key)
    col_keys = sorted({ck for _, ck in cells}, key=_sort_key)
    head = f'<th scope="col">{esc(_column_label(row_key))}</th><th scope="col">{esc(_column_label(size_key))}</th>'
    head += "".join(f'<th scope="col">{esc(col_prefix)}{esc(c)}</th>' for c in col_keys)
    body = []
    for rk in row_keys:
        size = sizes.get(rk)
        n_text = esc(size) if size is not None else "—"
        tds = []
        for ck in col_keys:
            v = cells.get((rk, ck))
            if v is None:
                tds.append('<td class="na" title="not yet observable"></td>')
                continue
            if v == 0:
                v = 0.0
            tooltip = f"{esc(rk)} → {esc(col_prefix)}{esc(ck)}: {v:.0f} {esc(unit)}".rstrip() + f" (n={n_text})"
            tds.append(f'<td class="h{heat_bucket(v)}" title="{tooltip}">{v:.0f}{esc(unit)}</td>')
        body.append(f'<tr><th scope="row" class="mono">{esc(rk)}</th><td>{fmt_cell(size)}</td>{"".join(tds)}</tr>')
    cap = f"<caption>{esc(caption)}</caption>" if caption else ""
    return (
        f'<div class="tw"><table class="heat">{cap}<thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )
