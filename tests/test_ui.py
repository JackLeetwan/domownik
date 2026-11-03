"""UI checks that need no browser: every page and window has a heading, every field a label, every button and
link something to read out, no duplicate ids. Plus a few rules from the phone audit that live in the CSS/JS."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from domownik.main import create_app
from tests.test_security import _routes, _seed, _url

STATIC = Path(__file__).parents[1] / "src" / "domownik" / "static"
HX = {"HX-Request": "true"}
SKIP = re.compile(r"\.ics$|/files/|/export|/eksport|/n/done|/static/|/healthz|/manifest|/sw\.js|/api/|/logout|/wyloguj")


class Page(HTMLParser):
    """Collects what the checks below look at: headings, fields and their labels, buttons/links and their text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.h1 = 0
        self.title = ""
        self.ids: dict[str, int] = {}
        self.label_for: set[str] = set()
        self.fields: list[tuple[dict, bool]] = []  # (attrs, inside a <label>)
        self.clickables: list[list] = []  # [tag, attrs, text]
        self.imgs: list[dict] = []
        self._stack: list[str] = []
        self._open: list[list] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if "id" in a:
            self.ids[a["id"]] = self.ids.get(a["id"], 0) + 1
        if tag == "h1":
            self.h1 += 1
        if tag == "title":
            self._in_title = True
        if tag == "label" and a.get("for"):
            self.label_for.add(a["for"])
        if tag in ("input", "select", "textarea"):
            self.fields.append((a, "label" in self._stack))
        if tag == "img":
            self.imgs.append(a)
            for c in self._open:  # an image with alt text names its button/link
                c[2] += a.get("alt", "")
        if tag in ("a", "button", "summary") and not (tag == "a" and "href" not in a):
            self._open.append([tag, a, a.get("aria-label", "") + a.get("title", "")])
        if tag not in ("input", "img", "br", "hr", "meta", "link"):
            self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if self._open and self._open[-1][0] == tag:
            self.clickables.append(self._open.pop())
        if tag in self._stack:
            while self._stack and self._stack.pop() != tag:
                pass

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        for c in self._open:
            c[2] += data


def _problems(html: str, *, page: bool) -> list[str]:
    p = Page()
    p.feed(html)
    out = []
    if page and p.h1 < 1:
        out.append("no <h1>")
    if page and not p.title.strip():
        out.append("no <title>")
    out += [f"id {k!r} ×{n}" for k, n in p.ids.items() if n > 1]
    for a, wrapped in p.fields:
        if a.get("type") in ("hidden", "submit", "button", "reset"):
            continue
        if not (
            wrapped or a.get("id") in p.label_for or a.get("aria-label") or a.get("aria-labelledby") or a.get("title")
        ):
            out.append(f"field without a label: name={a.get('name')!r}")
    out += [f"<{t}> without text: {a}" for t, a, text in p.clickables if not text.strip()]
    out += [f"<img> without alt: {a.get('src')}" for a in p.imgs if "alt" not in a]
    return out


@pytest.fixture
def seeded(auth_client, db, user):
    _seed(db, user)
    return auth_client


def _get_paths() -> list[str]:
    paths = []
    for method, path, _ in _routes(create_app()):
        if method == "GET" and not SKIP.search(path):
            paths.append(_url(path))
    return sorted(set(paths))


def test_every_page_and_window_is_readable(seeded):
    bad = {}
    seen = 0
    for path in _get_paths():
        for hx in (False, True):
            r = seeded.get(path, headers=HX if hx else {}, follow_redirects=False)
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                continue
            seen += 1
            full = "<html" in r.text[:500].lower()
            problems = _problems(r.text, page=full)
            if problems:
                bad[f"{'hx ' if hx else ''}{path}"] = problems
    assert seen > 60, "the walk reached too few pages to mean anything"
    assert not bad, "\n".join(f"{k}: {v}" for k, v in bad.items())


def test_checker_catches_problems():
    html = '<html><title></title><input name="q"><button></button><a href="/x"></a><img src="a.png"><i id="d"></i><i id="d"></i>'
    got = " | ".join(_problems(html, page=True))
    for needle in (
        "no <h1>",
        "no <title>",
        "name='q'",
        "<button> without text",
        "<a> without text",
        "without alt",
        "'d' ×2",
    ):
        assert needle in got
    ok = '<html><title>X</title><h1>X</h1><label>Q <input name="q"></label><button aria-label="Zamknij">✕</button>'
    assert _problems(ok, page=True) == []


# ---------------------------------------------------------------- rules from the phone audit (CSS/JS)


def _css() -> str:
    return (STATIC / "app.css").read_text()


def test_touch_targets_rule_covers_the_small_controls():
    m = re.search(r"@media \(pointer: coarse\) \{(.*?)\n\}", _css(), re.S)
    assert m, "the touch-screen block is gone"
    block = m.group(1)
    for sel in (
        ".seg .seg-btn",
        ".cal-views a",
        ".who [role=button]",
        ".shop-check",
        ".todo-check",
        ".chip-btn",
        ".mic",
    ):
        assert sel in block, f"{sel} lost its finger-sized hit area"
    assert "min-height: 2.5rem" in block


def test_time_fields_share_the_row_on_a_phone():
    # three columns with "Powiadom" on its own line left the time inputs ~90px wide: "18:00" showed as "18:("
    assert re.search(
        r"max-width: 575\.98px\) \{ \.task-form \.time-row \{ grid-template-columns: minmax\(0, 1fr\) minmax\(0, 1fr\); \}",
        _css(),
    )


def test_summary_line_height_and_select_one_line():
    css = _css()
    assert ".todo-body > summary" in css and "line-height: 1.4" in css
    assert re.search(r"selectedcontent \{[^}]*white-space: nowrap", css)


def test_slow_submit_shows_busy_state():
    js = (STATIC / "app.js").read_text()
    assert 'setAttribute("aria-busy", "true")' in js and "loadend" in js


def test_asset_versions_bumped():
    base = (STATIC.parent / "templates" / "base.html").read_text()
    assert int(re.search(r"app\.css\?v=(\d+)", base).group(1)) >= 54
    assert int(re.search(r"app\.js\?v=(\d+)", base).group(1)) >= 30


def test_one_style_for_titles_and_view_switches():
    css = _css()
    # every page title the same size (phone and desktop), and every view switch drawn by one rule
    assert re.search(r"main h1, main \.cal-title \{ font-size: 1\.75rem", css)
    assert (
        "main .who > [role=button]:not(.outline), .timeline-toolbar .zoom > button[aria-pressed=true], .cal-views > a[aria-current=page]"
        in css
    )
    js = (STATIC / "app.js").read_text()
    assert "markRange(savedRange)" in js  # the timeline zoom shows which range is on
