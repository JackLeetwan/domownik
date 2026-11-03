"""Comments under tasks and notes: add, delete own only, push to the person concerned."""

from __future__ import annotations

import json

from sqlalchemy import select

from domownik.config import today
from domownik.models import BoardNote, Comment, Item, PushSubscription, Task, User
from domownik.security import hash_password

HX = {"HX-Request": "true"}


def _other(db) -> User:
    u = User(email="ola@example.com", name="Ola", password_hash=hash_password("tajnehaslo2"))
    db.add(u)
    db.commit()
    return u


def _task(db, **kw) -> Task:
    item = Item(name="Dom", kind="house")
    db.add(item)
    db.commit()
    t = Task(
        item_id=item.id,
        title="Wymiana filtra",
        category="service",
        recurrence={"mode": "once"},
        next_due_date=today(),
        lead_days=1,
        direction="none",
        **kw,
    )
    db.add(t)
    db.commit()
    return t


def test_task_comment_and_push_to_assignee(auth_client, db, user, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "domownik.webpush.deliver", lambda sub, data, urgent=False: sent.append(json.loads(data)) or True
    )
    other = _other(db)
    db.add(PushSubscription(user_id=other.id, endpoint="https://p.example/o", p256dh="k", auth="a"))
    t = _task(db, assignee_id=other.id)
    assert "Napisz komentarz" in auth_client.get(f"/tasks/{t.id}", headers=HX).text

    r = auth_client.post(f"/komentarze/zadanie/{t.id}", data={"body": "Filtr leży w szafce"}, headers=HX)
    assert r.status_code == 200 and "Filtr leży w szafce" in r.text and f'id="comments-zadanie-{t.id}"' in r.text
    assert sent and sent[0]["title"] == "💬 Wymiana filtra" and sent[0]["body"] == "Jan: Filtr leży w szafce"
    assert auth_client.post(f"/komentarze/zadanie/{t.id}", data={"body": "  "}, headers=HX).status_code == 400

    c = db.scalar(select(Comment))
    r = auth_client.post(f"/komentarze/{c.id}/usun", headers=HX)
    assert r.status_code == 200 and "Filtr leży" not in r.text


def test_cannot_delete_someone_elses(auth_client, db, user):
    other = _other(db)
    t = _task(db)
    db.add(Comment(task_id=t.id, user_id=other.id, body="moje"))
    db.commit()
    c = db.scalar(select(Comment))
    assert auth_client.post(f"/komentarze/{c.id}/usun", headers=HX).status_code == 403


def test_note_comment_rerenders_card_and_cascade(auth_client, db, user):
    n = BoardNote(body="Paczka w piątek", created_by_id=user.id)
    db.add(n)
    db.commit()
    r = auth_client.post(f"/komentarze/notatka/{n.id}", data={"body": "Odebrałem"}, headers=HX)
    assert r.status_code == 200 and f'id="note-{n.id}"' in r.text and "Odebrałem" in r.text
    assert "1 komentarz" in auth_client.get("/notatki").text
    nid = n.id
    auth_client.post(f"/notatki/{nid}/archive")
    auth_client.post(f"/notatki/{nid}/delete")
    db.expire_all()
    assert db.scalar(select(Comment)) is None


def test_task_delete_removes_comments(auth_client, db, user):
    t = _task(db)
    auth_client.post(f"/komentarze/zadanie/{t.id}", data={"body": "x"}, headers=HX)
    auth_client.post(f"/tasks/{t.id}/delete")
    db.expire_all()
    assert db.scalar(select(Comment)) is None
