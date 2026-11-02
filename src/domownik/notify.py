"""Web Push notifications: morning digest, evening "tomorrow", one-tap "done".

Sent by Domownik itself to every browser/phone a user enabled in Ustawienia – no extra app.
Payloads are end-to-end encrypted for the device (the push service only relays them).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from domownik import services, webpush
from domownik.config import get_settings, now
from domownik.domain.money import format_pln
from domownik.domain.status import humanize_days
from domownik.domain.tree import Tree
from domownik.models import NotificationLog, PushSubscription, Task, User

log = logging.getLogger("domownik.notify")

OVERDUE_REPEAT_DAYS = 3  # overdue tasks come back in the digest every N days
MAX_DONE_ACTIONS = 2  # Chrome on Android shows at most 2 buttons; tapping the body opens the app
DONE_TOKEN_MAX_AGE = 14 * 86400
MAX_LINES = 12


# ---------------------------------------------------------------- what to send


@dataclass
class Line:
    rank: int  # 0 overdue, 1 today, 2 soon, 3 tomorrow
    text: str
    tasks: list[Task]  # one task, or a group of payments
    due: date


@dataclass
class Digest:
    user: User
    slot: str  # "morning" / "evening"
    lines: list[Line] = field(default_factory=list)

    @property
    def counts(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for ln in self.lines:
            out[ln.rank] = out.get(ln.rank, 0) + len(ln.tasks)
        return out

    def title(self) -> str:
        if self.slot == "evening":
            return "Domownik: na jutro"
        c = self.counts
        parts = []
        if c.get(0):
            parts.append(f"{c[0]} zaległe")
        if c.get(1):
            parts.append(f"{c[1]} na dziś")
        if c.get(2):
            parts.append(f"{c[2]} wkrótce")
        return "Domownik: " + " · ".join(parts) if parts else "Domownik"

    def message(self) -> str:
        shown = self.lines[:MAX_LINES]
        text = "\n".join(ln.text for ln in shown)
        if len(self.lines) > MAX_LINES:
            text += f"\n…i {len(self.lines) - MAX_LINES} więcej"
        return text


def _already(db: Session, user: User, kind: str, *, task_id: int | None, ref: date | None, since: date) -> bool:
    stmt = select(NotificationLog.id).where(
        NotificationLog.user_id == user.id,
        NotificationLog.kind == kind,
        NotificationLog.sent_on >= since,
    )
    stmt = stmt.where(NotificationLog.task_id == task_id) if task_id else stmt.where(NotificationLog.task_id.is_(None))
    if ref is not None:
        stmt = stmt.where(NotificationLog.ref_date == ref)
    return db.scalar(stmt.limit(1)) is not None


def _amount(tasks: list[Task]) -> str:
    total = sum((t.amount_gr or 0) * (1 if t.direction == "income" else -1) for t in tasks if t.direction != "none")
    return f" · {format_pln(total, sign=True)}" if total else ""


def _line_text(rank: int, title: str, due: date, today: date, tasks: list[Task]) -> str:
    from domownik.web import fmt_date

    money = _amount(tasks)
    if rank == 0:
        return f"⚠️ {title} – zaległe ({humanize_days((due - today).days)}){money}"
    if rank == 1:
        return f"📅 Dziś: {title}{money}"
    if rank == 3:
        return f"🔔 Jutro: {title}{money}"
    return f"🔜 {fmt_date(due)} ({humanize_days((due - today).days)}): {title}{money}"


def build_digests(db: Session, slot: str, today: date, *, preview_for: User | None = None) -> list[Digest]:
    """Digests for every user with a device and the slot switched on. Does not write anything.

    preview_for: only this user's digest, regardless of devices and switches (settings page, test push).
    """
    with_device = set(db.scalars(select(PushSubscription.user_id)))
    users = [
        u
        for u in db.scalars(select(User).where(User.is_active.is_(True)))
        if (u.id in with_device and (u.notify_morning if slot == "morning" else u.notify_evening))
        or (preview_for is not None and u.id == preview_for.id)
    ]
    if preview_for is not None:
        users = [u for u in users if u.id == preview_for.id]
    if not users:
        return []
    tree = Tree.load(db)
    rows = [r for r in services.task_rows(db, tree, today) if r.due.date and not r.task.autopay]
    digests = {u.id: Digest(u, slot) for u in users}

    for user in users:
        picked: list[tuple[int, services.TaskRow]] = []
        for r in rows:
            if r.task.assignee_id not in (None, user.id):
                continue
            days = (r.due.date - today).days
            if slot == "evening":
                if days == 1 and not _already(db, user, "tomorrow", task_id=r.task.id, ref=r.due.date, since=today):
                    picked.append((3, r))
                continue
            if days < 0:
                since = today - timedelta(days=OVERDUE_REPEAT_DAYS - 1)
                if not _already(db, user, "overdue", task_id=r.task.id, ref=r.due.date, since=since):
                    picked.append((0, r))
            elif days == 0:
                picked.append((1, r))
            elif r.due.status == "soon" and not _already(
                db, user, "task", task_id=r.task.id, ref=r.due.date, since=date.min
            ):
                picked.append((2, r))
        # several payments on one day → one line
        groups: dict[tuple[int, date], list[services.TaskRow]] = {}
        for rank, r in picked:
            key = (rank, r.due.date) if r.task.category == "payment" else (rank, date.min, r.task.id)
            groups.setdefault(key, []).append(r)
        for key, rs in groups.items():
            rank, due = key[0], rs[0].due.date
            if len(rs) >= services.PAYMENT_GROUP_MIN:
                title = f"Opłaty – {len(rs)} pozycji"
            elif len(rs) > 1:
                title = ", ".join(r.task.full_title for r in rs)
            else:
                title = rs[0].task.full_title
            tasks = [r.task for r in rs]
            digests[user.id].lines.append(Line(rank, _line_text(rank, title, due, today, tasks), tasks, due))
        digests[user.id].lines.sort(key=lambda ln: (ln.rank, ln.due, ln.text))
    return list(digests.values())


# ---------------------------------------------------------------- one-tap "done"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().resolved_secret_key(), salt="push-done")


def done_token(task: Task, user: User) -> str:
    return _serializer().dumps({"t": task.id, "d": task.next_due_date.isoformat(), "u": user.id})


def read_done_token(token: str) -> dict[str, Any] | None:
    try:
        return _serializer().loads(token, max_age=DONE_TOKEN_MAX_AGE)
    except BadSignature, SignatureExpired:
        return None


# ---------------------------------------------------------------- sending


def payload(d: Digest) -> dict[str, Any]:
    """What the service worker (static/sw.js) turns into a notification. URLs are same-origin paths."""
    actions: list[dict[str, Any]] = []
    for ln in d.lines:
        if len(actions) >= MAX_DONE_ACTIONS:
            break
        if len(ln.tasks) == 1 and ln.rank in (0, 1, 3) and ln.tasks[0].next_due_date:
            t = ln.tasks[0]
            label = "✓ " + (t.title if len(t.title) <= 22 else t.title[:21] + "…")
            actions.append(
                {"action": f"done-{len(actions)}", "title": label, "url": f"/n/done/{done_token(t, d.user)}"}
            )
    return {
        "title": d.title(),
        "body": d.message(),
        "url": "/",
        "tag": f"domownik-{d.slot}",
        "urgent": bool(d.counts.get(0)),
        "actions": actions,
    }


def send(db: Session, user: User, body: dict[str, Any]) -> int:
    """Push to every device of the user. Returns how many got it; raises if all failed (retry later)."""
    subs = list(db.scalars(select(PushSubscription).where(PushSubscription.user_id == user.id)))
    delivered, errors = 0, []
    data = json.dumps(body, ensure_ascii=False)
    for sub in subs:
        try:
            if webpush.deliver(sub, data, urgent=bool(body.get("urgent"))):
                sub.last_ok_at = now().replace(tzinfo=None)
                delivered += 1
            else:
                db.delete(sub)  # unsubscribed / expired on the device
        except webpush.PushError as exc:
            errors.append(exc)
    db.commit()
    if not delivered and errors:
        raise errors[0]
    return delivered


RANK_KIND = {0: "overdue", 1: "today", 2: "task", 3: "tomorrow"}


def record(db: Session, d: Digest, today: date) -> None:
    for ln in d.lines:
        kind = RANK_KIND[ln.rank]
        for t in ln.tasks:
            db.add(NotificationLog(user_id=d.user.id, task_id=t.id, kind=kind, ref_date=ln.due, sent_on=today))


def run_slot(db: Session, slot: str, today: date) -> int:
    """Send the slot's digests to users who have not had it today. Returns messages sent."""
    sent = 0
    for d in build_digests(db, slot, today):
        if _already(db, d.user, slot, task_id=None, ref=None, since=today):
            continue
        if d.lines:
            try:
                send(db, d.user, payload(d))
            except webpush.PushError as exc:
                log.warning("push: %s – %s", d.user.name, exc)
                continue  # try again on the next tick
            record(db, d, today)
            sent += 1
        db.add(NotificationLog(user_id=d.user.id, task_id=None, kind=slot, sent_on=today))
        db.commit()
    return sent


def tick(db: Session, now: datetime) -> None:
    """Scheduler entry point (every few minutes): autopay, then any slot whose hour has come.

    Catch-up is built in – after a restart at 9:30 the morning digest still goes out.
    """
    s = get_settings()
    today = now.date()
    services.settle_autopay(db, today)
    if s.morning_hour <= now.hour < s.evening_hour:
        run_slot(db, "morning", today)
    if now.hour >= s.evening_hour:
        run_slot(db, "evening", today)


def send_test(db: Session, user: User, digest: Digest | None) -> int:
    if digest and digest.lines:
        body = payload(digest)
        body["title"] = "[próba] " + body["title"]
    else:
        body = {
            "title": "Domownik: próba",
            "body": "Powiadomienia działają. Rano dostaniesz tu skrót terminów.",
            "url": "/",
            "tag": "domownik-test",
            "actions": [],
        }
    return send(db, user, body)
