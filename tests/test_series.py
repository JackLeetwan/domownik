"""Short task form, series with an end (instalments), "Przełóż"."""

from datetime import timedelta

from sqlalchemy import select

from domownik import notify, services
from domownik.config import today
from domownik.domain.tree import Tree
from domownik.models import Completion, Item, Task, User
from domownik.security import hash_password


def _item(db, name="Dom", kind="house") -> Item:
    item = Item(name=name, kind=kind)
    db.add(item)
    db.commit()
    return item


def _series(db, item, *, count=6, done=0, due=None, **kw) -> Task:
    t = Task(
        item_id=item.id,
        title="Pralka",
        category="payment",
        recurrence={"mode": "monthly", "day": (due or today()).day, "count": count, "done": done},
        next_due_date=due or today(),
        direction="expense",
        amount_gr=50000,
        **kw,
    )
    db.add(t)
    db.commit()
    return t


def _form(item, **kw) -> dict:
    return {"title": "Zadanie", "item_id": item.id, "freq": "once", "end": "never", "direction": "expense"} | kw


# ---------------------------------------------------------------- form


def test_new_form_defaults_one_off_with_date_and_details_closed(auth_client, db):
    item = _item(db)
    page = auth_client.get(f"/tasks/new?item={item.id}").text
    assert '<option value="once" selected>' in page
    assert 'name="next_due_date" value="" required' in page
    assert '<details class="more" >' in page  # "Więcej szczegółów" collapsed
    assert "Pozycja w budżecie" in page and "dni wcześniej" in page
    assert '<option value="km"' not in page  # mileage only for cars


def test_new_form_in_payments_zone_defaults_monthly(auth_client, db):
    zone = _item(db, "Opłaty", "group")
    for n in range(2):
        db.add(
            Task(item_id=zone.id, title=f"Rachunek {n}", category="payment", recurrence={"mode": "monthly", "day": 10})
        )
    db.commit()
    page = auth_client.get(f"/tasks/new?item={zone.id}").text
    assert '<option value="month" selected>' in page and '<option value="payment" selected>' in page


def test_car_form_offers_mileage(auth_client, db):
    car = _item(db, "Auto 1", "car")
    page = auth_client.get(f"/tasks/new?item={car.id}").text
    assert '<option value="km"' in page and 'name="next_due_km"' in page


def test_create_short_form_without_details(auth_client, db):
    item = _item(db)
    due = today() + timedelta(days=5)
    r = auth_client.post(
        "/tasks/new",
        data=_form(item, title="Przegląd kotła", freq="year", next_due_date=due.isoformat(), amount="300"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    t = db.scalar(select(Task).where(Task.title == "Przegląd kotła"))
    assert t.recurrence == {"mode": "fixed", "dates": [f"{due.month:02d}-{due.day:02d}"]}
    assert t.next_due_date == due and t.amount_gr == 30000 and t.direction == "expense"
    assert t.lead_days == 7 and t.category == "other" and t.assignee_id is None and t.active


def test_create_with_details(auth_client, db, user):
    item = _item(db)
    due = today() + timedelta(days=3)
    auth_client.post(
        "/tasks/new",
        data=_form(
            item,
            title="Czynsz",
            freq="month",
            next_due_date=due.isoformat(),
            amount="1 000",
            direction="income",
            category="income",
            lead_days="2",
            assignee_id=str(user.id),
            autopay="1",
            notes="od najemcy",
        ),
    )
    t = db.scalar(select(Task).where(Task.title == "Czynsz"))
    assert t.recurrence == {"mode": "monthly", "day": due.day}
    assert (t.direction, t.amount_gr, t.lead_days, t.assignee_id, t.autopay) == ("income", 100000, 2, user.id, True)
    # editing: "Więcej szczegółów" opens because something in it differs from the defaults
    page = auth_client.get(f"/tasks/{t.id}/edit").text
    assert '<details class="more" open>' in page and '<option value="month" selected>' in page


def test_date_is_required_in_short_form(auth_client, db):
    item = _item(db)
    r = auth_client.post("/tasks/new", data=_form(item, freq="month"))
    assert r.status_code == 422 and "kiedy" in r.text.lower()
    assert db.scalar(select(Task)) is None


def test_custom_rule_and_series_end(auth_client, db):
    item = _item(db)
    due = today() + timedelta(days=1)
    auth_client.post(
        "/tasks/new",
        data=_form(
            item,
            title="Filtr",
            freq="custom",
            mode="interval",
            every="2",
            unit="week",
            anchor="completion",
            next_due_date=due.isoformat(),
            end="count",
            count="3",
        ),
    )
    t = db.scalar(select(Task).where(Task.title == "Filtr"))
    assert t.recurrence == {
        "mode": "interval",
        "every": 2,
        "unit": "week",
        "anchor": "completion",
        "count": 3,
        "done": 0,
    }
    r = auth_client.post("/tasks/new", data=_form(item, freq="month", next_due_date=due.isoformat(), end="count"))
    assert r.status_code == 422 and "ile razy" in r.text


def test_edit_keeps_series_counter(auth_client, db):
    item = _item(db)
    t = _series(db, item, done=2)
    auth_client.post(
        f"/tasks/{t.id}/edit",
        data=_form(
            item, title="Pralka", freq="month", next_due_date=t.next_due_date.isoformat(), end="count", count="6"
        ),
    )
    db.expire_all()
    assert db.get(Task, t.id).recurrence["done"] == 2


def test_preview_waits_for_date(auth_client):
    r = auth_client.get("/tasks/preview", params={"freq": "month", "end": "never"})
    assert "<strong>co miesiąc</strong>" in r.text and "wpisz datę" in r.text and "kolejne:" not in r.text
    due = today()
    r = auth_client.get(
        "/tasks/preview", params={"freq": "month", "end": "count", "count": "6", "next_due_date": due.isoformat()}
    )
    assert "6 razy" in r.text and "kolejne" in r.text and "ostatni" in r.text


# ---------------------------------------------------------------- series


def test_series_number_last_instalment_and_undo(auth_client, db):
    item = _item(db)
    t = _series(db, item, done=4)
    assert "rata 5/6" in auth_client.get(f"/tasks/{t.id}").text
    assert "rata 5/6" in auth_client.get(f"/items/{item.id}").text
    auth_client.post(f"/tasks/{t.id}/done", data={"done_on": today().isoformat()})
    db.expire_all()
    t = db.get(Task, t.id)
    assert t.active and t.recurrence["done"] == 5
    modal = auth_client.get(f"/tasks/{t.id}").text
    assert "rata 6/6" in modal and "ostatni termin tej serii" in modal
    last_due = t.next_due_date
    auth_client.post(f"/tasks/{t.id}/done", data={"done_on": today().isoformat()})
    db.expire_all()
    t = db.get(Task, t.id)
    assert not t.active and t.next_due_date is None and t.recurrence["done"] == 6
    # nothing more on the timeline
    data = auth_client.get(
        "/api/timeline", params={"start": today().isoformat(), "end": (today() + timedelta(days=400)).isoformat()}
    ).json()
    assert not [p for p in data["points"] if p["task_id"] == t.id and p["kind"] == "due"]
    # undo of the last instalment brings the series back
    comp = db.scalar(select(Completion).where(Completion.task_id == t.id).order_by(Completion.id.desc()))
    auth_client.post(f"/completions/{comp.id}/undo")
    db.expire_all()
    t = db.get(Task, t.id)
    assert t.active and t.next_due_date == last_due and t.recurrence["done"] == 5


def test_skip_uses_up_an_instalment(auth_client, db):
    item = _item(db)
    t = _series(db, item, count=2)
    auth_client.post(f"/tasks/{t.id}/skip")
    db.expire_all()
    t = db.get(Task, t.id)
    assert t.active and t.recurrence["done"] == 1 and db.scalar(select(Completion)) is None
    auth_client.post(f"/tasks/{t.id}/skip")
    db.expire_all()
    assert db.get(Task, t.id).active is False


def test_timeline_points_are_numbered(auth_client, db):
    item = _item(db)
    t = _series(db, item, count=3, due=today() + timedelta(days=1))
    data = services.timeline(db, Tree.load(db), today(), today(), today() + timedelta(days=400))
    titles = [p["title"] for p in data["points"] if p["task_id"] == t.id]
    assert titles == ["Pralka (rata 1/3)", "Pralka (rata 2/3)", "Pralka (rata 3/3)"]


def test_notification_shows_instalment(db, user):
    item = _item(db)
    _series(db, item, done=2)
    (d,) = notify.build_digests(db, "morning", today(), preview_for=user)
    assert "Pralka (rata 3/6)" in d.message()


# ---------------------------------------------------------------- "Przełóż"


def test_postpone_moves_only_this_occurrence(auth_client, db):
    item = _item(db)
    due = today() + timedelta(days=2)
    t = Task(
        item_id=item.id,
        title="Czynsz",
        category="payment",
        recurrence={"mode": "monthly", "day": due.day},
        next_due_date=due,
        direction="expense",
    )
    db.add(t)
    db.commit()
    modal = auth_client.get(f"/tasks/{t.id}").text
    assert "Przełóż" in modal and "O tydzień" in modal
    new = due + timedelta(days=7)
    r = auth_client.post(f"/tasks/{t.id}/postpone", data={"to": new.isoformat()})
    assert r.status_code == 200
    db.expire_all()
    t = db.get(Task, t.id)
    assert t.next_due_date == new and t.recurrence["postponed_from"] == due.isoformat()
    assert db.scalar(select(Completion)) is None
    # done late on the new day: the next one is still on the usual day of the month
    auth_client.post(f"/tasks/{t.id}/done", data={"done_on": today().isoformat()})
    db.expire_all()
    t = db.get(Task, t.id)
    assert t.next_due_date.day == due.day and t.next_due_date > due and "postponed_from" not in t.recurrence


def test_postpone_rejects_past_and_counts_no_instalment(auth_client, db):
    item = _item(db)
    t = _series(db, item, done=1)
    r = auth_client.post(f"/tasks/{t.id}/postpone", data={"to": (today() - timedelta(days=1)).isoformat()})
    assert r.status_code == 422
    auth_client.post(f"/tasks/{t.id}/postpone", data={"to": (today() + timedelta(days=1)).isoformat()})
    db.expire_all()
    t = db.get(Task, t.id)
    assert t.next_due_date == today() + timedelta(days=1) and t.recurrence["done"] == 1


# ---------------------------------------------------------------- household member tiles


def test_member_tile_counts_tasks_of_the_linked_account(auth_client, db, user):
    other = User(email="ala@example.com", name="Ala", password_hash=hash_password("tajnehaslo2"))
    db.add(other)
    db.flush()
    home = _item(db)
    me = Item(name="Jan", kind="member", user_id=user.id)
    db.add(me)
    db.add(
        Task(item_id=home.id, title="Śmieci", recurrence={"mode": "once"}, next_due_date=today(), assignee_id=user.id)
    )
    db.add(
        Task(item_id=home.id, title="Okna", recurrence={"mode": "once"}, next_due_date=today(), assignee_id=other.id)
    )
    db.commit()
    st = services.zone_stats(db, Tree.load(db), [me], today())[me.id]
    assert st.task_count == 1 and st.next_row.task.title == "Śmieci" and st.soon == 1
