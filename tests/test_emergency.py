"""'Na wypadek': hand-written entries, what the app gathers by itself, printout, freshness, reminder, privacy."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from domownik import emergency
from domownik.config import now, today
from domownik.models import (
    BudgetCategory,
    Contact,
    EmergencyEntry,
    EmergencyPrint,
    Item,
    RecurringIncome,
    Task,
    Todo,
    User,
)
from domownik.security import hash_password

MONTHLY = {"mode": "interval", "every": 1, "unit": "month", "anchor": "due"}
YEARLY = {"mode": "interval", "every": 1, "unit": "year", "anchor": "due"}


@pytest.fixture
def home(db):
    money = Item(name="Finanse", kind="group")
    cars = Item(name="Samochody", kind="group")
    db.add_all([money, cars])
    db.flush()
    car = Item(name="Kombi", kind="car", parent_id=cars.id)
    loans = BudgetCategory(name="Kredyt i raty", kind="fixed")
    db.add_all([car, loans])
    db.flush()
    due = today() + timedelta(days=20)
    db.add_all(
        [
            Task(
                item_id=money.id,
                title="Polisa na życie",
                category="insurance",
                recurrence=MONTHLY,
                next_due_date=due,
                amount_gr=15000,
                notes="Uposażeni: dzieci po połowie",
            ),
            Task(
                item_id=car.id, title="OC", category="insurance", recurrence=YEARLY, next_due_date=due, amount_gr=60000
            ),
            Task(
                item_id=money.id,
                title="Hipoteka",
                category="payment",
                recurrence=MONTHLY,
                next_due_date=due,
                amount_gr=300000,
                budget_category_id=loans.id,
            ),
            Task(item_id=money.id, title="Leasing laptopa", category="payment", recurrence=MONTHLY, next_due_date=due),
            Task(
                item_id=money.id,
                title="Serwis streamingowy",
                category="payment",
                recurrence=MONTHLY,
                next_due_date=due,
                amount_gr=4900,
                autopay=True,
            ),
            Task(item_id=car.id, title="Wymiana oleju", category="service", recurrence=YEARLY, next_due_date=due),
            RecurringIncome(name="Pensja", amount_gr=700000, day=10, starts_on=today().replace(day=1)),
            Contact(name="Biuro rachunkowe", role="Inne", phones="600 100 200"),
        ]
    )
    db.commit()
    return money


def test_overview_sorts_what_the_app_knows(db, home):
    o = emergency.overview(db, today())
    assert {r.title for r in o.insurance} == {"Polisa na życie", "OC"}
    assert {r.title for r in o.debts} == {"Hipoteka", "Leasing laptopa"}  # by category and by title
    (bill,) = o.bills
    assert bill.title == "Serwis streamingowy" and bill.flag == "schodzi samo z konta"
    assert [r.title for r in o.income] == ["Pensja"] and [r.title for r in o.property] == ["Kombi"]
    assert "Wymiana oleju" not in {r.title for r in o.insurance + o.debts + o.bills}
    life = next(r for r in o.insurance if r.title == "Polisa na życie")
    assert (
        life.where == "Finanse" and "150 zł" in life.detail.replace("\xa0", " ") and life.notes.startswith("Uposażeni")
    )


def test_page_entries_and_secrets(auth_client, db, home):
    page = auth_client.get("/na-wypadek")
    assert page.status_code == 200 and "Teczka nie była jeszcze drukowana" in page.text
    assert "Polisa na życie" in page.text and "Biuro rachunkowe" in page.text and "schodzi samo z konta" in page.text

    r = auth_client.post(
        "/na-wypadek/wpisy", data={"section": "where", "title": "Testament", "body": "Segregator w szafie"}
    )
    assert r.status_code in (200, 303)
    e = db.query(EmergencyEntry).one()
    eid = e.id
    assert (e.section, e.title, e.body) == ("where", "Testament", "Segregator w szafie")

    auth_client.post(f"/na-wypadek/wpisy/{e.id}", data={"title": "Testament", "body": "U notariusza w centrum"})
    db.expire_all()
    assert db.get(EmergencyEntry, e.id).body == "U notariusza w centrum"

    for body in ("hasło: Kot123!", "PIN = 4821", "karta 4111 1111 1111 1111"):
        bad = auth_client.post("/na-wypadek/wpisy", data={"section": "digital", "title": "Bank", "body": body})
        assert bad.status_code == 400 and "nie wpisuj" in bad.text
    assert db.query(EmergencyEntry).count() == 1
    ok = auth_client.post(
        "/na-wypadek/wpisy", data={"section": "digital", "title": "Hasła", "body": "PIN do telefonu zna Ola"}
    )
    assert ok.status_code in (200, 303) and db.query(EmergencyEntry).count() == 2

    assert auth_client.post("/na-wypadek/wpisy", data={"section": "zle", "title": "X"}).status_code == 400
    auth_client.post(f"/na-wypadek/wpisy/{eid}/usun")
    db.expire_all()
    assert db.get(EmergencyEntry, eid) is None
    assert auth_client.post("/na-wypadek/wpisy/9999/usun").status_code == 404


def test_print_freshness_and_dashboard(auth_client, db, home):
    auth_client.post(
        "/na-wypadek/wpisy", data={"section": "people", "title": "Opiekun dla dzieci", "body": "Ciocia Ola"}
    )
    r = auth_client.post("/na-wypadek/druk", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/na-wypadek/druk?drukuj=1"
    paper = auth_client.get("/na-wypadek/druk?drukuj=1").text
    assert "Co zrobić po kolei" in paper and "Ciocia Ola" in paper and "Polisa na życie" in paper
    assert "window.print()" in paper and "bottombar" not in paper  # no app shell on paper
    assert "SD-Z2" in paper and "6 miesięcy" in paper

    assert "Wydruk jest aktualny" in auth_client.get("/na-wypadek").text
    assert "Od wydruku teczki" not in auth_client.get("/").text

    # everything so far happened an hour ago (created_at has whole seconds only)
    for table, col in (("task", "created_at"), ("contact", "created_at"), ("emergency_entry", "updated_at")):
        db.execute(sa.text(f"UPDATE {table} SET {col} = datetime({col}, '-1 hour')"))
    p = db.query(EmergencyPrint).one()
    p.printed_at -= timedelta(minutes=30)
    db.commit()
    db.add(
        Task(
            item_id=home.id,
            title="Kredyt gotówkowy",
            category="payment",
            recurrence=MONTHLY,
            next_due_date=today() + timedelta(days=5),
        )
    )
    db.commit()
    fresh = emergency.freshness(db, now())
    assert fresh.changes == ["Kredyt gotówkowy"] and fresh.needs_print
    assert "Kredyt gotówkowy" in auth_client.get("/na-wypadek").text
    assert "Od wydruku teczki „Na wypadek”: 1 zmiana" in auth_client.get("/").text

    p.printed_at -= timedelta(days=400)
    db.commit()
    assert emergency.freshness(db, now()).too_old


def test_yearly_reminder_once_in_the_finance_zone(auth_client, db, home):
    auth_client.post("/na-wypadek/przypomnienie")
    auth_client.post("/na-wypadek/przypomnienie")
    (t,) = db.query(Task).filter(Task.title == emergency.REVIEW_TITLE).all()
    assert t.item_id == home.id and t.recurrence["unit"] == "year" and t.next_due_date.year == today().year + 1
    assert "Przypomnienie o przeglądzie" in auth_client.get("/na-wypadek").text


def test_prepare_goes_to_todos_once(auth_client, db, home):
    auth_client.post("/na-wypadek/przygotuj/testament")
    auth_client.post("/na-wypadek/przygotuj/testament")
    (td,) = db.query(Todo).all()
    assert td.title.startswith("Spisać testament")
    assert "w „Do zrobienia”" in auth_client.get("/na-wypadek").text
    assert auth_client.post("/na-wypadek/przygotuj/nic").status_code == 404


def test_someone_elses_private_task_stays_out(auth_client, db, home):
    other = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    db.add(other)
    db.commit()
    db.add(
        Task(
            item_id=home.id,
            title="Prywatna polisa",
            category="insurance",
            recurrence=YEARLY,
            next_due_date=today(),
            private_to_id=other.id,
        )
    )
    db.commit()
    assert "Prywatna polisa" not in auth_client.get("/na-wypadek").text
    assert "Prywatna polisa" not in auth_client.get("/na-wypadek/druk").text


def test_secret_detector():
    assert emergency.looks_secret("Hasło: abc") and emergency.looks_secret("pin=1234")
    assert not emergency.looks_secret("Hasła są w Bitwardenie, dostęp awaryjny ma Ola")
    assert not emergency.looks_secret("Konto 12 1140 2004 0000 3502 1234 5678")  # an account number is fine


def test_menu_has_the_page(auth_client):
    assert 'href="/na-wypadek"' in auth_client.get("/").text


def test_migration_0028(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn, conn.begin():
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        assert {"emergency_entry", "emergency_print"} <= set(sa.inspect(conn).get_table_names())
        command.downgrade(cfg, "7c1f3a9d2e48")
        assert "emergency_entry" not in set(sa.inspect(conn).get_table_names())
        command.upgrade(cfg, "head")
