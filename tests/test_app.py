from datetime import date, timedelta

from sqlalchemy import select

from domownik import services
from domownik.config import today
from domownik.domain.tree import Tree
from domownik.models import Completion, Item, Measurement, Task, Transaction


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_wrong_password_and_rate_limit(client, user):
    from tests.conftest import csrf_of

    token = csrf_of(client.get("/login").text)
    for _ in range(5):
        r = client.post("/login", data={"email": user.email, "password": "zle", "csrf": token})
        assert r.status_code == 401
    r = client.post("/login", data={"email": user.email, "password": "tajnehaslo1", "csrf": token})
    assert r.status_code == 429


def test_csrf_required(auth_client):
    r = auth_client.post("/setup/zones", headers={"X-CSRF-Token": "bad"})
    assert r.status_code == 403


def test_empty_dashboard_and_seed(auth_client, db):
    r = auth_client.get("/")
    assert "Utwórz domyślne strefy" in r.text
    r = auth_client.post("/setup/zones", follow_redirects=False)
    assert r.status_code == 303
    names = [i.name for i in db.scalars(select(Item).where(Item.parent_id.is_(None)))]
    assert names == ["Samochody", "Dom", "Mieszkanie wynajęte", "Dzieci", "Okazje"]
    okazje = db.scalar(select(Item).where(Item.name == "Okazje"))
    titles = {t.title for t in okazje.tasks}
    assert "Wielkanoc" in titles and "Boże Narodzenie – prezenty" in titles
    assert "Rocznica ślubu" not in titles  # needs a user date
    page = auth_client.get("/")
    assert "Samochody" in page.text and 'id="timeline"' in page.text


def _seed(auth_client):
    auth_client.post("/setup/zones")


def test_create_car_from_template(auth_client, db):
    _seed(auth_client)
    cars = db.scalar(select(Item).where(Item.name == "Samochody"))
    oc_due = today() + timedelta(days=20)
    r = auth_client.post(
        "/items/new/car",
        data={
            "parent_id": cars.id,
            "name": "Skoda",
            "icon": "🚗",
            "attr_plate": "WX 12345",
            "pick_0": "1",
            "title_0": "Ubezpieczenie OC",
            "due_0": oc_due.isoformat(),
            "amount_0": "1 350,50",
            "pick_3": "1",
            "title_3": "Wymiana oleju i filtrów",
            "due_3": "",
            "amount_3": "600",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    car = db.scalar(select(Item).where(Item.name == "Skoda"))
    assert car.parent_id == cars.id and car.attributes == {"plate": "WX 12345"}
    tasks = {t.title: t for t in car.tasks}
    assert set(tasks) == {"Ubezpieczenie OC", "Wymiana oleju i filtrów"}
    assert tasks["Ubezpieczenie OC"].next_due_date == oc_due
    assert tasks["Ubezpieczenie OC"].amount_gr == 135050
    assert tasks["Ubezpieczenie OC"].budget_category.name == "Ubezpieczenia"
    assert tasks["Wymiana oleju i filtrów"].recurrence["km"] == 15000

    page = auth_client.get(f"/items/{car.id}")
    assert "Ubezpieczenie OC" in page.text and "WX 12345" in page.text
    # zone tile on the dashboard shows the upcoming OC
    dash = auth_client.get("/")
    assert "Ubezpieczenie OC" in dash.text


def test_birthday_needs_date_and_lands_next_year_if_passed(auth_client, db):
    _seed(auth_client)
    kids = db.scalar(select(Item).where(Item.name == "Dzieci"))
    t = today()
    born = date(t.year - 4, 1, 2) if t > date(t.year, 1, 2) else date(t.year - 4, 12, 31)
    auth_client.post(
        "/items/new/child",
        data={
            "parent_id": kids.id,
            "name": "Zosia",
            "pick_0": "1",
            "title_0": "Urodziny",
            "due_0": born.isoformat(),
            "amount_0": "400",
        },
    )
    zosia = db.scalar(select(Item).where(Item.name == "Zosia"))
    bday = next(t for t in zosia.tasks if t.title == "Urodziny")
    assert bday.recurrence == {"mode": "fixed", "dates": [f"{born.month:02d}-{born.day:02d}"]}
    assert bday.next_due_date >= today()
    assert (bday.next_due_date.month, bday.next_due_date.day) == (born.month, born.day)


def test_complete_task_flow(auth_client, db, user):
    item = Item(name="Ekspres", kind="appliance")
    db.add(item)
    db.flush()
    due = today() - timedelta(days=3)
    task = Task(
        item_id=item.id,
        title="Odkamienianie",
        category="diy",
        recurrence={"mode": "interval", "every": 2, "unit": "month", "anchor": "completion"},
        next_due_date=due,
        amount_gr=4000,
        direction="expense",
    )
    db.add(task)
    db.commit()

    modal = auth_client.get(f"/tasks/{task.id}")
    assert "Odkamienianie" in modal.text and "3 dni temu" in modal.text

    r = auth_client.post(f"/tasks/{task.id}/done", data={"done_on": today().isoformat(), "amount": "45"})
    assert r.status_code == 200 and r.headers.get("HX-Refresh") == "true"
    db.expire_all()
    task = db.get(Task, task.id)
    from dateutil.relativedelta import relativedelta

    assert task.next_due_date == today() + relativedelta(months=2)
    comp = db.scalar(select(Completion).where(Completion.task_id == task.id))
    assert comp.due_date == due and comp.actual_amount_gr == 4500 and comp.performed_by_id == user.id
    tr = db.scalar(select(Transaction).where(Transaction.completion_id == comp.id))
    assert tr.amount_gr == -4500 and tr.item_id == item.id
    # history: expense with a minus, like the lists
    assert '<td class="h-amt">−45' in auth_client.get(f"/items/{item.id}").text

    # undo restores the previous due date and removes the transaction (cascade)
    auth_client.post(f"/completions/{comp.id}/undo")
    db.expire_all()
    assert db.get(Task, task.id).next_due_date == due
    assert db.scalar(select(Transaction)) is None


def test_complete_with_km_records_odometer_and_next_km(auth_client, db):
    car = Item(name="Auto", kind="car")
    db.add(car)
    db.flush()
    task = Task(
        item_id=car.id,
        title="Olej",
        category="service",
        recurrence={"mode": "interval", "every": 12, "unit": "month", "anchor": "completion", "km": 15000},
        next_due_date=today(),
        direction="none",
    )
    db.add(task)
    db.commit()
    auth_client.post(f"/tasks/{task.id}/done", data={"done_on": today().isoformat(), "km": "100000"})
    db.expire_all()
    assert db.get(Task, task.id).next_due_km == 115000
    m = db.scalar(select(Measurement).where(Measurement.item_id == car.id))
    assert m.value == 100000


def test_once_task_becomes_inactive(auth_client, db):
    item = Item(name="X", kind="other")
    db.add(item)
    db.flush()
    task = Task(item_id=item.id, title="Zebranie", recurrence={"mode": "once"}, next_due_date=today(), direction="none")
    db.add(task)
    db.commit()
    auth_client.post(f"/tasks/{task.id}/done", data={"done_on": today().isoformat()})
    db.expire_all()
    assert db.get(Task, task.id).active is False


def test_task_form_create_and_validation(auth_client, db):
    item = Item(name="Dom", kind="house")
    db.add(item)
    db.commit()
    bad = auth_client.post("/tasks/new", data={"title": "x", "item_id": item.id, "mode": "monthly", "day": "40"})
    assert bad.status_code == 422 and "1–31" in bad.text
    ok = auth_client.post(
        "/tasks/new",
        data={
            "title": "Rata kredytu",
            "item_id": item.id,
            "category": "payment",
            "mode": "monthly",
            "day": "5",
            "lead_days": "2",
            "direction": "expense",
            "amount": "2 100",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303
    t = db.scalar(select(Task).where(Task.title == "Rata kredytu"))
    assert t.recurrence == {"mode": "monthly", "day": 5} and t.amount_gr == 210000
    assert t.next_due_date.day == 5 and t.next_due_date >= today()


def test_preview(auth_client):
    r = auth_client.get("/tasks/preview", params={"mode": "fixed", "dates": "12-24"})
    assert "24 grudnia" in r.text
    r = auth_client.get("/tasks/preview", params={"mode": "monthly", "day": "99"})
    assert "error" in r.text


def test_timeline_api(auth_client, db):
    _seed(auth_client)
    t = today()
    r = auth_client.get("/api/timeline", params={"start": t.isoformat(), "end": (t + timedelta(days=400)).isoformat()})
    assert r.status_code == 200
    data = r.json()
    titles = {p["title"] for p in data["points"]}
    assert "Wielkanoc" in titles and "Dzień Matki" in titles
    assert data["groups"][0]["content"].endswith("Okazje")
    # every holiday occurs once a year → at least one point per holiday within 400 days
    xmas = [p for p in data["points"] if p["title"].startswith("Boże Narodzenie")]
    assert len(xmas) >= 1 and all(p["kind"] == "due" for p in xmas)


def test_zone_tree_and_breadcrumbs(auth_client, db):
    _seed(auth_client)
    dom = db.scalar(select(Item).where(Item.name == "Dom"))
    auth_client.post(
        "/items/new/coffee_machine",
        data={
            "parent_id": dom.id,
            "name": "Ekspres",
            "pick_0": "1",
            "title_0": "Odkamienianie ekspresu",
            "due_0": (today() - timedelta(days=1)).isoformat(),
            "amount_0": "40",
        },
    )
    page = auth_client.get(f"/items/{dom.id}")
    assert "Ekspres" in page.text and "Zadania w tej strefie" in page.text and "Odkamienianie ekspresu" in page.text
    dash = auth_client.get("/")
    assert "1 zaległe" in dash.text
    stats = services.zone_stats(db, Tree.load(db), [dom], today())
    assert stats[dom.id].overdue == 1


def test_move_item_into_own_child_rejected(auth_client, db):
    a = Item(name="A", kind="group")
    db.add(a)
    db.flush()
    b = Item(name="B", kind="group", parent_id=a.id)
    db.add(b)
    db.commit()
    r = auth_client.post(f"/items/{a.id}/edit", data={"name": "A", "parent_id": b.id})
    assert r.status_code == 400


def test_zone_offers_quick_add_tiles(auth_client, db):
    _seed(auth_client)
    cars = db.scalar(select(Item).where(Item.name == "Samochody"))
    assert cars.attributes == {"_suggest": ["car"]}
    page = auth_client.get(f"/items/{cars.id}")
    assert f'href="/items/new/car?parent={cars.id}"' in page.text and "＋ Samochód" in page.text
    assert "_suggest" not in page.text  # internal attribute stays hidden
    for name in ("Fiat", "Toyota"):
        auth_client.post("/items/new/car", data={"parent_id": cars.id, "name": name})
    page = auth_client.get(f"/items/{cars.id}")
    assert "Fiat" in page.text and "Toyota" in page.text


def test_quick_add_falls_back_for_zones_without_setting(auth_client, db):
    # zones created before suggestions existed: matched by default zone name, then by kind
    old = Item(name="Samochody", kind="group")
    house = Item(name="Domek letni", kind="house")
    db.add_all([old, house])
    db.commit()
    assert "＋ Samochód" in auth_client.get(f"/items/{old.id}").text
    assert "＋ Pompa ciepła" in auth_client.get(f"/items/{house.id}").text


def test_new_task_default_category(auth_client, db):
    from domownik.models import Item

    car = Item(name="Auto 1", kind="car")
    flat = Item(name="Mieszkanie 1", kind="apartment")
    db.add_all([car, flat])
    db.commit()
    page = auth_client.get(f"/tasks/new?item={car.id}").text
    assert '<option value="service" selected>' in page
    page = auth_client.get(f"/tasks/new?item={flat.id}").text
    assert '<option value="other" selected>' in page and '<option value="insurance" selected>' not in page
