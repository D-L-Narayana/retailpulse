"""Write query results as CSV or JSON files.

``build`` uses :func:`write_all` to emit ``<out>/csv/<query>.csv`` next to the dashboard (the
renderer turns the returned relative paths into "Download CSV" links) and ``retailpulse export``
exposes the same writer for both formats. Only the standard library is used.

CSV cells are inert by default
------------------------------
Spreadsheet applications interpret a CSV cell that starts with ``=``, ``+``, ``-`` or ``@`` as a
formula — also when that character is hidden behind leading whitespace, control or zero-width
characters — so a text value such as a store name ``=1+1`` taken from a database could execute
when the exported file is opened elsewhere. The CSV writers therefore prefix every *string* data
cell that :func:`is_formula_like` flags with a single ASCII apostrophe (``'=1+1``), the conventional
neutraliser. Numbers (``-12.5`` stays ``-12.5``), booleans, ``None`` (empty field), ordinary text and
the header row (column names from the project's own SQL files) are written unchanged. Pass
``raw=True`` (CLI: ``--raw-csv``) for lossless machine output; such a file must not be opened in a
spreadsheet without the consumer applying its own protection. JSON output is never altered. The
behaviour is verified by parsing the output with the ``csv`` module and through the CLI; no
spreadsheet application was exercised.
"""
from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

FORMATS = ("csv", "json")
FORMULA_TRIGGERS = frozenset("=+-@")
# Characters a spreadsheet skips before deciding whether a cell is a formula: ASCII whitespace and
# every other C0 control, DEL, no-break space, zero-width space/joiners, word joiner and the BOM.
INVISIBLE_LEADING = "".join(chr(c) for c in range(0x20)) + "\x7f \xa0​‌‍⁠﻿"


def is_formula_like(text: str) -> bool:
    """True when a spreadsheet could read ``text`` as a formula.

    Either the cell starts with a raw C0 control character or DEL (tab/CR-prefixed payloads), or its
    first character after stripping leading invisible characters is one of ``= + - @``.
    """
    if not text:
        return False
    if text[0] < " " or text[0] == "\x7f":
        return True
    stripped = text.lstrip(INVISIBLE_LEADING)
    return bool(stripped) and stripped[0] in FORMULA_TRIGGERS


def inert_cell(value: Any) -> Any:
    """Return ``value`` with a leading apostrophe when it is a formula-like string; anything else as-is."""
    if isinstance(value, str) and is_formula_like(value):
        return "'" + value
    return value


def rows_to_csv(rows: Sequence[Mapping[str, Any]], *, raw: bool = False) -> str:
    """Render ``rows`` as CSV text: header from the first row's keys, ``\\n`` line endings.

    Formula-like string cells are made inert (see the module docstring) unless ``raw=True``.
    ``None`` becomes an empty field (the ``csv`` module's convention); an empty sequence yields ``""``
    because there is no row to take a header from.
    """
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    if raw:
        writer.writerows(rows)
    else:
        writer.writerows({key: inert_cell(value) for key, value in row.items()} for row in rows)
    return buffer.getvalue()


def write_csv(path: str | Path, rows: Sequence[Mapping[str, Any]], *, raw: bool = False) -> None:
    """Write ``rows`` to ``path`` as UTF-8 CSV (inert cells unless ``raw``), creating parent directories.

    Empty rows produce an empty file.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="" hands line endings to the csv module, so files are "\n"-terminated on every platform.
    with target.open("w", encoding="utf-8", newline="") as fh:
        fh.write(rows_to_csv(rows, raw=raw))


def write_json(path: str | Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Write ``rows`` to ``path`` as a strict JSON array (no NaN/Infinity), creating parent directories.

    JSON is machine data: values are never rewritten.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps([dict(r) for r in rows], indent=1, allow_nan=False) + "\n", encoding="utf-8")


def write_all(
    out_dir: str | Path,
    results: Mapping[str, Sequence[Mapping[str, Any]]],
    fmt: str = "csv",
    *,
    raw: bool = False,
) -> dict[str, str]:
    """Write every result set under ``<out_dir>/<fmt>/`` and return ``{query_name: relative_posix_path}``.

    The relative paths (``csv/01_monthly_revenue.csv``) are what ``meta["exports"]`` carries into the
    dashboard, so they always use forward slashes regardless of platform. ``raw`` only affects CSV.
    """
    if fmt not in FORMATS:
        raise ValueError(f"unsupported export format {fmt!r}; expected one of: {', '.join(FORMATS)}")
    root = Path(out_dir)
    written: dict[str, str] = {}
    for name, rows in results.items():
        relpath = f"{fmt}/{name}.{fmt}"
        if fmt == "csv":
            write_csv(root / relpath, rows, raw=raw)
        else:
            write_json(root / relpath, rows)
        written[name] = relpath
    return written
