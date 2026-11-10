"""Supplements ("Omega-3, D3, kreatyna"): a daily ✓, a push at the hour, a streak and the stock.

Strictly private: every query here is for one owner, nobody else sees another person's supplements.

Push: supplements of one person with the same hour go out as one push with "✓ Wzięte" (ticks them all
off, signed token like the timers). Not ticked off → one more push at the evening hour (at least
AGAIN_AFTER after the first). A server that was down catches up on a push less than CATCH_UP late.

Stock: per_take units (capsules, portions) per ✓. When it lasts RESTOCK_DAYS or less the supplement goes
on the shared shopping list once; a bought pack (here, or ticked off as bought on the list) adds pack_size.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from domownik import services, webpush
from domownik.config import get_settings, now
from domownik.domain.grocery_names import key as name_key
from domownik.models import ShoppingItem, Supplement, SupplementDose, Transaction, User

log = logging.getLogger("domownik.supplements")

AGAIN_AFTER = timedelta(hours=2)
CATCH_UP = timedelta(hours=3)
TOKEN_MAX_AGE = 2 * 86400
STREAK_LOOKBACK = 400
BUDGET_CATEGORY = "Zdrowie"
SUGGESTIONS: list[tuple[str, str]] = [("Omega-3", "1 kaps."), ("Witamina D3", "2000 IU"), ("Kreatyna", "5 g")]
_TIME = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")


def _now() -> datetime:
    return now().replace(tzinfo=None)


def parse_time(raw: str | None) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    m = _TIME.match(raw)
    if not m:
        raise ValueError("Godzina w formacie 8:00")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def parse_number(raw: str | None, label: str, *, allow_zero: bool = False) -> float | None:
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{label}: podaj liczbę") from None
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero) or value > 100_000:
        raise ValueError(f"{label}: liczba od 0 do 100 000")
    return value


def fmt(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:g}".replace(".", ",")


def mine(db: Session, user: User, *, active: bool | None = True) -> list[Supplement]:
    stmt = select(Supplement).where(Supplement.user_id == user.id)
    if active is not None:
        stmt = stmt.where(Supplement.active.is_(active))
    return sorted(db.scalars(stmt), key=lambda s: (s.time or "99:99", s.id))


def get_own(db: Session, user: User, supplement_id: int) -> Supplement | None:
    s = db.get(Supplement, supplement_id)
    return s if s is not None and s.user_id == user.id else None


def taken_ids(db: Session, user: User, day: date) -> set[int]:
    return set(
        db.scalars(
            select(SupplementDose.supplement_id)
            .join(Supplement)
            .where(Supplement.user_id == user.id, SupplementDose.taken_on == day)
        )
    )


def days_left(s: Supplement) -> int | None:
    if s.stock is None:
        return None
    return int(max(s.stock, 0) // (s.per_take or 1))


def create(
    db: Session,
    user: User,
    *,
    name: str,
    dose: str | None,
    time: str | None,
    at: datetime | None = None,
    per_take: float = 1,
    pack_size: float | None = None,
    stock: float | None = None,
) -> Supplement:
    at = at or _now()
    s = Supplement(
        user_id=user.id,
        name=name[:60],
        dose=(dose or None) and dose[:40],
        time=time,
        per_take=per_take or 1,
        pack_size=pack_size,
        stock=stock,
        started_on=at.date(),
    )
    _skip_today_if_past(s, at)
    db.add(s)
    db.flush()
    check_restock(db, s)
    return s


def _skip_today_if_past(s: Supplement, at: datetime) -> None:
    """A new (or re-timed) supplement does not push about an hour already gone today – the card shows it."""
    if s.time and _due(s.time, at.date()) <= at:
        s.pushed_on = s.again_on = at.date()


def set_time(s: Supplement, time: str | None, at: datetime | None = None) -> None:
    if time != s.time:
        s.time = time
        s.pushed_on = s.again_on = None
        _skip_today_if_past(s, at or _now())


def pause(s: Supplement) -> None:
    s.active = False


def resume(s: Supplement, at: datetime | None = None) -> None:
    at = at or _now()
    s.active = True
    s.started_on = at.date()  # the pause does not break the streak of the others
    _skip_today_if_past(s, at)


# ---------------------------------------------------------------- ✓


def take(db: Session, s: Supplement, day: date, at: datetime | None = None) -> bool:
    """✓ for that day; False when it was already ticked off. The caller commits."""
    exists = db.scalar(
        select(SupplementDose.id).where(SupplementDose.supplement_id == s.id, SupplementDose.taken_on == day)
    )
    if exists:
        return False
    db.add(SupplementDose(supplement_id=s.id, taken_on=day, taken_at=at or _now()))
    if s.stock is not None:
        s.stock = max(0.0, s.stock - (s.per_take or 1))
    db.flush()
    check_restock(db, s)
    return True


def untake(db: Session, s: Supplement, day: date) -> bool:
    gone = db.execute(
        delete(SupplementDose).where(SupplementDose.supplement_id == s.id, SupplementDose.taken_on == day)
    ).rowcount
    if gone and s.stock is not None:
        s.stock += s.per_take or 1
    return bool(gone)


# ---------------------------------------------------------------- stock


def check_restock(db: Session, s: Supplement) -> bool:
    """Running out → once on the shared list (the owner adds it). True when it was put there now."""
    left = days_left(s)
    if left is None or not s.active or s.restock_listed or left > s.restock_days:
        return False
    from domownik.routers.shopping import add_entries

    owner = db.get(User, s.user_id)
    add_entries(db, [s.name], user=owner, note=f"💊 zostało na ~{left} dni")
    s.restock_listed = True
    return True


def bought_pack(db: Session, s: Supplement, user: User, *, price_gr: int | None, day: date, packs: int = 1) -> None:
    """A pack bought: stock up, off the shopping list, the price (if given) to the budget. The caller commits."""
    if s.pack_size:
        s.stock = (s.stock or 0) + s.pack_size * packs
    s.restock_listed = False
    k = name_key(s.name)
    for item in db.scalars(select(ShoppingItem).where(ShoppingItem.bought_on.is_(None))):
        if name_key(item.name) == k:
            item.bought_on = day
    if price_gr:
        cat = services.budget_category_by_name(db, BUDGET_CATEGORY, "variable")
        db.add(
            Transaction(
                booked_on=day,
                amount_gr=-abs(price_gr),
                budget_category_id=cat.id if cat else None,
                note=f"💊 {s.name}",
                created_by_id=user.id,
            )
        )


def bought_on_list(db: Session, items: list[ShoppingItem]) -> int:
    """'Zakończ zakupy': a supplement put on the list for running out and bought now gets its pack."""
    keys = {name_key(i.name) for i in items}
    n = 0
    for s in db.scalars(select(Supplement).where(Supplement.restock_listed.is_(True))):
        if name_key(s.name) in keys:
            if s.pack_size:
                s.stock = (s.stock or 0) + s.pack_size
            s.restock_listed = False
            n += 1
    return n


# ---------------------------------------------------------------- streak


def streak(db: Session, user: User, today: date) -> int:
    """Days in a row with every (active) supplement ticked off; today counts once all are taken."""
    sups = mine(db, user)
    if not sups:
        return 0
    since = today - timedelta(days=STREAK_LOOKBACK)
    rows = db.execute(
        select(SupplementDose.supplement_id, SupplementDose.taken_on).where(
            SupplementDose.supplement_id.in_([s.id for s in sups]), SupplementDose.taken_on >= since
        )
    ).all()
    by_day: dict[date, set[int]] = {}
    for sid, d in rows:
        by_day.setdefault(d, set()).add(sid)

    def complete(d: date) -> bool | None:
        needed = {s.id for s in sups if s.started_on <= d}
        if not needed:
            return None  # nothing to take yet that day – the streak stops here
        return needed <= by_day.get(d, set())

    n, d = 0, today
    if not complete(d):
        d -= timedelta(days=1)  # today still open – the streak up to yesterday holds
    while d >= since and complete(d):
        n += 1
        d -= timedelta(days=1)
    return n


# ---------------------------------------------------------------- today's card


@dataclass
class Group:
    time: str | None
    rows: list[tuple[Supplement, bool]]

    @property
    def open(self) -> list[Supplement]:
        return [s for s, done in self.rows if not done]


def today_groups(db: Session, user: User, day: date) -> list[Group]:
    taken = taken_ids(db, user, day)
    groups: dict[str | None, Group] = {}
    for s in mine(db, user):
        groups.setdefault(s.time, Group(s.time, [])).rows.append((s, s.id in taken))
    return list(groups.values())


# ---------------------------------------------------------------- push


def _due(time: str, day: date) -> datetime:
    h, m = (int(x) for x in time.split(":"))
    return datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m)


def _again_at(time: str, day: date) -> datetime:
    evening = datetime.combine(day, datetime.min.time()).replace(hour=get_settings().evening_hour)
    return max(evening, _due(time, day) + AGAIN_AFTER)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().resolved_secret_key(), salt="push-supplement")


def token(user: User, day: date, sups: list[Supplement]) -> str:
    return _serializer().dumps({"u": user.id, "d": day.isoformat(), "s": [s.id for s in sups]})


def read_token(value: str) -> tuple[int, date, list[int]] | None:
    try:
        data = _serializer().loads(value, max_age=TOKEN_MAX_AGE)
        return int(data["u"]), date.fromisoformat(data["d"]), [int(i) for i in data["s"]]
    except BadSignature, SignatureExpired, KeyError, TypeError, ValueError:
        return None


def _label(s: Supplement) -> str:
    return f"{s.name} {s.dose}" if s.dose else s.name


def payload(user: User, day: date, time: str, sups: list[Supplement], *, again: bool) -> dict[str, Any]:
    names = ", ".join(s.name for s in sups)
    body = " · ".join(_label(s) for s in sups)
    if again:
        body = f"Jeszcze nie odhaczone (miało być o {time}). {body}"
    return {
        "title": f"💊 {names}",
        "body": body,
        "url": "/forma",
        "tag": f"domownik-supl-{user.id}-{time}",
        "actions": [{"action": "supl-done", "title": "✓ Wzięte", "url": f"/n/supl/{token(user, day, sups)}"}],
    }


def run(db: Session, at: datetime | None = None) -> int:
    """Scheduler step (every minute). Returns how many pushes went out."""
    from domownik import notify

    at = (at or _now()).replace(tzinfo=None)
    day = at.date()
    sups = list(db.scalars(select(Supplement).where(Supplement.active.is_(True), Supplement.time.is_not(None))))
    by_group: dict[tuple[int, str], list[Supplement]] = {}
    for s in sups:
        by_group.setdefault((s.user_id, s.time), []).append(s)
    sent = 0
    for (user_id, time), group in sorted(by_group.items()):
        due = _due(time, day)
        if at < due:
            continue
        user = db.get(User, user_id)
        taken = taken_ids(db, user, day)
        open_ = [s for s in group if s.id not in taken]
        if any(s.pushed_on != day for s in group):
            for s in group:
                s.pushed_on = day  # first, so a failing push service does not repeat it every minute
            db.commit()
            if open_ and at - due <= CATCH_UP:
                sent += _send(db, notify, user, payload(user, day, time, open_, again=False))
            continue
        again = _again_at(time, day)
        if at < again or all(s.again_on == day for s in group) or again.date() != day:
            continue
        for s in group:
            s.again_on = day
        db.commit()
        if open_ and at - again <= CATCH_UP:
            sent += _send(db, notify, user, payload(user, day, time, open_, again=True))
    return sent


def _send(db: Session, notify, user: User, body: dict[str, Any]) -> int:
    try:
        return 1 if notify.send(db, user, body) else 0
    except webpush.PushError as exc:
        log.warning("supplement push: %s – %s", user.name, exc)
        return 0
