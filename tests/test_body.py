from __future__ import annotations

import io
from datetime import date, datetime, timedelta

import pytest
from PIL import Image

from domownik import body
from domownik.models import BodyCheck, BodyPhoto, User
from domownik.security import hash_password

T = date(2026, 11, 10)


def _jpeg(color=(200, 120, 90), size=(900, 1200), gps=True) -> io.BytesIO:
    im = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # Make
    exif[0x0112] = 6  # orientation: rotated – must come out upright
    if gps:
        exif[0x8825] = {1: "N", 2: (52.0, 13.0, 0.0)}
    out = io.BytesIO()
    im.save(out, "JPEG", exif=exif)
    out.seek(0)
    return out


def _other(db) -> User:
    u = User(email="ala@example.com", name="Ala", password_hash=hash_password("tajnehaslo2"))
    db.add(u)
    db.commit()
    return u


def test_numbers():
    assert body.parse_number("72,45", "Waga", *body.KG_RANGE, "kg") == 72.5
    assert body.parse_number("", "Waga", *body.KG_RANGE, "kg") is None
    with pytest.raises(body.BodyError, match="od 25 do 300 kg"):
        body.parse_number("7", "Waga", *body.KG_RANGE, "kg")
    with pytest.raises(body.BodyError):
        body.parse_number("abc", "Talia", *body.CM_RANGE, "cm")
    assert body.fmt(73.0) == "73" and body.fmt(0.8, signed=True) == "+0,8" and body.fmt(-0.5, signed=True) == "−0,5"


def test_summary_average_change_and_goal(db, user):
    # four weeks ago ~71, now ~72 with a 1.5 kg jump on one day that the average smooths out
    for i, kg in enumerate([71.2, 70.8, 71.0]):
        body.add_weight(db, user, T - timedelta(days=30 - i), kg)
    for i, kg in enumerate([71.8, 73.3, 71.9]):
        body.add_weight(db, user, T - timedelta(days=6 - i * 2), kg)
    body.add_weight(db, user, T, 72.0)
    body.add_weight(db, user, T, 72.2)  # the same day again corrects it
    body.prefs(db, user).goal_kg = 78
    db.commit()
    s = body.summary(db, user, T)
    assert s.today_kg == 72.2
    assert s.avg_n == 4 and round(s.avg, 2) == round((71.8 + 73.3 + 71.9 + 72.2) / 4, 2)
    assert s.change is not None and 1.0 < s.change < 1.5  # vs the average around 13.10
    assert round(s.to_goal, 1) == round(78 - s.avg, 1)
    assert not s.stale
    assert body.summary(db, user, T + timedelta(days=9)).stale


def test_chart(db, user):
    for i in range(20):
        body.add_weight(db, user, T - timedelta(days=i * 2), 72 - i * 0.1)
    entries = body.weights(db, user)
    c = body.chart(entries, 78, T - timedelta(days=90), T)
    assert c is not None and c["goal_y"] is None  # 78 is too far from ~70–72 to squash the line
    assert c["goal_off"] == "up" and len(c["dots"]) == 20
    near = body.chart(entries, 73, T - timedelta(days=90), T)
    assert near["goal_y"] is not None and near["goal_off"] is None
    assert body.chart(entries[:1], None, T - timedelta(days=90), T) is None


def test_check_with_photos_is_clean_and_private(db, user):
    c = body.save_check(db, user, None, T, {"waist": 80.5, "arm": 36.0}, {"front": ("a.jpg", _jpeg())}, set())
    db.commit()
    ph = body.photo_of(c, "front")
    path = body.photo_path(ph)
    assert path.is_file() and body.photo_path(ph, thumb=True).is_file()
    assert path.parent.name == str(user.id) and path.parent.parent.name == "body"
    with Image.open(path) as im:
        assert len(im.getexif()) == 0  # no place, no phone
        assert im.size == (1200, 900)  # turned upright (orientation 6) and kept under PHOTO_PX

    # a second measurement the same day completes the first; a new photo of a pose replaces the old file
    c2 = body.save_check(db, user, None, T, {"chest": 101.0}, {"front": ("b.jpg", _jpeg((10, 10, 10)))}, set())
    db.commit()
    assert c2.id == c.id and (c2.chest, c2.waist) == (101.0, 80.5)
    assert not path.exists() and len(c2.photos) == 1

    other = _other(db)
    assert body.get_check(db, other, c.id) is None
    assert body.get_photo(db, other, c2.photos[0].id) is None

    files = [body.photo_path(c2.photos[0]), body.photo_path(c2.photos[0], thumb=True)]
    body.delete_check(db, c2)
    db.commit()
    assert db.query(BodyPhoto).count() == 0 and not any(f.exists() for f in files)


def test_bad_photo_saves_nothing(db, user):
    with pytest.raises(body.BodyError, match="Bok"):
        body.save_check(db, user, None, T, {"waist": 80.0}, {"side": ("x.jpg", io.BytesIO(b"not an image"))}, set())
    db.rollback()
    assert db.query(BodyCheck).count() == 0
    with pytest.raises(body.BodyError, match="choć jeden"):
        body.save_check(db, user, None, T, {}, {}, set())


def test_morning_pushes(db, user, monkeypatch):
    sent = []
    monkeypatch.setattr(body, "_send", lambda db, u, b: sent.append((u.id, b["title"])) or 1)
    other = _other(db)  # never weighed: no push for them
    body.add_weight(db, user, T - timedelta(days=3), 72.0)
    body.save_check(db, user, None, T - timedelta(days=14), {"waist": 80.0}, {}, set())
    db.commit()

    assert body.run(db, datetime(2026, 11, 10, 7, 0)) == 0  # too early
    assert body.run(db, datetime(2026, 11, 10, 7, 31)) == 2
    assert sent == [(user.id, "⚖️ Waga?"), (user.id, "📏 Czas na pomiar")]
    assert body.run(db, datetime(2026, 11, 10, 9, 0)) == 0  # once
    assert body.run(db, datetime(2026, 11, 11, 8, 0)) == 0  # quiet for the gap
    assert body.run(db, datetime(2026, 11, 13, 8, 0)) == 2  # still nothing done: reminded again
    assert all(uid != other.id for uid, _ in sent)

    body.add_weight(db, user, date(2026, 11, 13), 72.4)
    body.prefs(db, user).reminders = False
    db.commit()
    assert body.run(db, datetime(2026, 11, 20, 8, 0)) == 0


# ---------------------------------------------------------------- over HTTP


def test_pages(auth_client, db, user):
    r = auth_client.get("/forma")
    assert r.status_code == 200 and "⚖️ Sylwetka" in r.text and "Pierwszy pomiar" in r.text

    r = auth_client.post("/forma/waga", data={"kg": "72,4"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/forma#sylwetka"
    r = auth_client.post("/forma/waga", data={"kg": "7"})
    assert r.status_code == 422 and "od 25 do 300 kg" in r.text

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    auth_client.post("/forma/waga", data={"kg": "72,0", "day": yesterday, "back": "sylwetka"})
    auth_client.post("/forma/sylwetka/ustawienia", data={"goal": "78", "every": "14", "reminders": "1"})
    r = auth_client.get("/forma/sylwetka")
    assert r.status_code == 200 and "<svg" in r.text and "cel 78" in r.text

    r = auth_client.post(
        "/forma/sylwetka/pomiar",
        data={"waist": "80,5", "chest": "", "kg": ""},
        files={"photo_front": ("front.jpg", _jpeg().read(), "image/jpeg")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    c = db.query(BodyCheck).one()
    ph = c.photos[0]
    img = auth_client.get(f"/forma/sylwetka/zdjecie/{ph.id}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert "sandbox" in img.headers["content-security-policy"]
    assert auth_client.get(f"/forma/sylwetka/zdjecie/{ph.id}?maly=1").headers["content-type"] == "image/webp"
    assert "Talia" in auth_client.get(f"/forma/sylwetka/pomiar/{c.id}").text
    r = auth_client.get("/forma/sylwetka/porownaj?poza=side")
    assert r.status_code == 200 and "Brak zdjęcia „Bok”" in r.text


def test_someone_elses_is_404(auth_client, db, user):
    other = _other(db)
    c = body.save_check(db, other, None, T, {"waist": 70.0}, {"front": ("a.jpg", _jpeg())}, set())
    w = body.add_weight(db, other, T, 60.0)
    db.commit()
    ph = c.photos[0]
    assert auth_client.get(f"/forma/sylwetka/pomiar/{c.id}").status_code == 404
    assert auth_client.get(f"/forma/sylwetka/zdjecie/{ph.id}").status_code == 404
    assert auth_client.post(f"/forma/sylwetka/pomiar/{c.id}/usun").status_code == 404
    assert auth_client.post(f"/forma/waga/{w.id}/usun").status_code == 404
    page = auth_client.get("/forma/sylwetka").text
    assert "60 kg" not in page and "Talia 70" not in auth_client.get("/forma/sylwetka/porownaj").text
    assert db.get(BodyCheck, c.id) is not None
