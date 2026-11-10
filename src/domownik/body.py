"""⚖️ Sylwetka in "Forma": body weight, circumferences and photos – strictly the owner's.

Nobody else sees any of it: not the other household members, not an admin. Every query here is by the
logged-in person's id; a photo is served only to the person whose measurement it belongs to.

Weight: a weigh-in when you like (morning, after the toilet, before eating). A single day jumps 1–2 kg
(water, salt, carbs, creatine), so what counts is the average of the last 7 days. A gentle push comes in the
morning only after `weigh_gap_days` without a weigh-in, and only for someone who has started weighing.

Measurement ("pomiar", every `check_every_days`): chest / arm / waist / thigh in cm and three photos – front,
side, back. Photos are re-encoded on upload (JPEG, at most PHOTO_PX, no EXIF at all – no place, no phone,
no time) with a small WebP preview next to them in uploads/body/<user id>/.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from PIL import Image, ImageOps
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik import webpush
from domownik.config import get_settings, now
from domownik.models import BodyCheck, BodyPhoto, BodyPrefs, BodyWeight, User

log = logging.getLogger("domownik")

AVG_DAYS = 7
CHANGE_DAYS = 28  # "↑ 0,8 kg w 4 tygodnie"
POSES = [("front", "Przód"), ("side", "Bok"), ("back", "Tył")]
SITES = [
    ("chest", "Klatka", "na wysokości sutków, po spokojnym wydechu"),
    ("arm", "Ramię", "najszersze miejsce, napięte"),
    ("waist", "Talia", "na wysokości pępka, brzuch rozluźniony"),
    ("thigh", "Udo", "najszersze miejsce, stojąc"),
]
SITE_KEYS = [k for k, _, _ in SITES]
CHECK_EVERY_CHOICES = (14, 21, 28)
# the pace: a straight line through the weigh-ins of the last weeks (works when weighing once a week too)
PACE_DAYS = 42
PACE_MIN_SPAN = 14  # days between the first and the last weigh-in behind it
PACE_MIN_N = 3
# kg a week that make sense: gaining lean mass (faster is mostly fat) / losing (faster eats muscle)
GAIN_PACE = (0.1, 0.5)
LOSS_PACE = (0.2, 1.0)
GOAL_NEAR = 0.3  # kg – "reached"
ETA_MAX_WEEKS = 104
MONTHS_GEN = [
    "",
    "stycznia",
    "lutego",
    "marca",
    "kwietnia",
    "maja",
    "czerwca",
    "lipca",
    "sierpnia",
    "września",
    "października",
    "listopada",
    "grudnia",
]
# the morning push: from this hour, while it is still morning (a phone off at night catches up)
PUSH_FROM, PUSH_UNTIL = time(7, 30), time(11, 0)
CHECK_REPUSH_DAYS = 3  # a measurement not done: reminded again after this many days
PHOTO_PX = 1600
THUMB_PX = 480
KG_RANGE = (25.0, 300.0)
CM_RANGE = (10.0, 250.0)


class BodyError(ValueError):
    """Shown to the person as is (Polish)."""


# ---------------------------------------------------------------- numbers


def parse_number(value: Any, label: str, lo: float, hi: float, unit: str) -> float | None:
    s = str(value or "").strip().replace(",", ".").replace(" ", "")
    if not s:
        return None
    try:
        x = float(s)
    except ValueError as exc:
        raise BodyError(f"{label}: wpisz liczbę w {unit}") from exc
    if not lo <= x <= hi:
        raise BodyError(f"{label}: od {fmt(lo)} do {fmt(hi)} {unit}")
    return round(x, 1)


def fmt(x: float | None, signed: bool = False) -> str:
    """73.0 → "73", 73.45 → "73,5"; signed: "+0,8" / "−0,5" / "0"."""
    if x is None:
        return ""
    x = round(x, 1)
    s = f"{abs(x) if signed else x:.1f}".rstrip("0").rstrip(".").replace(".", ",")
    if signed and x:
        s = ("+" if x > 0 else "−") + s
    return s


# ---------------------------------------------------------------- settings


def prefs(db: Session, user: User) -> BodyPrefs:
    p = db.get(BodyPrefs, user.id)
    if p is None:
        p = BodyPrefs(user_id=user.id, reminders=True, weigh_gap_days=3, check_every_days=14)
        db.add(p)
        db.flush()
    return p


# ---------------------------------------------------------------- weight


def weights(db: Session, user: User, since: date | None = None) -> list[BodyWeight]:
    q = select(BodyWeight).where(BodyWeight.user_id == user.id)
    if since is not None:
        q = q.where(BodyWeight.day >= since)
    return list(db.scalars(q.order_by(BodyWeight.day)))


def get_weight(db: Session, user: User, weight_id: int) -> BodyWeight | None:
    w = db.get(BodyWeight, weight_id)
    return w if w is not None and w.user_id == user.id else None


def add_weight(db: Session, user: User, day: date, kg: float) -> BodyWeight:
    """One weigh-in a day: a second one the same day corrects the first."""
    w = db.scalar(select(BodyWeight).where(BodyWeight.user_id == user.id, BodyWeight.day == day))
    if w is None:
        w = BodyWeight(user_id=user.id, day=day, kg=kg)
        db.add(w)
    else:
        w.kg = kg
    prefs(db, user)  # someone who weighs gets the gentle reminder
    db.flush()
    return w


def average(entries: list[BodyWeight], day: date, days: int = AVG_DAYS) -> float | None:
    """Mean of the weigh-ins in the `days` days ending with `day` (however many there are)."""
    xs = [w.kg for w in entries if day - timedelta(days=days - 1) <= w.day <= day]
    return sum(xs) / len(xs) if xs else None


def trend(entries: list[BodyWeight], day: date, days: int = PACE_DAYS) -> tuple[float, int] | None:
    """kg a week from a least-squares line through the weigh-ins of `days` days ending with `day` – and the span
    in days behind it. None with too few weigh-ins or too short a stretch to say anything."""
    pts = [((w.day - day).days, w.kg) for w in entries if day - timedelta(days=days - 1) <= w.day <= day]
    if len(pts) < PACE_MIN_N:
        return None
    span = pts[-1][0] - pts[0][0]
    if span < PACE_MIN_SPAN:
        return None
    mx = sum(x for x, _ in pts) / len(pts)
    my = sum(y for _, y in pts) / len(pts)
    slope = sum((x - mx) * (y - my) for x, y in pts) / sum((x - mx) ** 2 for x, _ in pts)
    return slope * 7, span


@dataclass
class Pace:
    """Is the weight going to the goal at a sensible speed – and when will it get there."""

    per_week: float
    verdict: str  # ok | slow | fast | away (the other way than the goal)
    gaining: bool  # the goal is above
    eta: date | None

    @property
    def hint(self) -> str | None:
        if self.verdict == "away":
            return (
                "Waga spada, a cel jest wyżej – jesz za mało, dołóż ok. 300 kcal dziennie."
                if self.gaining
                else "Waga rośnie, a cel jest niżej – odejmij ok. 300 kcal dziennie."
            )
        if self.verdict == "slow":
            return (
                "Waga prawie stoi – jeśli tak zostanie, dołóż ok. 200–300 kcal dziennie."
                if self.gaining
                else "Waga prawie stoi – jeśli tak zostanie, odejmij ok. 200–300 kcal dziennie."
            )
        if self.verdict == "fast":
            return (
                "Szybciej niż 0,5 kg na tydzień przybywa głównie tłuszczu – rozważ ok. 200 kcal mniej."
                if self.gaining
                else "Ponad 1 kg na tydzień to ubywa też mięśni – rozważ ok. 200 kcal więcej."
            )
        return None

    @property
    def eta_text(self) -> str | None:
        if self.eta is None:
            return None
        part = "na początku" if self.eta.day <= 10 else ("w połowie" if self.eta.day <= 20 else "pod koniec")
        return f"{part} {MONTHS_GEN[self.eta.month]} {self.eta.year}"


def pace(entries: list[BodyWeight], goal: float | None, avg: float | None) -> Pace | None:
    if goal is None or avg is None or not entries or abs(goal - avg) < GOAL_NEAR:
        return None
    t = trend(entries, entries[-1].day)
    if t is None:
        return None
    per_week, _ = t
    gaining = goal > avg
    toward = per_week if gaining else -per_week  # + = to the goal
    lo, hi = GAIN_PACE if gaining else LOSS_PACE
    if toward < 0 and abs(per_week) >= lo / 2:
        verdict = "away"
    elif toward < lo:
        verdict = "slow"
    elif toward > hi:
        verdict = "fast"
    else:
        verdict = "ok"
    eta = None
    if toward >= lo / 2:
        weeks = abs(goal - avg) / toward
        if weeks <= ETA_MAX_WEEKS:
            eta = entries[-1].day + timedelta(days=round(weeks * 7))
    return Pace(per_week, verdict, gaining, eta)


@dataclass
class Summary:
    last: BodyWeight | None
    today_kg: float | None
    avg: float | None  # last 7 days (of the last weigh-in, so a week away does not blank it)
    avg_n: int  # weigh-ins behind it
    change: float | None  # avg now − avg CHANGE_DAYS earlier
    per_week: float | None
    goal: float | None
    to_goal: float | None  # goal − avg: + still to gain, − to lose
    days_since: int | None
    pace: Pace | None = None

    @property
    def stale(self) -> bool:
        """The average is from a while ago – say so instead of passing it as today's."""
        return self.days_since is not None and self.days_since >= AVG_DAYS


def summary(db: Session, user: User, today: date) -> Summary:
    p = db.get(BodyPrefs, user.id)
    goal = p.goal_kg if p else None
    entries = weights(db, user, since=today - timedelta(days=CHANGE_DAYS + AVG_DAYS + 400))
    if not entries:
        return Summary(None, None, None, 0, None, None, goal, None, None)
    last = entries[-1]
    avg = average(entries, last.day)
    n = sum(1 for w in entries if last.day - timedelta(days=AVG_DAYS - 1) <= w.day <= last.day)
    before = average(entries, last.day - timedelta(days=CHANGE_DAYS))
    change = avg - before if avg is not None and before is not None else None
    return Summary(
        last=last,
        today_kg=last.kg if last.day == today else None,
        avg=avg,
        avg_n=n,
        change=change,
        per_week=change / (CHANGE_DAYS / 7) if change is not None else None,
        goal=goal,
        to_goal=goal - avg if goal is not None and avg is not None else None,
        days_since=(today - last.day).days,
        pace=pace(entries, goal, avg),
    )


def chart(entries: list[BodyWeight], goal: float | None, start: date, end: date) -> dict | None:
    """Weigh-ins as dots, the 7-day average as a line, the goal as a dashed line – for an inline SVG.

    x is the date (gaps stay gaps), y the kg; the goal widens the range only when it is near the data
    (78 kg next to 60 kg would squash the line flat) – otherwise an arrow at the edge says where it is."""
    pts = [w for w in entries if start <= w.day <= end]
    if len(pts) < 2:
        return None
    every = sorted(entries, key=lambda w: w.day)
    avg = [(w.day, average(every, w.day)) for w in pts]
    values = [w.kg for w in pts]
    lo, hi = min(values), max(values)
    goal_in = goal is not None and lo - 5 <= goal <= hi + 5
    if goal_in:
        lo, hi = min(lo, goal), max(hi, goal)
    pad = max(0.3, (hi - lo) * 0.08)
    lo, hi = lo - pad, hi + pad
    first = min(start, pts[0].day)
    span_days = max(1, (end - first).days)
    w, h, left = 300, 130, 30

    def x(d: date) -> float:
        return round(left + (d - first).days / span_days * (w - left - 4), 1)

    def y(kg: float) -> float:
        return round(6 + (hi - kg) / (hi - lo) * (h - 12), 1)

    return {
        "w": w,
        "h": h,
        "left": left,
        "dots": [(x(e.day), y(e.kg)) for e in pts],
        "avg": " ".join(f"{x(d)},{y(a)}" for d, a in avg if a is not None),
        "goal_y": y(goal) if goal_in else None,
        "goal_off": None if goal is None or goal_in else ("up" if goal > hi else "down"),
        "goal": goal,
        "ticks": [(y(v), fmt(v)) for v in _ticks(lo, hi)],
        "from": first,
        "to": end,
    }


def _ticks(lo: float, hi: float) -> list[float]:
    step = next(s for s in (0.5, 1, 2, 5, 10, 20, 50) if (hi - lo) / s <= 4)
    v = (int(lo / step) + 1) * step
    out = []
    while v < hi:
        out.append(round(v, 1))
        v += step
    return out


# ---------------------------------------------------------------- measurements


def checks(db: Session, user: User) -> list[BodyCheck]:
    """Newest first."""
    return list(db.scalars(select(BodyCheck).where(BodyCheck.user_id == user.id).order_by(BodyCheck.day.desc())))


def get_check(db: Session, user: User, check_id: int) -> BodyCheck | None:
    c = db.get(BodyCheck, check_id)
    return c if c is not None and c.user_id == user.id else None


def get_photo(db: Session, user: User, photo_id: int) -> BodyPhoto | None:
    p = db.get(BodyPhoto, photo_id)
    return p if p is not None and p.check.user_id == user.id else None


def photo_of(c: BodyCheck, pose: str) -> BodyPhoto | None:
    return next((p for p in c.photos if p.pose == pose), None)


def next_check(db: Session, user: User, today: date) -> tuple[date | None, bool]:
    """(the day the next measurement is due, due now). None: no measurement yet."""
    last = db.scalar(select(func.max(BodyCheck.day)).where(BodyCheck.user_id == user.id))
    if last is None:
        return None, False
    p = db.get(BodyPrefs, user.id)
    due = last + timedelta(days=p.check_every_days if p else 14)
    return due, due <= today


def changes(cs: list[BodyCheck]) -> list[tuple[str, float, float, float | None]]:
    """Per site: (label, first value, latest value, change since the previous measurement) – from oldest to newest."""
    out = []
    for key, label, _ in SITES:
        vals = [getattr(c, key) for c in reversed(cs) if getattr(c, key) is not None]
        if vals:
            out.append((label, vals[0], vals[-1], vals[-1] - vals[-2] if len(vals) > 1 else None))
    return out


def save_check(
    db: Session,
    user: User,
    c: BodyCheck | None,
    day: date,
    sizes: dict[str, float | None],
    photos: dict[str, tuple[str, BinaryIO]],
    remove: set[str],
) -> BodyCheck:
    """New or edited measurement with its photos – all saved or (on an error) nothing. The caller commits."""
    other = db.scalar(select(BodyCheck).where(BodyCheck.user_id == user.id, BodyCheck.day == day))
    if c is None:
        c = other  # a second measurement the same day completes the first
    elif other is not None and other.id != c.id:
        raise BodyError(f"Pomiar z {day.strftime('%d.%m.%Y')} już jest – popraw tamten albo wybierz inny dzień")
    if c is None:
        c = BodyCheck(user_id=user.id, day=day)
        db.add(c)
    c.day = day
    for key in SITE_KEYS:
        if key in sizes:
            setattr(c, key, sizes[key])
    prefs(db, user)
    db.flush()
    old: list[Path] = []
    written: list[Path] = []
    try:
        for pose in remove - set(photos):
            p = photo_of(c, pose)
            if p is not None:
                old += _paths(p)
                c.photos.remove(p)
        for pose, upload in photos.items():
            p = photo_of(c, pose)
            if p is not None:
                old += _paths(p)
                c.photos.remove(p)
                db.flush()
            new = _store(user, pose, upload, written)
            c.photos.append(new)
        db.flush()
        if not any(getattr(c, k) is not None for k in SITE_KEYS) and not c.photos:
            raise BodyError("Wpisz choć jeden obwód albo dodaj zdjęcie")
    except BaseException:
        for f in written:
            f.unlink(missing_ok=True)
        raise
    for f in old:
        f.unlink(missing_ok=True)
    return c


def delete_check(db: Session, c: BodyCheck) -> None:
    paths = [f for p in c.photos for f in _paths(p)]
    db.delete(c)
    db.flush()
    for f in paths:
        f.unlink(missing_ok=True)


# ---------------------------------------------------------------- photo files


def _root() -> Path:
    return get_settings().uploads_dir.resolve()


def photo_path(p: BodyPhoto, thumb: bool = False) -> Path:
    """Absolute path of the stored photo (or its preview); refuses anything that is not one of our names."""
    parts = (p.storage_path or "").split("/")
    if len(parts) != 3 or parts[0] != "body" or not parts[1].isdigit() or not _is_name(parts[2]):
        raise FileNotFoundError(p.storage_path)
    path = (_root() / p.storage_path).resolve()
    if not path.is_relative_to(_root()):
        raise FileNotFoundError(p.storage_path)
    return path.with_name(path.stem + ".thumb.webp") if thumb else path


def _is_name(s: str) -> bool:
    stem, _, ext = s.partition(".")
    return ext == "jpg" and len(stem) == 32 and all(ch in "0123456789abcdef" for ch in stem)


def _paths(p: BodyPhoto) -> list[Path]:
    try:
        return [photo_path(p), photo_path(p, thumb=True)]
    except FileNotFoundError:
        return []


def _store(user: User, pose: str, upload: tuple[str, BinaryIO], written: list[Path]) -> BodyPhoto:
    """Re-encode the photo (upright, at most PHOTO_PX, JPEG without any metadata) + a WebP preview."""
    s = get_settings()
    label = dict(POSES)[pose]
    _, fobj = upload
    fobj.seek(0, 2)
    if fobj.tell() > s.max_upload_mb * 1024 * 1024:
        raise BodyError(f"{label}: zdjęcie jest za duże (najwyżej {s.max_upload_mb} MB)")
    fobj.seek(0)
    folder = _root() / "body" / str(user.id)
    folder.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(16)
    dest, thumb = folder / f"{name}.jpg", folder / f"{name}.thumb.webp"
    try:
        with Image.open(fobj) as im:
            if im.format not in ("JPEG", "MPO", "PNG", "WEBP"):
                raise BodyError(f"{label}: to nie jest zdjęcie JPG, PNG ani WebP")
            im.draft("RGB", (PHOTO_PX * 2, PHOTO_PX * 2))
            im = ImageOps.exif_transpose(im)
            im.thumbnail((PHOTO_PX, PHOTO_PX))
            im = im.convert("RGB")
            written.append(dest)
            im.save(dest, "JPEG", quality=85, optimize=True)  # no exif= → nothing of the original metadata
            small = im.copy()
            small.thumbnail((THUMB_PX, THUMB_PX))
            written.append(thumb)
            small.save(thumb, "WEBP", quality=78)
    except BodyError:
        raise
    except Exception as exc:
        log.info("body photo refused: %s", exc)
        raise BodyError(f"{label}: nie da się odczytać tego zdjęcia – zrób je jeszcze raz") from exc
    return BodyPhoto(
        pose=pose,
        storage_path=f"body/{user.id}/{name}.jpg",
        content_type="image/jpeg",
        size=dest.stat().st_size,
        has_thumb=True,
    )


# ---------------------------------------------------------------- the morning pushes


def _send(db: Session, user: User, body: dict[str, Any]) -> int:
    from domownik import notify

    try:
        return 1 if notify.send(db, user, body) else 0
    except webpush.PushError as exc:
        log.warning("body push: %s – %s", user.name, exc)
        return 0


def run(db: Session, at: datetime | None = None) -> int:
    """In the morning: no weigh-in for weigh_gap_days → "⚖️ Waga?"; a measurement due → "📏 Czas na pomiar".

    Only for people who have started (they have prefs – made by their first weigh-in or measurement),
    each push at most once in its gap."""
    at = (at or now()).replace(tzinfo=None)
    if not PUSH_FROM <= at.time() < PUSH_UNTIL:
        return 0
    today = at.date()
    sent = 0
    for p in list(db.scalars(select(BodyPrefs).where(BodyPrefs.reminders.is_(True)))):
        user = db.get(User, p.user_id)
        if user is None:
            continue
        last = db.scalar(select(func.max(BodyWeight.day)).where(BodyWeight.user_id == user.id))
        gap = max(1, p.weigh_gap_days)
        if (
            last is not None
            and (today - last).days >= gap
            and not (p.weigh_pushed_on and (today - p.weigh_pushed_on).days < gap)
        ):
            p.weigh_pushed_on = today
            db.commit()
            sent += _send(
                db,
                user,
                {
                    "title": "⚖️ Waga?",
                    "body": f"Ostatnio {(today - last).days} dni temu. Rano, po toalecie, przed jedzeniem.",
                    "url": "/forma#sylwetka",
                    "tag": f"domownik-waga-{user.id}",
                },
            )
        due, is_due = next_check(db, user, today)
        if (
            is_due
            and due is not None
            and not (p.check_pushed_on and (today - p.check_pushed_on).days < CHECK_REPUSH_DAYS)
        ):
            p.check_pushed_on = today
            db.commit()
            sent += _send(
                db,
                user,
                {
                    "title": "📏 Czas na pomiar",
                    "body": "Obwody i 3 zdjęcia – 3 minuty. Najlepiej teraz, przed jedzeniem.",
                    "url": "/forma/sylwetka/pomiar/nowy",
                    "tag": f"domownik-pomiar-{user.id}",
                },
            )
    return sent
