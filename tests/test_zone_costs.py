"""Yearly zone costs: task payments, entries with a zone, incomes with a zone, savings excluded."""

from __future__ import annotations

from datetime import date

from domownik import zonecosts
from domownik.config import today
from domownik.models import BudgetCategory, Item, RecurringIncome, SavingsGoal, Task, Transaction


def _setup(db):
    y = today().year
    car_zone = Item(name="Samochody", kind="group")
    flat = Item(name="Mieszkanie wynajęte", kind="rental")
    db.add_all([car_zone, flat])
    db.flush()
    car = Item(name="Auto 1", kind="car", parent_id=car_zone.id)
    fuel = BudgetCategory(name="Paliwo", kind="variable")
    ins = BudgetCategory(name="Ubezpieczenia", kind="planned")
    rent_cat = BudgetCategory(name="Najem", kind="income")
    food = BudgetCategory(name="Jedzenie", kind="variable")
    db.add_all([car, fuel, ins, rent_cat, food])
    db.flush()
    oc = Task(
        item_id=car.id,
        title="OC",
        category="insurance",
        recurrence={"mode": "once"},
        next_due_date=date(y, 12, 1),
        amount_gr=120000,
    )
    rent = RecurringIncome(name="Czynsz najemcy", amount_gr=250000, day=10, starts_on=date(y, 1, 1), item_id=flat.id)
    goal = SavingsGoal(name="Wakacje", target_gr=100000)
    db.add_all([oc, rent, goal])
    db.flush()
    db.add_all(
        [
            Transaction(booked_on=date(y, 3, 5), amount_gr=-120000, budget_category_id=ins.id, task_id=oc.id),
            Transaction(booked_on=date(y, 3, 9), amount_gr=-30000, budget_category_id=fuel.id, item_id=car.id),
            Transaction(booked_on=date(y - 1, 6, 1), amount_gr=-100000, budget_category_id=fuel.id, item_id=car.id),
            Transaction(booked_on=date(y, 2, 10), amount_gr=250000, budget_category_id=rent_cat.id, income_id=rent.id),
            Transaction(booked_on=date(y, 2, 12), amount_gr=-40000, item_id=flat.id),
            Transaction(booked_on=date(y, 2, 13), amount_gr=-5000, budget_category_id=food.id),
            Transaction(booked_on=date(y, 2, 14), amount_gr=-50000, goal_id=goal.id),
        ]
    )
    db.commit()
    return y, car_zone, flat


def test_year_costs(db):
    y, _, _ = _setup(db)
    zones, rest = zonecosts.year_costs(db, y)
    assert [z.item.name for z in zones] == ["Samochody", "Mieszkanie wynajęte"]
    cars = zones[0]
    assert cars.expense_gr == 150000 and cars.prev_expense_gr == 100000 and cars.months[2] == 150000
    assert cars.by_item[0].name == "Auto 1" and cars.by_item[0].gr == 150000
    assert {p.name for p in cars.by_category} == {"Ubezpieczenia", "Paliwo"}
    rental = zones[1]
    assert rental.income_gr == 250000 and rental.expense_gr == 40000 and rental.net_gr == 210000
    assert rest.expense_gr == 5000  # the savings deposit is not a cost


def test_pages(auth_client, db):
    y, _, flat = _setup(db)
    page = auth_client.get(f"/budzet/strefy?y={y}")
    assert page.status_code == 200 and "Samochody" in page.text and "Bez strefy" in page.text
    assert "📊 Koszty stref" in auth_client.get("/budzet").text
    zone_page = auth_client.get(f"/items/{flat.id}").text
    assert f"W {y}: wydano" in zone_page and "wpływy" in zone_page


def test_income_zone_saved_and_booked(auth_client, db):
    flat = Item(name="Mieszkanie", kind="rental")
    db.add(flat)
    db.commit()
    auth_client.post("/budzet/dochody", data={"name": "Najem", "amount": "2500", "day": "10", "item_id": flat.id})
    inc = db.query(RecurringIncome).one()
    assert inc.item_id == flat.id
    auth_client.post(f"/budzet/dochody/{inc.id}/wplynelo", data={"m": today().strftime("%Y-%m")})
    db.expire_all()
    assert db.query(Transaction).one().item_id == flat.id
