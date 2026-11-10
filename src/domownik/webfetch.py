"""Fetching a public web page or picture – for a recipe from a link, a shop's logo for a loyalty card.

Every hop (redirects too) must go to a public internet address: a link or a redirect can never reach the
home network or the phone itself. Sizes are capped; nothing is sent but a plain GET with a generic agent.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

import httpx

TIMEOUT = 15.0
AGENT = "Mozilla/5.0 (Linux; Android 14) Domownik"


class FetchError(Exception):
    """A message for the user (Polish)."""


@dataclass
class Fetched:
    body: bytes
    content_type: str
    url: str  # after redirects
    encoding: str | None

    @property
    def text(self) -> str:
        return self.body.decode(self.encoding or "utf-8", errors="replace")


def check(request: httpx.Request) -> None:
    host = request.url.host
    if request.url.scheme not in ("http", "https") or not host:
        raise FetchError("To nie jest link do strony")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise FetchError("Nie mogę połączyć się z tą stroną – sprawdź link i internet") from None
    if any(not ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos):
        raise FetchError("Link prowadzi do sieci lokalnej")


def get(url: str, *, max_bytes: int, timeout: float = TIMEOUT, what: str = "Strona") -> Fetched:
    try:
        with (
            httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                max_redirects=5,
                headers={"User-Agent": AGENT, "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5"},
                trust_env=False,
                event_hooks={"request": [check]},
            ) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code == 404:
                raise FetchError("Nie ma takiej strony (404) – sprawdź link")
            if resp.status_code >= 400:
                raise FetchError(f"Strona odpowiedziała błędem {resp.status_code}")
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise FetchError(f"{what} jest podejrzanie duża")
                chunks.append(chunk)
            return Fetched(
                b"".join(chunks),
                resp.headers.get("content-type", "").split(";")[0].strip().lower(),
                str(resp.url),
                resp.encoding,
            )
    except httpx.HTTPError as exc:
        raise FetchError(f"Nie udało się pobrać strony ({type(exc).__name__})") from None
