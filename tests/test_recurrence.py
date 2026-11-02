from datetime import date

import pytest

from domownik.domain.recurrence import (
    RuleError,
    describe,
    first_due,
    next_due,
    occurrences,
    validate,
)

D = date


class TestValidate:
    def test_interval_defaults_anchor(self):
        assert validate({"mode": "interval", "every": "3", "unit": "month"}) == {
            "mode": "interval",
            "every": 3,
            "unit": "month",
            "anchor": "completion",
        }

    def test_fixed_accepts_string_and_sorts_dedupes(self):
        r = validate({"mode": "fixed", "dates": "11-15, 03-15;03-15"})
        assert r["dates"] == ["03-15", "11-15"]

    @pytest.mark.parametrize(
        "rule",
        [
            {"mode": "nope"},
            {"mode": "interval", "every": 0, "unit": "month"},
            {"mode": "interval", "every": 1, "unit": "fortnight"},
            {"mode": "monthly", "day": 32},
            {"mode": "fixed", "dates": []},
            {"mode": "fixed", "dates": ["02-30"]},
            {"mode": "fixed", "dates": ["15.03"]},
            {"mode": "nth_weekday", "month": 5, "weekday": "sun", "n": 6},
        ],
    )
    def test_invalid(self, rule):
        with pytest.raises(RuleError):
            validate(rule)


class TestNextDue:
    def test_completion_anchor_counts_from_done(self):
        r = {"mode": "interval", "every": 2, "unit": "month", "anchor": "completion"}
        assert next_due(r, previous_due=D(2026, 1, 10), done_on=D(2026, 1, 25)) == D(2026, 3, 25)

    def test_due_anchor_ignores_late_completion(self):
        r = {"mode": "interval", "every": 1, "unit": "year", "anchor": "due"}
        assert next_due(r, previous_due=D(2026, 3, 1), done_on=D(2026, 3, 20)) == D(2027, 3, 1)

    def test_due_anchor_early_completion(self):
        r = {"mode": "interval", "every": 1, "unit": "year", "anchor": "due"}
        assert next_due(r, previous_due=D(2026, 3, 1), done_on=D(2026, 2, 1)) == D(2027, 3, 1)

    def test_due_anchor_skips_whole_missed_periods(self):
        r = {"mode": "interval", "every": 1, "unit": "month", "anchor": "due"}
        # 3 months late: jump to the period around the completion date
        assert next_due(r, previous_due=D(2026, 1, 5), done_on=D(2026, 4, 20)) >= D(2026, 4, 5)

    def test_monthly_advances_one_period_even_if_late(self):
        r = {"mode": "monthly", "day": 10}
        assert next_due(r, previous_due=D(2026, 1, 10), done_on=D(2026, 3, 1)) == D(2026, 2, 10)

    def test_monthly_clamps_to_month_end(self):
        r = {"mode": "monthly", "day": 31}
        assert next_due(r, previous_due=D(2026, 1, 31), done_on=D(2026, 1, 31)) == D(2026, 2, 28)
        assert next_due(r, previous_due=D(2026, 2, 28), done_on=D(2026, 2, 28)) == D(2026, 3, 31)

    def test_fixed_rolls_over_year(self):
        r = {"mode": "fixed", "dates": ["03-15", "05-15", "09-15", "11-15"]}
        assert next_due(r, previous_due=D(2026, 11, 15), done_on=D(2026, 11, 14)) == D(2027, 3, 15)

    def test_fixed_feb_29_in_non_leap_year(self):
        r = {"mode": "fixed", "dates": ["02-29"]}
        assert next_due(r, previous_due=D(2024, 2, 29), done_on=D(2024, 2, 29)) == D(2025, 2, 28)
        assert next_due(r, previous_due=D(2027, 2, 28), done_on=D(2027, 2, 28)) == D(2028, 2, 29)

    def test_once_returns_none(self):
        assert next_due({"mode": "once"}, previous_due=D(2026, 1, 1), done_on=D(2026, 1, 1)) is None

    def test_week_interval(self):
        r = {"mode": "interval", "every": 1, "unit": "week"}
        assert next_due(r, previous_due=None, done_on=D(2026, 11, 2)) == D(2026, 11, 9)


class TestEaster:
    @pytest.mark.parametrize(
        ("year", "expected"),
        [(2025, D(2025, 4, 20)), (2026, D(2026, 4, 5)), (2027, D(2027, 3, 28)), (2028, D(2028, 4, 16))],
    )
    def test_easter_dates(self, year, expected):
        assert first_due({"mode": "easter", "offset": 0}, D(year, 1, 1)) == expected

    def test_corpus_christi_offset(self):
        assert first_due({"mode": "easter", "offset": 60}, D(2026, 1, 1)) == D(2026, 6, 4)

    def test_after_easter_goes_to_next_year(self):
        r = {"mode": "easter", "offset": 0}
        assert next_due(r, previous_due=D(2026, 4, 5), done_on=D(2026, 4, 5)) == D(2027, 3, 28)


class TestNthWeekday:
    def test_first_sunday_of_may(self):
        assert first_due({"mode": "nth_weekday", "month": 5, "weekday": "sun", "n": 1}, D(2026, 1, 1)) == D(2026, 5, 3)

    def test_last_friday_of_november(self):
        r = {"mode": "nth_weekday", "month": 11, "weekday": "fri", "n": -1}
        assert first_due(r, D(2026, 1, 1)) == D(2026, 11, 27)

    def test_fifth_weekday_missing_skips_year(self):
        # March 2026 has 5 Sundays (29th); 2027 does not, so the next one is in a later year
        r = {"mode": "nth_weekday", "month": 3, "weekday": "sun", "n": 5}
        assert first_due(r, D(2026, 1, 1)) == D(2026, 3, 29)
        d = first_due(r, D(2026, 4, 1))
        assert d.year > 2027 and d.month == 3 and d.day >= 29 and d.weekday() == 6


class TestFirstDue:
    def test_interval_from_last_done(self):
        r = {"mode": "interval", "every": 3, "unit": "month"}
        assert first_due(r, D(2026, 11, 2), last_done=D(2026, 10, 1)) == D(2027, 1, 1)

    def test_calendar_today_counts(self):
        assert first_due({"mode": "fixed", "dates": ["11-02"]}, D(2026, 11, 2)) == D(2026, 11, 2)

    def test_once_has_no_suggestion(self):
        assert first_due({"mode": "once"}, D(2026, 11, 2)) is None


class TestOccurrences:
    def test_interval_no_month_end_drift(self):
        r = {"mode": "interval", "every": 1, "unit": "month"}
        got = occurrences(r, D(2026, 1, 31), D(2026, 1, 1), D(2026, 5, 31))
        assert got == [D(2026, 1, 31), D(2026, 2, 28), D(2026, 3, 31), D(2026, 4, 30), D(2026, 5, 31)]

    def test_window_filters_start(self):
        r = {"mode": "interval", "every": 1, "unit": "week"}
        got = occurrences(r, D(2026, 11, 2), D(2026, 11, 10), D(2026, 11, 30))
        assert got == [D(2026, 11, 16), D(2026, 11, 23), D(2026, 11, 30)]

    def test_overdue_first_included(self):
        r = {"mode": "monthly", "day": 10}
        got = occurrences(r, D(2026, 10, 10), D(2026, 10, 1), D(2026, 12, 31))
        assert got == [D(2026, 10, 10), D(2026, 11, 10), D(2026, 12, 10)]

    def test_fixed_multiple_years(self):
        r = {"mode": "fixed", "dates": ["04-01", "10-15"]}
        got = occurrences(r, D(2026, 10, 15), D(2026, 1, 1), D(2027, 12, 31))
        assert got == [D(2026, 10, 15), D(2027, 4, 1), D(2027, 10, 15)]

    def test_once(self):
        assert occurrences({"mode": "once"}, D(2026, 12, 1), D(2026, 11, 1), D(2026, 12, 31)) == [D(2026, 12, 1)]
        assert occurrences({"mode": "once"}, D(2027, 1, 1), D(2026, 11, 1), D(2026, 12, 31)) == []

    def test_none_first(self):
        assert occurrences({"mode": "monthly", "day": 1}, None, D(2026, 1, 1), D(2026, 12, 31)) == []


class TestDescribe:
    @pytest.mark.parametrize(
        ("rule", "text"),
        [
            ({"mode": "interval", "every": 1, "unit": "year", "anchor": "due"}, "co rok (od terminu)"),
            ({"mode": "interval", "every": 3, "unit": "month"}, "co 3 miesiące"),
            ({"mode": "interval", "every": 5, "unit": "month"}, "co 5 miesięcy"),
            ({"mode": "interval", "every": 12, "unit": "month", "km": 15000}, "co 12 miesięcy lub 15 000 km"),
            ({"mode": "monthly", "day": 10}, "co miesiąc, 10. dnia"),
            ({"mode": "fixed", "dates": ["12-24"]}, "co roku: 24 grudnia"),
            ({"mode": "easter", "offset": 60}, "Wielkanoc +60 dni (co roku)"),
            ({"mode": "once"}, "jednorazowo"),
        ],
    )
    def test_describe(self, rule, text):
        assert describe(rule) == text


class TestSeries:
    MONTHLY = {"mode": "monthly", "day": 10}

    def test_validate_keeps_end_and_state(self):
        r = validate(self.MONTHLY | {"count": "6"})
        assert r == {"mode": "monthly", "day": 10, "count": 6, "done": 0}
        assert validate(self.MONTHLY | {"until": "2027-03-31"})["until"] == "2027-03-31"
        assert validate({"mode": "once", "count": 3}) == {"mode": "once"}

    @pytest.mark.parametrize(
        "extra", [{"count": 0.5}, {"count": 2000}, {"until": "31.03.2027"}, {"count": 2, "until": "2027-01-01"}]
    )
    def test_validate_rejects_bad_end(self, extra):
        with pytest.raises(RuleError):
            validate(self.MONTHLY | extra)

    def test_occurrences_stop_after_count(self):
        r = self.MONTHLY | {"count": 6, "done": 2}
        got = occurrences(r, D(2026, 1, 10), D(2026, 1, 1), D(2030, 1, 1))
        assert got == [D(2026, m, 10) for m in (1, 2, 3, 4)]

    def test_occurrences_stop_after_until(self):
        r = {"mode": "interval", "every": 1, "unit": "week", "anchor": "due", "until": "2026-01-20"}
        got = occurrences(r, D(2026, 1, 1), D(2026, 1, 1), D(2026, 12, 31))
        assert got == [D(2026, 1, 1), D(2026, 1, 8), D(2026, 1, 15)]

    def test_advance_counts_and_ends(self):
        from domownik.domain.recurrence import advance, series_position

        r = validate(self.MONTHLY | {"count": 3})
        assert series_position(r) == (1, 3)
        r, nxt = advance(r, previous_due=D(2026, 1, 10), done_on=D(2026, 1, 9))
        assert nxt == D(2026, 2, 10) and r["done"] == 1 and series_position(r) == (2, 3)
        r, nxt = advance(r, previous_due=nxt, done_on=nxt)
        r, nxt = advance(r, previous_due=D(2026, 3, 10), done_on=D(2026, 3, 10))
        assert nxt is None and r["done"] == 3  # the last instalment ends the series

    def test_advance_until(self):
        from domownik.domain.recurrence import advance

        r = self.MONTHLY | {"until": "2026-02-15"}
        _, nxt = advance(r, previous_due=D(2026, 1, 10), done_on=D(2026, 1, 10))
        assert nxt == D(2026, 2, 10)
        _, nxt = advance(r, previous_due=D(2026, 2, 10), done_on=D(2026, 2, 10))
        assert nxt is None

    def test_retreat_gives_instalment_back(self):
        from domownik.domain.recurrence import retreat

        assert retreat(self.MONTHLY | {"count": 3, "done": 3})["done"] == 2
        assert retreat(self.MONTHLY | {"count": 3, "done": 0})["done"] == 0
        assert "done" not in retreat(self.MONTHLY)

    def test_postponed_occurrence_keeps_the_rhythm(self):
        from domownik.domain.recurrence import advance, postpone

        r = postpone(self.MONTHLY | {"count": 6}, current_due=D(2026, 1, 10))
        assert r["postponed_from"] == "2026-01-10"
        # shown: the moved date, then the regular ones
        assert occurrences(r, D(2026, 1, 14), D(2026, 1, 1), D(2026, 3, 31)) == [
            D(2026, 1, 14),
            D(2026, 2, 10),
            D(2026, 3, 10),
        ]
        r2, nxt = advance(r, previous_due=D(2026, 1, 14), done_on=D(2026, 1, 14))
        assert nxt == D(2026, 2, 10) and "postponed_from" not in r2 and r2["done"] == 1
        due = {"mode": "interval", "every": 1, "unit": "month", "anchor": "due"}
        r = postpone(due, current_due=D(2026, 1, 31))
        _, nxt = advance(r, previous_due=D(2026, 2, 3), done_on=D(2026, 2, 3))
        assert nxt == D(2026, 2, 28)

    def test_postponed_past_next_regular_date(self):
        from domownik.domain.recurrence import advance, postpone

        weekly = {"mode": "interval", "every": 1, "unit": "week", "anchor": "due"}
        r = postpone(weekly, current_due=D(2026, 1, 1))
        assert occurrences(r, D(2026, 1, 10), D(2026, 1, 1), D(2026, 1, 25)) == [
            D(2026, 1, 10),
            D(2026, 1, 15),
            D(2026, 1, 22),
        ]
        _, nxt = advance(r, previous_due=D(2026, 1, 10), done_on=D(2026, 1, 10))
        assert nxt == D(2026, 1, 15)

    def test_completion_anchor_ignores_postpone(self):
        from domownik.domain.recurrence import postpone

        r = postpone({"mode": "interval", "every": 1, "unit": "month"}, current_due=D(2026, 1, 1))
        assert "postponed_from" not in r

    def test_describe_series(self):
        assert describe(self.MONTHLY | {"count": 6}) == "co miesiąc, 10. dnia, 6 razy"
        assert describe(self.MONTHLY | {"until": "2027-03-31"}) == "co miesiąc, 10. dnia, do 31 marca 2027"
