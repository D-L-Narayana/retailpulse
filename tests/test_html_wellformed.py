"""Structural well-formedness and CSP-compatibility checks on the rendered dashboard.

The dashboard is one static HTML file with zero JavaScript, served under the ``/retailpulse/`` base path
on GitHub Pages, so every link must be relative and nothing may rely on inline styles or scripts.  The
checks use only ``html.parser`` and ``xml.etree`` from the standard library, like the project itself.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import pytest

from retailpulse import cli

VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
RAW_TEXT_ELEMENTS = frozenset({"pre", "code", "script", "style"})
URL_ATTRIBUTES = frozenset({"href", "src", "action", "poster", "data", "formaction", "cite"})
ALLOWED_ABSOLUTE_URLS = ("https://github.com/D-L-Narayana/retailpulse",)
LITERAL_JUNK = re.compile(r"\b(?:None|nan|NaN|undefined)\b")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
SVG_FRAGMENT = re.compile(r"<svg\b.*?</svg>", re.DOTALL)
STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL | re.IGNORECASE)

BUILDS: dict[str, list[str]] = {
    "empty": ["--orders", "0", "--seed", "1"],
    "single": ["--orders", "1", "--seed", "2"],
    "small": ["--orders", "1500", "--seed", "3"],
}
PAGES = list(BUILDS)
NON_EMPTY_PAGES = ["single", "small"]

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class Checker(HTMLParser):
    """Tag-stack HTML checker: records structural facts and violations while parsing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.errors: list[str] = []
        self.stack: list[str] = []
        self.counts: Counter[str] = Counter()
        self.ids: list[str] = []
        self.fragment_links: list[str] = []
        self.nav_links: list[str] = []
        self.urls: list[tuple[str, str, str]] = []
        self.inline_styles: list[str] = []
        self.event_handlers: list[str] = []
        self.sections: list[dict[str, Any]] = []
        self.metas: list[dict[str, str | None]] = []
        self.html_lang: str | None = None
        self.title = ""
        self.text: list[str] = []  # visible text nodes outside <pre>/<code>/<style>/<script>
        self.junk_text: list[str] = []
        self._open_sections: list[dict[str, Any]] = []
        self._nav_depth = 0
        self._raw_depth = 0
        self._in_title = False

    def _inspect(self, tag: str, attrs: list[tuple[str, str | None]]) -> dict[str, str | None]:
        self.counts[tag] += 1
        if tag == "script":
            self.errors.append(f"<script> element at line {self.getpos()[0]}")
        seen: set[str] = set()
        for name, value in attrs:
            if name in seen:
                self.errors.append(f"<{tag}> repeats attribute {name!r}")
            seen.add(name)
            if name == "id":
                self.ids.append(value or "")
            elif name == "style":
                self.inline_styles.append(f"<{tag} style={value!r}>")
            elif name.startswith("on"):
                self.event_handlers.append(f"<{tag} {name}=...>")
            elif name in URL_ATTRIBUTES and value is not None:
                self.urls.append((tag, name, value))
                if value.startswith("#"):
                    self.fragment_links.append(value)
                    if self._nav_depth and tag == "a":
                        self.nav_links.append(value)
        return dict(attrs)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = self._inspect(tag, attrs)
        if tag == "html":
            self.html_lang = a.get("lang")
        elif tag == "meta":
            self.metas.append(a)
        elif tag == "section":
            section = {"id": a.get("id"), "h2": False}
            self.sections.append(section)
            self._open_sections.append(section)
        elif tag == "h2":
            for section in self._open_sections:
                section["h2"] = True
        elif tag == "nav":
            self._nav_depth += 1
        elif tag == "title":
            self._in_title = True
        if tag in RAW_TEXT_ELEMENTS:
            self._raw_depth += 1
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect(tag, attrs)  # self-closing (<line .../>, <meta .../>): nothing to balance

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            self.errors.append(f"end tag for void element </{tag}>")
            return
        if not self.stack:
            self.errors.append(f"stray </{tag}> with no open element (line {self.getpos()[0]})")
            return
        top = self.stack.pop()
        if top != tag:
            self.errors.append(f"expected </{top}> but found </{tag}> (line {self.getpos()[0]})")
        if tag == "section" and self._open_sections:
            self._open_sections.pop()
        elif tag == "nav":
            self._nav_depth = max(0, self._nav_depth - 1)
        elif tag == "title":
            self._in_title = False
        if tag in RAW_TEXT_ELEMENTS:
            self._raw_depth = max(0, self._raw_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._raw_depth == 0:
            if data.strip():
                self.text.append(data.strip())
            if LITERAL_JUNK.search(data):
                self.junk_text.append(data.strip()[:80])

    def finish(self) -> Checker:
        self.close()
        if self.stack:
            self.errors.append(f"unclosed elements at end of document: {self.stack}")
        return self


@dataclass
class Page:
    name: str
    rc: int
    html: str
    checker: Checker
    data: dict[str, Any]


@pytest.fixture(scope="module")
def pages(tmp_path_factory) -> dict[str, Page]:
    """One small CLI build per scenario.  The exit code is recorded, not asserted, so a data-quality gate
    verdict is tested on its own (see ``test_build_exit_code_follows_the_severity_contract``) while the
    structural checks run on the artefacts, which the build writes before it evaluates the gate."""
    built: dict[str, Page] = {}
    for name, flags in BUILDS.items():
        target = tmp_path_factory.mktemp(f"build-{name}")
        rc = cli.main(["build", "--out", str(target), *flags])
        assert (target / "index.html").is_file(), (name, rc)
        assert (target / "data.json").is_file(), (name, rc)
        html = (target / "index.html").read_text(encoding="utf-8")
        checker = Checker()
        checker.feed(html)
        checker.finish()
        data = json.loads((target / "data.json").read_text(encoding="utf-8"))
        built[name] = Page(name, rc, html, checker, data)
    return built


def _error_rows(dq_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in dq_rows if r.get("severity", "error") == "error"]


@pytest.mark.parametrize("name", PAGES)
def test_build_exit_code_follows_the_severity_contract(pages, name):
    page = pages[name]
    dq = page.data["results"]["08_data_quality"]
    expected = 1 if any(r["violations"] for r in _error_rows(dq)) else 0
    assert page.rc == expected


@pytest.mark.parametrize("name", PAGES)
def test_data_quality_kpi_follows_the_severity_contract(pages, name):
    """§3.5: the dashboard KPI says PASS unless an error-severity check fired; warn rows only add a count."""
    page = pages[name]
    dq = page.data["results"]["08_data_quality"]
    errors_fired = any(r["violations"] for r in _error_rows(dq))
    words = {word for chunk in page.checker.text for word in chunk.split()}
    assert ("FAIL" in words) == errors_fired, sorted(w for w in words if w in {"PASS", "FAIL"})
    assert ("PASS" in words) == (not errors_fired)


# --------------------------------------------------------------------------- structure


@pytest.mark.parametrize("name", PAGES)
def test_elements_are_balanced(pages, name):
    c = pages[name].checker
    assert c.errors == []
    assert c.counts["html"] == 1 and c.counts["head"] == 1 and c.counts["body"] == 1
    assert pages[name].html.lstrip()[:15].lower().startswith("<!doctype html")


@pytest.mark.parametrize("name", PAGES)
def test_exactly_one_main(pages, name):
    assert pages[name].checker.counts["main"] == 1


@pytest.mark.parametrize("name", PAGES)
def test_ids_are_unique_and_fragment_links_resolve(pages, name):
    c = pages[name].checker
    assert "" not in c.ids
    duplicates = sorted(i for i, n in Counter(c.ids).items() if n > 1)
    assert duplicates == []
    unresolved = sorted({link[1:] for link in c.fragment_links} - set(c.ids) - {""})
    assert unresolved == []


@pytest.mark.parametrize("name", PAGES)
def test_every_section_has_an_h2(pages, name):
    c = pages[name].checker
    assert c.sections, "the dashboard renders no <section>"
    assert all(section["h2"] for section in c.sections)
    assert c.counts["details"] >= 1 and c.counts["pre"] >= 1  # the SQL is shown on the page


@pytest.mark.parametrize("name", PAGES)
def test_no_literal_none_or_nan_text(pages, name):
    assert pages[name].checker.junk_text == []


@pytest.mark.parametrize("name", PAGES)
def test_svg_fragments_are_well_formed_xml(pages, name):
    fragments = SVG_FRAGMENT.findall(pages[name].html)
    if name == "small":
        assert fragments
    for fragment in fragments:
        root = ET.fromstring(fragment)
        assert _local(root.tag) == "svg"
        assert root.get("role") == "img"
        titles = [(el.text or "").strip() for el in root if _local(el.tag) == "title"]
        accessible_name = root.get("aria-label") or root.get("aria-labelledby") or any(titles)
        assert accessible_name, fragment[:120]


@pytest.mark.parametrize("name", NON_EMPTY_PAGES)
def test_every_svg_has_a_title(pages, name):
    fragments = SVG_FRAGMENT.findall(pages[name].html)
    if name == "small":
        assert fragments
    for fragment in fragments:
        root = ET.fromstring(fragment)
        titles = [el for el in root if _local(el.tag) == "title"]
        assert titles, fragment[:120]
        assert (titles[0].text or "").strip()


@pytest.mark.parametrize("name", PAGES)
def test_one_section_per_query_result(pages, name):
    page = pages[name]
    n_queries = len(page.data["results"])
    assert len(page.checker.sections) >= n_queries
    assert page.checker.counts["details"] >= n_queries  # "Show SQL" per query
    for query in page.data["results"]:
        assert query in page.html  # each query's SQL file name / id appears on the page


# --------------------------------------------------------------------------- CSP-compatibility statics


@pytest.mark.parametrize("name", PAGES)
def test_no_inline_style_attributes(pages, name):
    assert pages[name].checker.inline_styles == []


@pytest.mark.parametrize("name", PAGES)
def test_no_script_elements_or_event_handlers(pages, name):
    c = pages[name].checker
    assert c.counts["script"] == 0
    assert c.event_handlers == []
    assert [u for u in c.urls if u[2].strip().lower().startswith("javascript:")] == []


@pytest.mark.parametrize("name", PAGES)
def test_urls_are_relative_or_data_uris(pages, name):
    bad: list[tuple[str, str, str]] = []
    for tag, attr, value in pages[name].checker.urls:
        v = value.strip()
        if v.startswith("#") or v.lower().startswith("data:"):
            continue
        if v.startswith("/"):  # absolute paths break the /retailpulse/ base path on Pages
            bad.append((tag, attr, value))
        elif SCHEME.match(v) and not v.startswith(ALLOWED_ABSOLUTE_URLS):
            bad.append((tag, attr, value))
    assert bad == []


@pytest.mark.parametrize("name", PAGES)
def test_csp_meta_present(pages, name):
    metas = pages[name].checker.metas
    csp = [m for m in metas if (m.get("http-equiv") or "").lower() == "content-security-policy"]
    assert len(csp) == 1
    content = csp[0].get("content") or ""
    assert "default-src" in content
    assert "unsafe-inline" not in content and "unsafe-eval" not in content
    assert "frame-ancestors" not in content  # ignored inside <meta>; belongs in the response header only


@pytest.mark.parametrize("name", PAGES)
def test_csp_meta_hashes_the_single_inline_stylesheet(pages, name):
    page = pages[name]
    csp = [m for m in page.checker.metas if (m.get("http-equiv") or "").lower() == "content-security-policy"]
    assert len(csp) == 1
    policy = csp[0].get("content") or ""
    styles = STYLE_BLOCK.findall(page.html)
    assert len(styles) == 1, "exactly one <style> element so a single CSP hash covers it"
    digest = base64.b64encode(hashlib.sha256(styles[0].encode("utf-8")).digest()).decode("ascii")
    assert f"'sha256-{digest}'" in policy


@pytest.mark.parametrize("name", PAGES)
def test_head_basics(pages, name):
    c = pages[name].checker
    assert c.html_lang == "en"
    assert c.title.strip()
    assert any("charset" in m for m in c.metas)
    viewport = [m for m in c.metas if (m.get("name") or "").lower() == "viewport"]
    assert viewport and "width=device-width" in (viewport[0].get("content") or "")


@pytest.mark.parametrize("name", PAGES)
def test_meta_description_present(pages, name):
    metas = pages[name].checker.metas
    description = [m for m in metas if (m.get("name") or "").lower() == "description"]
    assert len(description) == 1 and (description[0].get("content") or "").strip()


@pytest.mark.parametrize("name", PAGES)
def test_nav_links_to_every_section(pages, name):
    c = pages[name].checker
    assert c.counts["nav"] >= 1
    section_ids = [section["id"] for section in c.sections]
    assert all(section_ids), "every <section> needs an id"
    assert {f"#{i}" for i in section_ids} <= set(c.nav_links)
