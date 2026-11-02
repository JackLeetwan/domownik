from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON}


ITEM_KINDS: dict[str, tuple[str, str]] = {
    # kind: (label, default icon)
    "group": ("Grupa", "📁"),
    "car": ("Samochód", "🚗"),
    "house": ("Dom", "🏠"),
    "apartment": ("Mieszkanie", "🏢"),
    "rental": ("Mieszkanie na wynajem", "🔑"),
    "appliance": ("Urządzenie", "🔧"),
    "member": ("Domownik", "🧑"),
    "child": ("Dziecko", "🧒"),
    "person": ("Osoba", "🙂"),
    "other": ("Inne", "📦"),
}

MEMBER_KINDS = ("member", "child")

CATEGORIES: dict[str, tuple[str, str]] = {
    # category: (label, icon)
    "insurance": ("Ubezpieczenie", "🛡️"),
    "inspection": ("Przegląd", "🔍"),
    "service": ("Serwis", "🛠️"),
    "diy": ("Do zrobienia samemu", "🧽"),
    "payment": ("Płatność", "💳"),
    "income": ("Przychód", "💰"),
    "tax": ("Podatek", "🧾"),
    "health": ("Zdrowie", "🩺"),
    "event": ("Wydarzenie", "📅"),
    "occasion": ("Okazja / prezent", "🎁"),
    "shopping": ("Zakupy", "🛒"),
    "other": ("Inne", "📌"),
}

DIRECTIONS = {"expense": "Wydatek", "income": "Przychód", "none": "Bez kwoty"}


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    notify_morning: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    notify_evening: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PushSubscription(Base):
    """One browser / phone that agreed to receive Web Push notifications."""

    __tablename__ = "push_subscription"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    label: Mapped[str | None] = mapped_column(String(120))  # "Android · Chrome" – to tell devices apart
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped[User] = relationship()

    def info(self) -> dict:
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


class NotificationLog(Base):
    """What was pushed to whom – deduplication of digests and per-task reminders.

    kind: "morning" / "evening" with task_id NULL = the digest slot ran that day;
    with task_id = this occurrence (ref_date = its due date) was announced as
    "task" (entered the reminder window), "today", "overdue" or "tomorrow".
    """

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    ref_date: Mapped[date | None] = mapped_column(Date)
    sent_on: Mapped[date] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Item(Base):
    """A node in the zone tree: zone (root), group, car, appliance, person…"""

    __tablename__ = "item"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="other")
    name: Mapped[str] = mapped_column(String(120))
    icon: Mapped[str | None] = mapped_column(String(16))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    notes: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    parent: Mapped[Item | None] = relationship(remote_side="Item.id", back_populates="children")
    children: Mapped[list[Item]] = relationship(back_populates="parent", order_by="Item.sort_order, Item.name")
    tasks: Mapped[list[Task]] = relationship(back_populates="item", cascade="all, delete-orphan")
    issues: Mapped[list[Issue]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="Issue.started_on.desc(), Issue.id.desc()"
    )
    todos: Mapped[list[Todo]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="Todo.sort_order, Todo.id"
    )
    journal: Mapped[list[JournalEntry]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        order_by="JournalEntry.noted_on.desc(), JournalEntry.id.desc()",
    )

    @property
    def display_icon(self) -> str:
        return self.icon or ITEM_KINDS.get(self.kind, ("", "📦"))[1]

    @property
    def is_member(self) -> bool:
        """Household member – gets the 'Zdrowie i sprawy' section and a tile in 'Domownicy'."""
        return self.kind in MEMBER_KINDS


class BudgetCategory(Base):
    __tablename__ = "budget_category"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("budget_category.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    # income | fixed | planned | variable
    kind: Mapped[str] = mapped_column(String(20))
    icon: Mapped[str | None] = mapped_column(String(16))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Task(Base):
    __tablename__ = "task"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(20), default="other")
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    recurrence: Mapped[dict[str, Any]] = mapped_column(default=lambda: {"mode": "once"})
    lead_days: Mapped[int] = mapped_column(Integer, default=7)
    next_due_date: Mapped[date | None] = mapped_column(Date, index=True)
    next_due_km: Mapped[int | None] = mapped_column(Integer)
    # Money in grosze (1/100 PLN); amount is the planned value of one occurrence
    amount_gr: Mapped[int | None] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(10), default="expense")
    budget_category_id: Mapped[int | None] = mapped_column(ForeignKey("budget_category.id", ondelete="SET NULL"))
    autopay: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    gcal_event_id: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    item: Mapped[Item] = relationship(back_populates="tasks")
    assignee: Mapped[User | None] = relationship()
    budget_category: Mapped[BudgetCategory | None] = relationship()
    completions: Mapped[list[Completion]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="Completion.done_on.desc()"
    )

    @property
    def category_icon(self) -> str:
        return CATEGORIES.get(self.category, ("", "📌"))[1]

    @property
    def category_label(self) -> str:
        return CATEGORIES.get(self.category, ("Inne", ""))[0]


class Completion(Base):
    __tablename__ = "completion"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("task.id", ondelete="CASCADE"), index=True)
    due_date: Mapped[date | None] = mapped_column(Date)  # which occurrence was ticked off
    done_on: Mapped[date] = mapped_column(Date)
    km: Mapped[int | None] = mapped_column(Integer)
    actual_amount_gr: Mapped[int | None] = mapped_column(Integer)
    vendor: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    performed_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    task: Mapped[Task] = relationship(back_populates="completions")
    performed_by: Mapped[User | None] = relationship()


class Measurement(Base):
    __tablename__ = "measurement"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    # odometer | water_cold | water_hot | electricity | gas | heat | height | shoe_size | clothes_size
    kind: Mapped[str] = mapped_column(String(20))
    measured_on: Mapped[date] = mapped_column(Date)
    value: Mapped[float]
    note: Mapped[str | None] = mapped_column(String(200))


class Transaction(Base):
    __tablename__ = "transaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    booked_on: Mapped[date] = mapped_column(Date, index=True)
    # + income / − expense, grosze
    amount_gr: Mapped[int] = mapped_column(Integer)
    budget_category_id: Mapped[int | None] = mapped_column(ForeignKey("budget_category.id", ondelete="SET NULL"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("item.id", ondelete="SET NULL"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id", ondelete="SET NULL"))
    completion_id: Mapped[int | None] = mapped_column(ForeignKey("completion.id", ondelete="CASCADE"))
    note: Mapped[str | None] = mapped_column(String(300))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Issue(Base):
    """Something wrong that lasts: a fault (knocking suspension) or a health problem (cough, knee pain…)."""

    __tablename__ = "issue"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    started_on: Mapped[date] = mapped_column(Date)
    resolved_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    item: Mapped[Item] = relationship(back_populates="issues")


class Todo(Base):
    """Something to do some day, without a date ("polish the headlights"). Gets a date → becomes a Task."""

    __tablename__ = "todo"

    id: Mapped[int] = mapped_column(primary_key=True)
    # None = general household matter, not tied to a car/house/person
    item_id: Mapped[int | None] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    est_amount_gr: Mapped[int | None] = mapped_column(Integer)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    done_on: Mapped[date | None] = mapped_column(Date)
    done_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    item: Mapped[Item | None] = relationship(back_populates="todos")
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])
    done_by: Mapped[User | None] = relationship(foreign_keys=[done_by_id])


class JournalEntry(Base):
    """Dated observation about an item ("knocking from the front when turning left")."""

    __tablename__ = "journal_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    noted_on: Mapped[date] = mapped_column(Date)
    body: Mapped[str] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    item: Mapped[Item] = relationship(back_populates="journal")
    created_by: Mapped[User | None] = relationship()


class ShoppingItem(Base):
    """One position on the shared shopping list: open → in cart (checked_at) → bought (bought_on)."""

    __tablename__ = "shopping_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    qty: Mapped[str | None] = mapped_column(String(30))
    section: Mapped[str] = mapped_column(String(30), default="inne")
    note: Mapped[str | None] = mapped_column(String(200))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("item.id", ondelete="SET NULL"))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    bought_on: Mapped[date | None] = mapped_column(Date, index=True)
    added_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    item: Mapped[Item | None] = relationship()
    added_by: Mapped[User | None] = relationship()


class BoardNote(Base):
    """A sticky note on the shared board ("Wi-Fi gości: …", "do piątku odebrać paczkę")."""

    __tablename__ = "board_note"

    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_until: Mapped[date | None] = mapped_column(Date)  # after this day it goes to the archive
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_by: Mapped[User | None] = relationship()

    def is_active(self, today: date) -> bool:
        return not self.archived and (self.valid_until is None or self.valid_until >= today)
