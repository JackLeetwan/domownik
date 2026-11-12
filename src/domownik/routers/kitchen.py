"""/kuchnia: week meal plan (jadłospis), recipes (przepisy), pantry (spiżarnia)."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from domownik import documents, kitchen, recipe_link
from domownik.config import today
from domownik.db import get_db
from domownik.domain.shopping import guess_section, parse_entry
from domownik.models import MEALS, PANTRY_PLACES, Attachment, MealPlan, PantryItem, Recipe, User
from domownik.routers.calendar import day_title
from domownik.routers.shopping import add_entries
from domownik.security import current_user, verify_csrf
from domownik.web import quick_saved, render

router = APIRouter(prefix="/kuchnia")


def _redirect(url: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.headers["HX-Redirect"] = url
    return resp


def _error(msg: str) -> HTMLResponse:
    return HTMLResponse(msg, status_code=400, headers={"HX-Reswap": "none"})


def _date(raw, default: date | None = None) -> date | None:
    raw = str(raw or "").strip()
    try:
        return date.fromisoformat(raw) if raw else default
    except ValueError:
        raise HTTPException(400, "Data w formacie RRRR-MM-DD") from None


def _int(raw) -> int | None:
    raw = str(raw or "").strip()
    return int(raw) if raw.isdigit() else None


@router.get("")
def kitchen_home():
    return RedirectResponse("/kuchnia/jadlospis", status_code=303)


# ---------------------------------------------------------------- meal plan


@router.get("/jadlospis")
def plan_page(
    request: Request, t: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    now = today()
    monday = kitchen.week_start(_date(t, now))
    days = kitchen.week_plan(db, monday)
    return render(
        request,
        "kitchen_plan.html",
        {
            "user": user,
            "tab": "plan",
            "days": days,
            "monday": monday,
            "prev": monday - timedelta(days=7),
            "next": monday + timedelta(days=7),
            "today_d": now,
            "MEALS": MEALS,
            "recipes": list(db.scalars(select(Recipe).order_by(Recipe.name))),
            "day_title": day_title,
            "has_recipes_in_week": any(m.recipe_id for d in days for ms in d.meals.values() for m in ms),
        },
    )


@router.post("/jadlospis", dependencies=[Depends(verify_csrf)])
async def plan_add(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    day = _date(form.get("day"))
    if day is None:
        return _error("Wybierz dzień")
    meal = form.get("meal") if form.get("meal") in MEALS else "lunch"
    text = " ".join(str(form.get("what") or "").split())[:200]
    recipe = db.get(Recipe, _int(form.get("recipe_id"))) if _int(form.get("recipe_id")) else None
    recipe = recipe or kitchen.find_recipe(db, text)
    if not recipe and not text:
        return _error("Wpisz, co będzie do jedzenia, albo wybierz przepis")
    db.add(
        MealPlan(
            day=day,
            meal=meal,
            recipe_id=recipe.id if recipe else None,
            text=None if recipe else text,
            created_by_id=user.id,
        )
    )
    db.commit()
    return _redirect(f"/kuchnia/jadlospis?t={kitchen.week_start(day).isoformat()}#d{day.isoformat()}")


@router.post("/jadlospis/{plan_id}/usun", dependencies=[Depends(verify_csrf)])
def plan_delete(plan_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.get(MealPlan, plan_id)
    if row is None:
        raise HTTPException(404, "Nie ma takiej pozycji")
    week = kitchen.week_start(row.day)
    db.delete(row)
    db.commit()
    return _redirect(f"/kuchnia/jadlospis?t={week.isoformat()}")


# ---------------------------------------------------------------- ingredients → shopping list


def _shop_modal(request: Request, db: Session, user: User, recipes: list[Recipe], title: str) -> HTMLResponse:
    pantry = kitchen.pantry_items(db)
    return render(
        request,
        "partials/kitchen_shop.html",
        {"user": user, "needs": kitchen.needs(recipes, pantry), "title": title, "many": len(recipes) > 1},
    )


@router.get("/jadlospis/zakupy")
def plan_shopping(
    request: Request, t: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    monday = kitchen.week_start(_date(t, today()))
    recipes, seen = [], set()
    for d in kitchen.week_plan(db, monday):
        for ms in d.meals.values():
            for m in ms:
                if m.recipe and m.recipe.id not in seen:
                    seen.add(m.recipe.id)
                    recipes.append(m.recipe)
    return _shop_modal(request, db, user, recipes, f"Składniki na tydzień od {monday.day}.{monday.month:02d}")


@router.post("/do-zakupow", dependencies=[Depends(verify_csrf)])
async def to_shopping(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    entries = [str(e) for e in form.getlist("entry") if str(e).strip()]
    if not entries:
        return _error("Zaznacz, co dopisać do zakupów")
    added = add_entries(db, entries, user=user, add_up=True)
    db.commit()
    already = len(entries) - added
    msg = f"Dopisano do zakupów: {added}" + (f" (już na liście – ilość dodana: {already})" if already else "")
    return quick_saved(msg)


# ---------------------------------------------------------------- recipes


def _fill_recipe(r: Recipe, form) -> None:
    name = " ".join(str(form.get("name") or "").split())[:120]
    if not name:
        raise ValueError("Podaj nazwę przepisu")
    r.name = name
    r.ingredients = str(form.get("ingredients") or "").strip() or None
    r.steps = str(form.get("steps") or "").strip() or None
    link = str(form.get("link") or "").strip()[:300]
    if link and not link.startswith(("http://", "https://")):
        link = "https://" + link
    r.link = link or None
    r.servings = _int(form.get("servings"))
    r.minutes = _int(form.get("minutes"))
    r.tags = ", ".join(t.strip() for t in str(form.get("tags") or "").split(",") if t.strip())[:120] or None


@router.get("/przepisy")
def recipes_page(
    request: Request,
    q: str | None = None,
    tag: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    allr = list(db.scalars(select(Recipe).options(selectinload(Recipe.photos)).order_by(Recipe.name)))
    tags = sorted({t for r in allr for t in r.tag_list}, key=str.lower)
    shown = allr
    if q:
        ql = q.lower()
        shown = [
            r
            for r in shown
            if ql in r.name.lower() or ql in (r.ingredients or "").lower() or ql in (r.tags or "").lower()
        ]
    if tag:
        shown = [r for r in shown if tag in r.tag_list]
    return render(
        request,
        "kitchen_recipes.html",
        {"user": user, "tab": "recipes", "recipes": shown, "total": len(allr), "tags": tags, "q": q or "", "tag": tag},
    )


@router.get("/przepisy/nowy")
def recipe_new(request: Request, user: User = Depends(current_user)):
    return render(request, "kitchen_recipe_form.html", {"user": user, "tab": "recipes", "r": None})


@router.post("/przepisy", dependencies=[Depends(verify_csrf)])
async def recipe_create(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    r = Recipe(created_by_id=user.id)
    try:
        _fill_recipe(r, form)
    except ValueError as exc:
        return _error(str(exc))
    db.add(r)
    db.commit()
    return _redirect(f"/kuchnia/przepisy/{r.id}")


async def _import_link(db: Session, user: User, link: str) -> Recipe:
    """A recipe link (Cookidoo or any recipe site) → a recipe here (the one imported before, if any – no
    duplicate). Raises RecipeLinkError."""
    url = recipe_link.find_url(link)
    same = db.scalar(select(Recipe).where(Recipe.link == url.split("?")[0].split("#")[0]))
    if same is not None:
        return same
    got = await run_in_threadpool(recipe_link.load, url)
    same = db.scalar(select(Recipe).where(Recipe.link == got.url))
    if same is not None:
        return same
    r = Recipe(
        created_by_id=user.id,
        name=got.name,
        ingredients="\n".join(got.ingredients) or None,
        steps="\n".join(got.steps) or None,
        link=got.url[:300],
        minutes=got.minutes,
        servings=got.servings,
        tags=", ".join([recipe_link.site(got.url), *got.tags])[:120],
    )
    db.add(r)
    db.commit()
    return r


@router.post("/przepisy/z-linku", dependencies=[Depends(verify_csrf)])
async def recipe_from_link(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """A recipe link → a recipe here; the recipe page then opens 'do zakupów' at once."""
    form = await request.form()
    try:
        r = await _import_link(db, user, str(form.get("link") or ""))
    except recipe_link.RecipeLinkError as exc:
        return _error(str(exc))
    return _redirect(f"/kuchnia/przepisy/{r.id}?zakupy=1")


# ---------------------------------------------------------------- from the shopping list: pick a recipe


def _matching(recipes: list[Recipe], q: str) -> list[Recipe]:
    ql = q.strip().lower()
    if not ql:
        return recipes
    return [
        r
        for r in recipes
        if ql in r.name.lower() or ql in (r.ingredients or "").lower() or ql in (r.tags or "").lower()
    ]


@router.get("/do-zakupow/przepis")
def pick_recipe(request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    """/zakupy → '📖 Z przepisu': a window with the recipes (pick one → its ingredients) or a recipe link.
    With HX-Target 'pick-recipe-list' (typing in the search box) only the list comes back."""
    allr = [r for r in db.scalars(select(Recipe).order_by(Recipe.name)) if r.ingredient_list]
    shown = _matching(allr, q)
    only_list = request.headers.get("hx-target") == "pick-recipe-list"
    return render(
        request,
        "partials/kitchen_pick_recipe.html",
        {"user": user, "recipes": shown, "total": len(allr), "q": q, "only_list": only_list},
    )


@router.post("/do-zakupow/z-linku", dependencies=[Depends(verify_csrf)])
async def link_to_shopping(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """A recipe link from the shopping list: imported (or found) and straight to its ingredients."""
    form = await request.form()
    try:
        r = await _import_link(db, user, str(form.get("link") or ""))
    except recipe_link.RecipeLinkError as exc:
        return _error(str(exc))
    return _shop_modal(request, db, user, [r], f"Składniki: {r.name}")


def _recipe(db: Session, rid: int) -> Recipe:
    r = db.get(Recipe, rid)
    if r is None:
        raise HTTPException(404, "Nie ma takiego przepisu")
    return r


@router.get("/przepisy/{rid}")
def recipe_page(
    request: Request,
    rid: int,
    zakupy: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    r = _recipe(db, rid)
    pantry = kitchen.pantry_items(db)
    upcoming = list(
        db.scalars(select(MealPlan).where(MealPlan.recipe_id == r.id, MealPlan.day >= today()).order_by(MealPlan.day))
    )
    return render(
        request,
        "kitchen_recipe.html",
        {
            "user": user,
            "tab": "recipes",
            "r": r,
            "needs": kitchen.needs([r], pantry),
            "upcoming": upcoming,
            "MEALS": MEALS,
            "today_d": today(),
            "open_shop": bool(zakupy),
        },
    )


@router.get("/przepisy/{rid}/zakupy")
def recipe_shopping(request: Request, rid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    r = _recipe(db, rid)
    return _shop_modal(request, db, user, [r], f"Składniki: {r.name}")


@router.get("/przepisy/{rid}/edycja")
def recipe_edit_form(request: Request, rid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "kitchen_recipe_form.html", {"user": user, "tab": "recipes", "r": _recipe(db, rid)})


@router.post("/przepisy/{rid}/edycja", dependencies=[Depends(verify_csrf)])
async def recipe_update(request: Request, rid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    r = _recipe(db, rid)
    form = await request.form()
    try:
        _fill_recipe(r, form)
    except ValueError as exc:
        db.rollback()
        return _error(str(exc))
    db.commit()
    return _redirect(f"/kuchnia/przepisy/{r.id}")


@router.post("/przepisy/{rid}/usun", dependencies=[Depends(verify_csrf)])
def recipe_delete(rid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Days in the plan that used it keep its name as plain text."""
    r = _recipe(db, rid)
    for m in db.scalars(select(MealPlan).where(MealPlan.recipe_id == r.id)):
        m.text, m.recipe_id = r.name, None
    for att in list(r.photos):
        documents.delete_attachment(db, att)  # the files too, not only the rows
    db.delete(r)
    db.commit()
    return _redirect("/kuchnia/przepisy")


MAX_RECIPE_PHOTOS = 12


def _photos(request: Request, r: Recipe):
    return render(request, "partials/recipe_photos.html", {"r": r})


@router.post("/przepisy/{rid}/zdjecia", dependencies=[Depends(verify_csrf)])
async def add_recipe_photos(
    request: Request, rid: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """The cookbook page, the dish as served – all saved or none; 422 with the reason."""
    r = _recipe(db, rid)
    files = documents.uploads_in(await request.form())
    if len(r.photos) + len(files) > MAX_RECIPE_PHOTOS:
        return _error(f"Przepis może mieć najwyżej {MAX_RECIPE_PHOTOS} zdjęć")
    try:
        documents.store_files(db, None, [(f.filename or "", f.file) for f in files], user=user, recipe_id=r.id)
    except documents.UploadError as exc:
        db.rollback()
        return _error(str(exc))
    db.commit()
    db.refresh(r)
    return _photos(request, r)


@router.post("/przepisy/{rid}/zdjecia/{att_id}/usun", dependencies=[Depends(verify_csrf)])
def delete_recipe_photo(
    request: Request, rid: int, att_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    r = _recipe(db, rid)
    att = db.get(Attachment, att_id)
    if att is None or att.recipe_id != r.id:
        raise HTTPException(404, "Nie ma takiego zdjęcia")
    documents.delete_attachment(db, att)
    db.commit()
    db.refresh(r)
    return _photos(request, r)


# ---------------------------------------------------------------- pantry


@router.get("/spizarnia")
def pantry_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    t = today()
    items = kitchen.pantry_sorted(kitchen.pantry_items(db))
    places = [
        (k, lbl, [p for p in items if (p.place if p.place in PANTRY_PLACES else "pantry") == k])
        for k, lbl in PANTRY_PLACES.items()
    ]
    return render(
        request,
        "kitchen_pantry.html",
        {
            "user": user,
            "tab": "pantry",
            "places": places,
            "total": len(items),
            "alerts": [p for p in items if kitchen.pantry_status(p, t) in ("expired", "soon")],
            "status": lambda p: kitchen.pantry_status(p, t),
            "PANTRY_PLACES": PANTRY_PLACES,
            "today_d": t,
        },
    )


@router.post("/spizarnia", dependencies=[Depends(verify_csrf)])
async def pantry_add(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Several at once like the shopping list: 'mleko, 2 jogurty, mąka 1 kg'."""
    form = await request.form()
    place = form.get("place") if form.get("place") in PANTRY_PLACES else "pantry"
    expires = _date(form.get("expires_on"))
    added = 0
    for part in str(form.get("text") or "").replace("\n", ",").split(","):
        name, qty = parse_entry(part)
        if name:
            db.add(PantryItem(name=name[:120], qty=qty, place=place, expires_on=expires, added_by_id=user.id))
            added += 1
    if not added:
        return _error("Wpisz, co jest w domu, np. „mleko, 2 jogurty”")
    db.commit()
    if request.headers.get("x-quick-add") == "1":
        return quick_saved("Dodano do spiżarni")
    return _redirect("/kuchnia/spizarnia")


def _pantry(db: Session, pid: int) -> PantryItem:
    p = db.get(PantryItem, pid)
    if p is None:
        raise HTTPException(404, "Nie ma takiej pozycji")
    return p


@router.post("/spizarnia/{pid}/edycja", dependencies=[Depends(verify_csrf)])
async def pantry_edit(request: Request, pid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = _pantry(db, pid)
    form = await request.form()
    p.name = " ".join(str(form.get("name") or "").split())[:120] or p.name
    p.qty = str(form.get("qty") or "").strip()[:30] or None
    p.place = form.get("place") if form.get("place") in PANTRY_PLACES else p.place
    p.expires_on = _date(form.get("expires_on"))
    db.commit()
    return _redirect("/kuchnia/spizarnia")


@router.post("/spizarnia/{pid}/usun", dependencies=[Depends(verify_csrf)])
def pantry_used(pid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.delete(_pantry(db, pid))
    db.commit()
    return _redirect("/kuchnia/spizarnia")


@router.post("/spizarnia/{pid}/kup", dependencies=[Depends(verify_csrf)])
def pantry_rebuy(pid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Used up → on the shopping list, gone from the pantry."""
    p = _pantry(db, pid)
    add_entries(db, [p.name], user=user)
    db.delete(p)
    db.commit()
    return _redirect("/kuchnia/spizarnia")


def bought_to_pantry(db: Session, items, user: User) -> int:
    """'Zakończ zakupy' with 'do spiżarni': each bought thing lands at home, fridge / freezer guessed."""
    n = 0
    for i in items:
        place = kitchen.PLACE_BY_SECTION.get(i.section or guess_section(i.name), "pantry")
        db.add(PantryItem(name=i.name, qty=i.qty, place=place, added_by_id=user.id))
        n += 1
    return n
