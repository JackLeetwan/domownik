import pytest

from domownik.domain.shopping import add_qty, guess_section, parse_entry


@pytest.mark.parametrize(
    ("text", "name", "qty"),
    [
        ("mleko", "Mleko", None),
        ("2 mleko", "Mleko", "2"),
        ("2x mleko", "Mleko", "×2"),
        ("masło x2", "Masło", "×2"),
        ("jajka 10 szt", "Jajka", "10 szt"),
        ("1,5 kg ziemniaki", "Ziemniaki", "1,5 kg"),
        ("  papier   ścierny 2000 ", "Papier ścierny 2000", None),
        ("woda 6 l", "Woda", "6 l"),
        ("", "", None),
    ],
)
def test_parse_entry(text, name, qty):
    assert parse_entry(text) == (name, qty)


@pytest.mark.parametrize(
    ("name", "section"),
    [
        ("Mleko", "nabial"),
        ("Ser żółty", "nabial"),
        ("Chleb razowy", "pieczywo"),
        ("Papier toaletowy", "chemia"),
        ("Papier ścierny 2000", "budowlane"),
        ("Płatki owsiane", "suche"),
        ("Płatki kosmetyczne", "chemia"),
        ("Pomidory", "warzywa"),
        ("Apap", "apteka"),
        ("Coś dziwnego", "inne"),
        ("Serwetki", "inne"),
        ("Serek wiejski", "nabial"),
        ("Porzeczki", "inne"),
        ("Pory", "warzywa"),
    ],
)
def test_guess_section(name, section):
    assert guess_section(name) == section


@pytest.mark.parametrize(
    ("a", "b", "total"),
    [
        ("250 g", "250 g", "500 g"),
        ("1 kg", "500 g", "1,5 kg"),
        ("1 l", "250 ml", "1,25 l"),
        ("2", "3", "5"),
        ("×2", "×1", "×3"),
        ("2 szt", "1", "3 szt"),
        ("1,5", "1,5", "3"),
        ("2 op", "1 op", "3 op"),
        (None, "3", "3"),
        ("2", None, "2"),
        ("250 g", "2", "250 g + 2"),
    ],
)
def test_add_qty(a, b, total):
    assert add_qty(a, b) == total
