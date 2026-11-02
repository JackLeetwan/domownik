from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from domownik.db import get_db
from domownik.models import User

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerificationError, InvalidHashError:
        return False


# ---------------------------------------------------------------- login rate limit

_FAILS: dict[str, deque[float]] = defaultdict(deque)
MAX_FAILS = 5
WINDOW_S = 300


def login_blocked(key: str) -> bool:
    q = _FAILS[key]
    cutoff = time.monotonic() - WINDOW_S
    while q and q[0] < cutoff:
        q.popleft()
    return len(q) >= MAX_FAILS


def register_fail(key: str) -> None:
    _FAILS[key].append(time.monotonic())


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


async def verify_csrf(request: Request) -> None:
    """Checks X-CSRF-Token header (htmx) or the csrf form field."""
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf")
    if not expected or not sent or not secrets.compare_digest(str(sent), expected):
        raise HTTPException(status_code=403, detail="Nieprawidłowy token CSRF – odśwież stronę.")
