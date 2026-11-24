# EGSim – Dummy-Energiegemeinschaften für die Handelsplattform

EGSim simuliert Energiegemeinschaften (EGs), die mit eurer Handelsplattform sprechen (**Datenvertrag 0.3.1**).
Er verhält sich wie echte EGs: Er meldet sich an, reicht Angebote und Bedarfe ein, ändert und storniert sie und holt
nach dem Settlement Zuteilungen und Handelsabschlüsse ab. Dabei prüft er **jede Antwort gegen die Schemas des Vertrags**
(siehe „Schema-Prüfung“). Dazu kommen rund 130 Negativ- und Vertragstests (Fehlercodes, Rollen, Mandantentrennung,
Validierung, Filter, Idempotenz) und ein **Katalog gezielter Randfälle** (auch gleichzeitige und doppelte Requests).

Bedient wird alles über eine **Web-UI**.

## Schnellstart

```bash
pip install -e ".[dev]"          # Python ≥ 3.10, Abhängigkeit: requests (unter Windows zusätzlich tzdata)

egsim ui --mock --open           # Demo: eingebauter Mock der Plattform, alles vorkonfiguriert
egsim ui --open                  # gegen eure Plattform; Konfiguration in egsim-ui.json
```

Der Mock setzt den Datenvertrag vollständig um. Damit lassen sich Simulator und Vertrag testen, solange eure
Plattform noch nicht steht. Sobald sie läuft, tragt ihr in der UI ihre Adresse ein, z. B. `http://localhost:8080`.

## Warum die Plattform die EGs kennen muss

Der Simulator schickt Daten, als wäre er eine EG. Laut Datenvertrag braucht aber jede Anfrage einer EG einen API-Key
oder ein Token. Die Plattform erkennt daran, *welche* EG spricht: Jede Position bekommt deren `energyCommunityId`,
Zuteilungen und Trades gehören zu ihr, und keine EG darf fremde Daten sehen. Unbekannte Schlüssel lehnt die Plattform
mit 401 ab. Die EG muss also **auf der Plattform** existieren. Im Simulator selbst wird nichts eingerichtet.

Dafür gibt es drei Wege:

1. **Admin-Key der Test-Plattform in der UI eintragen (empfohlen).** EGSim legt die EGs beim Start selbst an
   (`POST /energy-communities`), schaltet sie frei und holt die Zugangsdaten. Pro EG reichen Name und Lastprofil.
2. **Feste Test-EGs in eurer Plattform** (Seed-Daten im Dev-Modus) und deren API-Keys in EGSim eintragen.
3. **Ohne Admin:** In der UI „Registrieren“ klicken, der Admin schaltet die EG frei und übergibt die Zugangsdaten.

Mit Admin-Key kann EGSim außerdem den Markt selbst schließen, das komplette Tagesergebnis (`/result`) nachrechnen
und die Admin-Checks ausführen. Den Admin-Key nur bei Test-Plattformen eintragen.

## Die UI

| Tab | Inhalt |
|---|---|
| **Lauf** | Plattform-URL, optionaler Admin-Zugang, Liefertage und Verhalten (Seed, Parallelität, Änderungen, Stornos) sowie die Prüfungen. Lauf starten/abbrechen mit Live-Log. „Nur Ergebnisse prüfen“ holt Ergebnisse nach einem späteren Marktschluss ab. |
| **Energiegemeinschaften** | EGs anlegen und konfigurieren: Zugang, Erzeugung und Verbrauch mit Lastprofil, Preisvorstellung, Grafik des Tagesverlaufs. Buttons: „Automatisch einrichten“, „Registrieren“, „Verbindung testen“. Vorschau der Positionen. |
| **Szenarien** | Randfälle auswählen und ausführen, Ergebnis je Szenario mit Positionen und Abweichungen. |
| **Manuell** | Einzelne Requests als gewählte EG, auch freie Requests mit absichtlich kaputtem JSON. Im Demo-Modus: Märkte anlegen und schließen. |
| **Ergebnis** | Fehler, Warnungen, Checks, Szenarien, Märkte, Latenz je Endpoint. Frühere Berichte, Download als JSON. |
| **Requests** | Jeder Request mit Body und Antwort, filterbar. API-Keys, Passwörter und Tokens werden ausgeblendet. |

Änderungen landen sofort in `egsim-ui.json`. Die Datei enthält Zugangsdaten und steht in `.gitignore`.

## Ein Lauf

1. Admin-Zugang prüfen (falls eingetragen), EGs anmelden bzw. automatisch einrichten.
2. `GET /markets/{deliveryDate}`: Auflösung, Preise und `closesAt` lesen.
3. Ist der Markt offen: Positionen aus den Lastprofilen einreichen, einen Teil ändern und stornieren.
4. Negativtests bei offenem Markt.
5. Markt schließen (mit Admin-Key und Option) oder bis zu N Minuten auf den Betreiber warten.
6. Negativtests bei geschlossenem Markt, Ergebnisprüfung, Konto- und Admin-Checks.
7. Bericht in `reports/egsim-<Zeitstempel>.json`.

**Lastprofile**: `solar`, `wind`, `household`, `business`, `night`, `flat`. Im Tagesmarkt eine Position mit der
Tagesmenge, im 15-Minuten-Markt nach Profil auf die Viertelstunden verteilt. Hat eine EG Erzeugung und Verbrauch,
meldet sie den Saldo. Gleicher Seed = gleiche Mengen und Preise.

## Szenarien (Randfälle)

Jedes Szenario legt feste Positionen an und kennt das erwartete Ergebnis. Die Erwartungen sind von Hand aus dem
Vertrag abgeleitet. Seit 0.3 hat jede Plattform ihre eigene Preis- und Zuteilungsregel, verbindlich sind nur die
Invarianten. Deshalb gibt es die Einstellung **Zuteilungsregel** (`run.allocationRule`):

- `invariants` (Standard): passt für jede Plattform. Geprüft werden Status, Invarianten (Ankauf = min(S, D), keine
  Zuteilung über der Menge, Reserve, Summen, Betrag = Menge × Preis, Marge) und Erwartungen, die daraus zwingend folgen.
  Bei Überangebot reicht eine zur Regel passende `reason` (`PARTIALLY_ALLOCATED`/`NOT_ALLOCATED`/`FULLY_ALLOCATED`).
- `gruppe1`: zusätzlich exakt nach dem Referenzbeispiel Gruppe 1 (pro rata, Rundungsrest nach `createdAt`, Reserve pro
  rata, Beträge aus den festen Preisen in `Market.prices`). Nur für Plattformen, die genau so rechnen.

Fehlt `Market.prices` (seit 0.3 optional), rechnet EGSim Preisvorstellungen relativ zu 0,10 EUR/kWh.

| ID | Szenario | Erwartung |
|---|---|---|
| SZ-01 | Angebot = Bedarf | beide voll, keine Reserve |
| SZ-02 | Preisvorstellung = Ankaufspreis | gehandelt |
| SZ-03 | Preisvorstellung 0,05 € über Ankaufspreis | trotzdem voll gehandelt (Preisvorstellung ist kein Mindestpreis) |
| SZ-04 | 3 × 5 kWh Angebot, 10 kWh Bedarf | zusammen 10 kWh; Gruppe 1: 3,334 / 3,333 / 3,333, Rest an das zuerst eingereichte Angebot |
| SZ-05 | 100 + 60 kWh Angebot, 120 kWh Bedarf | zusammen 120 kWh, keine Reserve; Gruppe 1: 75 / 45 kWh |
| SZ-06 | nur Bedarf | volle Reserve (oder `RESERVE_SHORTAGE`) |
| SZ-07 | nur Angebot | `NOT_NEEDED`, kein Trade |
| SZ-08 | 0,001 kWh | Trade über 0,001 kWh, Betrag gerundet |
| SZ-09 | 1.000.000 kWh | große Beträge ohne Rundungsfehler |
| SZ-10 | 0,125 kWh | Beträge kaufmännisch gerundet |
| SZ-11 | Ändern und Stornieren | zählt der letzte Stand, Storno ohne Zuteilung |
| SZ-12 | EG nach dem Einreichen gesperrt (Admin) | Position `REJECTED` mit `rejectionReason ENERGY_COMMUNITY_NOT_ACTIVE` |
| SZ-13 | Unterangebot: 30 kWh Angebot, 50 kWh Bedarf | Angebot voll, 20 kWh aus der Reserve |
| SZ-14 | Beispielablauf aus dem Vertrag 0.3 (100 + 50 kWh Angebot, das zweite mit hoher Preisvorstellung, 170 kWh Bedarf) | beide Angebote voll, Reserve 20 kWh; Gruppe 1 (0,10 / 0,14 / 0,20): Einkauf 15,00, Verkauf 23,80, Reserve 4,00, Marge 4,80 EUR |
| SZ-15 | Zwei Angebote, zwei Bedarfe, Unterangebot | Angebote voll, Reserve 20 kWh; Gruppe 1: pro rata 12 / 8 kWh, je ein Trade aus Angeboten und aus der Reserve |
| SZ-16 | Doppelter Request: dasselbe Angebot gleichzeitig 5-mal mit verschiedenen Keys | genau eine Position, 4 × `409 POSITION_ALREADY_EXISTS`, ein Trade |
| SZ-17 | Parallele Einreichung von vier EGs | keine Überbuchung: zusammen 40 kWh gekauft, Summen stimmen; Gruppe 1: je 20 kWh, genau 4 Trades |
| SZ-18 | Dieselbe Position 3-mal gleichzeitig mit **demselben** Idempotency-Key | dreimal 201 mit derselben Position, nur ein Trade |
| SZ-20 | jede Viertelstunde des Tages | 96 bzw. 92/100 an Umstellungstagen angenommen |
| SZ-21 | Lasttest | alle EGs gleichzeitig, keine Serverfehler, Latenz p95 |

Mit Admin-Key prüfen die Szenarien zusätzlich das Tagesergebnis (`/result`) und die gehandelten Mengen im Intervall
(doppelte Trades fallen auf). Die Beträge von SZ-14 (Marge 4,80) gelten nur mit `gruppe1`, im Tagesmarkt und wenn das
Kraftwerk die Reserve voll geliefert hat.

Im **15-Minuten-Markt** bekommt jedes Szenario eine eigene Viertelstunde ab 10:00 Uhr Wiener Zeit, alle laufen auf
einem Markt. Der Lasttest nutzt 15:00–19:00 Uhr. Im **Tagesmarkt** braucht jedes Szenario einen eigenen Liefertag.
Die genutzten Viertelstunden sollten leer sein, am besten einen eigenen Testmarkt nehmen (Feld „Liefertage für
Szenarien“). Mit Admin-Key legt EGSim fehlende Szenario-EGs selbst an. Ohne Admin-Key braucht es so viele EGs mit
Zugangsdaten, wie die UI anzeigt (maximal vier).

Nicht vom Simulator auslösbar ist alles, was eure Plattform entscheidet: Reserve reicht nicht (Kraftwerk),
Settlement schlägt fehl (`FAILED`), Token läuft nach einer Stunde ab. Das testet ihr mit Testeinstellungen in der
Plattform. EGSim prüft dann, ob Zuteilungen und Trades dazu passen.

## Vertragsstand

`contract/Datenvertrag.yaml` ist **Fassung 0.3.1** (0.3 plus Präzisierungen, keine neuen Felder). EGSim akzeptiert
`Api-Version` 0.3.x. Was sich gegenüber 0.2 für den Simulator geändert hat:

- **Preise und Zuteilung:** Die Preisvorstellung ist nur informativ, `PRICE_NOT_MET` gibt es nicht mehr, neu ist
  `NOT_ALLOCATED`. `Market.prices` ist eine optionale Indikation, `MarketResult.prices` sind Durchschnittspreise
  (gewichtet nach Menge, `null` ohne Geschäft). `tradableOfferedKwh` entfällt, Summen sind unbegrenzt.
- **Idempotenz:** `POST /positions` verlangt `Idempotency-Key` (UUID), die Registrierung erlaubt ihn. Gleicher Key und
  Inhalt → gespeicherte Antwort (auch nach `closesAt`), anderer Inhalt → `409 IDEMPOTENCY_KEY_REUSED`, Key gilt pro
  Identität, 401/403/429 werden nicht gespeichert, parallele Requests erzeugen keine zweite Position (`IDEM-01` bis `IDEM-09`, SZ-18).
  Der Simulator wiederholt nach Timeout oder 5xx mit demselben Key.
- **`communityCode`:** Pflicht bei der Registrierung (3–40 Zeichen, Kleinbuchstaben, Ziffern, einzelne Bindestriche),
  eindeutig (`409 COMMUNITY_CODE_ALREADY_REGISTERED`), unveränderlich (`REG-11` bis `REG-15`). In der UI je EG
  einstellbar, leer = Vorschlag aus dem Namen.
- **Header:** `X-Request-Id` in jeder Antwort (mitgeschickte wird übernommen), `requestId` in jedem Fehler,
  `Api-Version`, bei `429` `Retry-After` (`REQ-01/02`, Header-Prüfung bei jeder Antwort).
- **Rate-Limit:** Login und Registrierung können `429 RATE_LIMITED` liefern. `RATE-01` (optional, UI „Rate-Limit-Test“)
  provoziert das mit Fehlversuchen; EGSim selbst wartet einmal `Retry-After` ab (bis 10 s).
- **Sonstiges:** `GET /markets?status=` (`MKT-01` bis `03`, Liefertag „open“ in der UI), historische Liefertage sind
  erlaubt (`DELIVERY_DATE_IN_PAST` entfällt), `DELETE` auf eine stornierte Position → `200`, `PUT` ersetzt die Position
  (fehlende Preisvorstellung wird entfernt, `POS-09`), neue Prüfreihenfolge.

## Schema-Prüfung

Jede Antwort der Plattform wird gegen die Schemas des Datenvertrags geprüft (`contract/Datenvertrag.yaml`, daraus
erzeugt als `egsim/contract.json`): Pflichtfelder, Typen (eine Zahl als String `"10"` fällt auf), Wertebereiche,
Nachkommastellen, Formate (UUID, Datum, RFC-3339-Zeitstempel, `intervalStart` nur voll auf der Viertelstunde in UTC),
erlaubte Enum-Werte und das Fehlerformat (`code` aus der Liste des Vertrags, `message`, `requestId`, `details`). Außerdem:

- Antworten müssen JSON mit `Content-Type: application/json` sein, `204` hat keinen Body.
- Ein Statuscode, der für den Endpoint nicht im Vertrag steht, ist eine Warnung (401, 403, 429 und 500 gelten überall).
- Header: `X-Request-Id` (1–128 Zeichen, gleich der mitgeschickten), `Api-Version` (andere Nebenversion = Warnung),
  `requestId` im Fehler gleich dem Header, `Retry-After` bei 429.
- Passwörter, API-Keys und Hashes dürfen nur in der Antwort von `POST /energy-communities/{id}/credentials` stehen,
  das Token nur im Login.

Abweichungen stehen im Bericht als Befund mit Bereich `contract` (je Endpoint, Status und Meldung einmal, mit Anzahl)
und in `contractIssues`. Ausschalten: `run.validateSchema = false` bzw. in der UI „Schema-Prüfung“.

Neue Vertragsversion: YAML in `contract/` ersetzen und `python tools/gen_contract.py --check` ausführen (braucht
PyYAML, nur für Entwickler). Das Skript prüft dabei auch, ob alle Beispiele des Vertrags zu seinen Schemas passen.

## Ergebnisprüfung

- **Immer (EG-Sicht):** Mandantentrennung, Positionsstatus nach dem Settlement, genau eine Zuteilung je Position,
  `reason` passend zu Menge und Typ, Trades passend zur Zuteilung, Beträge = Menge × Preis (kaufmännisch gerundet).
  Weichen Trade-Preise von der Preisindikation ab, ist das nur ein Hinweis.
- **Mit Admin-Key:** Invarianten 3–8 je Intervall aus allen Positionen, `/allocations` und `/trades`, dazu das
  Tagesergebnis `/result`: Summen, Reserve (bei 15 Minuten Summe der Intervalle), Beträge, Marge und die
  Durchschnittspreise (`null` ohne Geschäft). Doppelte Handelsabschlüsse fallen auf.
- **Mit `allocationRule = gruppe1`:** zusätzlich exakte Nachrechnung nach dem Referenzbeispiel (braucht vollständige
  `Market.prices`).
- **Marktzustand nach dem Settlement:** `SETTLED` mit `closedAt` und `settledAt` (nicht davor), `failureReason` leer,
  `resolution` und `closesAt` unverändert. Eine geänderte Preisindikation ist nur ein Hinweis.
- **Positionen:** `rejectionReason` nur bei `REJECTED` und dann `ENERGY_COMMUNITY_NOT_ACTIVE`.
- **Ohne Admin, „Nur simulierte EGs am Markt“:** Komplette Nachrechnung aus den Daten der simulierten EGs.

## Negativtests

| Gruppe | Beispiele |
|---|---|
| `AUTH` | fehlende/ungültige Zugangsdaten → 401, falsches Passwort, Logout macht Token ungültig |
| `ROLE` | EG listet alle EGs, ruft Tagesergebnis ab, ändert Status, vergibt sich Zugangsdaten → 403 |
| `TEN` | fremde Positionen abrufen/ändern/stornieren, fremde IDs und Filter → 403 |
| `VAL`, `INT` | Grenzen und Nachkommastellen, Typen, Datumsformat, unbekannte Felder, kaputtes JSON, `intervalStart` |
| `NF`, `STATE`, `POS`, `PROF` | 400/404-Codes, doppelte Position, `MARKET_NOT_SETTLED`, `POSITION_NOT_MODIFIABLE` |
| `CLOSED` | nach Marktschluss: Einreichen/Ändern/Stornieren → 409, Position `PROCESSED` |
| `CLOSES` | `closesAt` erreicht, Markt noch `OPEN` (Betreiber schließt später): Einreichen/Ändern/Stornieren → 409 `MARKET_CLOSED`. Läuft, wenn der Simulator auf den Marktschluss wartet |
| `POS` (PUT) | `quantityKwh` 0 oder mit 4 Nachkommastellen, Preis mit 6 Stellen oder negativ, Bedarf mit Preisvorstellung → 400 |
| `FIL` | Filter `type`, `status`, `energyCommunityId`, `intervalStart`, `direction`, `positionId` wirken (auch die Gegenprobe), ungültige Filterwerte → 400 (vor 409) |
| `NF-07` | Position für einen Liefertag ohne Markt → 404 `MARKET_NOT_FOUND` |
| `ADM` (Admin) | Statusübergänge, PENDING/gesperrte EG → 403 auch bei Positionen einreichen/ändern/stornieren/auflisten, neue Zugangsdaten machen alte API-Keys **und Tokens** ungültig, Löschen (204/409), Admin sieht fremde Positionen und EGs, Markt-Schließen idempotent (Zuteilungen und Trades bleiben gleich) und auch bei 4 gleichzeitigen Aufrufen |
| `REG` (optional) | Registrierung mit ungültigen Daten → 400, doppelte E-Mail bzw. `communityCode` → 409, `communityCode` fehlt/ungültig → 400, im Profil nicht änderbar, Idempotency-Key bei der Registrierung |
| `IDEM`, `REQ`, `MKT` | Idempotency-Key (siehe Vertragsstand), X-Request-Id/requestId, `GET /markets` mit Filter |
| `RATE` (optional) | Login-Fehlversuche → `429 RATE_LIMITED` mit `Retry-After` |

## Hosten

**Lokal:** `egsim ui` läuft auf `127.0.0.1:8090`. Die Plattform lokal starten und `http://localhost:<port>` eintragen.

**VPS mit Docker Compose** (Simulator und Plattform auf demselben Server):

```bash
cp deploy/.env.example .env      # UI-Passwort und Domain eintragen
docker compose up -d --build
```

- Caddy stellt die UI per HTTPS unter `EGSIM_DOMAIN` bereit (Let's Encrypt, die Domain muss auf den VPS zeigen).
- Die UI verlangt ein Passwort (Benutzer `egsim`, Passwort aus `EGSIM_UI_PASSWORD`).
- Eure Plattform als weiteren Service in `docker-compose.yml` eintragen (Vorlage ist drin). EGSim erreicht sie
  intern unter `http://plattform:8080`, die Plattform-API muss dafür nicht öffentlich sein.
- Ohne Domain: Caddy weglassen und per SSH-Tunnel zugreifen: `ssh -L 8090:localhost:8090 <vps>`.

**Kubernetes:** `deploy/kubernetes/egsim.yaml` enthält Deployment, Service und Volume. Das Image baut GitHub Actions
bei jedem Push nach `ghcr.io/renshredder/egsim`. Das UI-Passwort kommt aus einem Secret, als Basis-URL tragt ihr den
Service der Plattform im Cluster ein.

Ohne Docker: Mit `EGSIM_UI_PASSWORD=… egsim ui --host 0.0.0.0` ist die UI auch direkt passwortgeschützt.

## Ohne UI (z. B. CI)

```bash
egsim run -c egsim-ui.json                     # Lauf mit der gespeicherten UI-Konfiguration
egsim run -c egsim-ui.json --close             # Markt per Admin-Key selbst schließen
                                               # (run.parallelClose = 5 in der JSON: 5 gleichzeitige close-Aufrufe)
egsim run -c egsim-ui.json --scenarios         # ausgewählte Szenarien (oder --scenarios SZ-01,SZ-04)
egsim run -c egsim-ui.json --wait 30           # bis zu 30 min auf das Settlement warten
egsim run -c egsim-ui.json --verify-only       # nur Ergebnisse prüfen
egsim mock --demo-egs 3 --auto-settle          # Mock als eigenen Server (Port 8080, Admin-Key admin-key)
```

Exit-Code `0` = alles vertragskonform, `1` = Befunde, `2` = Konfigurationsfehler. `${VAR}` in der JSON-Datei wird
aus Umgebungsvariablen gelesen, so müssen keine Secrets in der Datei stehen.

## Annahmen zum Datenvertrag (bitte abstimmen)

1. **Markt anlegen:** Laut Vertrag legt der Betreiber den Markt außerhalb der API an (Deployment-Konfiguration oder
   Initialisierungsprozess). Bis dahin antworten `GET /markets/{deliveryDate}` und `POST /positions` mit
   `404 MARKET_NOT_FOUND`. EGSim setzt voraus, dass der Markt existiert, und findet offene Märkte über `GET /markets`.
2. **Unbekannte vs. fremde EG-ID:** 404 für unbekannt, 403 für fremd (Prüfreihenfolge).
3. **Stornierte Position:** Sie blockiert den Slot nicht für eine neue Position (SZ-20, SZ-21).
4. **Zugangsdaten für PENDING-EGs:** Die Vergabe ist erlaubt, eine Anmeldung damit ergibt 403 (`ADM-07` bis `ADM-09`).
5. **`createdAt` nach PUT:** Es bleibt unverändert (bei Gruppe 1 bestimmt es die Rundungsreihenfolge).
6. **Gleichzeitige Requests mit verschiedenen Keys** für denselben Slot: genau einer wird angenommen, die anderen
   bekommen `409 POSITION_ALREADY_EXISTS` (SZ-16).

## Projektstruktur

```
egsim/
  ui/server.py, ui/static/index.html   Web-UI (Standardbibliothek + Vanilla-JS), optional Basic Auth
  simulator.py    Ablauf: Anmelden/Einrichten, Einreichen, Ändern, Stornieren, Schließen/Warten, Prüfen
  scenarios.py    Szenario-Katalog
  checks.py       Negativ-, Konto- und Admin-Checks
  verify.py       Ergebnisprüfung (EG-Sicht, vollständig, Admin inkl. /result)
  settlement.py   Referenzrechnung der Handels- und Preislogik (Decimal)
  profiles.py     Lastprofile → Positionen
  config.py       Konfiguration (JSON)
  client.py       HTTP-Client, Request-Log
  mockserver.py   In-Memory-Mock der API
  contract.py     Schema-Prüfer für Antworten (ohne Zusatzabhängigkeit), liest contract.json
  contract.json   Schemas und Antworten des Vertrags, erzeugt aus contract/Datenvertrag.yaml
contract/         Datenvertrag.yaml (Quelle der Schemas)
tools/            gen_contract.py (YAML → contract.json, nur für Entwickler)
deploy/           Caddyfile, .env-Vorlage, Kubernetes-Manifest
tests/            pytest: Vertragsbeispiele, Profile/Config, Ende-zu-Ende gegen Mock inkl. Szenarien und fehlerhafter Plattformen
```

Gegen den Mock zeigt ein „OK“ nur, dass der Ablauf funktioniert, denn Mock und Prüfung nutzen dieselbe
Referenzlogik (Gruppe 1). Die Logik selbst sichern die Unit-Tests mit den Beispielen aus dem Vertrag und die handgeschriebenen
Szenario-Erwartungen ab.

```bash
pytest -q
```
