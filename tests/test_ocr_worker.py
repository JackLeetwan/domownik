"""deploy/pc/ocr_worker.py – the pure parts: where a page is cut into strips, how strips are joined."""

from __future__ import annotations

import importlib.util
from itertools import pairwise
from pathlib import Path

from PIL import Image, ImageDraw

_spec = importlib.util.spec_from_file_location("ocr_worker", Path(__file__).parents[1] / "deploy/pc/ocr_worker.py")
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)


def _page(width=1240, height=1754, line=24, gap=12) -> Image.Image:
    """A page of black "text lines" with white gaps between them."""
    im = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(im)
    y = 60
    while y + line < height - 60:
        d.rectangle((80, y, width - 80, y + line), fill="black")
        y += line + gap
    return im


def test_small_image_is_one_strip():
    assert w.cut_points(Image.new("RGB", (800, 1000), "white")) == [(0, 1000)]


def test_cuts_go_between_lines_and_strips_stay_small():
    im = _page()
    strips = w.cut_points(im)
    assert len(strips) == 3 and strips[0][0] == 0 and strips[-1][1] == im.height
    ink = w.row_ink(im)
    for (_, bottom), (top, _) in pairwise(strips):
        assert bottom == top and ink[top] <= w.BLANK_ROW  # an empty row – no overlap needed
    for top, bottom in strips:
        assert im.width * (bottom - top) <= w.STRIP_PIXELS * 1.1


def test_no_empty_row_means_overlap():
    im = Image.effect_noise((1240, 1754), 120).convert("RGB")  # "text" everywhere, no gap anywhere
    strips = w.cut_points(im)
    for (_, bottom), (top, _) in pairwise(strips):
        assert bottom - top == 2 * w.OVERLAP


def test_join_drops_lines_repeated_in_the_overlap():
    a = "PARAGON FISKALNY\nOLEJ 32,49\nSUMA PLN 81,48"
    b = "SUMA  PLN 81,48\nKARTA 81,48 PLN\n2026-09-17 17:37"
    assert w.join([a, b]) == "PARAGON FISKALNY\nOLEJ 32,49\nSUMA PLN 81,48\n\nKARTA 81,48 PLN\n2026-09-17 17:37"


def test_join_merges_a_line_read_slightly_differently():
    a = "SUMA PTU 1,31\nSUMA PLN 6,98"
    b = "SUMA PLN 6.98\nKARTA DEBIT MASTERCARD 6,98 PLN"
    assert w.join([a, b]) == "SUMA PTU 1,31\nSUMA PLN 6,98\n\nKARTA DEBIT MASTERCARD 6,98 PLN"


def test_shadowed_photo_still_has_gaps():
    """A grey, unevenly lit sheet: the gaps between lines are still found."""
    im = _page()
    shade = Image.linear_gradient("L").resize(im.size).point(lambda v: 90 + v // 3)
    im = Image.composite(
        im, Image.merge("RGB", (shade, shade, shade)), im.convert("L").point(lambda v: 255 if v < 128 else 0)
    )
    ink = w.row_ink(im)
    for (_, bottom), (top, _) in pairwise(w.cut_points(im)):
        assert bottom == top and ink[top] <= w.BLANK_ROW


def test_loop_is_noticed():
    assert w.looks_looped("y la hija " * 100)
    assert not w.looks_looped("PARAGON FISKALNY\nOLEJ 32,49\nSUMA PLN 81,48")


def test_prepare_turns_and_shrinks():
    im = w.prepare(Image.new("RGB", (4000, 3000), "white"))
    assert max(im.size) == w.MAX_SIDE
