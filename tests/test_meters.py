"""Meters: usage since the last reading, averages, months, cost, reset; the car odometer only shown."""

from __future__ import annotations

import io
import json
from datetime import date, timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from domownik import meters, services
from domownik.config import get_settings, today
from domownik.meters import Reading
from domownik.models import Attachment, Item, Measurement, Meter, MeterReading, Task

HX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")


def _meter(db, **kw) -> Meter:
    home = Item(name="Dom", kind="house")
    db.add(home)
    db.flush()
    m = Meter(item_id=home.id, name=kw.pop("name", "Prąd"), kind="electricity", unit="kWh", **kw)
    db.add(m)
    db.commit()
    return m


# ---------------------------------------------------------------- domain


def test_usage_average_and_cost():
    rs = [Reading(date(2026, 1, 1), 1000), Reading(date(2026, 1, 31), 1300), Reading(date(2026, 3, 2), 1900)]
    st = meters.compute(rs, unit="kWh", today=date(2026, 3, 10), unit_price=1.0)
    assert st.last.value == 1900
    assert st.since_prev == 600 and st.since_days == 30
    assert st.daily == pytest.approx(900 / 60)
    assert st.monthly == pytest.approx(15 * meters.DAYS_PER_MONTH)
    assert st.cost(st.since_prev) == 60000  # 600 kWh × 1 zł in grosze
    months = {m.first: m.used for m in st.months}
    assert months[date(2026, 1, 1)] == pytest.approx(300)  # 2.01–31.01 → 30 days × 10
    assert months[date(2026, 2, 1)] == pytest.approx(28 * 20)
    assert months[date(2026, 3, 1)] == pytest.approx(2 * 20)
    assert months[date(2025, 12, 1)] is None
    assert len(st.months) == 12 and st.months[-1].first == date(2026, 3, 1)


def test_reset_starts_over():
    rs = [
        Reading(date(2026, 1, 1), 5000),
        Reading(date(2026, 1, 11), 5100),
        Reading(date(2026, 1, 20), 3, reset=True),  # new meter
        Reading(date(2026, 1, 30), 103),
    ]
    st = meters.compute(rs, unit="m³", today=date(2026, 2, 1))
    assert st.since_prev == 100
    assert st.daily == pytest.approx(200 / 20)  # the 9 days around the swap are not counted
    only_reset = meters.compute(rs[:3], unit="m³", today=date(2026, 2, 1))
    assert only_reset.since_prev is None and only_reset.last.reset


def test_no_price_no_cost_and_empty():
    st = meters.compute([], unit="kWh", today=date(2026, 1, 1))
    assert st.last is None and st.daily is None and st.cost(10) is None
    st = meters.compute([Reading(date(2026, 1, 1), 5)], unit="kWh", today=date(2026, 1, 1))
    assert st.since_prev is None and st.daily is None


def test_check_new_warns_on_lower_value():
    rs = [Reading(date(2026, 1, 1), 100), Reading(date(2026, 2, 1), 200)]
    assert "mniejszy niż poprzedni" in meters.check_new(rs, date(2026, 3, 1), 150, False)
    assert meters.check_new(rs, date(2026, 3, 1), 150, True) is None  # replaced meter
    assert meters.check_new(rs, date(2026, 3, 1), 200, False) is None
    assert "większy niż późniejszy" in meters.check_new(rs, date(2026, 1, 15), 250, False)


def test_chart_svg_and_numbers():
    rs = [Reading(date(2026, 1, 1), 0), Reading(date(2026, 3, 1), 590)]
    svg = str(meters.bar_chart(meters.compute(rs, unit="kWh", today=date(2026, 3, 5), unit_price=0.5)))
    assert svg.startswith("<svg") and svg.count('class="mc-bar"') == 3 and "brak odczytów" in svg
    assert "zł" in svg  # cost in the bar tooltip
    assert meters.fmt_num(12345.6) == "12\u00a0345,6" and meters.fmt_num(3.0) == "3"
    assert meters.parse_number("1 234,5") == 1234.5
    with pytest.raises(ValueError):
        meters.parse_number("-3")


# ---------------------------------------------------------------- web


def test_add_meter_and_readings(auth_client, db):
    home = Item(name="Dom", kind="house")
    db.add(home)
    db.commit()
    assert "＋ Licznik" in auth_client.get(f"/items/{home.id}").text
    r = auth_client.post(
        f"/items/{home.id}/liczniki", data={"kind": "water_cold", "name": "", "unit": "", "unit_price": "12,5"}
    )
    assert r.status_code == 200
    m = db.scalar(select(Meter))
    assert (m.name, m.unit, m.unit_price) == ("Woda zimna", "m³", 12.5)
    d1, d2 = today() - timedelta(days=30), today()
    for d, v in ((d1, "100"), (d2, "103,5")):
        r = auth_client.post("/liczniki/odczyty", data={"meter_id": m.id, "value": v, "read_on": d.isoformat()})
        assert r.status_code == 200, r.text
    page = auth_client.get(f"/liczniki/{m.id}").text
    assert "103,5 m³" in page and "3,5 m³" in page and "43,75\u00a0zł" in page  # 3.5 × 12.5
    assert "Woda zimna" in auth_client.get(f"/items/{home.id}").text


def test_lower_reading_rejected_unless_reset(auth_client, db):
    m = _meter(db)
    auth_client.post("/liczniki/odczyty", data={"meter_id": m.id, "value": "500", "read_on": "2026-01-01"})
    r = auth_client.post(
        "/liczniki/odczyty", data={"meter_id": m.id, "value": "20", "read_on": today().isoformat()}, headers=HX
    )
    assert r.status_code == 422 and "Nowy licznik / reset" in r.text
    assert r.headers["HX-Retarget"] == "#reading-error"
    r = auth_client.post(
        "/liczniki/odczyty",
        data={"meter_id": m.id, "value": "20", "read_on": today().isoformat(), "is_reset": "1"},
        headers=HX,
    )
    assert r.status_code == 200
    assert [x.is_reset for x in db.scalars(select(MeterReading).order_by(MeterReading.id))] == [False, True]


def test_future_date_and_garbage_rejected(auth_client, db):
    m = _meter(db)
    tomorrow = (today() + timedelta(days=1)).isoformat()
    assert (
        auth_client.post("/liczniki/odczyty", data={"meter_id": m.id, "value": "1", "read_on": tomorrow}).status_code
        == 422
    )
    assert auth_client.post("/liczniki/odczyty", data={"meter_id": m.id, "value": "abc"}).status_code == 422
    assert db.scalar(select(MeterReading)) is None


def test_reading_with_photo(auth_client, db):
    m = _meter(db)
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, "JPEG")
    r = auth_client.post(
        "/liczniki/odczyty",
        data={"meter_id": m.id, "value": "42"},
        files={"files": ("licznik.jpg", buf.getvalue(), "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    reading = db.scalar(select(MeterReading))
    att = db.get(Attachment, reading.attachment_id)
    assert att.item_id == m.item_id and att.doc_kind == "photo" and "42 kWh" in att.description


def test_quick_add_reading(auth_client, db):
    assert "Odczyt licznika" not in auth_client.get("/dodaj", headers=HX).text  # no meters yet
    m = _meter(db)
    assert "Odczyt licznika" in auth_client.get("/dodaj", headers=HX).text
    form = auth_client.get("/dodaj/reading", headers=HX | {"HX-Current-URL": f"http://t/items/{m.item_id}"}).text
    assert f'<option value="{m.id}" selected>' in form
    form = auth_client.get("/dodaj/reading", headers=HX | {"HX-Current-URL": f"http://t/liczniki/{m.id}"}).text
    assert f'<option value="{m.id}" selected>' in form
    r = auth_client.post("/liczniki/odczyty", data={"meter_id": m.id, "value": "7"}, headers=HX | {"X-Quick-Add": "1"})
    assert json.loads(r.headers["HX-Trigger"]) == {"dm-added": "Dodano odczyt"}
    r = auth_client.post("/liczniki/odczyty", data={"meter_id": m.id, "value": "1"}, headers=HX | {"X-Quick-Add": "1"})
    assert r.status_code == 422 and r.headers["HX-Retarget"] == "#qa-error"


def test_reminder_is_a_plain_monthly_task(auth_client, db):
    m = _meter(db)
    auth_client.post(f"/liczniki/{m.id}/przypomnienie")
    auth_client.post(f"/liczniki/{m.id}/przypomnienie")  # no duplicate
    tasks = list(db.scalars(select(Task)))
    assert len(tasks) == 1
    assert tasks[0].title == "Spisz licznik: Prąd" and tasks[0].recurrence == {"mode": "monthly", "day": 31}
    auth_client.post(f"/liczniki/{m.id}/edit", data={"kind": "electricity", "name": "Prąd dzienny", "unit": "kWh"})
    db.expire_all()
    assert db.scalar(select(Task)).title == "Spisz licznik: Prąd dzienny"


def test_delete_reading_and_meter(auth_client, db):
    m = _meter(db)
    auth_client.post("/liczniki/odczyty", data={"meter_id": m.id, "value": "7"})
    rid = db.scalar(select(MeterReading.id))
    auth_client.post(f"/liczniki/odczyty/{rid}/delete")
    assert db.scalar(select(MeterReading)) is None
    item_id = m.item_id
    auth_client.post(f"/liczniki/{m.id}/delete")
    db.expire_all()
    assert db.scalar(select(Meter)) is None and db.get(Item, item_id) is not None


def test_car_odometer_shown_and_forecast_untouched(auth_client, db):
    car = Item(name="Auto 1", kind="car")
    db.add(car)
    db.flush()
    t = today()
    db.add_all(
        [
            Measurement(item_id=car.id, kind="odometer", measured_on=t - timedelta(days=60), value=10000),
            Measurement(item_id=car.id, kind="odometer", measured_on=t, value=11200),
        ]
    )
    task = Task(
        item_id=car.id, title="Olej", recurrence={"mode": "interval", "every": 15000, "km": 15000}, next_due_km=13000
    )
    db.add(task)
    db.commit()
    before = services.km_readings(db, {car.id})
    due_before = services.task_due(task, t, before)
    page = auth_client.get(f"/items/{car.id}").text
    assert "🚗 Przebieg" in page and "11\u00a0200 km" in page and "1\u00a0200 km" in page and "20/dzień" in page
    assert "＋ Licznik" not in page  # a car has no utility meters to add
    after = services.km_readings(db, {car.id})
    assert after == before and services.task_due(task, t, after) == due_before
    assert db.scalar(select(Meter)) is None  # nothing copied out of measurement


def test_migration_0013_up_and_down(tmp_path):
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
            command.upgrade(cfg, "7c1e5a9d2b40")
            conn.execute(
                sa.text(
                    "INSERT INTO item (id, kind, name, sort_order, attributes, archived) VALUES (1, 'car', 'A', 0, '{}', 0)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO measurement (item_id, kind, measured_on, value) VALUES (1, 'odometer', '2026-01-01', 5)"
                )
            )
            command.upgrade(cfg, "2f8b6d4e1a93")
            conn.execute(
                sa.text(
                    "INSERT INTO meter (id, item_id, name, kind, unit, sort_order) VALUES (1, 1, 'P', 'gas', 'm³', 0)"
                )
            )
            conn.execute(sa.text("INSERT INTO meter_reading (meter_id, read_on, value) VALUES (1, '2026-01-01', 3)"))
            assert conn.execute(sa.text("SELECT is_reset FROM meter_reading")).scalar() == 0  # server default
            command.downgrade(cfg, "7c1e5a9d2b40")
            tables = {r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
            assert not tables & {"meter", "meter_reading"}
            assert conn.execute(sa.text("SELECT count(*) FROM measurement")).scalar() == 1
            command.upgrade(cfg, "head")
