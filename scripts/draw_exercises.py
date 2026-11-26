#!/usr/bin/env python3
"""Our own pictures for exercises free-exercise-db has none of (most now replaced by stock photos – STOCK below): a side-view silhouette, two frames
(start and end position), the same look as standing-quad-stretch. Writes static/exercises/<slug>.webp
and <slug>-1.webp (320×213, like the photos).

    python3 scripts/draw_exercises.py              # all
    python3 scripts/draw_exercises.py cobra pigeon # some (keys of POSES)
    python3 scripts/draw_exercises.py --sheet      # + a contact sheet of every frame (to look at them)

Needs Chromium (the Playwright one is found by itself, or CHROME=/path) and cwebp.
A pose: a figure (hip, torso angle, limb angles – degrees, 0 = to the right, 90 = down, the figure faces
right) plus props. The far limbs are lighter and behind the body, the near ones darker in front.
"""

from __future__ import annotations

import glob
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src/domownik/static/exercises"
W, H, FLOOR = 960, 640, 560
NEAR, FAR, HALO, ACCENT, PROP, PROP2 = "#2f5d78", "#8eaabd", "#eee5d8", "#e07a2f", "#6b5a48", "#9c8a74"
BAND = "#d9534f"
TOR, NECK, HEAD = 125, 26, 34
UA, FA, TH, SH, FT = 88, 80, 130, 122, 34


def d(a: float) -> tuple[float, float]:
    r = math.radians(a)
    return math.cos(r), math.sin(r)


def go(p, length, a):
    dx, dy = d(a)
    return (p[0] + dx * length, p[1] + dy * length)


def chain(p, *segs):
    pts = [p]
    for length, a in segs:
        pts.append(go(pts[-1], length, a))
    return pts


def poly(pts, w, c, extra=""):
    s = " ".join(f"{x:.0f},{y:.0f}" for x, y in pts)
    return (f'<polyline points="{s}" fill="none" stroke="{c}" stroke-width="{w}" '
            f'stroke-linecap="round" stroke-linejoin="round" {extra}/>')


def limb(pts, w, c=NEAR):
    return poly(pts, w + 10, HALO) + poly(pts, w, c)


def ik(root, target, a, b, bend):
    """Two bones from root towards target; bend: +1 / -1 – which side the joint goes to (clockwise on screen /
    counter-clockwise). Out of reach: straight towards it."""
    dx, dy = target[0] - root[0], target[1] - root[1]
    dist = max(abs(a - b) + 1, min(math.hypot(dx, dy), a + b - 0.5))
    base = math.degrees(math.atan2(dy, dx))
    alpha = math.degrees(math.acos((a * a + dist * dist - b * b) / (2 * a * dist)))
    j = go(root, a, base + bend * alpha)
    e = go(j, b, math.degrees(math.atan2(target[1] - j[1], target[0] - j[0])))
    return [root, j, e]


class Fig:
    """hip: (x, y); torso: angle hip → shoulder; head: angle neck → head (default: the torso's).
    An arm: (upper, fore) angles or ((x, y) of the wrist, bend). A leg: (thigh, shin, foot) angles or
    ((x, y) of the ankle, bend, foot angle). front=True: seen from the front (jumping jacks)."""

    def __init__(self, hip, torso=-90, head=None, narm=(95, 95), farm=(95, 95), nleg=(90, 90, 0), fleg=(90, 90, 0),
                 front=False, hands=False):
        self.hip, self.front = hip, front
        self.sh = go(hip, TOR, torso)
        nb = go(self.sh, 12, torso)
        self.head = go(nb, NECK + HEAD, torso if head is None else head)
        self.neck = (nb, go(nb, NECK, torso if head is None else head))
        off = 30 if front else 0
        perp = torso + 90
        self.n_sh, self.f_sh = go(self.sh, off, perp), go(self.sh, -off, perp)
        self.n_hip, self.f_hip = go(hip, off * 0.6, perp), go(hip, -off * 0.6, perp)
        self.narm, self.farm = self._arm(self.n_sh, narm), self._arm(self.f_sh, farm)
        self.nleg, self.fleg = self._leg(self.n_hip, nleg), self._leg(self.f_hip, fleg)
        self.hands = hands

    @staticmethod
    def _arm(root, spec):
        if isinstance(spec[0], tuple):
            return ik(root, spec[0], UA, FA, spec[1])
        return chain(root, (UA, spec[0]), (FA, spec[1]))

    @staticmethod
    def _leg(root, spec):
        if isinstance(spec[0], tuple):
            pts = ik(root, spec[0], TH, SH, spec[1])
            return pts + [go(pts[-1], FT, spec[2])]
        return chain(root, (TH, spec[0]), (SH, spec[1]), (FT, spec[2]))

    def svg(self):
        far = NEAR if self.front else FAR
        parts = [poly(self.fleg, 36, far), poly(self.farm, 26, far)]
        tw = 80 if self.front else 68
        parts.append(poly([go(self.hip, 8, self._ang()), go(self.sh, -6, self._ang())], tw, NEAR))
        parts.append(poly(self.neck, 24, NEAR))
        parts.append(f'<circle cx="{self.head[0]:.0f}" cy="{self.head[1]:.0f}" r="{HEAD}" fill="{NEAR}"/>')
        parts.append(limb(self.nleg, 36) if not self.front else poly(self.nleg, 36, NEAR))
        parts.append(limb(self.narm, 26) if not self.front else poly(self.narm, 26, NEAR))
        if self.hands:
            x, y = self.narm[-1]
            parts.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="16" fill="{NEAR}"/>')
        return "".join(parts)

    def _ang(self):
        return math.degrees(math.atan2(self.sh[1] - self.hip[1], self.sh[0] - self.hip[0]))


def plank(sh, ankle, **k):
    """A body straight from the shoulder to the ankle (a plank, seen from the side or the front)."""
    a = math.degrees(math.atan2(ankle[1] - sh[1], ankle[0] - sh[0]))
    hip = go(sh, TOR, a)
    return Fig(hip, torso=a + 180, **k)


# ---------------------------------------------------------------- props
def mat(x0=250, x1=710):
    return f'<rect x="{x0}" y="{FLOOR + 26}" width="{x1 - x0}" height="14" rx="7" fill="#7f9db0" opacity=".55"/>'


def wall(x):
    return (f'<rect x="{x}" y="0" width="{W - x}" height="{FLOOR}" fill="#ddd2c2"/>'
            f'<rect x="{x}" y="0" width="8" height="{FLOOR}" fill="#c9bba7"/>')


def wall_left(x):
    return (f'<rect x="0" y="0" width="{x}" height="{FLOOR}" fill="#ddd2c2"/>'
            f'<rect x="{x - 8}" y="0" width="8" height="{FLOOR}" fill="#c9bba7"/>')


def door(x):
    return (f'<rect x="{x}" y="40" width="26" height="{FLOOR - 40}" fill="{PROP2}"/>'
            f'<rect x="{x + 26}" y="40" width="4" height="{FLOOR - 40}" fill="{PROP}" opacity=".5"/>')


def step(x, w=150, h=60):
    return (f'<rect x="{x}" y="{FLOOR - h}" width="{w}" height="{h}" rx="6" fill="{PROP2}"/>'
            f'<rect x="{x}" y="{FLOOR - h}" width="{w}" height="10" rx="5" fill="{PROP}"/>')


def bench(x, w=230, h=92):
    return (f'<rect x="{x}" y="{FLOOR - h}" width="{w}" height="24" rx="10" fill="{PROP}"/>'
            f'<rect x="{x + 20}" y="{FLOOR - h + 20}" width="18" height="{h - 20}" fill="{PROP2}"/>'
            f'<rect x="{x + w - 38}" y="{FLOOR - h + 20}" width="18" height="{h - 20}" fill="{PROP2}"/>')


def chair(x, seat=FLOOR - 104):  # the back at x, the seat to the right, its top at y = seat
    return (f'<rect x="{x}" y="{seat - 150}" width="16" height="{FLOOR - seat + 150}" fill="{PROP}"/>'
            f'<rect x="{x}" y="{seat}" width="150" height="18" rx="6" fill="{PROP}"/>'
            f'<rect x="{x + 130}" y="{seat + 18}" width="14" height="{FLOOR - seat - 18}" fill="{PROP2}"/>')


def anchor(x, y):
    return f'<circle cx="{x}" cy="{y}" r="10" fill="{PROP}"/>'


def band(*pts):
    return poly(pts, 9, BAND, 'opacity=".9"')


def barbell(c):
    x, y = c
    return (f'<circle cx="{x:.0f}" cy="{y:.0f}" r="48" fill="{PROP}"/>'
            f'<circle cx="{x:.0f}" cy="{y:.0f}" r="38" fill="{PROP2}"/>'
            f'<circle cx="{x:.0f}" cy="{y:.0f}" r="10" fill="{PROP}"/>')


def arrow(x0, y0, x1, y1, bend=0.0):
    """A curved accent arrow from (x0, y0) to (x1, y1); bend: how far the middle bows out (px)."""
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = x1 - x0, y1 - y0
    n = math.hypot(dx, dy) or 1
    cx, cy = mx - dy / n * bend, my + dx / n * bend
    a = math.atan2(y1 - cy, x1 - cx)
    tip = [(x1 + math.cos(a) * 16, y1 + math.sin(a) * 16),
           (x1 + math.cos(a + 2.4) * 18, y1 + math.sin(a + 2.4) * 18),
           (x1 + math.cos(a - 2.4) * 18, y1 + math.sin(a - 2.4) * 18)]
    t = " ".join(f"{x:.0f},{y:.0f}" for x, y in tip)
    return (f'<path d="M{x0:.0f} {y0:.0f} Q{cx:.0f} {cy:.0f} {x1:.0f} {y1:.0f}" fill="none" stroke="{ACCENT}" '
            f'stroke-width="9" stroke-linecap="round"/><polygon points="{t}" fill="{ACCENT}"/>')


BG = (f'<defs><linearGradient id="w" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f4eee5"/>'
      f'<stop offset="1" stop-color="#e9dfd0"/></linearGradient></defs><rect width="{W}" height="{H}" fill="url(#w)"/>'
      f'<rect y="{FLOOR}" width="{W}" height="{H - FLOOR}" fill="#cdb490"/>'
      f'<rect y="{FLOOR - 4}" width="{W}" height="6" fill="#b89c76"/>')


def frame(*parts, back=(), front=()):
    body = "".join(p.svg() if isinstance(p, Fig) else p for p in parts)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">{BG}'
            f'{"".join(back)}{body}{"".join(front)}</svg>')


# ---------------------------------------------------------------- the poses: key → (slug, start, end)
def poses():
    P = {}
    LF = FLOOR - 18  # a leg resting on the floor (its middle)
    HF = FLOOR - 13  # a hand on the floor
    STAND = FLOOR - 18 - TH - SH  # the hip of a person standing straight

    def all_fours(x=360):
        """Knees under the hips, hands under the shoulders."""
        hip = (x, LF - TH)
        return dict(hip=hip, torso=-13, head=-5, nleg=(90, 180, 180), fleg=(90, 180, 180),
                    narm=(90, 90), farm=(90, 90))

    # ---- strength: new
    kn = (330, LF)
    hip0 = go(kn, TH, -40)
    hip1 = go(kn, TH, -12)
    P["knee_push_up"] = ("knee-push-up", frame(
        mat(), Fig(hip0, torso=-40, head=-28, narm=((540, HF), 1), farm=((540, HF), 1),
                   nleg=(140, 180, 180), fleg=(140, 180, 180)),
    ), frame(
        mat(), Fig(hip1, torso=-12, head=-8, narm=((540, HF), -1), farm=((540, HF), -1),
                   nleg=(168, 180, 180), fleg=(168, 180, 180)),
        arrow(640, 330, 640, 420),
    ))
    P["wall_sit"] = ("wall-sit", frame(
        wall_left(430), Fig((466, STAND), narm=(92, 92), farm=(92, 92), nleg=((470, LF), -1, 0),
                            fleg=((462, LF), -1, 0)),
    ), frame(
        wall_left(430), Fig((466, LF - SH), narm=((572, LF - SH - 14), -1), farm=((566, LF - SH - 14), -1),
                            nleg=((596, LF), -1, 0), fleg=((590, LF), -1, 0)),
        arrow(700, 240, 700, 340),
    ))
    af = all_fours()
    P["bird_dog"] = ("bird-dog", frame(mat(), Fig(**af)), frame(
        mat(), Fig(**{**af, "narm": (-12, -12), "fleg": (182, 182, 172)}),
        arrow(110, 360, 60, 350, 8), arrow(700, 330, 770, 310, -8),
    ))

    # ---- strength: no picture so far
    P["pike_push_up"] = ("pike-push-up", frame(
        mat(), Fig((480, 300), torso=55, head=70, narm=((640, HF), -1), farm=((640, HF), -1),
                   nleg=((370, LF), 1, 25), fleg=((370, LF), 1, 25)),
    ), frame(
        mat(), Fig((480, 330), torso=70, head=95, narm=((640, HF), -1), farm=((640, HF), -1),
                   nleg=((370, LF), 1, 25), fleg=((370, LF), 1, 25)),
        arrow(690, 380, 690, 470),
    ))
    kh = (440, LF - TH)
    ksh = go(kh, TOR, -90)
    anc = (680, 50)
    reach = go(ksh, 165, math.degrees(math.atan2(anc[1] - ksh[1], anc[0] - ksh[0])))
    pulled = (ksh[0] + 30, ksh[1] + 30)
    kneel = dict(nleg=(90, 180, 180), fleg=(90, 180, 180))
    P["band_pulldown"] = ("band-pulldown", frame(
        anchor(*anc), mat(), Fig(kh, narm=(reach, 1), farm=(reach, 1), **kneel), band(anc, reach),
    ), frame(
        anchor(*anc), mat(), Fig(kh, narm=(pulled, -1), farm=(pulled, -1), **kneel), band(anc, pulled),
        arrow(600, 150, 560, 260, -20),
    ))
    sit = (380, FLOOR - 34)
    feet = (380 + TH + SH, LF)
    loop = (feet[0] + 20, feet[1] - 30)
    legs = dict(nleg=(feet, -1, -80), fleg=(feet, -1, -80))
    ssh0, ssh1 = go(sit, TOR, -86), go(sit, TOR, -96)
    h0 = (ssh0[0] + 160, ssh0[1] + 40)
    h1 = (ssh1[0] + 40, ssh1[1] + 50)
    P["band_row"] = ("band-row", frame(
        mat(), Fig(sit, torso=-86, narm=(h0, -1), farm=(h0, -1), **legs), band(h0, loop),
    ), frame(
        mat(), Fig(sit, torso=-96, narm=(h1, 1), farm=(h1, 1), **legs), band(h1, loop),
        arrow(560, 250, 470, 250, -14),
    ))
    st = Fig((470, STAND))
    down = chain(st.n_sh, (UA, 92), (FA, 92))[-1]
    curled = chain(st.n_sh, (UA, 96), (FA, -20))[-1]
    foot = (500, FLOOR - 6)
    P["band_curl"] = ("band-curl", frame(
        Fig((470, STAND), narm=(92, 92), farm=(92, 92)), band(foot, down),
    ), frame(
        Fig((470, STAND), narm=(96, -20), farm=(96, -20)), band(foot, curled),
        arrow(620, 330, 610, 230, -24),
    ))
    anc2 = (660, 60)
    up = chain(st.n_sh, (UA, 96), (FA, -20))[-1]
    P["band_pushdown"] = ("band-pushdown", frame(
        anchor(*anc2), Fig((470, STAND), narm=(96, -20), farm=(96, -20)), band(anc2, up),
    ), frame(
        anchor(*anc2), Fig((470, STAND), narm=(92, 90), farm=(92, 90)), band(anc2, down),
        arrow(620, 240, 610, 340, 24),
    ))
    def squat_bar(fig):
        bar = (fig.sh[0] - 10, fig.sh[1] + 14)
        return [barbell(bar)], (bar, 1)
    s0 = Fig((470, STAND), nleg=((476, LF), -1, 0), fleg=((464, LF), -1, 0))
    back0, grip0 = squat_bar(s0)
    s1 = Fig((430, LF - 190), torso=-78, nleg=((500, LF), -1, 0), fleg=((488, LF), -1, 0))
    back1, grip1 = squat_bar(s1)
    P["bb_sumo_squat"] = ("barbell-sumo-squat", frame(
        Fig((470, STAND), narm=grip0, farm=grip0, nleg=((476, LF), -1, 0), fleg=((464, LF), -1, 0)), *back0,
    ), frame(
        Fig((430, LF - 190), torso=-78, narm=grip1, farm=grip1, nleg=((500, LF), -1, 0), fleg=((488, LF), -1, 0)),
        *back1, arrow(660, 170, 660, 270),
    ))
    top = FLOOR - 40
    P["poliquin_step"] = ("poliquin-step", frame(
        step(320, 170, 40), Fig((470, top - 18 - TH - SH), narm=(92, 92), farm=(92, 92), nleg=((474, top - 18), -1, 0),
                            fleg=((520, top + 26), -1, 20)),
    ), frame(
        step(320, 170, 40), Fig((440, top - 18 - 175), torso=-82, narm=(-10, 0), farm=(-10, 0),
                            nleg=((486, top - 18), -1, 0), fleg=((590, LF), -1, 0)),
        arrow(600, 320, 640, 400, -20),
    ))
    btop = FLOOR - 110
    elbow = (300, HF)
    csh = (300, HF - UA)
    P["copenhagen"] = ("copenhagen-plank", frame(
        bench(650, 220, 110),
        Fig(go(csh, TOR, 22), torso=-158, head=-170, narm=(90, 0), farm=(-90, -90),
            nleg=((760, btop - 16), 1, 0), fleg=((700, LF), 1, 0)),
    ), frame(
        bench(650, 220, 110),
        plank(csh, (760, btop - 16), head=-178, narm=(90, 0), farm=(-90, -90), nleg=((760, btop - 16), 1, 0),
              fleg=((700, btop + 30), 1, 0)),
        arrow(430, 520, 430, 440),
    ))
    psh = (330, HF - UA)
    toes = (770, LF)
    # forearm plank → the hips turned towards the floor on one side (seen from the side: they dip, an arc shows the turn)
    P["plank_rotation"] = ("plank-rotation", frame(
        mat(), plank(psh, toes, head=-178, narm=(90, 0), farm=(90, 0), nleg=(toes, 1, 90), fleg=(toes, 1, 90)),
    ), frame(
        mat(), Fig(go(psh, TOR, 12), torso=-168, head=-176, narm=(90, 0), farm=(90, 0), nleg=(toes, -1, 90),
                   fleg=(toes, -1, 90)),
        '<path d="M400 420 A60 34 0 1 0 510 420" fill="none" stroke="#e07a2f" stroke-width="9" stroke-linecap="round"/>',
        '<polygon points="520,412 502,432 524,436" fill="#e07a2f"/>',
    ))

    # ---- stretches: new
    post = 360
    wrist = (post + 20, 90)
    P["doorway_chest"] = ("doorway-chest-stretch", frame(
        door(post), Fig((450, STAND), narm=(92, 92), farm=(wrist, 1), nleg=((454, LF), -1, 0),
                        fleg=((446, LF), -1, 0)),
    ), frame(
        door(post), Fig((478, STAND + 8), torso=-80, narm=(92, 92), farm=(wrist, 1), nleg=((560, LF), -1, 0),
                        fleg=((420, LF), -1, 0)),
        arrow(560, 160, 630, 170, -12),
    ))
    tsh = go(af["hip"], TOR, 40)
    P["thread_needle"] = ("thread-the-needle", frame(mat(), Fig(**af)), frame(
        mat(), Fig(af["hip"], torso=40, head=12, nleg=(90, 180, 180), fleg=(90, 180, 180),
                   narm=((tsh[0] - 150, HF), 1), farm=((tsh[0] + 70, HF), -1)),
        arrow(600, 360, 560, 450, -20),
    ))
    P["down_dog"] = ("downward-dog", frame(mat(), Fig(**af)), frame(
        mat(), Fig((490, 290), torso=63, head=75, narm=((650, HF), -1), farm=((650, HF), -1),
                   nleg=((400, LF - 10), 1, 8), fleg=((400, LF - 10), 1, 8)),
        arrow(500, 380, 490, 240, -20),
    ))
    seat = (470, FLOOR - 34)
    legs9 = dict(nleg=(8, 196, 180), fleg=(150, 180, 180))
    P["hips_90_90"] = ("hips-90-90", frame(
        mat(), Fig(seat, torso=-90, narm=((560, HF), 1), farm=((400, HF), -1), **legs9),
    ), frame(
        mat(), Fig(seat, torso=-30, head=-20, narm=((680, HF), 1), farm=((660, HF), 1), **legs9),
        arrow(500, 260, 640, 330, -40),
    ))
    cseat = FLOOR - 104
    chip = (450, cseat - 18)
    clegs = dict(nleg=((586, LF), -1, 0), fleg=((578, LF), -1, 0))
    c0 = Fig(chip, **clegs)
    on_head = (c0.head[0] - 6, c0.head[1] - 44)
    P["neck_levator"] = ("neck-levator-stretch", frame(
        chair(380, cseat), Fig(chip, narm=(92, 0), farm=(92, 0), **clegs),
    ), frame(
        chair(380, cseat), Fig(chip, head=-40, narm=(on_head, -1), farm=(92, 0), hands=True, **clegs),
        arrow(620, 110, 600, 200, -24),
    ))
    P["jumping_jacks"] = ("jumping-jacks", frame(
        Fig((480, STAND), front=True, narm=(82, 86), farm=(98, 94), nleg=(88, 90, 0), fleg=(92, 90, 180)),
    ), frame(
        Fig((480, STAND - 10), front=True, narm=(-60, -75), farm=(-120, -105), nleg=(70, 76, 0),
            fleg=(110, 104, 180)),
    ))

    # ---- stretches: no picture so far
    lie = (430, FLOOR - 34)
    ly = dict(nleg=(180, 180, 180), fleg=(180, 180, 180))
    P["cobra"] = ("cobra-stretch", frame(
        mat(), Fig(lie, torso=0, head=-4, narm=((560, HF), -1), farm=((560, HF), -1), **ly),
    ), frame(
        mat(), Fig(lie, torso=-42, head=-62, narm=((560, HF), 1), farm=((560, HF), 1), **ly),
        arrow(660, 480, 660, 380, -20),
    ))
    pg = (480, FLOOR - 40)
    pl = dict(nleg=(4, 176, 180), fleg=(178, 180, 180))
    P["pigeon"] = ("pigeon-stretch", frame(
        mat(), Fig(pg, torso=-82, narm=((540, HF), 1), farm=((530, HF), 1), **pl),
    ), frame(
        mat(), Fig(pg, torso=-8, head=-4, narm=((700, HF), 1), farm=((690, HF), 1), **pl),
        arrow(520, 230, 640, 330, -40),
    ))
    return P


def chrome() -> str:
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    found = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
    if not found:
        sys.exit("Brak Chromium – ustaw CHROME=/ścieżka")
    return found[-1]


def render(svg: str, png: Path) -> None:
    src = png.with_suffix(".svg")
    src.write_text(svg, encoding="utf-8")
    subprocess.run([chrome(), "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                    f"--window-size={W},{H}", f"--screenshot={png}", src.as_uri()],
                   check=True, capture_output=True)


# Drawings replaced by stock photos (Pexels, free to use without attribution: pexels.com/license): slug → photo id
# (pexels.com/photo/<id>/), one frame, 320×213. Running the script without names leaves them alone.
STOCK = {
    "wall-sit": 5038859,
    "bird-dog": 6454196,
    "pike-push-up": 3758148,
    "band-pulldown": 4920470,
    "band-row": 5067749,
    "band-curl": 4920435,
    "band-pushdown": 30672398,
    "barbell-sumo-squat": 1552249,
    "poliquin-step": 4587386,
    "plank-rotation": 4775188,
    "doorway-chest-stretch": 5743233,
    "thread-the-needle": 9194299,
    "downward-dog": 6303735,
    "hips-90-90": 4804308,
    "neck-levator-stretch": 4584274,
    "jumping-jacks": 4853080,
    "cobra-stretch": 3823076,
    "pigeon-stretch": 3822138,
    "standing-quad-stretch": 7787917,
}


def main() -> None:
    from PIL import Image

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    all_poses = poses()
    keys = args or [k for k, (slug, *_) in all_poses.items() if slug not in STOCK]
    tmp = Path(os.environ.get("DRAW_TMP") or tempfile.mkdtemp(prefix="draw-ex-"))
    tmp.mkdir(parents=True, exist_ok=True)
    thumbs = []
    for key in keys:
        slug, *frames = all_poses[key]
        for i, svg in enumerate(frames):
            png = tmp / f"{slug}-{i}.png"
            render(svg, png)
            small = Image.open(png).convert("RGB").resize((320, 213), Image.LANCZOS)
            small.save(png)
            out = OUT / f"{slug}{'-1' if i else ''}.webp"
            subprocess.run(["cwebp", "-quiet", "-q", "70", str(png), "-o", str(out)], check=True)
            thumbs.append((key, small))
    if "--sheet" in sys.argv:
        cols = 4
        rows = (len(thumbs) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * 320, rows * 213), "white")
        for n, (_, im) in enumerate(thumbs):
            sheet.paste(im, ((n % cols) * 320, (n // cols) * 213))
        sheet.save(tmp / "sheet.png")
        print(tmp / "sheet.png")


if __name__ == "__main__":
    main()
