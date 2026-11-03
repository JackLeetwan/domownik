"""'🔒 Tylko dla mnie' on tasks, notes and todos – what the forms set (filtering itself: db._private_filter)."""

from __future__ import annotations

from domownik.models import User

FIELD = "private_field"  # hidden marker: the form shows the switch (quick forms without it change nothing)


def apply(obj, form, user: User) -> None:
    """Set / clear obj.private_to_id from the form's switch. Raises ValueError for a private thing given away."""
    if FIELD not in form:
        return
    private = bool(form.get("private"))
    if private and getattr(obj, "assignee_id", None) not in (None, user.id):
        raise ValueError("Prywatne może być tylko Twoje – zmień „Kto” na siebie albo nikogo")
    if private:
        obj.private_to_id = user.id
    elif obj.private_to_id in (None, user.id):  # only the owner can share it (others never see it anyway)
        obj.private_to_id = None
