"""✉️ Z poczty – emails from the kindergarten teacher turned into calendar entries, after a person says yes.

Once a day (DOMOWNIK_MAIL_CHECK_AT, 18:30 – before the 19:00 "na jutro" push) the server reads the mailbox
over IMAP, **read-only**: the folder is opened with EXAMINE and messages are fetched with BODY.PEEK, so
nothing is marked read, moved or deleted. Only emails from DOMOWNIK_MAIL_FROM are taken. Each one is kept
whole (MailMessage) and waits in 📥 Do rozpoznania; its dates come from plain rules (domain.mailparse) and
are only proposals – nothing lands in the calendar until someone ticks and saves them.

Built to fail loudly, not silently:
- every email from the sender shows up, even one with no date in it (to read and dismiss);
- each check looks back to a week before the last good one, so a missed night or a dead phone catches up,
  and the Message-ID keeps an email from coming twice;
- a failed check is retried every hour, written on the page, pushed once a day and shown on the dashboard
  when no check has worked for two days.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import imaplib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from domownik.config import get_settings
from domownik.domain import mailparse
from domownik.models import DayEvent, Item, MailMessage, MailState, Task, User

log = logging.getLogger("domownik")

LOOK_BACK_DAYS = 7  # before the last good check – a missed night is caught up
FIRST_LOOK_BACK_DAYS = 14  # the very first check
RETRY_AFTER = timedelta(hours=1)
WARN_AFTER = timedelta(days=2)
TIMEOUT = 30
MAX_MESSAGES = 200
MAX_BODY = 50_000


class MailError(Exception):
    """A check that did not work – the text is shown on the page (never with the password)."""


# ---------------------------------------------------------------- settings


def senders() -> list[str]:
    return [a.strip().lower() for a in get_settings().mail_from.split(",") if a.strip()]


def configured() -> bool:
    s = get_settings()
    return bool(s.mail_user and s.mail_password and senders())


def check_time() -> time:
    raw = get_settings().mail_check_at.strip()
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", raw)
    if m and int(m[1]) < 24 and int(m[2]) < 60:
        return time(int(m[1]), int(m[2]))
    return time(18, 30)


def state(db: Session) -> MailState:
    st = db.get(MailState, 1)
    if st is None:
        st = MailState(id=1)
        db.add(st)
        db.flush()
    return st


# ---------------------------------------------------------------- reading the mailbox


def _imap_date(d: date) -> str:
    return f"{d.day:02d}-{('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')[d.month - 1]}-{d.year}"


def fetch_raw(since: date) -> list[bytes]:
    """Every email from a watched sender since the day, as raw bytes. Never changes the mailbox."""
    s = get_settings()
    try:
        box = imaplib.IMAP4_SSL(s.mail_imap_host, timeout=TIMEOUT)
    except (TimeoutError, OSError) as exc:
        raise MailError(f"Brak połączenia z serwerem poczty ({s.mail_imap_host}): {exc.__class__.__name__}") from None
    try:
        try:
            box.login(s.mail_user, s.mail_password)
        except imaplib.IMAP4.error:
            raise MailError("Poczta odrzuciła logowanie – sprawdź DOMOWNIK_MAIL_USER i hasło aplikacji") from None
        typ, _ = box.select(_quote(s.mail_folder), readonly=True)  # EXAMINE: read-only
        if typ != "OK":
            raise MailError(f"Nie ma folderu „{s.mail_folder}” w skrzynce")
        out: list[bytes] = []
        for sender in senders():
            typ, data = box.search(None, "FROM", _quote(sender), "SINCE", _imap_date(since))
            if typ != "OK":
                raise MailError("Serwer poczty nie przyjął wyszukiwania")
            nums = (data[0] or b"").split()[-MAX_MESSAGES:]
            for num in nums:
                typ, parts = box.fetch(num, "(BODY.PEEK[])")  # PEEK: the email stays unread
                if typ != "OK":
                    raise MailError("Nie udało się pobrać wiadomości")
                out.extend(p[1] for p in parts if isinstance(p, tuple) and len(p) > 1)
        return out
    except (TimeoutError, OSError, imaplib.IMAP4.abort) as exc:
        raise MailError(f"Połączenie z pocztą zerwane: {exc.__class__.__name__}") from None
    except imaplib.IMAP4.error as exc:
        raise MailError(f"Błąd serwera poczty: {str(exc)[:120]}") from None
    finally:
        try:
            box.logout()
        except Exception:
            pass


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _Text(HTMLParser):
    BREAK = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "table", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.skip += 1
        if tag in self.BREAK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self.skip = max(0, self.skip - 1)
        if tag in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    p.close()
    text = "".join(p.parts)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


@dataclass
class Parsed:
    message_id: str
    sender: str
    subject: str
    sent_at: datetime  # local, naive
    body: str


def read_message(raw: bytes) -> Parsed | None:
    """Raw email → the parts kept. None when it is not from a watched sender (IMAP FROM is a substring match)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    _name, addr = email.utils.parseaddr(str(msg.get("From", "")))
    addr = addr.lower()
    if addr not in senders():
        return None
    try:
        sent = email.utils.parsedate_to_datetime(str(msg.get("Date")))
        sent_local = (sent if sent.tzinfo else sent.replace(tzinfo=get_settings().tz)).astimezone(get_settings().tz)
    except TypeError, ValueError, IndexError:
        sent_local = datetime.now(get_settings().tz)
    body = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            content = part.get_content()
        except LookupError, ValueError:  # unknown charset – keep what can be read
            content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        body = html_to_text(content) if part.get_content_type() == "text/html" else str(content)
    mid = str(msg.get("Message-ID") or "").strip() or "sha256:" + hashlib.sha256(raw).hexdigest()
    return Parsed(
        message_id=mid[:500],
        sender=addr[:200],
        subject=str(msg.get("Subject") or "").strip()[:300],
        sent_at=sent_local.replace(tzinfo=None, microsecond=0),
        body=body[:MAX_BODY],
    )


# ---------------------------------------------------------------- the daily check


def due(db: Session, now: datetime) -> bool:
    """Once a day after check_time; after a failure again every hour until one works."""
    if not configured():
        return False
    st = db.get(MailState, 1)
    slot = datetime.combine(now.date(), check_time())
    local = now.replace(tzinfo=None)
    if local < slot:
        return False
    if st is None or st.last_run_at is None:
        return True
    if st.last_ok_at is not None and st.last_ok_at >= slot:
        return False  # today's check worked
    return st.last_run_at < slot or (st.error is not None and local - st.last_run_at >= RETRY_AFTER)


def store(db: Session, raws: list[bytes]) -> list[MailMessage]:
    """Keep the emails not seen before. Returns the new ones (the caller commits)."""
    new: list[MailMessage] = []
    for raw in raws:
        try:
            p = read_message(raw)
        except Exception:  # one broken email must not hide the others
            log.exception("mail: cannot read a message")
            continue
        if p is None or db.scalar(select(MailMessage.id).where(MailMessage.message_id == p.message_id)):
            continue
        m = MailMessage(
            message_id=p.message_id, sender=p.sender, subject=p.subject, sent_at=p.sent_at, body=p.body, status="new"
        )
        db.add(m)
        db.flush()
        new.append(m)
    return new


def check(db: Session, now: datetime, *, fetch: Callable[[date], list[bytes]] = fetch_raw, push: bool = True) -> int:
    """One check of the mailbox. Returns how many new emails; the error stays in MailState."""
    st = state(db)
    local = now.replace(tzinfo=None, microsecond=0)
    st.last_run_at = local
    since = (
        (st.last_ok_at - timedelta(days=LOOK_BACK_DAYS)).date()
        if st.last_ok_at
        else (local - timedelta(days=FIRST_LOOK_BACK_DAYS)).date()
    )
    try:
        new = store(db, fetch(since))
    except Exception as exc:
        db.rollback()
        if not isinstance(exc, MailError):
            log.exception("mail: check failed")
        st = state(db)
        st.last_run_at = local
        st.error = (str(exc) if isinstance(exc, MailError) else f"Nieoczekiwany błąd ({exc.__class__.__name__})")[:300]
        _push_error(db, st, local.date())
        db.commit()
        log.warning("mail: %s", exc)
        return 0
    st.last_ok_at, st.error = local, None
    db.commit()
    if new and push:
        _push_new(db, new)
    return len(new)


def run_due(db: Session, now: datetime) -> None:
    """Scheduler step (notify.tick)."""
    if due(db, now):
        check(db, now)


def _users_with_push(db: Session) -> list[User]:
    return list(db.scalars(select(User)))


def _push_new(db: Session, new: list[MailMessage]) -> None:
    from domownik import notify

    n_dates = sum(len(proposals(db, m).proposals) for m in new)
    label = get_settings().mail_label
    what = (
        f"{n_dates} {_plural(n_dates, 'termin', 'terminy', 'terminów')} do zatwierdzenia" if n_dates else "przeczytaj"
    )
    body = {
        "title": f"✉️ {label}: {len(new)} {_plural(len(new), 'nowy mail', 'nowe maile', 'nowych maili')}",
        "body": f"{what} – " + "; ".join(m.subject or "(bez tematu)" for m in new[:3]),
        "url": "/zdjecia",
        "tag": "domownik-mail",
        "actions": [],
    }
    for u in _users_with_push(db):
        try:
            notify.send(db, u, body)
        except Exception as exc:
            log.warning("mail push: %s – %s", u.name, exc)


def _push_error(db: Session, st: MailState, today: date) -> None:
    from domownik import notify

    if st.error_pushed_on == today:
        return
    st.error_pushed_on = today
    body = {
        "title": "⚠️ Domownik nie sprawdził poczty",
        "body": f"{st.error} – maile z przedszkola mogą czekać. Szczegóły w 📥 Do rozpoznania.",
        "url": "/zdjecia",
        "tag": "domownik-mail-error",
        "actions": [],
    }
    for u in _users_with_push(db):
        try:
            notify.send(db, u, body)
        except Exception as exc:
            log.warning("mail push: %s – %s", u.name, exc)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def warning(db: Session, now: datetime) -> str | None:
    """A line for the dashboard when reading the mailbox has not worked for a while."""
    if not configured():
        return None
    st = db.get(MailState, 1)
    local = now.replace(tzinfo=None)
    if st is None or st.last_ok_at is None:
        if st is not None and st.error:
            return f"⚠️ Domownik nie może sprawdzić poczty: {st.error}"
        return None
    if local - st.last_ok_at > WARN_AFTER:
        days = (local.date() - st.last_ok_at.date()).days
        why = f": {st.error}" if st.error else ""
        return f"⚠️ Poczta nie była sprawdzona od {days} dni{why} – maile z przedszkola mogą czekać."
    return None


# ---------------------------------------------------------------- waiting emails and what they propose


def pending(db: Session) -> list[MailMessage]:
    return list(db.scalars(select(MailMessage).where(MailMessage.status == "new").order_by(MailMessage.sent_at)))


def count(db: Session) -> int:
    return db.scalar(select(func.count(MailMessage.id)).where(MailMessage.status == "new")) or 0


def _words(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"\w{4,}", text.lower())}


def on_day(db: Session, day: date, end: date) -> list[str]:
    """Titles already in Domownik on those days – day entries and tasks."""
    evs = db.scalars(select(DayEvent).where(DayEvent.starts_on <= end, DayEvent.ends_on >= day))
    tasks = db.scalars(select(Task).where(Task.active.is_(True), Task.next_due_date >= day, Task.next_due_date <= end))
    return [e.title for e in evs] + [t.title for t in tasks]


def proposals(db: Session, m: MailMessage) -> mailparse.Result:
    res = mailparse.parse(m.subject, m.body, m.sent_at.date())
    for p in res.proposals:
        there = on_day(db, p.day, p.end)
        same = [t for t in there if _words(t) & _words(p.title + " " + p.sentence)]
        if same:
            p.warnings.append("Chyba już jest w Domowniku: " + ", ".join(same[:3]))
    return res


def default_item(db: Session) -> Item | None:
    """Where timed entries go: the one used last, else the first child."""
    st = db.get(MailState, 1)
    if st and st.item_id and (it := db.get(Item, st.item_id)):
        return it
    return db.scalar(select(Item).where(Item.kind == "child").order_by(Item.id))


@dataclass
class Entry:
    title: str
    day: date
    end: date
    at: time | None
    icon: str
    notes: str


def note_for(m: MailMessage, paragraph: str) -> str:
    return f"{paragraph}\n\n✉️ Z maila „{m.subject or 'bez tematu'}” ({m.sent_at:%d.%m.%Y})."


def apply(
    db: Session,
    m: MailMessage,
    entries: list[Entry],
    *,
    item_id: int | None,
    shopping: list[str],
    user: User,
) -> tuple[int, int]:
    """Create what was confirmed: an hour → a one-off task in the item (with a push before it); no hour →
    an entry of the family calendar. Returns (entries, shopping items). The caller commits."""
    from domownik.routers.shopping import add_entries

    item = db.get(Item, item_id) if item_id else None
    if any(e.at for e in entries) and item is None:
        raise MailError("Wybierz, do czego dopisać terminy z godziną")
    made = 0
    for e in entries:
        if not e.title.strip():
            raise MailError("Każdy zaznaczony termin musi mieć nazwę")
        if e.end < e.day:
            raise MailError(f"„{e.title}”: koniec przed początkiem")
        if e.at and item is not None:
            db.add(
                Task(
                    item_id=item.id,
                    title=e.title.strip()[:200],
                    category="other",
                    recurrence={"mode": "once", "date": e.day.isoformat()},
                    next_due_date=e.day,
                    due_time=e.at.strftime("%H:%M"),
                    remind_minutes=60,
                    lead_days=3,
                    direction="none",
                    notes=e.notes,
                )
            )
        else:
            db.add(
                DayEvent(
                    title=e.title.strip()[:120],
                    icon=(e.icon.strip() or mailparse.DEFAULT_ICON)[:8],
                    starts_on=e.day,
                    ends_on=e.end,
                    notes=e.notes,
                    created_by_id=user.id,
                )
            )
        made += 1
    bought = add_entries(db, shopping, user=user, note=f"✉️ {m.subject}"[:200]) if shopping else 0
    m.status, m.done_by_id, m.done_at = "done", user.id, datetime.now(get_settings().tz).replace(tzinfo=None)
    if item is not None:
        state(db).item_id = item.id
    return made, bought


def dismiss(db: Session, m: MailMessage, user: User) -> None:
    m.status, m.done_by_id, m.done_at = "dismissed", user.id, datetime.now(get_settings().tz).replace(tzinfo=None)


def recent_done(db: Session, limit: int = 5) -> list[MailMessage]:
    return list(
        db.scalars(
            select(MailMessage)
            .where(or_(MailMessage.status == "done", MailMessage.status == "dismissed"))
            .order_by(MailMessage.done_at.desc())
            .limit(limit)
        )
    )
