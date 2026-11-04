"""Log slow requests (DOMOWNIK_SLOW_MS) – what is slow in everyday use, not only on the pages one guesses.

One line per request that took at least the threshold, from the first byte in to the last byte out:
    SLOW  231 ms  GET  /items/{item_id}  200  htmx
The route template is logged, never the real path or the query string – no ids of private things, no
calendar or notification tokens in the log. `scripts/phone-slow-report.sh` sums the lines up.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("domownik.slow")

SKIP_PREFIXES = ("/static/", "/healthz")


class SlowRequestLog:
    def __init__(self, app, threshold_ms: int):
        self.app = app
        self.threshold_ms = threshold_ms

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.threshold_ms <= 0 or scope["path"].startswith(SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 0

        async def timed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                ms = (time.perf_counter() - start) * 1000
                if ms >= self.threshold_ms:
                    log.warning(
                        "SLOW %5.0f ms  %-4s %s  %s%s", ms, scope["method"], _label(scope), status, _kind(scope)
                    )

        await self.app(scope, receive, timed_send)


def _label(scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path or "(bez trasy)"


def _kind(scope) -> str:
    headers = dict(scope.get("headers") or [])
    if headers.get(b"hx-boosted") == b"true":
        return "  boost"
    if headers.get(b"hx-request") == b"true":
        return "  htmx"
    return ""
