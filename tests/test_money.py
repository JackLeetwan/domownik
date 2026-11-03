import pytest

from domownik.domain.money import InvalidAmount, parse_pln


@pytest.mark.parametrize(
    ("raw", "gr"), [("1 234,50", 123450), ("1234.5", 123450), ("54,90 zł", 5490), ("", None), (None, None)]
)
def test_parse_pln_ok(raw, gr):
    assert parse_pln(raw) == gr


@pytest.mark.parametrize("raw", ["test", "nan", "inf", "1e999", "12,3,4"])
def test_parse_pln_rejects_garbage(raw):
    with pytest.raises(InvalidAmount):
        parse_pln(raw)
    with pytest.raises(ValueError):  # existing `except ValueError` handlers keep working
        parse_pln(raw)
