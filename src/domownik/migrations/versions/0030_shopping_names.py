"""open shopping rows in the list's own form ('Mleka' → 'Mleko', 'Laska cynamonu' → 'Cynamon · 1 laska'),
rows of the same product merged with quantities added up

Revision ID: 5b8e2d7c4a19
Revises: 2e7c5a9b8d14
Create Date: 2026-11-06 15:20:00
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from domownik.domain.grocery_names import key
from domownik.domain.shopping import add_qty, guess_section, parse_entry

revision: str = "5b8e2d7c4a19"
down_revision: str | None = "2e7c5a9b8d14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, name, qty, section, checked_at FROM shopping_item WHERE bought_on IS NULL ORDER BY id")
    ).all()
    kept: dict[str, dict] = {}
    for r in rows:
        number_only = r.qty and re.fullmatch(r"\d+(?:[.,]\d+)?", r.qty)
        name, qty = parse_entry(f"{r.qty} {r.name}" if number_only else r.name)
        if not name:
            continue
        qty = qty if number_only else (r.qty or qty)
        section = guess_section(name) if r.section == "inne" else r.section
        k = key(name)
        if k in kept:
            first = kept[k]
            first["qty"] = add_qty(first["qty"], qty)
            first["checked_at"] = first["checked_at"] and r.checked_at
            conn.execute(sa.text("DELETE FROM shopping_item WHERE id = :id"), {"id": r.id})
        else:
            kept[k] = {"id": r.id, "name": name, "qty": qty, "section": section, "checked_at": r.checked_at}
    for v in kept.values():
        conn.execute(
            sa.text(
                "UPDATE shopping_item SET name = :name, qty = :qty, section = :section, checked_at = :checked_at"
                " WHERE id = :id"
            ),
            v | {"qty": (v["qty"] or None) and v["qty"][:30]},
        )


def downgrade() -> None:
    pass  # the old spellings are not kept
