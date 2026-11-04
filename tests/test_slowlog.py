from __future__ import annotations

import logging
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from domownik.slowlog import SlowRequestLog


def _app(threshold_ms: int) -> FastAPI:
    app = FastAPI()

    @app.get("/n/done/{token}")
    def slow(token: str):
        time.sleep(0.03)
        return {"ok": True}

    @app.get("/fast")
    def fast():
        return {"ok": True}

    app.add_middleware(SlowRequestLog, threshold_ms=threshold_ms)
    return app


def test_slow_request_logged_with_route_template_only(caplog):
    with caplog.at_level(logging.WARNING, logger="domownik.slow"), TestClient(_app(10)) as c:
        c.get("/n/done/SEKRETNYTOKEN?kwota=123", headers={"HX-Request": "true"})
        c.get("/fast")
    lines = [r.getMessage() for r in caplog.records if r.name == "domownik.slow"]
    assert len(lines) == 1
    assert "GET" in lines[0] and "/n/done/{token}" in lines[0] and "200" in lines[0] and "htmx" in lines[0]
    assert "SEKRETNYTOKEN" not in lines[0] and "kwota" not in lines[0]


def test_zero_threshold_is_off(caplog):
    with caplog.at_level(logging.WARNING, logger="domownik.slow"), TestClient(_app(0)) as c:
        c.get("/n/done/x")
    assert not [r for r in caplog.records if r.name == "domownik.slow"]
