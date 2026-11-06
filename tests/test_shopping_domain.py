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
        ("2 op", "1 op", "3 op."),
        ("1 łyżka", "2 łyżki", "3 łyżki"),
        ("1,5 łyżeczki", "1 łyżeczka", "2,5 łyżeczki"),
        ("3 łyżki", "2 łyżki", "5 łyżek"),
        (None, "3", "3"),
        ("2", None, "2"),
        ("250 g", "2", "250 g + 2"),
    ],
)
def test_add_qty(a, b, total):
    assert add_qty(a, b) == total


@pytest.mark.parametrize(
    ("text", "name", "qty"),
    [
        ("250 g mleka", "Mleko", "250 g"),
        ("175 g cukru", "Cukier", "175 g"),
        ("5 jajek", "Jajka", "5"),
        ("1 jajko", "Jajka", "1"),
        ("5 żółtek", "Żółtka", "5"),
        ("100 g masła", "Masło", "100 g"),
        ("1 laska cynamonu", "Cynamon", "1 laska"),
        ("szczypta soli", "Sól", "1 szczypta"),
        ("1,5 łyżeczki soli", "Sól", "1,5 łyżeczki"),
        ("2 ząbki czosnku", "Czosnek", "2 ząbki"),
        ("1 opakowanie ciasta francuskiego", "Ciasto francuskie", "1 op."),
        ("200 g mąki pszennej typ 450", "Mąka pszenna typ 450", "200 g"),
        ("100 g sera żółtego", "Ser żółty", "100 g"),
        ("500 g mięsa mielonego", "Mięso mielone", "500 g"),
        ("20 g świeżych drożdży", "Świeże drożdże", "20 g"),
        ("50 g cukru pudru", "Cukier puder", "50 g"),
        ("40 g bułki tartej", "Bułka tarta", "40 g"),
        ("250 g śmietany, min. 30% tłuszczu", "Śmietana, min. 30% tłuszczu", "250 g"),
        # left as written: text after a preposition, unknown words, nominatives
        ("skórka z cytryny", "Skórka z cytryny", None),
        ("filet z piersi kurczaka", "Filet z piersi kurczaka", None),
        ("Male wino do gotowania", "Male wino do gotowania", None),
        ("papier ścierny 2000", "Papier ścierny 2000", None),
    ],
)
def test_recipe_lines_in_the_lists_form(text, name, qty):
    assert parse_entry(text) == (name, qty)
