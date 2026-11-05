"""Standing orders (autopay), payment groups on the dashboard, 'paid all', daily backup."""

import tarfile
from datetime import timedelta

from sqlalchemy import select

from domownik import services
from domownik.config import today
from domownik.domain.tree import Tree
from domownik.models import Completion, Item, Task, Transaction

MONTHLY_10 = {"mode": "monthly", "day": 10}


def _home(db) -> Item:
    item = Item(name="Dom", kind="house")
    db.add(item)
    db.commit()
    return item


def _payment(db, item, title, due, *, amount=10000, autopay=False) -> Task:
    task = Task(
        item_id=item.id,
        title=title,
        category="payment",
        recurrence=MONTHLY_10,
        next_due_date=due,
        amount_gr=amount,
        direction="expense",
        autopay=autopay,
    )
    db.add(task)
    db.commit()
    return task


def test_autopay_books_due_payments_and_catches_up(db):
    item = _home(db)
    t = today()
    late = t - timedelta(days=40)  # two monthly occurrences already passed
    auto = _payment(db, item, "Netflix", late, amount=4900, autopay=True)
    manual = _payment(db, item, "Prąd", late)
    future = _payment(db, item, "Spotify", t + timedelta(days=3), autopay=True)

    booked = services.settle_autopay(db, t)

    assert booked >= 2
    assert auto.next_due_date > t
    comps = list(db.scalars(select(Completion).where(Completion.task_id == auto.id)))
    assert len(comps) == booked
    assert all(c.done_on == c.due_date and c.performed_by_id is None for c in comps)
    assert all(services.is_autopay_completion(c) for c in comps)
    txs = list(db.scalars(select(Transaction).where(Transaction.task_id == auto.id)))
    assert [tx.amount_gr for tx in txs] == [-4900] * booked
    assert manual.next_due_date == late  # not a standing order – stays overdue
    assert future.next_due_date == t + timedelta(days=3)
    assert services.settle_autopay(db, t) == 0  # idempotent


def test_undo_of_autopay_turns_autopay_off(db):
    item = _home(db)
    t = today()
    task = _payment(db, item, "Netflix", t, autopay=True)
    services.settle_autopay(db, t)
    comp = db.scalar(select(Completion).where(Completion.task_id == task.id))
    services.undo_completion(db, comp)
    assert task.next_due_date == t and task.autopay is False
    assert services.settle_autopay(db, t) == 0  # would loop forever otherwise


def test_group_payments_collapses_three_or_more_on_one_day(db):
    item = _home(db)
    t = today()
    day = t + timedelta(days=8)
    for name in ("Netflix", "Spotify", "Play"):
        _payment(db, item, name, day)
    _payment(db, item, "Woda", t + timedelta(days=9))
    other = Task(
        item_id=item.id,
        title="Przegląd",
        category="inspection",
        recurrence={"mode": "once"},
        next_due_date=day,
        direction="none",
    )
    db.add(other)
    db.commit()

    rows = services.task_rows(db, Tree.load(db), t)
    entries = services.group_payments(rows)

    groups = [e for e in entries if isinstance(e, services.PaymentGroup)]
    assert len(groups) == 1
    g = groups[0]
    assert g.date == day and len(g.rows) == 3 and g.total_gr == -30000
    singles = [e.task.title for e in entries if not isinstance(e, services.PaymentGroup)]
    assert singles == ["Przegląd", "Woda"]  # other categories and lone payments stay as rows


def test_dashboard_shows_group_and_paid_all(auth_client, db):
    item = _home(db)
    t = today()
    day = t + timedelta(days=5)
    tasks = [_payment(db, item, n, day) for n in ("Netflix", "Spotify", "Play")]
    auto = _payment(db, item, "Max", day, autopay=True)

    page = auth_client.get("/")
    assert "Opłaty – " in page.text and "Zapłacone wszystkie (3)" in page.text
    assert "1 płatne automatycznie" in page.text

    r = auth_client.post("/tasks/bulk-done", data={"task_id": [str(x.id) for x in tasks]}, follow_redirects=False)
    assert r.status_code == 303
    for task in tasks:
        db.refresh(task)
        assert task.next_due_date > day
    db.refresh(auto)
    assert auto.next_due_date == day  # not part of the form
    assert db.scalar(select(Completion).where(Completion.task_id == tasks[0].id)).actual_amount_gr == 10000


def test_paid_all_returns_to_the_page_it_was_on(auth_client, db):
    item = _home(db)
    day = today() + timedelta(days=5)
    tasks = [_payment(db, item, n, day) for n in ("Netflix", "Spotify", "Play")]

    page = auth_client.get("/zadania").text
    assert 'action="/tasks/bulk-done?next=/zadania"' in page
    r = auth_client.post(
        "/tasks/bulk-done?next=/zadania", data={"task_id": [str(x.id) for x in tasks]}, follow_redirects=False
    )
    assert r.headers["location"] == "/zadania"
    # only paths of this site
    r = auth_client.post("/tasks/bulk-done?next=//evil.example", data={"task_id": []}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_scripts_load_once_in_head(auth_client, db):
    _home(db)
    html = auth_client.get("/").text
    head, body = html.split("</head>", 1)
    for src in ("/static/vendor/htmx.min.js", "/static/vendor/vis-timeline-graph2d.min.js", "/static/app.js"):
        assert src in head and src not in body  # hx-boost re-runs scripts inside <body>


def test_backup_rotates_and_skips_secrets(engine, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from domownik import cli
    from domownik.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "data_dir", tmp_path)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "secret_key").write_text("tajne")
    db_file = tmp_path / "db.sqlite"
    import sqlite3

    sqlite3.connect(db_file).close()

    runner = CliRunner()
    auto = tmp_path / "backups" / "auto"
    auto.mkdir(parents=True)
    for i in range(3):
        (auto / f"domownik-2020010{i}-000000.tar.gz").write_bytes(b"old")
    result = runner.invoke(cli.app, ["backup", "--keep", "2"])
    assert result.exit_code == 0, result.output
    files = sorted(auto.glob("domownik-*.tar.gz"))
    assert len(files) == 2 and files[-1].name.startswith("domownik-20")
    with tarfile.open(files[-1]) as tar:
        names = tar.getnames()
    assert "db.sqlite" in names and not any(n.startswith("secrets") for n in names)
