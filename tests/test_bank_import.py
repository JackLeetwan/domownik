"""Bank statement import: parsing an mBank-like CSV, proposals (dup / task / income / rule / bank category),
booking, learning rules and never offering a line twice."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from domownik import bankimport
from domownik.models import (
    BankLine,
    BankRule,
    BudgetCategory,
    Completion,
    Item,
    RecurringIncome,
    Task,
    Transaction,
)

MBANK = """mBank S.A. Bankowość Detaliczna;
Lista operacji;

#Za okres:;01.11.2026;30.11.2026;
#Waluta;#Wpływy;#Wydatki;
PLN;12 000,00;-3 000,00;

#Data operacji;#Opis operacji;#Rachunek;#Kategoria;#Kwota;
2026-11-03;"ZAKUP PRZY UŻYCIU KARTY BIEDRONKA 1234 WARSZAWA";"eKonto 1111";"Żywność i chemia domowa";-54,90 PLN;
2026-11-03;"ZAKUP PRZY UŻYCIU KARTY BIEDRONKA 1234 WARSZAWA";"eKonto 1111";"Żywność i chemia domowa";-54,90 PLN;
2026-11-10;"PRZELEW WYCHODZĄCY LEASING RATA 3";"eKonto 1111";"Opłaty";-1 250,00 PLN;
2026-11-10;"PRZELEW PRZYCHODZĄCY WYNAGRODZENIE";"eKonto 1111";"Wpływy";8 000,00 PLN;
2026-11-12;"ZAKUP PRZY UŻYCIU KARTY APTEKA POD LIPAMI";"eKonto 1111";"Zdrowie i uroda";-32,10 PLN;
2026-11-13;"PRZELEW WŁASNY NA OSZCZĘDNOŚCI";"eKonto 1111";"Przelew własny";-500,00 PLN;
2026-11-14;"ZAKUP PRZY UŻYCIU KARTY ORLEN 77";"eKonto 1111";"Paliwo";-250,00 PLN;
;;;#Saldo końcowe;10 000,00 PLN;
"""


def raw(text: str = MBANK) -> bytes:
    return text.encode("cp1250")


def test_parse_mbank_and_generic():
    lines = bankimport.parse_statement(raw())
    assert len(lines) == 7
    first = lines[0]
    assert first.booked_on == date(2026, 11, 3) and first.amount_gr == -5490
    assert "BIEDRONKA" in first.description and first.bank_category == "Żywność i chemia domowa"
    assert lines[0].ref != lines[1].ref  # two identical purchases are two lines
    assert lines[3].amount_gr == 800000
    generic = bankimport.parse_statement(b'Data,Opis,Kwota\n03.11.2026,Kawiarnia,"-12,50"\n')
    assert generic[0].amount_gr == -1250 and generic[0].booked_on == date(2026, 11, 3)
    with pytest.raises(bankimport.StatementError):
        bankimport.parse_statement(b"cos;innego\n1;2\n")


def test_amounts_and_merchant_key():
    assert bankimport.parse_amount("-1 234,56 PLN") == -123456
    assert bankimport.parse_amount("1.234,50") == 123450
    assert bankimport.parse_amount("12.5") == 1250
    assert bankimport.parse_amount("abc") is None
    assert bankimport.merchant_key("ZAKUP PRZY UŻYCIU KARTY BIEDRONKA 1234 WARSZAWA") == "biedronka warszawa"
    assert bankimport.merchant_key("Płatność BLIK · Allegro") == "allegro"


@pytest.fixture
def setup(db):
    food = BudgetCategory(name="Jedzenie", kind="variable")
    health = BudgetCategory(name="Zdrowie", kind="variable")
    fuel = BudgetCategory(name="Paliwo", kind="variable")
    salary_cat = BudgetCategory(name="Pensja", kind="income")
    car = Item(name="Auto", kind="car")
    db.add_all([food, health, fuel, salary_cat, car])
    db.commit()
    lease = Task(
        item_id=car.id,
        title="Leasing – rata",
        category="payment",
        recurrence={"mode": "interval", "every": 1, "unit": "month", "anchor": "due"},
        next_due_date=date(2026, 11, 10),
        lead_days=3,
        amount_gr=125000,
        direction="expense",
    )
    salary = RecurringIncome(
        name="Pensja Jana", amount_gr=800000, day=10, starts_on=date(2026, 1, 1), budget_category_id=salary_cat.id
    )
    # the first Biedronka was entered by hand already
    manual = Transaction(booked_on=date(2026, 11, 3), amount_gr=-5490, budget_category_id=food.id, note="zakupy")
    db.add_all([lease, salary, manual, BankRule(pattern="apteka", budget_category_id=health.id)])
    db.commit()
    return {"food": food, "health": health, "fuel": fuel, "lease": lease, "salary": salary, "manual": manual}


def test_proposals(db, setup):
    pv = bankimport.propose(db, bankimport.parse_statement(raw()), date(2026, 11, 20))
    by_desc = [(p.line.description, p.choice) for p in pv.proposals]
    choices = [c for _, c in by_desc]
    assert choices[0] == f"dup:{setup['manual'].id}"
    assert choices[1] != choices[0]  # the 2nd identical purchase is not the same dup again
    assert f"task:{setup['lease'].id}" in choices
    assert f"inc:{setup['salary'].id}" in choices
    apteka = next(p for p in pv.proposals if "APTEKA" in p.line.description)
    assert apteka.choice == f"cat:{setup['health'].id}" and apteka.learned
    own = next(p for p in pv.proposals if "WŁASNY" in p.line.description)
    assert own.choice == "skip"
    orlen = next(p for p in pv.proposals if "ORLEN" in p.line.description)
    assert orlen.choice == f"cat:{setup['fuel'].id}"  # the bank's category "Paliwo" names ours


def test_preview_and_save(auth_client, db, setup):
    page = auth_client.post("/budzet/import", files={"file": ("historia.csv", raw(), "text/csv")})
    assert page.status_code == 200 and "Zapisz w budżecie" in page.text and "Leasing – rata" in page.text
    lines = bankimport.parse_statement(raw())
    pv = bankimport.propose(db, lines, date(2026, 11, 20))
    form = {"remember": "1"}
    for i, p in enumerate(pv.proposals):
        ln = p.line
        choice = p.choice
        if "BIEDRONKA" in ln.description and choice == "skip":
            choice = f"cat:{setup['food'].id}"  # picked by hand → learned
        form |= {
            f"ref_{i}": ln.ref,
            f"d_{i}": ln.booked_on.isoformat(),
            f"a_{i}": str(ln.amount_gr),
            f"t_{i}": ln.description,
            f"p_{i}": p.choice,
            f"c_{i}": choice,
        } | ({f"r_{i}": "1"} if p.learned else {})
    r = auth_client.post("/budzet/import/zapisz", data=form)
    assert r.status_code == 200 and "Zaimportowano" in r.text

    db.expire_all()
    lease = db.get(Task, setup["lease"].id)
    assert lease.next_due_date == date(2026, 12, 10)
    comp = db.scalar(select(Completion).where(Completion.task_id == lease.id))
    assert comp.actual_amount_gr == 125000 and comp.done_on == date(2026, 11, 10)
    assert db.scalar(select(Transaction).where(Transaction.income_id == setup["salary"].id)).amount_gr == 800000
    food_tx = db.scalars(select(Transaction).where(Transaction.budget_category_id == setup["food"].id)).all()
    assert len(food_tx) == 2  # the hand-entered one + the second purchase, not three
    assert (
        db.scalar(select(BankRule).where(BankRule.pattern == "biedronka warszawa")).budget_category_id
        == setup["food"].id
    )
    assert db.scalar(select(func.count(BankLine.ref))) == 7

    # the same file again: nothing new
    again = auth_client.post("/budzet/import", files={"file": ("historia.csv", raw(), "text/csv")})
    assert "Nic nowego" in again.text


def test_bad_file_and_rules_page(auth_client, db, setup):
    r = auth_client.post("/budzet/import", files={"file": ("x.csv", b"nic tu nie ma", "text/csv")})
    assert r.status_code == 200 and "nagłówka" in r.text
    page = auth_client.get("/budzet/import").text
    assert "apteka" in page
    rule = db.scalar(select(BankRule))
    auth_client.post(f"/budzet/import/reguly/{rule.id}/usun")
    db.expire_all()
    assert db.scalar(select(func.count(BankRule.id))) == 0
    assert "Import z banku" in auth_client.get("/budzet").text


def test_migration_0025(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()
        with conn.begin():
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            assert "item_id" in {r[1] for r in conn.execute(sa.text('PRAGMA table_info("recurring_income")'))}
            command.downgrade(cfg, "8f2d6a4c0e93")
            assert "item_id" not in {r[1] for r in conn.execute(sa.text('PRAGMA table_info("recurring_income")'))}
            command.upgrade(cfg, "head")


def test_card_purchase_is_not_a_bill(db):
    """Fuel for exactly the amount of some payment is not that payment – a card purchase needs a common word."""
    item = Item(name="Dom", kind="house")
    food = BudgetCategory(name="Jedzenie", kind="variable")
    db.add_all([item, food])
    db.commit()
    db.add(
        Task(
            item_id=item.id,
            title="Zajęcia sportowe",
            category="payment",
            recurrence={"mode": "once"},
            next_due_date=date(2026, 11, 10),
            amount_gr=26000,
        )
    )
    db.commit()
    text = (
        "#Data operacji;#Opis operacji;#Kategoria;#Kwota;\n"
        '2026-11-09;"ZAKUP PRZY UŻYCIU KARTY ORLEN 12";"Paliwo";-260,00;\n'
        '2026-11-09;"ZAKUP PRZY UŻYCIU KARTY LIDL";"Żywność i chemia domowa";-40,00;\n'
    )
    pv = bankimport.propose(db, bankimport.parse_statement(text.encode()), date(2026, 11, 20))
    assert pv.proposals[0].choice == "skip"
    assert pv.proposals[1].choice == f"cat:{food.id}"  # mBank's "Żywność…" → our "Jedzenie"
