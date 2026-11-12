from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, object_session, relationship

from domownik.domain import recurrence as _recurrence


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
    "warranty": ("Gwarancja / umowa", "📄"),
    "document": ("Dokument", "🪪"),
    "other": ("Inne", "📌"),
}

DIRECTIONS = {"expense": "Wydatek", "income": "Przychód", "none": "Bez kwoty"}

# a counted series in these categories is a run of instalments ("rata 3/6"); elsewhere just "3/6"
INSTALMENT_CATEGORIES = ("payment", "tax", "insurance", "income")


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    notify_morning: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    notify_evening: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # calendar subscription (/ics/<token>.ics): only the SHA-256 of the random token is kept
    ics_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    ics_scope: Mapped[str] = mapped_column(String(10), default="all", server_default="all")  # all | mine
    # own menu: {"bar": [two shortcuts of the phone's bottom bar], "hidden": [menu links not used]}
    menu: Mapped[dict[str, Any] | None] = mapped_column(JSON)
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


class Timer(Base):
    """A countdown ("Pranie – 2 h"): one push to whoever started it when it ends, and one to everyone 15 min
    later if nobody reacted. Not a Task – it has no date, never reaches the timeline, budget or history.

    Times are naive local, like the rest of the scheduler. finished_at is set by "Zrobione" and "Anuluj".
    """

    __tablename__ = "timer"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(80))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    minutes: Mapped[int] = mapped_column(Integer)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    renotified_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    # started for a chore ("Pranie"): "Zrobione" ticks the chore off as well
    chore_id: Mapped[int | None] = mapped_column(ForeignKey("chore.id", ondelete="SET NULL"))
    # the next step offered after it ("Suszarka – 2 h?") was waved away, or the timer was cancelled
    followup_dismissed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped[User | None] = relationship()
    chore: Mapped[Chore | None] = relationship()


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
    # kind == "member": the Domownik account of this person (their assigned tasks, notifications)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped[User | None] = relationship()
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
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        order_by="Attachment.created_at.desc(), Attachment.id.desc()",
    )
    warranties: Mapped[list[Warranty]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="Warranty.ends_on, Warranty.id"
    )
    contacts: Mapped[list[Contact]] = relationship(
        secondary="contact_item", back_populates="items", order_by="Contact.name"
    )
    meters: Mapped[list[Meter]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="Meter.sort_order, Meter.id"
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
    # envelope: how much may go out of this category in a month (on top of the planned task dates)
    monthly_limit_gr: Mapped[int | None] = mapped_column(Integer)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    @property
    def is_income(self) -> bool:
        return self.kind == "income"


BUDGET_KINDS: dict[str, str] = {
    "income": "Dochody",
    "fixed": "Stałe opłaty",
    "planned": "Zaplanowane",
    "variable": "Codzienne (koperty)",
}


class RecurringIncome(Base):
    """Money that comes every month (salary, rent from a tenant), confirmed month by month in /budzet.

    Not a Task on purpose: no reminders, not on the timeline, and each month is confirmed on its own
    (a late salary does not hold the next month back).
    """

    __tablename__ = "recurring_income"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    amount_gr: Mapped[int] = mapped_column(Integer)  # planned, positive
    day: Mapped[int] = mapped_column(Integer, default=10)  # expected day of the month (31 = last day)
    budget_category_id: Mapped[int | None] = mapped_column(ForeignKey("budget_category.id", ondelete="SET NULL"))
    # zone it belongs to (rent from a tenant → the rented flat) – counted in the yearly zone costs
    item_id: Mapped[int | None] = mapped_column(ForeignKey("item.id", ondelete="SET NULL"))
    starts_on: Mapped[date] = mapped_column(Date)  # first month it is expected (1st day of that month)
    ends_on: Mapped[date | None] = mapped_column(Date)  # last day it is expected; None = open-ended
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    budget_category: Mapped[BudgetCategory | None] = relationship()
    item: Mapped[Item | None] = relationship()

    def expected_in(self, first: date, last: date) -> bool:
        return self.starts_on <= last and (self.ends_on is None or self.ends_on >= first)


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
    # optional hour of the due day ("14:30") – an appointment; ends_at only for a span ("14:30–15:30")
    due_time: Mapped[str | None] = mapped_column(String(5))
    ends_at: Mapped[str | None] = mapped_column(String(5))
    # push N minutes before due_time (None = only the morning / evening digest)
    remind_minutes: Mapped[int | None] = mapped_column(Integer)
    # points for whoever ticks it off; with needs_approval someone else has to confirm them
    points: Mapped[int | None] = mapped_column(Integer)
    needs_approval: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # visible only to this account (None = shared with the household) – see db.private_filter
    private_to_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
    # part of a health issue ("Kolano": the follow-up visit, "Sprawdzić decyzję PZU") – shown on its page
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issue.id", ondelete="SET NULL"), index=True)
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
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])
    budget_category: Mapped[BudgetCategory | None] = relationship()
    completions: Mapped[list[Completion]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="Completion.done_on.desc()"
    )
    # files stay with the item (Dokumenty) when the task goes – hence no delete cascade
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="task", passive_deletes=True, order_by="Attachment.id"
    )
    comments: Mapped[list[Comment]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="Comment.id"
    )
    steps: Mapped[list[TaskStep]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskStep.sort_order, TaskStep.id"
    )

    @property
    def steps_done(self) -> int:
        return sum(1 for s in self.steps if s.done)

    @property
    def category_icon(self) -> str:
        return CATEGORIES.get(self.category, ("", "📌"))[1]

    @property
    def category_label(self) -> str:
        return CATEGORIES.get(self.category, ("Inne", ""))[0]

    def series_label_at(self, offset: int = 0) -> str | None:
        """'rata 3/6' for the current occurrence of a counted series (offset = occurrences later)."""
        pos = _recurrence.series_position(self.recurrence or {})
        if pos is None:
            return None
        k, n = min(pos[0] + offset, pos[1]), pos[1]
        return f"rata {k}/{n}" if self.category in INSTALMENT_CATEGORIES else f"{k}/{n}"

    @property
    def series_label(self) -> str | None:
        return self.series_label_at(0)

    def title_at(self, offset: int = 0) -> str:
        """Title with the series position, for places that show plain text (timeline, notifications)."""
        lbl = self.series_label_at(offset)
        return f"{self.title} ({lbl})" if lbl else self.title

    @property
    def full_title(self) -> str:
        return self.title_at(0)

    @property
    def time_label(self) -> str | None:
        """'14:30' or '14:30–15:30' – None for an all-day task."""
        if not self.due_time:
            return None
        return f"{self.due_time}–{self.ends_at}" if self.ends_at else self.due_time


class TaskStep(Base):
    """One point of a task's checklist ("zdjąć filtr", "przepłukać"). Unticked again for the next occurrence."""

    __tablename__ = "task_step"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("task.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    done: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    task: Mapped[Task] = relationship(back_populates="steps")


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
    # invoice / receipt photographed when ticking the task off (the files stay on the item if this goes)
    attachments: Mapped[list[Attachment]] = relationship(back_populates="completion", passive_deletes=True)


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
    # confirmation of a monthly income ("✓ Wpłynęło")
    income_id: Mapped[int | None] = mapped_column(ForeignKey("recurring_income.id", ondelete="SET NULL"))
    # money put into (−) or taken back out of (+) a savings goal
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("savings_goal.id", ondelete="SET NULL"), index=True)
    note: Mapped[str | None] = mapped_column(String(300))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    budget_category: Mapped[BudgetCategory | None] = relationship()
    item: Mapped[Item | None] = relationship()
    task: Mapped[Task | None] = relationship()
    completion: Mapped[Completion | None] = relationship()
    created_by: Mapped[User | None] = relationship()


class SavingsGoal(Base):
    """Money put aside for something ("Wakacje 2027: 6000 zł"). Deposits are budget transactions with goal_id."""

    __tablename__ = "savings_goal"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    icon: Mapped[str | None] = mapped_column(String(16))
    target_gr: Mapped[int] = mapped_column(Integer)
    due_on: Mapped[date | None] = mapped_column(Date)
    done_on: Mapped[date | None] = mapped_column(Date)  # reached / closed – moves to "Zrealizowane"
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
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
    # visible only to this account (None = shared with the household) – see db.private_filter
    private_to_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
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
    # visible only to this account (None = shared with the household) – see db.private_filter
    private_to_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
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


class StandingMark(Base):
    """What someone said about a standing payment found in the bank (domownik.recurring): not one ("skip"),
    or confirmed over ("ended" – it comes back if a charge after `last_on` shows up)."""

    __tablename__ = "standing_mark"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)  # recurring.key of the payee
    state: Mapped[str] = mapped_column(String(10))  # skip | ended
    last_on: Mapped[date | None] = mapped_column(Date)  # the last charge when it was marked
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LoyaltyCard(Base):
    """A store's loyalty card of the house (shared): its number and the kind of barcode the till scans."""

    __tablename__ = "loyalty_card"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    code: Mapped[str] = mapped_column(String(120))
    fmt: Mapped[str] = mapped_column(String(20), default="code128")  # loyalty.FORMATS
    note: Mapped[str | None] = mapped_column(String(120))
    site: Mapped[str | None] = mapped_column(String(120))  # the shop's website – its logo and colour come from it
    color: Mapped[str | None] = mapped_column(String(7))  # "#c8102e"
    logo_path: Mapped[str | None] = mapped_column(String(120))  # under uploads/, see loyalty.logo_file
    used_at: Mapped[datetime | None] = mapped_column(DateTime)  # shown at a till – the most used come first
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    @property
    def text_color(self) -> str:
        from domownik.brands import text_on

        return text_on(self.color)

    @property
    def initial(self) -> str:
        return (self.name.strip()[:1] or "?").upper()

    @property
    def hue(self) -> int:
        """A colour of its own (from the name, stable) – the tiles are told apart at a glance."""
        return sum(ord(ch) * (i + 1) for i, ch in enumerate(self.name.lower())) * 47 % 360


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
    title: Mapped[str | None] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    # checklist note: [{"t": "mleko", "d": false}, …] – None for a plain text note
    items: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    color: Mapped[str | None] = mapped_column(String(12))  # NOTE_COLORS key; None = by its label (see shade)
    label: Mapped[str | None] = mapped_column(String(40))  # free category ("Rachunki", "Wakacje")
    # visible only to this account (None = shared with the household) – see db.private_filter
    private_to_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_until: Mapped[date | None] = mapped_column(Date)  # after this day it goes to the archive
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    comments: Mapped[list[Comment]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="Comment.id"
    )
    # photos (the files are removed by board.delete_note, the rows also by the FK)
    photos: Mapped[list[Attachment]] = relationship(
        primaryjoin="BoardNote.id == foreign(Attachment.note_id)", viewonly=True, order_by="Attachment.id"
    )

    def is_active(self, today: date) -> bool:
        return not self.archived and (self.valid_until is None or self.valid_until >= today)

    @property
    def shade(self) -> str | None:
        """The card colour: the chosen one, else one per label (in the order the labels appeared), else yellow."""
        if self.color or not self.label:
            return self.color
        db = object_session(self)
        return note_label_shades(db, self.label).get(self.label) if db is not None else None

    @property
    def is_list(self) -> bool:
        return self.items is not None

    @property
    def items_done(self) -> int:
        return sum(1 for i in self.items or [] if i.get("d"))

    @property
    def text(self) -> str:
        """Everything searchable / previewable as one string."""
        parts = [self.title or "", self.body or ""] + [i.get("t", "") for i in self.items or []]
        return " · ".join(p for p in parts if p)

    @property
    def preview(self) -> str:
        if self.is_list:
            head = self.title or "Lista"
            return f"{head} ({self.items_done}/{len(self.items or [])})"
        return f"{self.title}: {self.body}" if self.title else self.body


NOTE_COLORS: dict[str, str] = {
    "yellow": "Żółta",
    "green": "Zielona",
    "blue": "Niebieska",
    "pink": "Różowa",
    "gray": "Szara",
    "orange": "Pomarańczowa",
    "purple": "Fioletowa",
    "teal": "Turkusowa",
}


class NoteLabelColor(Base):
    """The colour of a whole note category ("Fit" – pink), chosen on the notes board; overrides AUTO_SHADES."""

    __tablename__ = "note_label_color"

    label: Mapped[str] = mapped_column(String(40), primary_key=True)
    color: Mapped[str] = mapped_column(String(12))


# colours of labelled notes without their own colour – yellow stays for notes without a label
AUTO_SHADES = ("blue", "green", "pink", "orange", "purple", "teal", "gray")


def note_label_shades(db, label: str | None = None) -> dict[str, str]:
    """{label: colour}, in the order the labels appeared – a new label never recolours the old ones.
    Cached for the request (db.info); a label missing from the cache reloads it."""
    shades = db.info.get("note_label_shades")
    if shades is None or (label is not None and label not in shades):
        rows = db.execute(
            select(BoardNote.label)
            .where(BoardNote.label.is_not(None))
            .group_by(BoardNote.label)
            .order_by(func.min(BoardNote.id))
        )
        shades = {lbl: AUTO_SHADES[i % len(AUTO_SHADES)] for i, (lbl,) in enumerate(rows)}
        shades |= {c.label: c.color for c in db.scalars(select(NoteLabelColor)) if c.label in shades}  # set by hand
        db.info["note_label_shades"] = shades
    return shades


DOC_KINDS: dict[str, tuple[str, str]] = {
    # doc_kind: (label, icon)
    "policy": ("Polisa", "🛡️"),
    "invoice": ("Faktura / paragon", "🧾"),
    "warranty": ("Gwarancja", "📄"),
    "contract": ("Umowa", "📝"),
    "manual": ("Instrukcja", "📘"),
    "registration": ("Dowód / rejestracja", "🪪"),
    "photo": ("Zdjęcie", "📷"),
    # medical records (🩺 Zdrowie)
    "discharge": ("Wypis / karta informacyjna", "🏥"),
    "results": ("Wyniki badań", "🧪"),
    "imaging": ("RTG / MR / USG", "🩻"),
    "referral": ("Skierowanie", "📨"),
    "prescription": ("Recepta / zalecenia", "💊"),
    "other": ("Inne", "📎"),
}
MEDICAL_DOC_KINDS = ("discharge", "results", "imaging", "referral", "prescription")


CAPTURE_KINDS: dict[str, tuple[str, str]] = {
    # what a quick photo is: (icon, label)
    "service": ("🔧", "Serwis / naprawa"),
    "tyres": ("🛞", "Opony"),
    "receipt": ("🧾", "Paragon / faktura"),
    "policy": ("📄", "Polisa / umowa"),
    "recipe": ("📖", "Przepis"),
    "other": ("📎", "Inne"),
}


class Attachment(Base):
    """A file kept with an item: photo, policy PDF, invoice, manual… The bytes live in data/uploads/files."""

    __tablename__ = "attachment"

    id: Mapped[int] = mapped_column(primary_key=True)
    # the item it belongs to; None for a photo of a note on the board (note_id) or of a recipe (recipe_id)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    note_id: Mapped[int | None] = mapped_column(ForeignKey("board_note.id", ondelete="CASCADE"), index=True)
    # a photo of a recipe: the cookbook page, the dish as served (item None, like a note's photo)
    recipe_id: Mapped[int | None] = mapped_column(ForeignKey("recipe.id", ondelete="CASCADE"), index=True)
    # set when added while ticking a task off ("faktura za serwis")
    completion_id: Mapped[int | None] = mapped_column(ForeignKey("completion.id", ondelete="SET NULL"), index=True)
    # added in the task's form (a photo of what is to be done, the letter, the leaflet)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id", ondelete="SET NULL"), index=True)
    # a medical record kept with a health issue of a household member ("Kolano – wypis, MR")
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issue.id", ondelete="SET NULL"), index=True)
    # visible only to this account (None = shared with the household) – see db.private_filter
    private_to_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
    # relative to the uploads dir, e.g. "files/12/<32 hex>.jpg" – random, never derived from the upload's name
    storage_path: Mapped[str] = mapped_column(String(120), unique=True)
    original_name: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    doc_kind: Mapped[str] = mapped_column(String(20), default="other")
    description: Mapped[str | None] = mapped_column(String(300))
    has_thumb: Mapped[bool] = mapped_column(Boolean, default=False)
    # a photo snapped on the go ("📥 Do rozpoznania"): waits until it is described at home
    inbox: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    capture_kind: Mapped[str | None] = mapped_column(String(20))  # CAPTURE_KINDS – tapped right after the photo
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    item: Mapped[Item | None] = relationship(back_populates="attachments")
    completion: Mapped[Completion | None] = relationship(back_populates="attachments")
    task: Mapped[Task | None] = relationship(back_populates="attachments")
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def is_pdf(self) -> bool:
        return self.content_type == "application/pdf"

    @property
    def kind_label(self) -> str:
        return DOC_KINDS.get(self.doc_kind, DOC_KINDS["other"])[0]

    @property
    def icon(self) -> str:
        if self.is_pdf:
            return "📕"
        if self.is_image:
            return "🖼️"
        return DOC_KINDS.get(self.doc_kind, DOC_KINDS["other"])[1]


class Warranty(Base):
    """Warranty or contract with an end date ("gwarancja producenta 5 lat", "umowa najmu").

    The end date is reminded through an ordinary one-off Task (task_id) – the timeline, calendar,
    "Najbliższe terminy" and push notifications pick it up without any extra code.
    """

    __tablename__ = "warranty"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)  # None = open-ended (no reminder)
    notes: Mapped[str | None] = mapped_column(Text)
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachment.id", ondelete="SET NULL"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id", ondelete="SET NULL"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    item: Mapped[Item] = relationship(back_populates="warranties")
    attachment: Mapped[Attachment | None] = relationship()
    task: Mapped[Task | None] = relationship()


# ---------------------------------------------------------------- week schedule and house chores

WEEKDAY_SHORT = ("pn", "wt", "śr", "czw", "pt", "sob", "nd")
SCHEDULE_MODES = ("fixed", "days", "weeks")


class ScheduleEvent(Base):
    """A fixed weekly slot ("Robotyka, wt 17:00–18:00", "odbiór z przedszkola pn–pt 16:00").

    Who takes care of it (assign_mode):
      fixed – assignee_id, or nobody = shared by everyone;
      days  – day_assignees {"0": user_id | None, …} (Monday = 0), e.g. who picks the kids up on which day;
      weeks – assignee_id in the week of rota_anchor (a Monday) and every other week, alt_assignee_id in between.
    Not a Task: no due date to tick off, never on the timeline – it shows in /grafik, the dashboard's
    "Dziś" card, the calendar's day list and the morning / evening push.
    """

    __tablename__ = "schedule_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    weekdays: Mapped[str] = mapped_column(String(20))  # "0,2,4" – Monday = 0
    starts_at: Mapped[str] = mapped_column(String(5))  # "17:00"
    ends_at: Mapped[str | None] = mapped_column(String(5))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("item.id", ondelete="SET NULL"))
    assign_mode: Mapped[str] = mapped_column(String(10), default="fixed", server_default="fixed")
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    alt_assignee_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    day_assignees: Mapped[dict[str, Any]] = mapped_column(default=dict)
    rota_anchor: Mapped[date | None] = mapped_column(Date)
    starts_on: Mapped[date | None] = mapped_column(Date)  # e.g. the school year
    ends_on: Mapped[date | None] = mapped_column(Date)
    notify: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    item: Mapped[Item | None] = relationship()
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])
    alt_assignee: Mapped[User | None] = relationship(foreign_keys=[alt_assignee_id])
    skips: Mapped[list[ScheduleSkip]] = relationship(
        back_populates="event", cascade="all, delete-orphan", order_by="ScheduleSkip.on_date"
    )

    @property
    def days(self) -> list[int]:
        return sorted({int(x) for x in (self.weekdays or "").split(",") if x.strip().isdigit() and int(x) < 7})

    @property
    def time_label(self) -> str:
        return f"{self.starts_at}–{self.ends_at}" if self.ends_at else self.starts_at


class ScheduleSkip(Base):
    """One occurrence called off ("odwołane w tym tygodniu")."""

    __tablename__ = "schedule_skip"
    __table_args__ = (UniqueConstraint("event_id", "on_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("schedule_event.id", ondelete="CASCADE"), index=True)
    on_date: Mapped[date] = mapped_column(Date)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    event: Mapped[ScheduleEvent] = relationship(back_populates="skips")


class Chore(Base):
    """A house chore done every N days by people taking turns ("łazienka co 7 dni, Ala ↔ Ola").

    Not a Task: a chore has no hard due date – it goes stale (green → red) and is listed by urgency in
    /obowiazki; it must not crowd the timeline or "Najbliższe terminy".
    """

    __tablename__ = "chore"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    room: Mapped[str | None] = mapped_column(String(60))  # free text: "Łazienka", "Kuchnia"…
    every_days: Mapped[int] = mapped_column(Integer, default=7)
    est_minutes: Mapped[int | None] = mapped_column(Integer)
    points: Mapped[int | None] = mapped_column(Integer)
    needs_approval: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    rotation: Mapped[list[int]] = mapped_column(JSON, default=list)  # user ids in turn order; [] = anyone
    uses_timer: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # ⏱ 1/2/3 h on its row
    turn_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    turn_user: Mapped[User | None] = relationship(foreign_keys=[turn_user_id])
    history: Mapped[list[ChoreDone]] = relationship(
        back_populates="chore", cascade="all, delete-orphan", order_by="ChoreDone.id.desc()"
    )


class ChoreDone(Base):
    """A chore ticked off: who did it, when, and whose turn it was (to undo the rotation step)."""

    __tablename__ = "chore_done"

    id: Mapped[int] = mapped_column(primary_key=True)
    chore_id: Mapped[int] = mapped_column(ForeignKey("chore.id", ondelete="CASCADE"), index=True)
    done_on: Mapped[date] = mapped_column(Date)
    done_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    turn_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    chore: Mapped[Chore] = relationship(back_populates="history")
    done_by: Mapped[User | None] = relationship(foreign_keys=[done_by_id])


# ---------------------------------------------------------------- contacts: tradespeople, services, doctors…

# proposals for the role field – free text is allowed too
CONTACT_ROLES: dict[str, str] = {
    # role: icon
    "Mechanik / serwis auta": "🔧",
    "Hydraulik": "🚰",
    "Elektryk": "💡",
    "Fachowiec / remonty": "🛠️",
    "Przedszkole / szkoła": "🏫",
    "Lekarz / przychodnia": "🩺",
    "Weterynarz": "🐾",
    "Ubezpieczyciel / agent": "🛡️",
    "Administracja / wspólnota": "🏢",
    "Sklep / dostawca": "🛒",
}

contact_item = Table(
    "contact_item",
    Base.metadata,
    Column("contact_id", ForeignKey("contact.id", ondelete="CASCADE"), primary_key=True),
    Column("item_id", ForeignKey("item.id", ondelete="CASCADE"), primary_key=True, index=True),
)


class Contact(Base):
    """Someone to call: the mechanic (both cars), the kindergarten, the insurance agent, the GP."""

    __tablename__ = "contact"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))  # person or the place itself
    company: Mapped[str | None] = mapped_column(String(120))
    role: Mapped[str | None] = mapped_column(String(60))
    phones: Mapped[str | None] = mapped_column(Text)  # one number per line
    email: Mapped[str | None] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(300))
    address: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    items: Mapped[list[Item]] = relationship(secondary=contact_item, back_populates="contacts", order_by="Item.name")

    @property
    def phone_list(self) -> list[str]:
        return [p.strip() for p in (self.phones or "").splitlines() if p.strip()]

    @property
    def role_icon(self) -> str:
        return CONTACT_ROLES.get(self.role or "", "📇")

    @property
    def label(self) -> str:
        """'Jan Kowalski (Auto-Serwis)' – for the vendor suggestions."""
        if self.company and self.company.lower() != self.name.lower():
            return f"{self.name} ({self.company})"
        return self.name


# ---------------------------------------------------------------- utility meters (electricity, water, gas, heat)

METER_KINDS: dict[str, tuple[str, str, str]] = {
    # kind: (label, icon, default unit)
    "electricity": ("Prąd", "⚡", "kWh"),
    "water_cold": ("Woda zimna", "🚰", "m³"),
    "water_hot": ("Woda ciepła", "♨️", "m³"),
    "gas": ("Gaz", "🔥", "m³"),
    "heat": ("Ciepło", "🌡️", "GJ"),
    "other": ("Inny", "🔢", ""),
}
METER_UNITS = ("kWh", "m³", "GJ", "km", "l")


class Meter(Base):
    """A meter of a house / flat ("Prąd – taryfa dzienna", "Woda zimna – łazienka") with its readings.

    Not Measurement: a place can have two meters of one kind, a meter has its own unit, price and a
    reset when it is replaced – and the car odometer (Measurement kind="odometer") drives the mileage
    forecasts of tasks, which must stay untouched.
    """

    __tablename__ = "meter"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20), default="other")
    unit: Mapped[str] = mapped_column(String(10), default="")
    unit_price: Mapped[float | None]  # zł per unit, for the cost estimate
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    item: Mapped[Item] = relationship(back_populates="meters")
    readings: Mapped[list[MeterReading]] = relationship(
        back_populates="meter", cascade="all, delete-orphan", order_by="MeterReading.read_on, MeterReading.id"
    )

    @property
    def icon(self) -> str:
        return METER_KINDS.get(self.kind, METER_KINDS["other"])[1]

    @property
    def kind_label(self) -> str:
        return METER_KINDS.get(self.kind, METER_KINDS["other"])[0]


class MeterReading(Base):
    """One reading. is_reset = a new / zeroed meter: the reading starts over, nothing is counted since the last."""

    __tablename__ = "meter_reading"

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meter.id", ondelete="CASCADE"), index=True)
    read_on: Mapped[date] = mapped_column(Date)
    value: Mapped[float]
    is_reset: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    note: Mapped[str | None] = mapped_column(String(200))
    # photo of the meter, kept with the item's documents
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachment.id", ondelete="SET NULL"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    meter: Mapped[Meter] = relationship(back_populates="readings")
    attachment: Mapped[Attachment | None] = relationship()


# ---------------------------------------------------------------- kitchen: recipes, meal plan, pantry

MEALS: dict[str, str] = {"breakfast": "Śniadanie", "lunch": "Obiad", "dinner": "Kolacja", "other": "Inne"}
PANTRY_PLACES: dict[str, str] = {"pantry": "Spiżarnia", "fridge": "Lodówka", "freezer": "Zamrażarka"}


class Recipe(Base):
    """A household recipe: ingredients one per line ("200 g mąki"), steps as free text."""

    __tablename__ = "recipe"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    ingredients: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(300))
    servings: Mapped[int | None] = mapped_column(Integer)
    minutes: Mapped[int | None] = mapped_column(Integer)
    tags: Mapped[str | None] = mapped_column(String(120))  # "obiad, szybkie, wege"
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # photos (the files are removed by kitchen.recipe_delete, the rows also by the FK)
    photos: Mapped[list[Attachment]] = relationship(
        primaryjoin="Recipe.id == foreign(Attachment.recipe_id)", viewonly=True, order_by="Attachment.id"
    )

    @property
    def ingredient_list(self) -> list[str]:
        return [x.strip(" -•*\t") for x in (self.ingredients or "").splitlines() if x.strip(" -•*\t")]

    @property
    def tag_list(self) -> list[str]:
        return [t.strip() for t in (self.tags or "").split(",") if t.strip()]


class MealPlan(Base):
    """One meal on one day of the week plan: a recipe or just a few words ("pizza na mieście")."""

    __tablename__ = "meal_plan"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    meal: Mapped[str] = mapped_column(String(12), default="lunch")
    recipe_id: Mapped[int | None] = mapped_column(ForeignKey("recipe.id", ondelete="SET NULL"))
    text: Mapped[str | None] = mapped_column(String(200))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    recipe: Mapped[Recipe | None] = relationship()

    @property
    def label(self) -> str:
        return self.recipe.name if self.recipe else (self.text or "—")


class PantryItem(Base):
    """Something kept at home, optionally with a best-before date (mleko, mąka, mrożone pierogi)."""

    __tablename__ = "pantry_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    qty: Mapped[str | None] = mapped_column(String(30))
    place: Mapped[str] = mapped_column(String(12), default="pantry")
    expires_on: Mapped[date | None] = mapped_column(Date, index=True)
    added_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------- points & rewards


class Reward(Base):
    """Something to spend points on ("Wyjście na lody", "30 min bajek")."""

    __tablename__ = "reward"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    icon: Mapped[str | None] = mapped_column(String(16))
    cost: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PointEntry(Base):
    """+ points earned (task / chore / given by hand) or − points spent on a reward.

    status: approved – counts; pending – waits for someone else to confirm; rejected – kept for the history.
    """

    __tablename__ = "point_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    points: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10), default="approved")
    task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id", ondelete="SET NULL"))
    completion_id: Mapped[int | None] = mapped_column(ForeignKey("completion.id", ondelete="CASCADE"), index=True)
    chore_done_id: Mapped[int | None] = mapped_column(ForeignKey("chore_done.id", ondelete="CASCADE"), index=True)
    reward_id: Mapped[int | None] = mapped_column(ForeignKey("reward.id", ondelete="SET NULL"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    decided_by: Mapped[User | None] = relationship(foreign_keys=[decided_by_id])


class Comment(Base):
    """A short word under a task or a note ("kupiłam filtr, leży w szafce")."""

    __tablename__ = "comment"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id", ondelete="CASCADE"), index=True)
    note_id: Mapped[int | None] = mapped_column(ForeignKey("board_note.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped[User | None] = relationship()


class BankRule(Base):
    """Learned from the bank import: a word in the description ("biedronka") → a budget category."""

    __tablename__ = "bank_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    pattern: Mapped[str] = mapped_column(String(80), unique=True)  # lower case, matched as a substring
    budget_category_id: Mapped[int] = mapped_column(ForeignKey("budget_category.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    budget_category: Mapped[BudgetCategory] = relationship()


class BankLine(Base):
    """One line of a bank statement that was already looked at (booked or skipped) – never offered again."""

    __tablename__ = "bank_line"

    ref: Mapped[str] = mapped_column(String(40), primary_key=True)  # hash of date, amount, description
    booked_on: Mapped[date] = mapped_column(Date)
    amount_gr: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(300))
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transaction.id", ondelete="SET NULL"))
    imported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ExternalCalendar(Base):
    """A calendar from outside, read by its ICS link: the kindergarten, the school, someone's work calendar.

    Fetched every hour (and on "Odśwież"); its events are shown in the calendar, the "Dziś" card and the
    morning / evening push – read-only, never on the timeline and never in Domownik's own ICS feed.
    """

    __tablename__ = "external_calendar"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    url: Mapped[str] = mapped_column(String(1000))
    icon: Mapped[str] = mapped_column(String(8), default="📅", server_default="📅")
    # whose calendar it is: their push gets its events; None = the whole family's
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    private_to_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    notify: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
    events: Mapped[list[ExternalEvent]] = relationship(
        back_populates="calendar", cascade="all, delete-orphan", passive_deletes=True
    )


class ExternalEvent(Base):
    """One occurrence of an external calendar's event (recurring ones are expanded when fetched)."""

    __tablename__ = "external_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_id: Mapped[int] = mapped_column(ForeignKey("external_calendar.id", ondelete="CASCADE"), index=True)
    starts_on: Mapped[date] = mapped_column(Date, index=True)
    ends_on: Mapped[date] = mapped_column(Date)  # last day (inclusive); = starts_on for a one-day event
    starts_at: Mapped[str | None] = mapped_column(String(5))  # "17:00"; None = all day
    ends_at: Mapped[str | None] = mapped_column(String(5))
    title: Mapped[str] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))

    calendar: Mapped[ExternalCalendar] = relationship(back_populates="events")

    @property
    def time_label(self) -> str:
        if not self.starts_at:
            return "cały dzień"
        return f"{self.starts_at}–{self.ends_at}" if self.ends_at and self.ends_at != self.starts_at else self.starts_at


# ---------------------------------------------------------------- "Na wypadek": what the family needs to know

EMERGENCY_SECTIONS: dict[str, tuple[str, str, str]] = {
    # key: (title, icon, what to write – shown as the hint of an empty section)
    "people": (
        "Dzieci i najbliżsi",
        "👨‍👩‍👦",
        "Kto ma się zająć dziećmi, kogo powiadomić w pierwszej kolejności, co dzieci lubią i czego potrzebują.",
    ),
    "money": (
        "Konta, oszczędności, inwestycje",
        "🏦",
        "Banki i konta, lokaty, IKE/IKZE/PPK, fundusze, kryptowaluty – co to jest i czy jest wskazany uposażony "
        "albo dyspozycja wkładem na wypadek śmierci. Bez numerów kart i haseł.",
    ),
    "where": (
        "Gdzie co leży",
        "📂",
        "Testament, akty urodzenia i małżeństwa, akt notarialny domu, polisy, dowody rejestracyjne, zapasowe "
        "klucze, sejf.",
    ),
    "digital": (
        "Hasła i konta w internecie",
        "🔑",
        "Gdzie są hasła (menedżer haseł, dostęp awaryjny, zapieczętowana koperta) i które konta trzeba zamknąć "
        "lub zachować (zdjęcia!). Samych haseł tu NIE wpisuj.",
    ),
    "work": (
        "Praca i firma",
        "💼",
        "Pracodawcy (odprawa, ubezpieczenie grupowe), działalność gospodarcza, księgowa, zawodowe składki.",
    ),
    "wishes": ("Nasza wola", "🕊️", "Pogrzeb, przekazanie rzeczy, słowa dla bliskich – cokolwiek chcecie przekazać."),
}


class EmergencyEntry(Base):
    """One piece of the family's emergency folder, written by hand (the rest is gathered from the app)."""

    __tablename__ = "emergency_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    section: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    updated_by: Mapped[User | None] = relationship(foreign_keys=[updated_by_id])


class EmergencyPrint(Base):
    """A printout of the folder – when, and by whom (the page warns once it is out of date)."""

    __tablename__ = "emergency_print"

    id: Mapped[int] = mapped_column(primary_key=True)
    printed_at: Mapped[datetime] = mapped_column(DateTime)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))

    user: Mapped[User | None] = relationship()


class Medication(Base):
    """A medicine a household member takes or took ("Ibuprofen 400 mg, 3× dziennie po jedzeniu, 7 dni").

    A list to look things up in (what, how much, since when, what for, the prescription) – no daily ✓ or pushes,
    that is what supplements in Forma are for. ends_on None = taken for good.
    """

    __tablename__ = "medication"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issue.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    dose: Mapped[str | None] = mapped_column(String(80))  # "1 tabl. 400 mg"
    schedule: Mapped[str | None] = mapped_column(String(120))  # "3× dziennie po jedzeniu"
    started_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    prescription: Mapped[str | None] = mapped_column(String(200))  # e-prescription code, who prescribed it
    # the prescription runs out / has to be renewed by → a health task (rx_task_id) that reminds a week before
    rx_until: Mapped[date | None] = mapped_column(Date)
    rx_task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)
    # visible only to this account (None = shared with the household) – see db.private_filter
    private_to_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    item: Mapped[Item] = relationship()
    issue: Mapped[Issue | None] = relationship()
    rx_task: Mapped[Task | None] = relationship()

    def current(self, day: date) -> bool:
        return (self.started_on is None or self.started_on <= day) and (self.ends_on is None or self.ends_on >= day)


class HealthLink(Base):
    """A link to medical records kept elsewhere: a Google Drive folder, the clinic's patient portal, IKP."""

    __tablename__ = "health_link"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issue.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000))
    # visible only to this account (None = shared with the household) – see db.private_filter
    private_to_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class HealthReading(Base):
    """A measured number of a person: blood pressure (value / value2), pulse, sugar, cholesterol, temperature."""

    __tablename__ = "health_reading"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    value: Mapped[float]
    value2: Mapped[float | None]  # the diastolic pressure
    unit: Mapped[str | None] = mapped_column(String(20))
    measured_on: Mapped[date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(200))
    # visible only to this account (None = shared with the household) – see db.private_filter
    private_to_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Supplement(Base):
    """A supplement one person takes every day ("Kreatyna 5 g o 8:00") – strictly private to its owner.

    Stock is counted in the units of per_take (capsules, portions): each ✓ takes per_take off, a bought pack
    adds pack_size. pushed_on / again_on: the day the reminder at `time` / the second one went out (or was
    skipped), so the minute job sends each once a day.
    """

    __tablename__ = "supplement"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    dose: Mapped[str | None] = mapped_column(String(40))  # "5 g", "1 kaps. 2000 IU"
    time: Mapped[str | None] = mapped_column(String(5))  # "08:00"; None = no push, just the checklist
    per_take: Mapped[float] = mapped_column(default=1, server_default="1")
    pack_size: Mapped[float | None]
    stock: Mapped[float | None]
    restock_days: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    restock_listed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    started_on: Mapped[date] = mapped_column(Date)  # the streak counts from here (resume sets it again)
    # a medicine from 🩺 Zdrowie reminded like a supplement ("🔔 Przypominaj mi") – paused once it ends
    medication_id: Mapped[int | None] = mapped_column(ForeignKey("medication.id", ondelete="SET NULL"))
    pushed_on: Mapped[date | None] = mapped_column(Date)
    again_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped[User] = relationship()


class SupplementDose(Base):
    """✓ Wzięte – one per supplement and day."""

    __tablename__ = "supplement_dose"
    __table_args__ = (UniqueConstraint("supplement_id", "taken_on"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    supplement_id: Mapped[int] = mapped_column(ForeignKey("supplement.id", ondelete="CASCADE"), index=True)
    taken_on: Mapped[date] = mapped_column(Date)
    taken_at: Mapped[datetime] = mapped_column(DateTime)


class Exercise(Base):
    """An exercise of the shared library ("Wyciskanie hantli na ławce płaskiej").

    pattern / muscle / equipment drive the substitutes (a busy machine → the same pattern first, then the same
    muscle; "wyjazd" → only equipment at hand). key = seeded from catalog/seed/exercises.yaml (None = own).
    en: the names it had in Jefit, "|"-separated, for the import.
    """

    __tablename__ = "exercise"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str | None] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    en: Mapped[str | None] = mapped_column(String(400))
    pattern: Mapped[str] = mapped_column(String(20))
    muscle: Mapped[str] = mapped_column(String(20))
    equipment: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(10), default="reps", server_default="reps")  # reps | time
    step: Mapped[float] = mapped_column(default=2.5, server_default="2.5")  # kg added at the top of the range
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class WorkoutPlan(Base):
    """A training day to repeat ("FBW 1"); shared by the household, the rotation goes in `position` order."""

    __tablename__ = "workout_plan"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    position: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    seed: Mapped[str | None] = mapped_column(String(30))  # the catalog plan it was made from (exercises.yaml key)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    items: Mapped[list[PlanItem]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanItem.position"
    )


class PlanItem(Base):
    """One exercise of a plan: sets × a range of reps (8–12) or seconds; superset = the same number pairs them."""

    __tablename__ = "plan_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("workout_plan.id", ondelete="CASCADE"), index=True)
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercise.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    sets: Mapped[int] = mapped_column(Integer, default=4)
    reps_min: Mapped[int | None] = mapped_column(Integer)
    reps_max: Mapped[int | None] = mapped_column(Integer)
    seconds: Mapped[int | None] = mapped_column(Integer)
    rest_seconds: Mapped[int] = mapped_column(Integer, default=90)
    superset: Mapped[int | None] = mapped_column(Integer)

    plan: Mapped[WorkoutPlan] = relationship(back_populates="items")
    exercise: Mapped[Exercise] = relationship()


class WorkoutGroup(Base):
    """👫 A training together: who started it and whom they invited. Each member has their own Workout –
    the sets and weights stay private; the partner sees only the exercises and "2/3"."""

    __tablename__ = "workout_group"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    invited: Mapped[list[int]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    workouts: Mapped[list[Workout]] = relationship(back_populates="group", order_by="Workout.id")
    created_by: Mapped[User | None] = relationship()


class Workout(Base):
    """One training of one person – private. equipment: what was at hand (None = a full gym)."""

    __tablename__ = "workout"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("workout_plan.id", ondelete="SET NULL"))
    day: Mapped[date] = mapped_column(Date, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    equipment: Mapped[list[str] | None] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(10), default="app", server_default="app")  # app | jefit
    note: Mapped[str | None] = mapped_column(String(300))
    rest_until: Mapped[datetime | None] = mapped_column(DateTime, index=True)  # the rest after a set ends then
    rest_pushed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # 👫 trained together: the workouts of one group share the order of exercises (WorkoutExercise.slot)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("workout_group.id", ondelete="SET NULL"), index=True)

    plan: Mapped[WorkoutPlan | None] = relationship()
    group: Mapped[WorkoutGroup | None] = relationship(back_populates="workouts")
    user: Mapped[User] = relationship()
    exercises: Mapped[list[WorkoutExercise]] = relationship(
        back_populates="workout", cascade="all, delete-orphan", order_by="WorkoutExercise.position"
    )


class WorkoutExercise(Base):
    """An exercise as done in one workout – copied from the plan, so a swap or a change stays in this one."""

    __tablename__ = "workout_exercise"

    id: Mapped[int] = mapped_column(primary_key=True)
    workout_id: Mapped[int] = mapped_column(ForeignKey("workout.id", ondelete="CASCADE"), index=True)
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercise.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    sets: Mapped[int] = mapped_column(Integer, default=3)
    reps_min: Mapped[int | None] = mapped_column(Integer)
    reps_max: Mapped[int | None] = mapped_column(Integer)
    seconds: Mapped[int | None] = mapped_column(Integer)
    rest_seconds: Mapped[int] = mapped_column(Integer, default=90)
    superset: Mapped[int | None] = mapped_column(Integer)
    swapped_from_id: Mapped[int | None] = mapped_column(ForeignKey("exercise.id", ondelete="SET NULL"))
    # in a workout trained together: the same slot is "the same place in the order" in the partner's workout
    slot: Mapped[int | None] = mapped_column(Integer)

    workout: Mapped[Workout] = relationship(back_populates="exercises")
    exercise: Mapped[Exercise] = relationship(foreign_keys=[exercise_id])
    swapped_from: Mapped[Exercise | None] = relationship(foreign_keys=[swapped_from_id])
    done: Mapped[list[WorkoutSet]] = relationship(
        back_populates="workout_exercise", cascade="all, delete-orphan", order_by="WorkoutSet.set_no"
    )


class WorkoutSet(Base):
    """A set done: weight (kg; a dumbbell = one hand) × reps, or seconds for a plank."""

    __tablename__ = "workout_set"

    id: Mapped[int] = mapped_column(primary_key=True)
    workout_exercise_id: Mapped[int] = mapped_column(ForeignKey("workout_exercise.id", ondelete="CASCADE"), index=True)
    set_no: Mapped[int] = mapped_column(Integer)
    weight: Mapped[float | None]
    reps: Mapped[int | None] = mapped_column(Integer)
    seconds: Mapped[int | None] = mapped_column(Integer)
    done_at: Mapped[datetime] = mapped_column(DateTime)

    workout_exercise: Mapped[WorkoutExercise] = relationship(back_populates="done")


class TrainingPrefs(Base):
    """Per person: trainings a week and after how many days without one a push says so."""

    __tablename__ = "training_prefs"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    weekly_goal: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    gap_days: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    gap_pushed_on: Mapped[date | None] = mapped_column(Date)


# ---------------------------------------------------------------- 🤸 Forma: rozciąganie (shared like the plans)


class Stretch(Base):
    """A stretch of the shared library ("Pozycja dziecka"); seeded from catalog/seed/stretches.yaml by `key`.

    type: dynamic (before a workout) | static (after); sided: the timer counts `seconds` for each side.
    """

    __tablename__ = "stretch"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    type: Mapped[str] = mapped_column(String(10))
    area: Mapped[str] = mapped_column(String(20))
    sided: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    seconds: Mapped[int] = mapped_column(Integer, default=30, server_default="30")


class StretchRoutine(Base):
    """A saved set of stretches ("Przed treningiem"); kind before / after is what the workout screens offer."""

    __tablename__ = "stretch_routine"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    kind: Mapped[str] = mapped_column(String(10), default="own", server_default="own")  # before | after | own
    position: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    seed: Mapped[str | None] = mapped_column(String(30))  # the catalog routine it was made from (stretches.yaml key)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    items: Mapped[list[StretchRoutineItem]] = relationship(
        back_populates="routine", cascade="all, delete-orphan", order_by="StretchRoutineItem.position"
    )


class StretchRoutineItem(Base):
    """One stretch of a routine; seconds None = the stretch's own default (per side when sided)."""

    __tablename__ = "stretch_routine_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    routine_id: Mapped[int] = mapped_column(ForeignKey("stretch_routine.id", ondelete="CASCADE"), index=True)
    stretch_id: Mapped[int] = mapped_column(ForeignKey("stretch.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    seconds: Mapped[int | None] = mapped_column(Integer)

    routine: Mapped[StretchRoutine] = relationship(back_populates="items")
    stretch: Mapped[Stretch] = relationship()

    @property
    def duration(self) -> int:
        return self.seconds or self.stretch.seconds


class StretchGroup(Base):
    """👫 Stretching together: one timer for everybody in it (each still has their own StretchSession – history).

    state: the timer as the browsers keep it (phase index, when it ends in server ms or the time left when
    paused, the results, ...); rev goes up with every change, a change sent from an older rev is refused.
    """

    __tablename__ = "stretch_group"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    invited: Mapped[list[int]] = mapped_column(JSON, default=list)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rev: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime)

    sessions: Mapped[list[StretchSession]] = relationship(back_populates="group", order_by="StretchSession.id")


class StretchSession(Base):
    """One stretching session of one person (private, like a workout).

    steps: the routine as it was at the start (JSON list of stretches: id, name, seconds, sided, img) – the timer
    runs from it, so editing the routine meanwhile changes nothing. done / total: stretches finished by the timer
    (not skipped) of all; seconds: the time it really took, pauses not counted.
    """

    __tablename__ = "stretch_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    routine_id: Mapped[int | None] = mapped_column(ForeignKey("stretch_routine.id", ondelete="SET NULL"))
    workout_id: Mapped[int | None] = mapped_column(ForeignKey("workout.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(60))
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    done: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    group_id: Mapped[int | None] = mapped_column(ForeignKey("stretch_group.id", ondelete="SET NULL"), index=True)

    routine: Mapped[StretchRoutine | None] = relationship()
    group: Mapped[StretchGroup | None] = relationship(back_populates="sessions")
    user: Mapped[User] = relationship()


# ---------------------------------------------------------------- 💪 Forma: sylwetka (strictly the owner's)


class BodyWeight(Base):
    """One weigh-in a day (kg). The trend is the 7-day average, not single days."""

    __tablename__ = "body_weight"
    __table_args__ = (UniqueConstraint("user_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    kg: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BodyCheck(Base):
    """A measurement every few weeks: circumferences (cm, each optional) and photos front / side / back."""

    __tablename__ = "body_check"
    __table_args__ = (UniqueConstraint("user_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    chest: Mapped[float | None]
    arm: Mapped[float | None]
    waist: Mapped[float | None]
    thigh: Mapped[float | None]
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    photos: Mapped[list[BodyPhoto]] = relationship(back_populates="check", cascade="all, delete-orphan")


class BodyPhoto(Base):
    """A photo of a measurement. Bytes in uploads/body/<user id>/ – served only to the owner."""

    __tablename__ = "body_photo"
    __table_args__ = (UniqueConstraint("check_id", "pose"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("body_check.id", ondelete="CASCADE"), index=True)
    pose: Mapped[str] = mapped_column(String(10))  # front | side | back
    storage_path: Mapped[str] = mapped_column(String(120), unique=True)
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    has_thumb: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    check: Mapped[BodyCheck] = relationship(back_populates="photos")


class BodyPrefs(Base):
    """Per person: goal weight and the gentle reminders (a weigh-in after N days without one, a measurement)."""

    __tablename__ = "body_prefs"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    goal_kg: Mapped[float | None]
    reminders: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    weigh_gap_days: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    check_every_days: Mapped[int] = mapped_column(Integer, default=14, server_default="14")
    weigh_pushed_on: Mapped[date | None] = mapped_column(Date)
    check_pushed_on: Mapped[date | None] = mapped_column(Date)
