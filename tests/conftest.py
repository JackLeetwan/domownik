from __future__ import annotations

import os
import re
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="domownik-test-")
os.environ["DOMOWNIK_DATA_DIR"] = _tmp
os.environ["DOMOWNIK_HTTPS_ONLY"] = "false"  # tests talk plain http; never inherit .env
os.environ["DOMOWNIK_BASE_URL"] = "http://testserver"

from fastapi.testclient import TestClient  # noqa: E402

from domownik import db as dbmod  # noqa: E402
from domownik.models import Base, User  # noqa: E402
from domownik.security import hash_password  # noqa: E402


@pytest.fixture
def engine(tmp_path):
    eng = dbmod.make_engine(f"sqlite:///{tmp_path / 'test.sqlite'}")
    Base.metadata.create_all(eng)
    dbmod.set_engine(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    with dbmod.session_factory()() as s:
        yield s


@pytest.fixture
def user(db):
    u = User(email="jan@example.com", name="Jan", password_hash=hash_password("tajnehaslo1"))
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def client(engine):
    from domownik import security
    from domownik.main import create_app

    security._FAILS.clear()

    with TestClient(create_app()) as c:
        yield c


def csrf_of(html: str) -> str:
    m = re.search(r'"X-CSRF-Token": "([^"]+)"', html)
    assert m, "csrf token not found"
    return m.group(1)


@pytest.fixture
def auth_client(client, user):
    page = client.get("/login")
    token = csrf_of(page.text)
    r = client.post(
        "/login", data={"email": "jan@example.com", "password": "tajnehaslo1", "csrf": token}, follow_redirects=False
    )
    assert r.status_code == 303
    client.headers["X-CSRF-Token"] = csrf_of(client.get("/").text)  # token rotates on login
    return client
