"""Savings goals: create with a start amount, deposit / withdraw as budget transactions, monthly pace, close."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from domownik import goals
from domownik.config import today
from domownik.models import BudgetCategory, Item, SavingsGoal, Transaction

HX = {"HX-Request": "true"}


def test_months_and_pace():
    assert goals.months_between(date(2026, 11, 3), date(2026, 11, 30)) == 1
    assert goals.months_between(date(2026, 11, 3), date(2027, 6, 1)) == 8
    g = SavingsGoal(name="Wakacje", target_gr=600000, due_on=date(2027, 4, 30))
    v = goals.GoalView(g, 120000)
    assert v.percent == 20 and v.left_gr == 480000
    assert v.monthly_gr(date(2026, 11, 3)) == 80000  # 6 months incl. November
    assert goals.GoalView(g, 700000).reached and goals.GoalView(g, 700000).percent == 100


def test_create_deposit_withdraw_close(auth_client, db):
    page = auth_client.get("/budzet").text
    assert 'id="cele"' in page and "Nowy cel" in page

    r = auth_client.post(
        "/budzet/cele",
        data={"name": "Wakacje 2027", "icon": "🏖️", "target": "6000", "due_on": "2027-06-30", "start": "500"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("#cele")
    g = db.scalar(select(SavingsGoal))
    cat = db.scalar(select(BudgetCategory).where(BudgetCategory.name == "Oszczędności"))
    tx = db.scalar(select(Transaction))
    assert tx.goal_id == g.id and tx.amount_gr == -50000 and tx.budget_category_id == cat.id

    auth_client.post(f"/budzet/cele/{g.id}/wplata", data={"amount": "250,50", "kind": "in"})
    r = auth_client.post(f"/budzet/cele/{g.id}/wplata", data={"amount": "1000", "kind": "out"}, headers=HX)
    assert r.status_code == 400 and "więcej" in r.text
    auth_client.post(f"/budzet/cele/{g.id}/wplata", data={"amount": "50,50", "kind": "out"})
    assert goals.saved_amounts(db) == {g.id: 70000}

    page = auth_client.get("/budzet").text
    assert "Wakacje 2027" in page and "700 zł" in page.replace("\xa0", " ")
    assert "odkładaj ok." in page

    auth_client.post(f"/budzet/cele/{g.id}/zakoncz")
    db.refresh(g)
    assert g.done_on == today()
    assert "Zrealizowane (1)" in auth_client.get("/budzet").text


def test_validation_and_delete_keeps_money(auth_client, db):
    r = auth_client.post("/budzet/cele", data={"name": "", "target": "100"}, headers=HX)
    assert r.status_code == 400 and "Nazwij" in r.text
    r = auth_client.post("/budzet/cele", data={"name": "X", "target": "abc"}, headers=HX)
    assert r.status_code == 400
    assert db.scalar(select(SavingsGoal)) is None

    auth_client.post("/budzet/cele", data={"name": "Auto", "target": "20000", "start": "100"})
    g = db.scalar(select(SavingsGoal))
    auth_client.post(f"/budzet/cele/{g.id}/usun")
    tx = db.scalar(select(Transaction))
    assert db.scalar(select(SavingsGoal)) is None and tx is not None and tx.goal_id is None


def test_goal_on_dashboard(auth_client, db):
    db.add(Item(name="Dom", kind="house"))
    db.commit()
    auth_client.post("/budzet/cele", data={"name": "Fundusz awaryjny", "target": "10000", "start": "2500"})
    dash = auth_client.get("/").text
    assert "Fundusz awaryjny:" in dash and "25%" in dash


def test_migration_0019(tmp_path):
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
            assert "goal_id" in {r[3] for r in conn.execute(sa.text('PRAGMA foreign_key_list("transaction")'))}
            command.downgrade(cfg, "e3a9f1c7b250")
            assert "goal_id" not in {r[1] for r in conn.execute(sa.text('PRAGMA table_info("transaction")'))}
            command.upgrade(cfg, "head")
