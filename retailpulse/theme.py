"""Design tokens, the dashboard stylesheet and its Content-Security-Policy.

``TOKENS`` is the single source of truth for every colour on the dashboard.  ``css()`` turns it into
``:root`` custom properties (light scheme), a ``prefers-color-scheme: dark`` override block, print rules,
the eleven heatmap intensity classes ``h0``..``h10`` and the chart series classes ``series-0``..``series-5``.
Text/background pairs keep a WCAG contrast of at least 4.5:1 in both schemes (``tests/test_report.py``).

The policy returned by :func:`csp_policy` pins the page's single ``<style>`` element by SHA-256, so
``css()`` must be deterministic: any change to its output changes the hash (and the policy with it).
"""
from __future__ import annotations

import base64
import hashlib
from urllib.parse import quote

HEAT_LEVELS = 11
SERIES_COUNT = 6
_SCHEMES = {"light": 0, "dark": 1}

# name -> (light value, dark value); every value is a lower-case #rrggbb colour.
_BASE: dict[str, tuple[str, str]] = {
    "ink": ("#111827", "#e5e7eb"),
    "muted": ("#5b6472", "#a3adc2"),
    "line": ("#e5e7eb", "#2a3346"),
    "bg": ("#fafafa", "#0b1020"),
    "card": ("#ffffff", "#151b2b"),
    "accent": ("#cc0000", "#f87171"),
    "accent-ink": ("#ffffff", "#0b1020"),
    "ok": ("#047857", "#34d399"),
    "warn": ("#b45309", "#fbbf24"),
    "code-bg": ("#0f172a", "#1e293b"),
    "code-ink": ("#e2e8f0", "#e2e8f0"),
    "s0": ("#cc0000", "#f87171"),
    "s1": ("#1d4ed8", "#60a5fa"),
    "s2": ("#047857", "#34d399"),
    "s3": ("#b45309", "#fbbf24"),
    "s4": ("#6d28d9", "#a78bfa"),
    "s5": ("#0e7490", "#22d3ee"),
}


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    """``#rgb`` or ``#rrggbb`` -> ``(r, g, b)`` integers."""
    digits = color.strip().lstrip("#")
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    if len(digits) != 6:
        raise ValueError(f"not a #rrggbb colour: {color!r}")
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _linear(channel: int) -> float:
    s = channel / 255
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    """WCAG 2.x relative luminance of a hex colour (0.0 black .. 1.0 white)."""
    r, g, b = (_linear(c) for c in hex_to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG contrast ratio between two hex colours (1.0 .. 21.0); symmetric."""
    hi, lo = sorted((relative_luminance(fg), relative_luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def blend(rgb: tuple[int, int, int], alpha: float, background: str) -> str:
    """Composite ``rgb`` at ``alpha`` over an opaque ``background`` colour -> ``#rrggbb``."""
    a = min(1.0, max(0.0, alpha))
    mixed = [round(c * a + b * (1 - a)) for c, b in zip(rgb, hex_to_rgb(background), strict=True)]
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def _heat_alpha(level: int) -> float:
    # Bucket 0 (0-9 %) keeps a faint tint so a genuine 0 % cell still reads as part of the scale.
    return 0.04 if level == 0 else level / (HEAT_LEVELS - 1)


def heat_palette(scheme: str = "light") -> list[tuple[str, str]]:
    """``(background, text)`` colours for the heatmap buckets ``h0``..``h10`` in the given scheme.

    Backgrounds are the accent colour composited over the card colour at increasing alpha; the text colour
    is whichever of black/white has the higher contrast, which guarantees at least 4.5:1 for every bucket.
    """
    if scheme not in _SCHEMES:
        raise ValueError(f"unknown colour scheme: {scheme!r}")
    idx = _SCHEMES[scheme]
    accent, card = hex_to_rgb(_BASE["accent"][idx]), _BASE["card"][idx]
    palette: list[tuple[str, str]] = []
    for level in range(HEAT_LEVELS):
        bg = blend(accent, _heat_alpha(level), card)
        fg = "#000000" if contrast_ratio("#000000", bg) >= contrast_ratio("#ffffff", bg) else "#ffffff"
        palette.append((bg, fg))
    return palette


def _heat_tokens() -> dict[str, tuple[str, str]]:
    light, dark = heat_palette("light"), heat_palette("dark")
    tokens: dict[str, tuple[str, str]] = {}
    for level in range(HEAT_LEVELS):
        tokens[f"h{level}-bg"] = (light[level][0], dark[level][0])
        tokens[f"h{level}-fg"] = (light[level][1], dark[level][1])
    return tokens


TOKENS: dict[str, tuple[str, str]] = {**_BASE, **_heat_tokens()}

_FONT = "system-ui,-apple-system,'Segoe UI',Roboto,Ubuntu,Cantarell,'Helvetica Neue',Arial,sans-serif"
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"


def css() -> str:
    """The complete stylesheet (deterministic; its SHA-256 is pinned by the CSP)."""
    light = "".join(f"--{name}:{values[0]};" for name, values in TOKENS.items())
    dark = "".join(f"--{name}:{values[1]};" for name, values in TOKENS.items() if values[1] != values[0])
    heat = "".join(
        f".heat td.h{level}{{background:var(--h{level}-bg);color:var(--h{level}-fg)}}" for level in range(HEAT_LEVELS)
    )
    series = "".join(f".series-{i}{{stroke:var(--s{i});fill:var(--s{i})}}" for i in range(SERIES_COUNT))
    rules = [
        f":root{{color-scheme:light dark;{light}}}",
        "*{box-sizing:border-box}",
        f"body{{margin:0;font:15px/1.5 {_FONT};color:var(--ink);background:var(--bg)}}",
        ".skip{position:absolute;left:-999px;top:8px;background:var(--card);color:var(--ink);"
        "border:1px solid var(--line);border-radius:6px;padding:8px 12px;z-index:10}.skip:focus{left:8px}",
        "header{background:var(--accent);color:var(--accent-ink);padding:28px 24px}header h1{margin:0;font-size:26px}"
        "header p{margin:4px 0 0}header .meta{font-size:13px}header a{color:inherit}",
        "main{max-width:1100px;margin:0 auto;padding:24px}",
        "nav ul{list-style:none;display:flex;flex-wrap:wrap;gap:6px 14px;margin:0 0 20px;padding:0;font-size:14px}",
        ".kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:24px}",
        ".kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}"
        ".kpi .v{font-size:24px;font-weight:700}.kpi .l{color:var(--muted);font-size:13px}"
        ".kpi.ok .v{color:var(--ok)}.kpi.fail .v{color:var(--accent)}",
        "section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;"
        "margin-bottom:20px}",
        "h2{margin:0 0 4px;font-size:18px}.sub{margin:0 0 6px;font-size:14px}.tech{margin:0 0 10px}"
        ".dl{margin:0 0 10px;font-size:13px}",
        "svg{width:100%;height:auto;display:block;margin:6px 0 10px}.grid{stroke:var(--line)}"
        ".ax{font-size:11px;fill:var(--muted)}.xl{font-size:10px}.base{stroke:var(--muted)}",
        series,
        ".line{fill:none;stroke-width:2.5;stroke-linejoin:round}.area{fill-opacity:.1;stroke:none}"
        ".dot{stroke:var(--card);stroke-width:1}.bar{stroke:none}.swatch{stroke:none}"
        ".legend-item text{font-size:12px;fill:var(--ink)}",
        ".tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}"
        "caption{text-align:left;color:var(--muted);font-size:12px;padding:0 0 6px}"
        "th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}"
        "th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:600}",
        ".dq td:last-child{white-space:normal;text-align:left;min-width:280px}",
        ".heat td,.heat th[scope=row]{text-align:center;min-width:44px}.heat th[scope=row]{text-align:left}" + heat,
        ".heat td.na{background:repeating-linear-gradient(135deg,var(--card) 0 4px,var(--line) 4px 6px);"
        "color:var(--muted)}",
        f".mono{{font-family:{_MONO}}}",
        "tr.hit td{font-weight:600}tr.sev-error.hit td{color:var(--accent)}tr.sev-warn.hit td{color:var(--warn)}",
        "details summary{cursor:pointer;color:var(--accent);font-weight:600;margin-top:10px}"
        "pre{background:var(--code-bg);color:var(--code-ink);padding:12px;border-radius:8px;overflow:auto;"
        "font-size:12px}",
        ".port{margin:8px 0 0;padding-left:20px;font-size:13px;color:var(--muted)}",
        ".muted{color:var(--muted);font-size:13px}footer{color:var(--muted);text-align:center;padding:24px;"
        "font-size:13px}a{color:var(--accent)}",
        "@media (max-width:640px){main{padding:12px}header{padding:18px 12px}section{padding:14px 12px}}",
        f"@media (prefers-color-scheme: dark){{:root{{{dark}}}"
        "header{background:var(--card);color:var(--ink);border-bottom:1px solid var(--line)}"
        "header h1{color:var(--accent)}}",
        "@media print{:root{--bg:#ffffff;--card:#ffffff;--ink:#000000;--muted:#333333;--line:#999999;"
        "--accent:#000000;--accent-ink:#ffffff}header{padding:12px 0}main{max-width:none;padding:0}"
        "section{break-inside:avoid}.skip,nav,.dl,summary,footer{display:none}a{color:inherit;text-decoration:none}}",
    ]
    return "\n".join(rules) + "\n"


def favicon_data_uri() -> str:
    """Inline SVG favicon as a percent-encoded ``data:`` URI (allowed by ``img-src data:``)."""
    accent = _BASE["accent"][0]
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        f"<rect width='32' height='32' rx='7' fill='{accent}'/>"
        "<polyline points='6,22 12,14 17,19 26,8' fill='none' stroke='#ffffff' stroke-width='3.5'"
        " stroke-linecap='round' stroke-linejoin='round'/></svg>"
    )
    return "data:image/svg+xml," + quote(svg, safe="'=,.-_~()")


def style_hash(css_text: str) -> str:
    """CSP hash source for a ``<style>`` element's exact text: ``sha256-<base64>``."""
    digest = hashlib.sha256(css_text.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def csp_policy(delivery: str = "meta", css_text: str | None = None) -> str:
    """Content-Security-Policy for the dashboard.

    ``delivery="meta"`` is the variant embedded in ``<meta http-equiv>``; ``"header"`` adds
    ``frame-ancestors 'none'``, which browsers only honour in a response header (and warn about in a
    meta tag).  ``css_text`` defaults to :func:`css`.
    """
    if delivery not in ("meta", "header"):
        raise ValueError(f"delivery must be 'meta' or 'header', not {delivery!r}")
    text = css() if css_text is None else css_text
    policy = (
        f"default-src 'none'; style-src '{style_hash(text)}'; img-src 'self' data:; "
        "base-uri 'none'; form-action 'none'"
    )
    if delivery == "header":
        policy += "; frame-ancestors 'none'"
    return policy
