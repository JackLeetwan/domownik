# Domownik

Rodzinny panel: strefy (Samochody, Dom, Mieszkanie wynajęte, Dzieci, Okazje…), zadania cykliczne,
oś czasu, historia z kosztami, powiadomienia i budżet domowy.

Plan: `~/.claude/plans/glowing-beaming-catmull.md`.

## Uruchomienie (Docker)

```bash
cp .env.example .env        # na tym PC: DOMOWNIK_UID=0 / DOMOWNIK_GID=0 (userns-remap)
docker compose up -d --build
docker compose exec app domownik create-user     # konto dla każdego domownika
```

Aplikacja słucha na `127.0.0.1:8000`. Dostęp z telefonów przez Tailscale:

```bash
sudo tailscale serve --bg 8000      # → https://<host>.<tailnet>.ts.net
```

Po włączeniu HTTPS ustaw w `.env`: `DOMOWNIK_HTTPS_ONLY=true` i `DOMOWNIK_BASE_URL=https://…`,
potem `docker compose up -d`.

## Budżet

`/budzet` – jeden wspólny budżet domu, miesiąc po miesiącu. Plan kategorii = koperta (miesięczny limit,
jeśli ustawiony) + terminy zadań z kwotą przypisanych do kategorii (serie rat, przełożone i pominięte
terminy liczone jak na osi czasu). Wykonanie to transakcje: zapłacone zadania (także automatycznie),
„Zakończ zakupy” z kwotą (Jedzenie) i ręczne wpisy. Płatność zadania liczy się w miesiącu terminu.
Stałe dochody (pensje, czynsz od najemcy) wpisuje się raz w **Kategorie i limity**, a co miesiąc
potwierdza „✓ Wpłynęło” – bez przypomnień i bez osi czasu.

**🏦 Import z banku** (`/budzet/import`) – CSV z historią rachunku (mBank; inne banki z kolumnami daty,
opisu i kwoty też). Najpierw podgląd z propozycją dla każdej operacji: opłacone zadanie z harmonogramu
(odhacza je), dochód co miesiąc, „już w budżecie” (wpisane ręcznie / „Zakończ zakupy” / autopay – nic się
nie dubluje), kategoria z zapamiętanej reguły albo z kategorii banku, pominięcie (przelewy własne). Wybrana
ręcznie kategoria staje się regułą („biedronka” → Jedzenie). Każda operacja jest zapamiętana – ten sam plik
można wgrać ponownie.

**📊 Koszty stref** (`/budzet/strefy?y=2026`) – ile w roku kosztowała każda strefa (auta, dom, mieszkanie
wynajęte…): płatności zadań, wpisy ze strefą, wpływy z dochodów przypisanych do strefy (czynsz od najemcy),
podział na elementy i kategorie, miesiące, porównanie z poprzednim rokiem. Skrót na stronie strefy.

## Grafik tygodnia i obowiązki

`/grafik` – stałe pozycje tygodnia z godziną (zajęcia dzieci, odbiór z przedszkola, basen, sprzątanie):
dni tygodnia, okres obowiązywania, kogo dotyczy i kto się zajmuje – jedna osoba, wspólnie, osoba na
każdy dzień albo naprzemiennie co tydzień. Pojedynczy dzień można odwołać. Dzisiejsze pozycje są w karcie
„Dziś” pod osią czasu i w porannym powiadomieniu, jutrzejsze w wieczornym; w kalendarzu tylko w liście dnia.

`/obowiazki` – obowiązki domowe co N dni z paskiem świeżości (zielony → czerwony), posortowane od
najpilniejszych, z rotacją „Teraz: osoba”. Po „✓ Zrobione” kolej przechodzi na osobę po tej, która
faktycznie zrobiła (odhaczenie za kogoś też się liczy); „cofnij” przywraca poprzednią kolej. Zaległe
obowiązki danej osoby to jedna linia porannego powiadomienia – nie trafiają na oś czasu ani do terminów.

## Szybkie dodawanie, kontakty, liczniki

„＋ Dodaj” (telefon: środkowy przycisk dolnego paska, desktop: w menu bocznym) – zadanie, zakup, notatka,
„do zrobienia”, wydatek albo odczyt licznika (gdy jest już jakiś licznik) z dowolnego ekranu; formularze korzystają z tych samych tras
co strony (nagłówek `X-Quick-Add`), po zapisie strona odświeża się w miejscu.

`/kontakty` – fachowcy, serwis auta, przedszkole, ubezpieczyciel, lekarz: telefony, e-mail, www, adres,
powiązanie z wieloma elementami (sekcja „Kontakty” na stronie elementu), podpowiedzi „Wykonawca” przy
„✓ Zrobione”, wyszukiwarka.

Liczniki (prąd, woda, gaz, ciepło) przy domu/mieszkaniu: odczyty ze zdjęciem, zużycie od poprzedniego,
średnie, wykres miesięczny (SVG z serwera), koszt z ceny za jednostkę, „nowy licznik / reset”.
Przebieg auta widać w tym samym układzie – tylko do odczytu, prognozy zadań z przebiegu bez zmian.

## Zadania z godziną, podzadania, zdjęcia

Zadanie może mieć godzinę (i koniec) – „📅 Wydarzenie” w „＋ Dodaj” to wizyta / spotkanie z godziną. Push
przychodzi przed godziną (o czasie … dzień wcześniej, raz na wystąpienie), godzina jest w kalendarzu, ICS,
porannym skrócie. W oknie zadania: kroki (checklista, po „Zrobione” zadania cyklicznego odznaczają się na
następny termin), zdjęcia / pliki z formularza (zostają też w dokumentach elementu) i komentarze (push do
osoby przypisanej). Punkty ⭐ i „Wymaga zatwierdzenia” – w „Więcej szczegółów”.

## Kalendarz

`/kalendarz` – miesiąc (polskie święta i dni wolne czerwonym numerem) albo `/kalendarz/tydzien` – siedem dni
jako lista. Filtr „Moje”.
Dwuklik na dzień (albo „＋” przy wybranym dniu) – „＋ Dodaj” wydarzenie, zadanie lub wydatek na ten dzień.

Kalendarze z zewnątrz (`/kalendarz/zewnetrzne`): link ICS przedszkola, szkoły, zajęć albo kalendarza z pracy
(Google: „Tajny adres w formacie iCal”). Pobierane co godzinę, wydarzenia cykliczne rozwinięte na ~13 miesięcy;
widać je w kalendarzu, w karcie „Dziś” i w powiadomieniu rano / wieczorem (dla całej rodziny albo jednej
osoby, „🔒 tylko dla mnie” – inni go nie widzą). Tylko do odczytu, nie trafiają na oś czasu ani do eksportu
ICS. Pobierane są tylko adresy publiczne (nie sieć lokalna). Na stronie widać z linku tylko nazwę serwera.

Przytrzymanie ikony aplikacji w telefonie: skróty „＋ Dodaj”, Zakupy, Kalendarz, Budżet.

Dziecko: na stronie dziecka „💉 Szczepienia i bilanse” – z daty urodzenia terminy szczepień obowiązkowych
(PSO) i bilansów zdrowia; zaznaczone stają się jednorazowymi zadaniami „Zdrowie” (przypomnienie 2 tygodnie
wcześniej).

Lista zakupów działa **offline**: service worker trzyma ostatnią wersję `/zakupy`, odhaczenia i dopisane
pozycje bez sieci czekają w telefonie i wysyłają się, gdy wróci zasięg (bez sieci każda strona otwiera
listę zakupów).

## Notatki, kuchnia, punkty

`/notatki` – tytuł, tekst albo lista do odhaczania na karcie, kolor, kategoria (chipsy filtra), filtr przy
pisaniu, komentarze. 🎤 dyktowanie (Web Speech API, tylko gdzie przeglądarka to umie) w zakupach, notatkach
i spiżarni – przy zakupach „i” rozdziela pozycje.

`/kuchnia` – jadłospis tygodnia (przepis albo tekst), przepisy (składniki w liniach, tagi), spiżarnia
(lodówka / zamrażarka / spiżarnia, data ważności). Składniki przepisu lub tygodnia → lista zakupów, z
odznaczonym tym, co jest w spiżarni; „Zakończ zakupy” może wrzucić kupione do spiżarni; rano push o
kończących się terminach.

`/punkty` – punkty za zadania i obowiązki (z zatwierdzaniem przez kogoś innego), nagrody, punkty ręcznie.
Budżet: 🎯 cele oszczędnościowe – wpłaty to wydatki w kategorii „Oszczędności”, tempo „odkładaj ok. X zł
miesięcznie”.

## Prywatne

„🔒 Tylko dla mnie” przy zadaniu, notatce i „do zrobienia”. Filtr siedzi w sesji bazy
(`db._private_filter`: `do_orm_execute` + `with_loader_criteria`) – każde zapytanie zalogowanej osoby
pomija cudze prywatne wiersze (listy, pulpit, kalendarz, oś czasu, wyszukiwarka, ICS, linki → 404).
Scheduler / CLI nie mają widza i filtrują same (`notify.py`). Wspólny budżet liczy kwoty wszystkich zadań
(`execution_options(all_private=True)`), cudze prywatne pokazuje jako „🔒 Prywatne”.

Ustawienia → **Menu**: dwa własne skróty dolnego paska i ukrywanie nieużywanych pozycji menu.

## Kalendarz w telefonie (ICS)

**Ustawienia → Kalendarz w telefonie** tworzy osobisty link `/ics/<token>.ics` (subskrypcja, bez logowania):
terminy zadań (z powtórzeniami, jak oś czasu), grafik tygodnia z godzinami (Europe/Warsaw) i końce gwarancji,
od 30 dni wstecz do roku naprzód; filtr „wszystko” / „moje i wspólne”. Bez kwot. W bazie jest tylko SHA-256
tokenu, „Wygeneruj nowy link” unieważnia stary, w logu dostępu uvicorna token jest zamazany (`/ics/…`).
iPhone: subskrybowany kalendarz (przez Tailscale). Kalendarz Google pobiera ICS ze swoich serwerów, więc
adresu w tailnecie nie zobaczy – na Androidzie potrzebna jest aplikacja typu ICSx⁵.

## Powiadomienia

Web Push wysyłany przez samego Domownika (service worker `/sw.js`, klucze VAPID w `data/secrets/`) –
bez dodatkowych aplikacji. Po zalogowaniu na nowym urządzeniu wyskakuje pytanie „Włączyć powiadomienia?”;
zarządzanie w **Ustawieniach**. Na iPhonie wymaga dodania do ekranu początkowego (iOS 16.4+).
Scheduler w kontenerze (co 5 min) wysyła rano (8:00) skrót: zaległe (co 3 dni), na dziś, nowe „wkrótce”;
wieczorem (19:00) – co jest jutro. Przycisk ✓ w powiadomieniu odhacza zadanie (podpisany link, 14 dni;
telefon musi być w tailnecie). Podgląd: `domownik notify morning`.
Na kopiach bazy uruchamiaj z `DOMOWNIK_SCHEDULER=false`. Po przywróceniu kopii (bez `secrets/`)
urządzenia trzeba włączyć ponownie.

## Kopie zapasowe

Codziennie o 3:30 (timer systemd użytkownika) `scripts/backup.sh`:
1. w kontenerze `domownik backup` → `data/backups/auto/` (14 ostatnich, bez klucza sesji),
2. kopiuje najnowsze archiwum na Dysk Google: `~/GoogleDrive/Domownik/kopie` (30 ostatnich).

```bash
ln -sf ~/domownik/deploy/systemd/domownik-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now domownik-backup.timer
systemctl --user start domownik-backup.service      # kopia od ręki
```

Przywrócenie: `docker compose exec app domownik import /data/backups/auto/<plik> --force`
(po przywróceniu wszyscy logują się ponownie – archiwum nie zawiera klucza sesji).

## Rozwój

```bash
uv sync
uv run pytest
DOMOWNIK_DATA_DIR=/tmp/dm uv run domownik serve --reload --port 8765
uv run domownik makemigration "opis zmiany"     # po zmianie models.py
```

## Komendy

| Komenda | Co robi |
|---|---|
| `domownik serve` | migracje + serwer |
| `domownik create-user` | nowe konto / reset hasła |
| `domownik seed-zones` | domyślne strefy (pusta baza) |
| `domownik export` / `import` | archiwum bazy i plików (backup, przeprowadzka) |
| `domownik backup` | codzienna kopia z rotacją (bez sekretów) |
| `domownik notify [morning\|evening] [--send]` | podgląd / wysyłka skrótu powiadomień |

## Struktura

- `src/domownik/domain/recurrence.py` – silnik terminów (czyste funkcje, testy w `tests/test_recurrence.py`)
- `src/domownik/services.py` – logika aplikacji (szablony → zadania, „Zrobione”, oś czasu, statystyki stref)
- `src/domownik/catalog/templates/*.yaml` – szablony elementów z proponowanymi zadaniami
- `src/domownik/catalog/seed/*.yaml` – domyślne strefy i kategorie budżetu
- `data/` – cały stan (SQLite, załączniki, sekrety, kopie) – poza gitem
