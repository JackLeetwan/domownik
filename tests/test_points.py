"""Points & rewards: earned on ✓ (tasks, chores), approval by someone else, undo, rewards, points by hand."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from domownik import points, services
from domownik.config import today
from domownik.models import Chore, Item, PointEntry, Reward, Task, User
from domownik.security import hash_password

HX = {"HX-Request": "true"}


def _other(db) -> User:
    u = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    db.add(u)
    db.commit()
    return u


def _task(db, **kw) -> Task:
    item = Item(name="Dom", kind="house")
    db.add(item)
    db.commit()
    t = Task(
        item_id=item.id,
        title="Wynieść śmieci",
        category="other",
        recurrence={"mode": "interval", "every": 1, "unit": "week", "anchor": "due"},
        next_due_date=today(),
        lead_days=1,
        direction="none",
        **kw,
    )
    db.add(t)
    db.commit()
    return t


def test_parse_points():
    assert points.parse_points("") is None and points.parse_points("0") is None and points.parse_points("15") == 15
    for bad in ("abc", "-3", "99999"):
        try:
            points.parse_points(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_task_points_approved_at_once_and_undo(db, user):
    t = _task(db, points=10)
    comp = services.complete_task(db, t, user=user, done_on=today(), amount_gr=None, km=None, vendor=None, notes=None)
    assert points.balance_of(db, user).approved == 10
    services.undo_completion(db, comp)
    assert points.balance_of(db, user).approved == 0
    assert db.scalar(select(PointEntry)) is None


def test_approval_by_someone_else(auth_client, db, user):
    other = _other(db)
    t = _task(db, points=20, needs_approval=True)
    services.complete_task(db, t, user=other, done_on=today(), amount_gr=None, km=None, vendor=None, notes=None)
    b = points.balance_of(db, other)
    assert (b.approved, b.pending_plus) == (0, 20)

    page = auth_client.get("/punkty").text  # logged in as Jan
    assert "Do zatwierdzenia" in page and "Wynieść śmieci" in page
    assert 'Punkty<span class="count">1</span>' in page  # menu badge: one thing waits for my ✓
    e = db.scalar(select(PointEntry))
    r = auth_client.post(f"/punkty/{e.id}/zatwierdz", follow_redirects=False)
    assert r.status_code == 303
    db.refresh(e)
    assert e.status == "approved" and e.decided_by_id == user.id
    assert points.balance_of(db, other).approved == 20
    assert auth_client.post(f"/punkty/{e.id}/zatwierdz", headers=HX).status_code == 400  # already decided


def test_cannot_approve_own(auth_client, db, user):
    t = _task(db, points=5, needs_approval=True)
    services.complete_task(db, t, user=user, done_on=today(), amount_gr=None, km=None, vendor=None, notes=None)
    e = db.scalar(select(PointEntry))
    r = auth_client.post(f"/punkty/{e.id}/zatwierdz", headers=HX)
    assert r.status_code == 400 and "ktoś inny" in r.text
    assert "Do zatwierdzenia" not in auth_client.get("/punkty").text


def test_task_form_saves_points(auth_client, db):
    item = Item(name="Dom", kind="house")
    db.add(item)
    db.commit()
    auth_client.post(
        "/tasks/new",
        data={
            "title": "Pościelić łóżko",
            "item_id": item.id,
            "freq": "once",
            "next_due_date": "2030-01-01",
            "lead_days": "1",
            "points": "3",
            "needs_approval": "1",
        },
    )
    t = db.scalar(select(Task))
    assert (t.points, t.needs_approval) == (3, True)
    assert "⭐3" in auth_client.get(f"/items/{item.id}").text
    r = auth_client.post(
        f"/tasks/{t.id}/edit",
        data={"title": "X", "item_id": item.id, "freq": "once", "next_due_date": "2030-01-01", "points": "abc"},
        headers=HX,
    )
    assert r.status_code == 422


def test_chore_points_and_undo(auth_client, db, user):
    auth_client.post("/obowiazki", data={"name": "Odkurzanie", "every_days": "7", "points": "4"})
    c = db.scalar(select(Chore))
    assert c.points == 4
    auth_client.post(f"/obowiazki/{c.id}/zrobione")
    assert points.balance_of(db, user).approved == 4
    auth_client.post(f"/obowiazki/{c.id}/cofnij")
    db.expire_all()
    assert points.balance_of(db, user).approved == 0


def test_reward_redeem_flow(auth_client, db, user):
    other = _other(db)
    db.add(PointEntry(user_id=user.id, points=30, reason="start", status="approved"))
    db.commit()
    auth_client.post("/punkty/nagrody", data={"name": "Lody", "icon": "🍦", "cost": "25"})
    r = db.scalar(select(Reward))
    auth_client.post(f"/punkty/nagrody/{r.id}/odbierz")
    b = points.balance_of(db, user)
    assert (b.approved, b.pending_minus, b.available) == (30, 25, 5)
    again = auth_client.post(f"/punkty/nagrody/{r.id}/odbierz", headers=HX)
    assert again.status_code == 400 and "Za mało" in again.text

    e = db.scalar(select(PointEntry).where(PointEntry.reward_id == r.id))
    points.decide(e, other, approve=True)
    db.commit()
    assert points.balance_of(db, user).approved == 5


def test_give_points_by_hand(auth_client, db, user):
    other = _other(db)
    auth_client.post("/punkty/przyznaj", data={"user_id": other.id, "points": "7", "reason": "pomoc przy zakupach"})
    assert points.balance_of(db, other).approved == 7
    r = auth_client.post("/punkty/przyznaj", data={"user_id": user.id, "points": "7"}, headers=HX)
    assert r.status_code == 400


def test_autopay_gives_no_points(db, user):
    t = _task(db, points=10)
    t.autopay = True
    t.next_due_date = today() - timedelta(days=1)
    db.commit()
    services.settle_autopay(db, today())
    assert db.scalar(select(PointEntry)) is None
