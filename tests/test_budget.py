"""Budget of a month (/budzet): plan from tasks, envelopes, monthly incomes, quick entries."""

from datetime import date, timedelta

from sqlalchemy import select

from domownik import budget, services
from domownik.config import today
from domownik.domain.tree import Tree
from domownik.models import BudgetCategory, Completion, Item, RecurringIncome, Task, Transaction

NOV = (2026, 11)
TODAY = date(2026, 11, 3)


def _cat(db, name, kind, limit=None):
    c = BudgetCategory(name=name, kind=kind, monthly_limit_gr=limit)
    db.add(c)
    db.flush()
    return c


def _item(db, name="Dom", kind="house", parent=None):
    it = Item(name=name, kind=kind, parent_id=parent.id if parent else None)
    db.add(it)
    db.flush()
    return it


def _task(db, item, title, rule, due, amount, cat=None, **kw):
    t = Task(
        item_id=item.id,
        title=title,
        category=kw.pop("category", "payment"),
        recurrence=rule,
        next_due_date=due,
        amount_gr=amount,
        direction=kw.pop("direction", "expense"),
        budget_category_id=cat.id if cat else None,
        **kw,
    )
    db.add(t)
    db.commit()
    return t


def _month(db, y, m, t=TODAY):
    return budget.compute_month(db, y, m, t)


# ---------------------------------------------------------------- plan of a month


def test_instalment_series_ends_in_the_middle_of_the_year(db):
    item, cat = _item(db), _cat(db, "Kredyt i raty", "fixed")
    _task(db, item, "Rata", {"mode": "monthly", "day": 15, "count": 6, "done": 0}, date(2027, 1, 15), 50000, cat)
    assert _month(db, 2027, 6).line(cat.id).plan_gr == 50000  # 6th instalment
    assert _month(db, 2027, 7).line(cat.id).plan_gr == 0  # series over
    jan = _month(db, 2027, 1).line(cat.id)
    assert [e.title for e in jan.entries] == ["Rata (rata 1/6)"]
    assert _month(db, 2027, 4).line(cat.id).entries[0].title == "Rata (rata 4/6)"


def test_yearly_task_is_planned_only_in_its_month(db):
    item, cat = _item(db, "Auto 1", "car"), _cat(db, "Ubezpieczenia", "planned")
    _task(db, item, "OC", {"mode": "fixed", "dates": ["03-15"]}, date(2027, 3, 15), 120000, cat, category="insurance")
    plans = {m: _month(db, 2027, m).line(cat.id).plan_gr for m in range(1, 13)}
    assert plans[3] == 120000
    assert sum(plans.values()) == 120000


def test_postponed_occurrence_moves_to_the_new_month_and_keeps_the_rhythm(db):
    item, cat = _item(db), _cat(db, "Media", "fixed")
    rule = {"mode": "monthly", "day": 28, "postponed_from": "2026-11-28"}
    _task(db, item, "Prąd", rule, date(2026, 12, 3), 20000, cat)
    assert _month(db, *NOV).line(cat.id).plan_gr == 0
    dec = _month(db, 2026, 12).line(cat.id)
    assert [e.date.day for e in dec.entries] == [3, 28]
    assert dec.plan_gr == 40000


def test_skipped_occurrence_leaves_the_plan(auth_client, db):
    item, cat = _item(db), _cat(db, "Subskrypcje", "fixed")
    t0 = today()
    # due next month on the rule's own day – a due date off the rule would add a second one that month
    nxt_month = (date(t0.year, t0.month, 1) + timedelta(days=32)).replace(day=5)
    task = _task(db, item, "Serwis", {"mode": "monthly", "day": 5}, nxt_month, 3000, cat)
    nxt = task.next_due_date
    before = _month(db, nxt.year, nxt.month, t0).line(cat.id).plan_gr
    assert before == 3000
    assert auth_client.post(f"/tasks/{task.id}/skip").status_code == 200
    db.expire_all()
    after = _month(db, nxt.year, nxt.month, t0).line(cat.id)
    assert after.plan_gr == 0 and not after.txs


def test_autopay_booked_counts_as_paid(db):
    item, cat = _item(db), _cat(db, "Subskrypcje", "fixed")
    _task(db, item, "Netflix", {"mode": "monthly", "day": 2}, date(2026, 11, 2), 4900, cat, autopay=True)
    services.settle_autopay(db, TODAY)
    ln = _month(db, *NOV).line(cat.id)
    assert [e.status for e in ln.entries] == ["paid"]
    assert ln.plan_gr == 4900 and ln.actual_gr == 4900 and ln.left_gr == 0
    # the next one is in December, booked automatically there
    dec = _month(db, 2026, 12).line(cat.id)
    assert [e.status for e in dec.entries] == ["auto"]


def test_done_with_another_cost_than_planned(db, user):
    item, cat = _item(db, "Auto 1", "car"), _cat(db, "Samochody", "planned")
    task = _task(db, item, "Wymiana oleju", {"mode": "once"}, date(2026, 11, 20), 30000, cat, category="service")
    services.complete_task(
        db, task, user=user, done_on=date(2026, 11, 2), amount_gr=34550, km=None, vendor=None, notes=None
    )
    ln = _month(db, *NOV).line(cat.id)
    assert ln.plan_gr == 30000 and ln.actual_gr == 34550
    assert ln.level == "over" and ln.left_gr == -4550
    e = ln.entries[0]
    assert e.status == "paid" and e.actual_gr == 34550 and e.planned_gr == 30000
    assert ln.loose_txs == []  # the payment is shown at its planned entry


def test_occurrence_paid_early_belongs_to_the_month_it_was_due(db, user):
    item, cat = _item(db), _cat(db, "Media", "fixed")
    task = _task(db, item, "Gaz", {"mode": "monthly", "day": 10}, date(2026, 11, 10), 15000, cat)
    services.complete_task(
        db, task, user=user, done_on=date(2026, 10, 30), amount_gr=15000, km=None, vendor=None, notes=None
    )
    assert _month(db, 2026, 10).line(cat.id).actual_gr == 0
    nov = _month(db, *NOV).line(cat.id)
    assert nov.actual_gr == 15000 and nov.plan_gr == 15000 and nov.entries[0].status == "paid"


def test_overdue_occurrence_of_an_earlier_month(db):
    item, cat = _item(db), _cat(db, "Media", "fixed")
    _task(db, item, "Woda", {"mode": "monthly", "day": 20}, date(2026, 10, 20), 8000, cat)
    oct_ = _month(db, 2026, 10).line(cat.id)
    assert oct_.entries[0].status == "overdue"
    b = _month(db, *NOV)
    assert b.line(cat.id).plan_gr == 8000  # 20 November – its own occurrence
    assert [e.date for e in b.overdue_before] == [date(2026, 10, 20)]
    assert _month(db, 2026, 12).overdue_before == []  # only the current month warns


def test_income_confirmed_with_another_amount_and_undone(db, user):
    cat = _cat(db, "Pensje", "income")
    inc = RecurringIncome(
        name="Pensja", amount_gr=500000, day=10, budget_category_id=cat.id, starts_on=date(2026, 9, 1)
    )
    db.add(inc)
    db.commit()
    ln = _month(db, *NOV).line(cat.id)
    assert ln.plan_gr == 500000 and ln.actual_gr == 0 and ln.entries[0].status == "expected"
    tx = budget.confirm_income(db, inc, *NOV, amount_gr=520000, today=TODAY, user_id=user.id)
    db.commit()
    assert tx.booked_on == TODAY  # confirmed before the 10th: it came today
    b = _month(db, *NOV)
    ln = b.line(cat.id)
    assert ln.actual_gr == 520000 and ln.entries[0].status == "received" and ln.entries[0].actual_gr == 520000
    assert b.income_plan_gr == 500000 and b.income_actual_gr == 520000
    # confirming again only corrects the amount
    budget.confirm_income(db, inc, *NOV, amount_gr=None, today=TODAY, user_id=user.id)
    db.commit()
    assert len(list(db.scalars(select(Transaction)))) == 1
    assert _month(db, *NOV).line(cat.id).actual_gr == 500000
    assert _month(db, 2026, 8).line(cat.id).plan_gr == 0  # before it started


def test_envelope_levels(db, user):
    cat = _cat(db, "Jedzenie", "variable", limit=50000)
    db.add(Transaction(booked_on=date(2026, 11, 2), amount_gr=-46000, budget_category_id=cat.id))
    db.commit()
    assert _month(db, *NOV).line(cat.id).level == "warn"  # over 90 %
    db.add(Transaction(booked_on=date(2026, 11, 3), amount_gr=-14000, budget_category_id=cat.id))
    db.commit()
    ln = _month(db, *NOV).line(cat.id)
    assert ln.plan_gr == 50000 and ln.actual_gr == 60000 and ln.level == "over" and ln.pct == 100
    assert ln.left_gr == -10000
    assert budget.level(50000, 10000) == "ok"


def test_limit_adds_to_planned_dates_and_totals(db):
    item = _item(db)
    cat = _cat(db, "Dom", "planned", limit=20000)
    inc_cat = _cat(db, "Pensje", "income")
    _task(db, item, "Kominiarz", {"mode": "once"}, date(2026, 11, 25), 25000, cat, category="service")
    db.add(
        RecurringIncome(
            name="Pensja", amount_gr=100000, day=1, budget_category_id=inc_cat.id, starts_on=date(2026, 1, 1)
        )
    )
    db.add(Transaction(booked_on=date(2026, 11, 2), amount_gr=-5000, budget_category_id=cat.id))
    db.commit()
    b = _month(db, *NOV)
    assert b.line(cat.id).plan_gr == 45000
    assert b.expense_plan_gr == 45000 and b.expense_actual_gr == 5000
    assert b.income_plan_gr == 100000
    assert b.remaining_gr == 100000 - 45000


def test_task_without_category_is_not_lost_and_can_be_assigned(db, user):
    item = _item(db, "Auto 1", "car")
    samochody = _cat(db, "Samochody", "planned")
    task = _task(db, item, "Myjnia", {"mode": "monthly", "day": 1}, date(2026, 11, 1), 6000, category="service")
    services.complete_task(
        db, task, user=user, done_on=date(2026, 11, 1), amount_gr=6000, km=None, vendor=None, notes=None
    )
    b = _month(db, *NOV)
    assert b.uncategorized.actual_gr == 6000 and b.uncategorized.plan_gr == 6000
    assert [t.id for t in b.unassigned_tasks] == [task.id]
    assert b.expense_plan_gr == 6000  # in the header too
    assert budget.suggest_category(db, Tree.load(db), item.id, "service") == samochody.id
    budget.assign_task_category(db, task, samochody.id)
    db.commit()
    b = _month(db, *NOV)
    assert b.uncategorized.empty and b.unassigned_tasks == []
    assert b.line(samochody.id).actual_gr == 6000


def test_tasks_of_archived_items_and_without_money_are_not_planned(db):
    cat = _cat(db, "Dom", "planned")
    gone = _item(db, "Stary dom")
    gone.archived = True
    item = _item(db)
    _task(db, gone, "Podatek", {"mode": "once"}, date(2026, 11, 15), 10000, cat)
    _task(db, item, "Sprzątanie", {"mode": "once"}, date(2026, 11, 15), None, cat)
    _task(db, item, "Info", {"mode": "once"}, date(2026, 11, 15), 500, cat, direction="none")
    assert _month(db, *NOV).line(cat.id).plan_gr == 0


def test_plan_of_a_day_matches_the_dashboard_payment_group(db):
    item, cat = _item(db), _cat(db, "Media", "fixed")
    t0 = today()
    due = t0 + timedelta(days=5)
    for n, amount in enumerate((10000, 2550, 4000)):
        _task(db, item, f"Opłata {n}", {"mode": "monthly", "day": due.day}, due, amount, cat if n else None)
    rows = services.task_rows(db, Tree.load(db), t0)
    group = next(g for g in services.group_payments(rows) if hasattr(g, "rows"))
    b = budget.compute_month(db, due.year, due.month, t0)
    assert sum(e.planned_gr for e in b.entries_on(due)) == -group.total_gr == 16550


def test_end_income(db, user):
    inc = RecurringIncome(name="Najem", amount_gr=200000, day=5, starts_on=date(2026, 1, 1))
    db.add(inc)
    db.commit()
    budget.confirm_income(db, inc, 2026, 10, amount_gr=None, today=TODAY, user_id=user.id)
    db.commit()
    assert budget.end_income(db, inc, TODAY) is False
    assert inc.ends_on == date(2026, 10, 31)  # November not confirmed – not expected any more
    assert _month(db, *NOV).income_plan_gr == 0
    fresh = RecurringIncome(name="Pomyłka", amount_gr=100, day=5, starts_on=date(2026, 11, 1))
    db.add(fresh)
    db.commit()
    assert budget.end_income(db, fresh, TODAY) is True


# ---------------------------------------------------------------- routes


def test_budget_page_and_nav(auth_client, db):
    _cat(db, "Jedzenie", "variable")
    db.commit()
    r = auth_client.get("/budzet")
    assert r.status_code == 200
    assert "Budżet" in r.text and "Dodaj wydatek lub wpływ" in r.text
    assert 'href="/budzet"' in r.text and "wkrótce" not in r.text
    assert auth_client.get("/budzet?m=2027-02").status_code == 200
    assert auth_client.get("/budzet?m=zly").status_code == 400
    assert auth_client.get("/budzet/kategorie").status_code == 200


def test_quick_entry_expense_and_income(auth_client, db):
    food = _cat(db, "Jedzenie", "variable")
    bonus = _cat(db, "Premie i inne", "income")
    db.commit()
    d = today().isoformat()
    r = auth_client.post(
        "/budzet/wpis", data={"amount": "54,90", "budget_category_id": food.id, "booked_on": d, "note": "sklep"}
    )
    assert r.status_code == 200  # followed redirect
    r = auth_client.post("/budzet/wpis", data={"amount": "300", "budget_category_id": bonus.id, "booked_on": d})
    txs = {t.budget_category_id: t for t in db.scalars(select(Transaction))}
    assert txs[food.id].amount_gr == -5490 and txs[food.id].note == "sklep" and txs[food.id].created_by_id
    assert txs[bonus.id].amount_gr == 30000
    bad = auth_client.post("/budzet/wpis", data={"amount": "", "budget_category_id": food.id})
    assert bad.status_code == 422 and "Podaj kwotę" in bad.text
    # the quick form remembers the last category
    page = auth_client.get("/budzet").text
    assert f'<option value="{food.id}" selected>' in page


def test_edit_and_delete_transaction(auth_client, db, user):
    food = _cat(db, "Jedzenie", "variable")
    tx = Transaction(booked_on=today(), amount_gr=-1000, budget_category_id=food.id, created_by_id=user.id)
    db.add(tx)
    db.commit()
    r = auth_client.post(f"/budzet/transakcje/{tx.id}/kwota", data={"amount": "12,50"}, follow_redirects=False)
    assert r.status_code == 303 and f"open={food.id}" in r.headers["location"]
    db.refresh(tx)
    assert tx.amount_gr == -1250
    auth_client.post(f"/budzet/transakcje/{tx.id}/usun")
    assert db.scalar(select(Transaction)) is None


def test_task_payment_amount_follows_completion_and_cannot_be_deleted_here(auth_client, db, user):
    item, cat = _item(db), _cat(db, "Media", "fixed")
    task = _task(db, item, "Prąd", {"mode": "once"}, today(), 10000, cat)
    comp = services.complete_task(
        db, task, user=user, done_on=today(), amount_gr=10000, km=None, vendor=None, notes=None
    )
    tx = db.scalar(select(Transaction))
    auth_client.post(f"/budzet/transakcje/{tx.id}/kwota", data={"amount": "120"})
    db.expire_all()
    assert db.get(Transaction, tx.id).amount_gr == -12000
    assert db.get(Completion, comp.id).actual_amount_gr == 12000
    assert auth_client.post(f"/budzet/transakcje/{tx.id}/usun").status_code == 400


def test_income_routes(auth_client, db):
    cat = _cat(db, "Pensje", "income")
    db.commit()
    r = auth_client.post(
        "/budzet/dochody", data={"name": "Pensja", "amount": "6 500", "day": "10", "budget_category_id": cat.id}
    )
    assert r.status_code == 200
    inc = db.scalar(select(RecurringIncome))
    assert inc.amount_gr == 650000 and inc.starts_on == today().replace(day=1)
    m = today().strftime("%Y-%m")
    page = auth_client.get("/budzet").text
    assert "✓ Wpłynęło" in page
    auth_client.post(f"/budzet/dochody/{inc.id}/wplynelo", data={"m": m, "amount": "6600"})
    tx = db.scalar(select(Transaction))
    assert tx.amount_gr == 660000 and tx.income_id == inc.id
    assert "cofnij" in auth_client.get("/budzet").text
    auth_client.post(f"/budzet/transakcje/{tx.id}/usun", data={"m": m})
    assert db.scalar(select(Transaction)) is None
    bad = auth_client.post(f"/budzet/dochody/{inc.id}", data={"name": "Pensja", "amount": "1", "day": "40"})
    assert bad.status_code == 422
    auth_client.post(f"/budzet/dochody/{inc.id}/zakoncz")
    assert db.scalar(select(RecurringIncome)) is None  # never confirmed → removed


def test_incomes_stay_off_the_timeline_and_agenda(auth_client, db):
    db.add(RecurringIncome(name="Pensja Ali", amount_gr=100, day=today().day, starts_on=today().replace(day=1)))
    _item(db)
    db.commit()
    t = today()
    assert "Pensja Ali" not in auth_client.get("/").text
    tl = auth_client.get(f"/api/timeline?start={t.isoformat()}&end={(t + timedelta(days=40)).isoformat()}").text
    assert "Pensja Ali" not in tl


def test_categories_add_edit_archive(auth_client, db):
    r = auth_client.post(
        "/budzet/kategorie", data={"name": "Zwierzęta", "kind": "variable", "icon": "🐶", "limit": "200"}
    )
    assert r.status_code == 200
    cat = db.scalar(select(BudgetCategory).where(BudgetCategory.name == "Zwierzęta"))
    assert cat.monthly_limit_gr == 20000 and cat.icon == "🐶"
    dup = auth_client.post("/budzet/kategorie", data={"name": "zwierzęta", "kind": "variable"})
    assert dup.status_code == 422
    auth_client.post(f"/budzet/kategorie/{cat.id}", data={"name": "Pies", "kind": "planned", "icon": "", "limit": ""})
    db.refresh(cat)
    assert (cat.name, cat.kind, cat.icon, cat.monthly_limit_gr) == ("Pies", "planned", None, None)
    auth_client.post(f"/budzet/kategorie/{cat.id}/archiwum")
    db.refresh(cat)
    assert cat.archived
    assert f'value="{cat.id}"' not in auth_client.get("/budzet").text.split("bud-quick-form")[1].split("</form>")[0]


def test_assign_category_from_budget(auth_client, db):
    item, cat = _item(db, "Auto 1", "car"), _cat(db, "Samochody", "planned")
    task = _task(db, item, "Myjnia", {"mode": "once"}, today() + timedelta(days=1), 5000, category="service")
    tomorrow = today() + timedelta(days=1)
    page = auth_client.get(f"/budzet?m={tomorrow.strftime('%Y-%m')}").text
    assert "bez kategorii" in page and f'<option value="{cat.id}" selected>' in page  # suggested
    auth_client.post(f"/budzet/zadania/{task.id}/kategoria", data={"budget_category_id": cat.id})
    db.refresh(task)
    assert task.budget_category_id == cat.id


def test_new_task_form_suggests_a_budget_category(auth_client, db):
    car = _item(db, "Auto 1", "car")
    cat = _cat(db, "Samochody", "planned")
    db.commit()
    page = auth_client.get(f"/tasks/new?item={car.id}").text
    assert f'<option value="{cat.id}" selected>' in page


def test_dashboard_budget_tile_below_timeline(auth_client, db):
    item, cat = _item(db), _cat(db, "Media", "fixed")
    _task(db, item, "Prąd", {"mode": "once"}, today(), 10000, cat)
    page = auth_client.get("/").text
    assert 'class="bud-tile' in page
    assert "wydano 0\xa0zł z 100\xa0zł" in page
    assert page.index("timeline-card") < page.index('class="bud-tile')


def test_shopping_finish_still_books_food(auth_client, db):
    services.ensure_budget_categories(db)
    db.commit()
    auth_client.post("/zakupy/zakoncz", data={"amount": "80"})
    tx = db.scalar(select(Transaction))
    food = db.scalar(select(BudgetCategory).where(BudgetCategory.name == "Jedzenie"))
    assert tx.amount_gr == -8000 and tx.budget_category_id == food.id
    t = today()
    assert budget.compute_month(db, t.year, t.month, t).line(food.id).actual_gr == 8000


def test_migration_0009_up_and_down(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from domownik import cli
    from domownik.db import make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    cfg = cli.alembic_config()
    with eng.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")  # like `domownik migrate`
        conn.commit()
        with conn.begin():
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "d8618a23c9a7")
            conn.execute(
                sa.text(
                    "INSERT INTO budget_category (id, name, kind, sort_order) VALUES (1, 'Jedzenie', 'variable', 0)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO \"transaction\" (id, booked_on, amount_gr, budget_category_id) VALUES (1, '2026-11-01', -100, 1)"
                )
            )
            command.upgrade(cfg, "head")
            row = conn.execute(sa.text("SELECT archived, monthly_limit_gr FROM budget_category")).one()
            assert tuple(row) == (0, None)
            assert conn.execute(sa.text('SELECT budget_category_id, income_id FROM "transaction"')).one() == (1, None)
            command.downgrade(cfg, "d8618a23c9a7")
            assert conn.execute(sa.text('SELECT count(*) FROM "transaction"')).scalar() == 1
