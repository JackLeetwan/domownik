from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domownik.db import get_db
from domownik.models import User
from domownik.security import (
    burn_password_check,
    current_user_optional,
    login_blocked,
    login_key,
    register_fail,
    reset_fails,
    verify_csrf,
    verify_password,
)
from domownik.web import render

router = APIRouter()


@router.get("/login")
def login_form(request: Request, user: User | None = Depends(current_user_optional), db: Session = Depends(get_db)):
    if user:
        return RedirectResponse("/", status_code=303)
    no_users = db.scalar(select(func.count(User.id))) == 0
    return render(request, "login.html", {"no_users": no_users})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    key = login_key(request.client.host if request.client else None, email)
    if login_blocked(key):
        return render(
            request, "login.html", {"error": "Za dużo prób. Spróbuj za kilka minut.", "email": email}, status_code=429
        )
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if user is None:
        burn_password_check(password)
    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        register_fail(key)
        return render(
            request, "login.html", {"error": "Nieprawidłowy e-mail lub hasło.", "email": email}, status_code=401
        )
    reset_fails(key)
    request.session.clear()
    request.session["uid"] = user.id
    resp = RedirectResponse("/", status_code=303)
    resp.headers["HX-Redirect"] = "/"
    return resp


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    resp = RedirectResponse("/login", status_code=303)
    resp.headers["HX-Redirect"] = "/login"
    return resp
