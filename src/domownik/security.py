from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from domownik.db import get_db, set_viewer
from domownik.models import User

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerificationError, InvalidHashError:
        return False


_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def burn_password_check(password: str) -> None:
    """Unknown e-mail: spend the same argon2 time, so the answer's timing does not tell which accounts exist."""
    verify_password(_DUMMY_HASH, password)


# ---------------------------------------------------------------- login rate limit
# Per address *and* overall: behind `tailscale serve` / Caddy every request comes from the same proxy IP,
# so the per-address key is effectively per e-mail – the overall cap stops guessing across many e-mails.

_FAILS: dict[str, deque[float]] = defaultdict(deque)
MAX_FAILS = 5
MAX_FAILS_ALL = 30
WINDOW_S = 300
ALL = "*"


def login_key(host: str | None, email: str) -> str:
    """The same key however the e-mail is typed – ' Jan@X.pl ' and 'jan@x.pl' are one account."""
    return f"{host or '?'}:{email.strip().lower()}"


def _recent(key: str) -> deque[float]:
    q = _FAILS[key]
    cutoff = time.monotonic() - WINDOW_S
    while q and q[0] < cutoff:
        q.popleft()
    return q


def login_blocked(key: str) -> bool:
    return len(_recent(key)) >= MAX_FAILS or len(_recent(ALL)) >= MAX_FAILS_ALL


def register_fail(key: str) -> None:
    _FAILS[key].append(time.monotonic())
    _FAILS[ALL].append(time.monotonic())


def reset_fails(key: str) -> None:
    _FAILS.pop(key, None)


# ---------------------------------------------------------------- session / current user


class LoginRequired(Exception):
    pass


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    set_viewer(db, user.id)  # this request's session hides other people's private rows
    return user


def current_user(user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        raise LoginRequired()
    return user


# ---------------------------------------------------------------- CSRF


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


FORM_MAX_FIELDS = 10000
# Before login only the small login / logout forms are posted – nobody unknown gets to make the server
# read (and spool to disk) a big body or an upload.
ANON_MAX_BODY = 16 * 1024


def _anonymous_body_ok(request: Request) -> bool:
    if request.session.get("uid"):
        return True
    length = request.headers.get("content-length") or ""
    ctype = request.headers.get("content-type") or ""
    return length.isdigit() and int(length) <= ANON_MAX_BODY and not ctype.startswith("multipart/")


async def verify_csrf(request: Request) -> None:
    """Checks X-CSRF-Token header (htmx) or the csrf form field."""
    if not _anonymous_body_ok(request):
        raise LoginRequired()  # not read at all – an upload after the session ran out goes to the login page
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if not sent:
        # parsed once and cached for the route – allow big forms (a bank statement sends ~7 fields per line)
        form = await request.form(max_fields=FORM_MAX_FIELDS)
        sent = form.get("csrf")
    if not expected or not sent or not secrets.compare_digest(str(sent), expected):
        raise HTTPException(status_code=403, detail="Nieprawidłowy token CSRF – odśwież stronę.")
