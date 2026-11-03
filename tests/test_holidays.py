"""Polish public holidays on the calendar."""

from datetime import date

from domownik.domain import holidays


def test_fixed_and_moveable_2026_2027():
    h26 = holidays.of_year(2026)
    assert h26[date(2026, 11, 11)] == "Święto Niepodległości"
    assert h26[date(2026, 4, 5)] == "Wielkanoc" and h26[date(2026, 4, 6)] == "Poniedziałek Wielkanocny"
    assert h26[date(2026, 5, 24)] == "Zielone Świątki" and h26[date(2026, 6, 4)] == "Boże Ciało"
    assert h26[date(2026, 12, 24)] == "Wigilia"
    assert len(h26) == 14
    h27 = holidays.of_year(2027)
    assert h27[date(2027, 3, 28)] == "Wielkanoc" and h27[date(2027, 5, 27)] == "Boże Ciało"
    assert holidays.name(date(2024, 12, 24)) is None  # not yet a day off
    assert holidays.name(date(2026, 11, 12)) is None


def test_calendar_shows_holiday(auth_client):
    page = auth_client.get("/kalendarz?m=2026-11&d=2026-11-11").text
    assert "cal-hol" in page and "Święto Niepodległości" in page
    assert "dzień wolny od pracy" in page
