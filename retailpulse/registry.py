"""SQL header metadata registry.

Each ``retailpulse/sql/*.sql`` file may start with a block of ``-- @key: value`` lines::

    -- @title: Monthly revenue & MoM growth
    -- @question: How is net revenue trending month over month, and what margin does it carry?
    -- @technique: LAG, trailing-window AVG
    -- @chart: line x=order_month y=revenue
    -- @limit: 24
    -- @portability: substr(order_date,1,7) → to_char(order_date,'YYYY-MM') (Postgres)

Parsing stops at the first non-comment line, so metadata can only live at the top of the file.  Missing keys
fall back to a title humanised from the file stem (``01_monthly_revenue`` → ``Monthly revenue``), an empty
question/technique, chart ``table`` and limit 15.  The specs drive the dashboard sections (``report.py``) and
the README query table (``readme_table`` / ``sync_readme``), so adding a metric is adding one ``.sql`` file.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import db

DEFAULT_CHART = "table"
DEFAULT_LIMIT = 15
START_MARKER = "<!-- queries:start -->"
END_MARKER = "<!-- queries:end -->"
KNOWN_KEYS = frozenset({"title", "question", "technique", "chart", "limit", "portability"})

_HEADER_LINE = re.compile(r"^--\s*@([A-Za-z][\w-]*)\s*:\s*(.*?)\s*$")
_LEADING_NUMBER = re.compile(r"^\d+_?")
_ACRONYMS = frozenset({"abc", "rfm", "ltv", "sku", "mom", "yoy", "dq", "sql", "kpi", "csv"})
# chart kind -> (required parameters, optional parameters)
_CHART_PARAMS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "line": (frozenset({"x", "y"}), frozenset({"series", "unit"})),
    "bar": (frozenset({"x", "y"}), frozenset({"unit"})),
    "heatmap": (frozenset({"row", "col", "value", "size"}), frozenset()),
}


@dataclass
class QuerySpec:
    """Metadata for one query file (``name`` is the file stem, e.g. ``01_monthly_revenue``)."""

    name: str
    title: str = ""
    question: str = ""
    technique: str = ""
    chart: str = DEFAULT_CHART
    limit: int = DEFAULT_LIMIT
    portability: list[str] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)


def humanise(name: str) -> str:
    """``01_monthly_revenue`` → ``Monthly revenue``; ``10_customer_ltv`` → ``Customer LTV``."""
    stem = _LEADING_NUMBER.sub("", name.strip())
    words = [w for w in stem.split("_") if w]
    out: list[str] = []
    for i, word in enumerate(words):
        lower = word.lower()
        if lower in _ACRONYMS:
            out.append(lower.upper())
        elif i == 0:
            out.append(lower.capitalize())
        else:
            out.append(lower)
    return " ".join(out)


def header_fields(text: str) -> dict[str, list[str]]:
    """Every ``-- @key: value`` line of the leading comment block, keyed by lower-cased key, values in order.

    Blank lines and plain ``--`` comments inside the block are skipped; the first non-comment line ends it.
    """
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("--"):
            break
        match = _HEADER_LINE.match(stripped)
        if match:
            fields.setdefault(match.group(1).lower(), []).append(match.group(2))
    return fields


def parse_header(text: str, name: str = "") -> QuerySpec:
    """Parse a query file's header block into a :class:`QuerySpec`, applying the documented fallbacks."""
    fields = header_fields(text)

    def first(key: str) -> str:
        values = fields.get(key)
        return values[0] if values else ""

    limit = DEFAULT_LIMIT
    raw_limit = first("limit")
    if raw_limit:
        try:
            parsed = int(raw_limit)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            limit = parsed
    return QuerySpec(
        name=name,
        title=first("title") or humanise(name),
        question=first("question"),
        technique=first("technique"),
        chart=first("chart") or DEFAULT_CHART,
        limit=limit,
        portability=[note for note in fields.get("portability", []) if note],
        extra={key: "\n".join(values) for key, values in fields.items() if key not in KNOWN_KEYS},
    )


def parse_chart(spec: str | None) -> dict[str, str]:
    """Parse the ``@chart`` grammar.

    ``table`` · ``none`` · ``line x=<col> y=<col> [series=<col>] [unit=%]`` · ``bar x=<col> y=<col> [unit=%|k]``
    · ``heatmap row=<col> col=<col> value=<col> size=<col>``.  Unknown kinds, missing required parameters and
    unknown parameters degrade gracefully (to ``{"kind": "table"}`` / dropped parameters).
    """
    tokens = (spec or "").split()
    if not tokens:
        return {"kind": DEFAULT_CHART}
    kind = tokens[0].lower()
    if kind in ("table", "none"):
        return {"kind": kind}
    if kind not in _CHART_PARAMS:
        return {"kind": DEFAULT_CHART}
    required, optional = _CHART_PARAMS[kind]
    params: dict[str, str] = {}
    for token in tokens[1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key, value = key.strip().lower(), value.strip()
        if value and (key in required or key in optional) and key not in params:
            params[key] = value
    if not required <= set(params):
        return {"kind": DEFAULT_CHART}
    return {"kind": kind, **params}


def load_specs(sql_dir: Path | str | None = None) -> dict[str, QuerySpec]:
    """Specs for every ``*.sql`` in ``db.SQL_DIR`` (or ``sql_dir``), in ``db.list_queries()`` order."""
    directory = db.SQL_DIR if sql_dir is None else Path(sql_dir)
    names = db.list_queries() if sql_dir is None else sorted(p.stem for p in directory.glob("*.sql"))
    specs: dict[str, QuerySpec] = {}
    for name in names:
        try:
            text = (directory / f"{name}.sql").read_text(encoding="utf-8")
        except OSError:
            text = ""
        specs[name] = parse_header(text, name)
    return specs


def _as_sorted_list(specs: Mapping[str, QuerySpec] | Iterable[QuerySpec]) -> list[QuerySpec]:
    values = list(specs.values()) if isinstance(specs, Mapping) else list(specs)
    return sorted(values, key=lambda spec: spec.name)


def _cell(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat.replace("|", "\\|") if flat else "—"


def readme_table(specs: Mapping[str, QuerySpec] | Iterable[QuerySpec]) -> str:
    """Markdown ``| File | Technique | Question answered |`` table, one row per spec in name order."""
    lines = ["| File | Technique | Question answered |", "|---|---|---|"]
    for spec in _as_sorted_list(specs):
        lines.append(f"| `{spec.name}.sql` | {_cell(spec.technique)} | {_cell(spec.question)} |")
    return "\n".join(lines)


def sync_readme(readme_text: str, specs: Mapping[str, QuerySpec] | Iterable[QuerySpec]) -> str:
    """Replace the block between the ``queries:start`` / ``queries:end`` markers; unchanged if they are absent."""
    start = readme_text.find(START_MARKER)
    if start < 0:
        return readme_text
    body_start = start + len(START_MARKER)
    end = readme_text.find(END_MARKER, body_start)
    if end < 0:
        return readme_text
    return readme_text[:body_start] + "\n" + readme_table(specs) + "\n" + readme_text[end:]


def readme_in_sync(readme_text: str, specs: Mapping[str, QuerySpec] | Iterable[QuerySpec]) -> bool:
    """``True`` when :func:`sync_readme` would leave the text unchanged (drift gate for ``docs --check``)."""
    return sync_readme(readme_text, specs) == readme_text
