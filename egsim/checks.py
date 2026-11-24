"""Negativ- und Vertragstests: prüfen Statuscodes, Fehlercodes, Rollen und Mandantentrennung.

Validierungstests nutzen eine EG, die für denselben Liefertag/dasselbe Intervall schon eine Position hat.
Ein Server, der die Validierung auslässt, antwortet dann mit 409 statt eine falsche Position anzulegen
(und verletzt nebenbei die vorgeschriebene Prüfreihenfolge 400 vor 409).
"""
from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Callable

from egsim.client import ApiClient, ApiResponse
from egsim.timeutil import day_bounds_utc, format_utc, normalize_interval, quarter_hours, resolve_delivery_date

if TYPE_CHECKING:
    from egsim.simulator import MarketRun, SimEG, Simulator, SubmittedPosition

log = logging.getLogger("egsim.checks")

Expect = list[tuple[int, tuple[str, ...]]]  # erlaubte (HTTP-Status, Fehlercodes); () = Code egal


class Checker:
    def __init__(self, sim: "Simulator", phase: str) -> None:
        self.sim = sim
        self.phase = phase
        self.results: list[dict] = []

    def run(self, cid: str, title: str, fn: Callable[[], ApiResponse], expect: Expect,
            *, note_on: dict[tuple[int, str], str] | None = None,
            on_unexpected_success: Callable[[ApiResponse], None] | None = None,
            extra: Callable[[ApiResponse], str | None] | None = None) -> ApiResponse | None:
        try:
            r = fn()
        except Exception as exc:  # noqa: BLE001 – ein kaputter Check soll den Lauf nicht abbrechen
            self._add(cid, title, "FAIL", expect, None, None, f"Ausnahme: {exc!r}")
            return None
        passed, via_details = False, False
        for status, codes in expect:
            if r.status != status:
                continue
            if not codes or r.code in codes:
                passed = True
            elif r.code == "VALIDATION_FAILED" and set(codes) & r.detail_codes:
                passed, via_details = True, True
            if passed:
                break
        note = None
        if via_details:
            note = "Code nur in details, oberster Code VALIDATION_FAILED"
        if note_on and (r.status, r.code) in note_on:
            note = note_on[(r.status, r.code)]
        if passed and extra:
            problem = extra(r)
            if problem:
                passed, note = False, problem
        if not passed and r.ok and on_unexpected_success:
            on_unexpected_success(r)
        self._add(cid, title, "PASS" if passed else "FAIL", expect, r.status, r.code, note, r.short())
        return r

    def skip(self, cid: str, title: str, reason: str) -> None:
        self._add(cid, title, "SKIP", [], None, None, reason)

    def _add(self, cid, title, result, expect, status, code, note, request=None) -> None:
        exp = " oder ".join(f"{s} {'/'.join(c) if c else ''}".strip() for s, c in expect)
        self.results.append({"id": cid, "title": title, "phase": self.phase, "result": result, "expected": exp,
                             "actualStatus": status, "actualCode": code, "note": note, "request": request})
        if result == "FAIL":
            self.sim.findings.error("check", f"{cid}: {title} – erwartet {exp}, erhalten {status} {code or ''}"
                                             f"{' (' + note + ')' if note else ''}", request)
        elif note and result == "PASS":
            self.sim.findings.info("check", f"{cid}: {note}", request)


def _err(status: int, *codes: str) -> tuple[int, tuple[str, ...]]:
    return status, codes


def _pick(run: "MarketRun") -> tuple["SubmittedPosition | None", "SubmittedPosition | None"]:
    """Zwei offene Positionen verschiedener EGs."""
    live = [sp for sp in run.positions if sp.id and not sp.cancelled and sp.status == "OPEN"]
    a = live[0] if live else None
    b = next((sp for sp in live if a and sp.eg.id != a.eg.id), None)
    return a, b


def _cleanup(sim: "Simulator", client: ApiClient) -> Callable[[ApiResponse], None]:
    def f(r: ApiResponse) -> None:
        pid = (r.body or {}).get("id") if isinstance(r.body, dict) else None
        if pid:
            client.cancel_position(pid)
            sim.findings.warning("check", f"Ungültige Position wurde angelegt ({pid}) und wieder storniert")
    return f


# =====================================================================================================================
def run_open_market_checks(sim: "Simulator", run: "MarketRun") -> list[dict]:
    c = Checker(sim, f"open:{run.delivery_date}")
    d = run.delivery_date
    res = run.market["resolution"]
    sp_a, sp_b = _pick(run)
    anon = sim.base
    rnd = str(uuid.uuid4())

    c.run("AUTH-01", "Ohne Zugangsdaten → 401", lambda: anon.call("GET", "/me", auth=False), [_err(401, "UNAUTHORIZED")])
    c.run("AUTH-02", "Ungültiger API-Key → 401", lambda: anon.with_auth(api_key="egsim-invalid-" + rnd).me(),
          [_err(401, "UNAUTHORIZED")])
    c.run("AUTH-03", "Ungültiges Bearer-Token → 401", lambda: anon.with_auth(token="egsim.invalid." + rnd).me(),
          [_err(401, "UNAUTHORIZED")])
    if not sp_a:
        c.skip("POS-*", "Positions-Checks", "keine offene eigene Position vorhanden")
        return c.results
    c.run("NF-01", "Markt ohne Eintrag → 404 MARKET_NOT_FOUND", lambda: sp_a.eg.client.get_market("2099-12-31"),
          [_err(404, "MARKET_NOT_FOUND")])
    c.run("NF-02", "Unbekannte EG → 404 ENERGY_COMMUNITY_NOT_FOUND", lambda: sp_a.eg.client.get_community(rnd),
          [_err(404, "ENERGY_COMMUNITY_NOT_FOUND")])
    c.run("NF-03", "Ungültige EG-ID → 400 INVALID_ID", lambda: sp_a.eg.client.get_community("not-a-uuid"),
          [_err(400, "INVALID_ID")])
    c.run("NF-06", "Ungültiges Datum im Pfad → 400 INVALID_DELIVERY_DATE", lambda: sp_a.eg.client.get_market("20-11-2026"),
          [_err(400, "INVALID_DELIVERY_DATE")])
    c.run("NF-07", "Position für einen Liefertag ohne Markt → 404 MARKET_NOT_FOUND",
          lambda: sp_a.eg.client.submit_position({"deliveryDate": "2099-12-31", "type": "DEMAND", "quantityKwh": Decimal(1)}),
          [_err(404, "MARKET_NOT_FOUND")], on_unexpected_success=_cleanup(sim, sp_a.eg.client))
    c.run("STATE-02", "Zuteilungen vor Settlement → 409 MARKET_NOT_SETTLED", lambda: sp_a.eg.client.allocations(d),
          [_err(409, "MARKET_NOT_SETTLED")])
    c.run("STATE-03", "Trades vor Settlement → 409 MARKET_NOT_SETTLED", lambda: sp_a.eg.client.trades(d),
          [_err(409, "MARKET_NOT_SETTLED")])

    eg = sp_a.eg
    cl = eg.client
    base = sp_a.planned.body(d)

    def body(**changes):
        b = dict(base)
        for k, v in changes.items():
            if v is _DROP:
                b.pop(k, None)
            else:
                b[k] = v
        return b

    def post(b):
        return lambda: cl.submit_position(b)

    cleanup = _cleanup(sim, cl)

    # --- Rollen
    c.run("ROLE-02", "EG listet alle EGs → 403", lambda: cl.list_communities(), [_err(403, "FORBIDDEN")])
    c.run("ROLE-03", "EG ruft Tagesergebnis ab → 403", lambda: cl.market_result(d), [_err(403, "FORBIDDEN")])
    c.run("ROLE-04", "EG ändert eigenen Status → 403", lambda: cl.set_status(eg.id, "ACTIVE"), [_err(403, "FORBIDDEN")])

    def reattach(r: ApiResponse) -> None:  # falls der Server das fälschlich erlaubt: neuen Key übernehmen
        if isinstance(r.body, dict) and r.body.get("apiKey"):
            eg.cfg.api_key, eg.cfg.password = r.body["apiKey"], r.body.get("password") or eg.cfg.password
            eg.client = cl.with_auth(api_key=eg.cfg.api_key)
            sim.findings.error("check", f"[{eg.key}] EG konnte sich selbst neue Zugangsdaten ausstellen – "
                                        "neuer API-Key wurde in die Konfiguration übernommen")
    c.run("ROLE-05", "EG vergibt sich selbst Zugangsdaten → 403", lambda: cl.issue_credentials(eg.id),
          [_err(403, "FORBIDDEN")], on_unexpected_success=reattach)
    cl = eg.client

    # --- Profil
    c.run("PROF-01", "Profil mit Feld status → 400 VALIDATION_FAILED",
          lambda: cl.update_community(eg.id, {"status": "ACTIVE"}), [_err(400, "VALIDATION_FAILED")])
    c.run("PROF-02", "Eigenes Profil ändern → 200",
          lambda: cl.update_community(eg.id, {"description": "Simulierte EG (EGSim), Profil geändert"}), [_err(200)])
    c.run("PROF-03", "Leerer Name → 400 VALIDATION_FAILED", lambda: cl.update_community(eg.id, {"name": "   "}),
          [_err(400, "VALIDATION_FAILED")])
    c.run("PROF-04", "Eigene EG abrufen → 200", lambda: cl.get_community(eg.id), [_err(200)],
          extra=lambda r: None if (r.body or {}).get("id") == eg.id else "falsche EG zurückgegeben")
    c.run("PROF-05", "Profil mit Feld communityCode → 400 VALIDATION_FAILED (unveränderlich)",
          lambda: cl.update_community(eg.id, {"communityCode": "egsim-neu-" + rnd[:6]}), [_err(400, "VALIDATION_FAILED")])

    # --- Validierung (Prüfreihenfolge: 400 vor 409)
    offer = base["type"] == "OFFER"
    c.run("VAL-01", "quantityKwh = 0 → 400 INVALID_QUANTITY", post(body(quantityKwh=Decimal(0))),
          [_err(400, "INVALID_QUANTITY")], on_unexpected_success=cleanup)
    c.run("VAL-02", "quantityKwh mit 4 Nachkommastellen → 400 INVALID_QUANTITY", post(body(quantityKwh=Decimal("1.2345"))),
          [_err(400, "INVALID_QUANTITY")], on_unexpected_success=cleanup)
    c.run("VAL-03", "quantityKwh > 1.000.000 → 400 INVALID_QUANTITY", post(body(quantityKwh=Decimal("1000000.001"))),
          [_err(400, "INVALID_QUANTITY")], on_unexpected_success=cleanup)
    c.run("VAL-04", "quantityKwh als String → 400", post(body(quantityKwh="10")),
          [_err(400, "VALIDATION_FAILED", "INVALID_QUANTITY")], on_unexpected_success=cleanup)
    ob = body(type="OFFER")
    c.run("VAL-05", "Preis mit 6 Nachkommastellen → 400 INVALID_PRICE",
          post({**ob, "priceExpectationEurPerKwh": Decimal("0.123456")}), [_err(400, "INVALID_PRICE")],
          on_unexpected_success=cleanup)
    c.run("VAL-06", "Negativer Preis → 400 INVALID_PRICE", post({**ob, "priceExpectationEurPerKwh": Decimal("-0.01")}),
          [_err(400, "INVALID_PRICE")], on_unexpected_success=cleanup)
    c.run("VAL-07", "Preis > 100 → 400 INVALID_PRICE", post({**ob, "priceExpectationEurPerKwh": Decimal("100.5")}),
          [_err(400, "INVALID_PRICE")], on_unexpected_success=cleanup)
    db = body(type="DEMAND")
    c.run("VAL-08", "DEMAND mit Preisvorstellung → 400 VALIDATION_FAILED",
          post({**db, "priceExpectationEurPerKwh": Decimal("0.1")}), [_err(400, "VALIDATION_FAILED")],
          on_unexpected_success=cleanup)
    c.run("VAL-09", "Unbekanntes Feld → 400 VALIDATION_FAILED", post(body(foo="bar")), [_err(400, "VALIDATION_FAILED")],
          on_unexpected_success=cleanup)
    c.run("VAL-10", "Pflichtfeld type fehlt → 400 VALIDATION_FAILED", post(body(type=_DROP)),
          [_err(400, "VALIDATION_FAILED")], on_unexpected_success=cleanup)
    c.run("VAL-11", "Ungültiger type → 400 INVALID_POSITION_TYPE", post(body(type="SELL")),
          [_err(400, "INVALID_POSITION_TYPE")], on_unexpected_success=cleanup)
    c.run("VAL-12", "deliveryDate falsches Format → 400 INVALID_DELIVERY_DATE", post(body(deliveryDate="20.11.2026")),
          [_err(400, "INVALID_DELIVERY_DATE")], on_unexpected_success=cleanup)
    # Vertrag 0.3: Das Kalenderdatum allein ist kein Ablehnungsgrund (historische Marktläufe), es zählt nur der Markt
    c.run("VAL-13", "Historischer Liefertag ohne Markt → 404 MARKET_NOT_FOUND (kein Ablehnen wegen Datum)",
          post(body(deliveryDate="2020-01-01",
                    intervalStart=_DROP if res == "DAY" else format_utc(quarter_hours(resolve_delivery_date("2020-01-01"))[48]))),
          [_err(404, "MARKET_NOT_FOUND")], on_unexpected_success=cleanup)
    c.run("VAL-14", "Kaputtes JSON → 400 VALIDATION_FAILED",
          lambda: cl.call("POST", "/positions", raw_body='{"deliveryDate": "' + d + '", '), [_err(400, "VALIDATION_FAILED")])
    c.run("VAL-15", "Fremde, ungültige energyCommunityId → 400 INVALID_ID", post(body(energyCommunityId="xyz")),
          [_err(400, "INVALID_ID", "VALIDATION_FAILED")], on_unexpected_success=cleanup)

    if res == "DAY":
        c.run("INT-01", "intervalStart im Tagesmarkt → 400 INVALID_INTERVAL",
              post(body(intervalStart=format_utc(day_bounds_utc(resolve_delivery_date(d))[0] + timedelta(hours=11)))),
              [_err(400, "INVALID_INTERVAL")], on_unexpected_success=cleanup)
    else:
        start = day_bounds_utc(resolve_delivery_date(d))[0]
        c.run("INT-01", "intervalStart fehlt im 15-Min-Markt → 400 INVALID_INTERVAL", post(body(intervalStart=_DROP)),
              [_err(400, "INVALID_INTERVAL")], on_unexpected_success=cleanup)
        c.run("INT-02", "intervalStart keine volle Viertelstunde → 400 INVALID_INTERVAL",
              post(body(intervalStart=format_utc(start + timedelta(minutes=7)))), [_err(400, "INVALID_INTERVAL")],
              on_unexpected_success=cleanup)
        c.run("INT-03", "intervalStart außerhalb des Liefertags → 400 INVALID_INTERVAL",
              post(body(intervalStart=format_utc(start - timedelta(minutes=15)))), [_err(400, "INVALID_INTERVAL")],
              on_unexpected_success=cleanup)
        c.run("INT-04", "intervalStart mit Offset statt UTC → 400 INVALID_INTERVAL",
              post(body(intervalStart=format_utc(start + timedelta(hours=12)).replace("Z", "+00:00"))),
              [_err(400, "INVALID_INTERVAL", "VALIDATION_FAILED")], on_unexpected_success=cleanup)

    # --- Zustand
    c.run("STATE-04", "Zweite Position im selben Intervall → 409 POSITION_ALREADY_EXISTS", post(dict(base)),
          [_err(409, "POSITION_ALREADY_EXISTS")], on_unexpected_success=cleanup)
    c.run("NF-04", "Unbekannte Position → 404 POSITION_NOT_FOUND", lambda: cl.get_position(rnd),
          [_err(404, "POSITION_NOT_FOUND")])
    c.run("NF-05", "Ungültige Positions-ID → 400 INVALID_ID", lambda: cl.get_position("123"), [_err(400, "INVALID_ID")])
    c.run("POS-01", "Eigene Position abrufen → 200", lambda: cl.get_position(sp_a.id), [_err(200)],
          extra=lambda r: None if (r.body or {}).get("id") == sp_a.id else "falsche Position zurückgegeben")
    c.run("POS-02", "PUT mit unbekanntem Feld → 400 VALIDATION_FAILED",
          lambda: cl.update_position(sp_a.id, {"quantityKwh": sp_a.quantity, "type": "DEMAND"}),
          [_err(400, "VALIDATION_FAILED")])
    c.run("POS-03", "PUT ohne quantityKwh → 400 VALIDATION_FAILED", lambda: cl.update_position(sp_a.id, {}),
          [_err(400, "VALIDATION_FAILED")])
    cancelled = next((sp for sp in run.positions if sp.cancelled and sp.eg.ready), None)
    if cancelled:
        ccl = cancelled.eg.client
        c.run("STATE-05", "Stornierte Position ändern → 409 POSITION_NOT_MODIFIABLE",
              lambda: ccl.update_position(cancelled.id, {"quantityKwh": Decimal(1)}), [_err(409, "POSITION_NOT_MODIFIABLE")])
        c.run("STATE-06", "Stornierte Position erneut stornieren → 200 ohne Änderung (idempotent)",
              lambda: ccl.cancel_position(cancelled.id), [_err(200)],
              extra=lambda r: None if (r.body or {}).get("status") == "CANCELLED"
              else f"Status {(r.body or {}).get('status')} statt CANCELLED")
    else:
        c.skip("STATE-05", "Stornierte Position ändern", "keine stornierte Position (behaviour.cancelProbability = 0)")

    def own_only(r: ApiResponse) -> str | None:
        rows = r.body if isinstance(r.body, list) else []
        foreign = [x for x in rows if x.get("energyCommunityId") != eg.id]
        return f"{len(foreign)} fremde Positionen in der Liste" if foreign else None
    c.run("TEN-01", "EG-Liste enthält nur eigene Positionen", lambda: cl.list_positions(deliveryDate=d), [_err(200)],
          extra=own_only)

    _put_validation_checks(c, sim, run)
    _filter_checks_open(c, sim, run, sp_a)
    _market_list_checks(c, run, cl)
    _request_id_checks(c, cl)
    _idempotency_checks(c, sim, run, sp_a, sp_b)

    # --- Mandantentrennung (zweite EG nötig)
    if not sp_b:
        c.skip("TEN-02", "Mandantentrennung", "nur eine EG mit offener Position – mindestens zwei EGs konfigurieren")
        return c.results
    other = sp_b.eg
    c.run("TEN-02", "Fremde energyCommunityId beim Einreichen → 403", post(body(energyCommunityId=other.id)),
          [_err(403, "FORBIDDEN")], on_unexpected_success=_cleanup(sim, other.client))
    c.run("TEN-03", "Fremde Position abrufen → 403", lambda: cl.get_position(sp_b.id), [_err(403, "FORBIDDEN")])
    c.run("TEN-04", "Fremde Position ändern → 403",
          lambda: cl.update_position(sp_b.id, {"quantityKwh": sp_b.quantity,
                                               **({"priceExpectationEurPerKwh": sp_b.price_expectation}
                                                  if sp_b.price_expectation is not None else {})}),
          [_err(403, "FORBIDDEN")])

    def mark_cancelled(r: ApiResponse) -> None:
        sp_b.cancelled, sp_b.status = True, "CANCELLED"
    c.run("TEN-05", "Fremde Position stornieren → 403", lambda: cl.cancel_position(sp_b.id), [_err(403, "FORBIDDEN")],
          on_unexpected_success=mark_cancelled)
    c.run("TEN-06", "Positionsfilter auf fremde EG → 403", lambda: cl.list_positions(energyCommunityId=other.id),
          [_err(403, "FORBIDDEN")])
    c.run("TEN-07", "Fremde EG abrufen → 403", lambda: cl.get_community(other.id), [_err(403, "FORBIDDEN")])
    c.run("TEN-08", "Fremdes Profil ändern → 403", lambda: cl.update_community(other.id, {"description": "x"}),
          [_err(403, "FORBIDDEN")])
    c.run("TEN-09", "Zuteilungsfilter auf fremde EG → 403", lambda: cl.allocations(d, energyCommunityId=other.id),
          [_err(403, "FORBIDDEN")])
    c.run("TEN-10", "Trade-Filter auf fremde EG → 403", lambda: cl.trades(d, energyCommunityId=other.id),
          [_err(403, "FORBIDDEN")])
    return c.results


class _Drop:
    pass


_DROP = _Drop()


def _only(field: str, value, *, must_include: str | None = None) -> Callable[[ApiResponse], str | None]:
    """Prüfung für Filter: alle Zeilen passen zum Filterwert, optional muss eine bestimmte Position enthalten sein."""
    def check(r: ApiResponse) -> str | None:
        rows = r.body if isinstance(r.body, list) else []
        norm = normalize_interval if field == "intervalStart" else (lambda x: x)
        bad = [x for x in rows if norm(x.get(field)) != norm(value)]
        if bad:
            return f"{len(bad)} von {len(rows)} Einträgen passen nicht zum Filter {field}={value}"
        if must_include and not any(x.get("id") == must_include for x in rows):
            return f"Position {must_include} fehlt in der gefilterten Liste"
        return None
    return check


def _put_validation_checks(c: Checker, sim: "Simulator", run: "MarketRun") -> None:
    """Menge und Preis werden bei PUT genauso geprüft wie beim Einreichen."""
    live = [x for x in run.positions if x.id and not x.cancelled and x.status == "OPEN" and x.eg.ready]
    if not live:
        return

    def restore(sp: "SubmittedPosition") -> Callable[[ApiResponse], None]:
        def f(r: ApiResponse) -> None:
            body = {"quantityKwh": sp.quantity}
            if sp.price_expectation is not None:
                body["priceExpectationEurPerKwh"] = sp.price_expectation
            sp.eg.client.update_position(sp.id, body)
            sim.findings.warning("check", f"Ungültige Änderung an Position {sp.id} wurde angenommen und zurückgesetzt")
        return f

    a = live[0]
    c.run("POS-04", "PUT mit quantityKwh = 0 → 400 INVALID_QUANTITY",
          lambda: a.eg.client.update_position(a.id, {"quantityKwh": Decimal(0)}), [_err(400, "INVALID_QUANTITY")],
          on_unexpected_success=restore(a))
    c.run("POS-05", "PUT mit 4 Nachkommastellen → 400 INVALID_QUANTITY",
          lambda: a.eg.client.update_position(a.id, {"quantityKwh": Decimal("1.2345")}), [_err(400, "INVALID_QUANTITY")],
          on_unexpected_success=restore(a))
    offer = next((x for x in live if x.planned.type == "OFFER"), None)
    if offer:
        c.run("POS-06", "PUT mit Preis mit 6 Nachkommastellen → 400 INVALID_PRICE",
              lambda: offer.eg.client.update_position(offer.id, {"quantityKwh": offer.quantity,
                                                                 "priceExpectationEurPerKwh": Decimal("0.123456")}),
              [_err(400, "INVALID_PRICE")], on_unexpected_success=restore(offer))
        c.run("POS-07", "PUT mit negativem Preis → 400 INVALID_PRICE",
              lambda: offer.eg.client.update_position(offer.id, {"quantityKwh": offer.quantity,
                                                                 "priceExpectationEurPerKwh": Decimal("-0.01")}),
              [_err(400, "INVALID_PRICE")], on_unexpected_success=restore(offer))
    else:
        c.skip("POS-06", "PUT mit ungültigem Preis", "kein offenes Angebot vorhanden")
    priced = next((x for x in live if x.planned.type == "OFFER" and x.price_expectation is not None), None)
    if priced:
        def price_removed(r: ApiResponse) -> str | None:
            pe = (r.body or {}).get("priceExpectationEurPerKwh") if isinstance(r.body, dict) else None
            back = {"quantityKwh": priced.quantity, "priceExpectationEurPerKwh": priced.price_expectation}
            priced.eg.client.update_position(priced.id, back)  # Ausgangszustand wiederherstellen
            return None if pe is None else f"Preisvorstellung {pe} ist noch gesetzt (PUT ersetzt, fehlendes Feld = keine)"
        c.run("POS-09", "PUT ohne Preisvorstellung entfernt sie (PUT ersetzt Menge und Preis)",
              lambda: priced.eg.client.update_position(priced.id, {"quantityKwh": priced.quantity}), [_err(200)],
              extra=price_removed)
    else:
        c.skip("POS-09", "PUT ohne Preisvorstellung", "kein offenes Angebot mit Preisvorstellung")
    demand = next((x for x in live if x.planned.type == "DEMAND"), None)
    if demand:
        # Vertrag 0.2 (korrigiert 19.11.) und 0.3: 400 VALIDATION_FAILED und die Position bleibt unverändert.
        # Andere Menge mitschicken, damit eine trotz 400 gespeicherte Teiländerung auffällt.
        new_qty = demand.quantity + 1 if demand.quantity + 1 <= Decimal(1_000_000) else demand.quantity - Decimal("0.5")

        def demand_unchanged(_r: ApiResponse) -> str | None:
            g = demand.eg.client.get_position(demand.id)
            if g.status != 200 or not isinstance(g.body, dict):
                return f"Position danach nicht abrufbar ({g.status})"
            problems = []
            try:
                q_now = Decimal(str(g.body.get("quantityKwh")))
            except ArithmeticError:
                return f"quantityKwh fehlt oder ist keine Zahl ({g.body.get('quantityKwh')!r})"
            if q_now != demand.quantity:
                problems.append(f"quantityKwh {demand.quantity} → {g.body.get('quantityKwh')}")
            if g.body.get("priceExpectationEurPerKwh") is not None:
                problems.append(f"priceExpectationEurPerKwh gesetzt ({g.body.get('priceExpectationEurPerKwh')})")
            if problems:
                restore(demand)(g)
                return "400 gemeldet, aber Position geändert: " + "; ".join(problems)
            return None
        c.run("POS-08", "PUT eines Bedarfs mit Preisvorstellung → 400 VALIDATION_FAILED, Position unverändert",
              lambda: demand.eg.client.update_position(demand.id, {"quantityKwh": new_qty,
                                                                   "priceExpectationEurPerKwh": Decimal("0.1")}),
              [_err(400, "VALIDATION_FAILED")], on_unexpected_success=restore(demand), extra=demand_unchanged)
    else:
        c.skip("POS-08", "PUT eines Bedarfs mit Preisvorstellung", "kein offener Bedarf vorhanden")


def _filter_checks_open(c: Checker, sim: "Simulator", run: "MarketRun", sp_a: "SubmittedPosition") -> None:
    """Query-Filter von GET /positions und ungültige Filterwerte aller Listen (400 kommt laut Prüfreihenfolge vor 409)."""
    d = run.delivery_date
    eg = sp_a.eg
    cl = eg.client
    live = [x for x in run.positions if x.id and not x.cancelled and x.status == "OPEN" and x.eg.ready]
    offer = next((x for x in live if x.planned.type == "OFFER"), None)
    if offer:
        c.run("FIL-01", "Positionen mit type=OFFER → nur Angebote",
              lambda: offer.eg.client.list_positions(deliveryDate=d, type="OFFER"), [_err(200)],
              extra=_only("type", "OFFER", must_include=offer.id))
    # die Gegenprobe trennt: Eine EG hat meist nur Angebote oder nur Bedarfe, ein ignorierter Filter fiele sonst nicht auf
    opposite = "DEMAND" if sp_a.planned.type == "OFFER" else "OFFER"
    c.run("FIL-01b", f"Positionen mit type={opposite} → keine Positionen des anderen Typs",
          lambda: cl.list_positions(deliveryDate=d, type=opposite), [_err(200)], extra=_only("type", opposite))
    c.run("FIL-02", "Positionen mit status=OPEN → nur offene", lambda: cl.list_positions(deliveryDate=d, status="OPEN"),
          [_err(200)], extra=_only("status", "OPEN", must_include=sp_a.id))
    c.run("FIL-02b", "Positionen mit status=PROCESSED vor Marktschluss → keine offenen",
          lambda: cl.list_positions(deliveryDate=d, status="PROCESSED"), [_err(200)], extra=_only("status", "PROCESSED"))
    c.run("FIL-03", "Positionen mit eigener energyCommunityId → nur eigene",
          lambda: cl.list_positions(deliveryDate=d, energyCommunityId=eg.id), [_err(200)],
          extra=_only("energyCommunityId", eg.id, must_include=sp_a.id))
    if (run.market or {}).get("resolution") == "QUARTER_HOUR" and sp_a.planned.interval_start:
        iv = sp_a.planned.interval_start
        c.run("FIL-04", "Positionen mit intervalStart → nur dieses Intervall",
              lambda: cl.list_positions(deliveryDate=d, intervalStart=iv), [_err(200)],
              extra=_only("intervalStart", iv, must_include=sp_a.id))
    c.run("FIL-05", "Positionen mit ungültigem type → 400 INVALID_POSITION_TYPE", lambda: cl.list_positions(type="SELL"),
          [_err(400, "INVALID_POSITION_TYPE")])
    c.run("FIL-06", "Positionen mit ungültiger energyCommunityId → 400 INVALID_ID",
          lambda: cl.list_positions(energyCommunityId="xyz"), [_err(400, "INVALID_ID")])
    c.run("FIL-07", "Positionen mit ungültigem deliveryDate → 400 INVALID_DELIVERY_DATE",
          lambda: cl.list_positions(deliveryDate="2026/11/20"), [_err(400, "INVALID_DELIVERY_DATE")])
    c.run("FIL-08", "Positionen mit ungültigem intervalStart → 400 INVALID_INTERVAL",
          lambda: cl.list_positions(deliveryDate=d, intervalStart="mittags"), [_err(400, "INVALID_INTERVAL")])
    c.run("FIL-09", "Positionen mit ungültigem status → 400 VALIDATION_FAILED", lambda: cl.list_positions(status="FOO"),
          [_err(400, "VALIDATION_FAILED")])
    c.run("FIL-10", "Zuteilungen mit ungültiger energyCommunityId → 400 INVALID_ID (vor 409)",
          lambda: cl.allocations(d, energyCommunityId="xyz"), [_err(400, "INVALID_ID")])
    c.run("FIL-11", "Zuteilungen mit ungültigem intervalStart → 400 INVALID_INTERVAL (vor 409)",
          lambda: cl.allocations(d, intervalStart="mittags"), [_err(400, "INVALID_INTERVAL")])
    c.run("FIL-12", "Trades mit ungültiger direction → 400 VALIDATION_FAILED (vor 409)",
          lambda: cl.trades(d, direction="SIDEWAYS"), [_err(400, "VALIDATION_FAILED")])
    c.run("FIL-13", "Trades mit ungültiger positionId → 400 INVALID_ID (vor 409)",
          lambda: cl.trades(d, positionId="xyz"), [_err(400, "INVALID_ID")])


def _filter_checks_settled(c: Checker, run: "MarketRun", sp: "SubmittedPosition") -> None:
    d = run.delivery_date
    cl = sp.eg.client
    c.run("FIL-20", "Trades mit direction=PLATFORM_BUYS → nur Ankäufe", lambda: cl.trades(d, direction="PLATFORM_BUYS"),
          [_err(200)], extra=_only("direction", "PLATFORM_BUYS"))
    c.run("FIL-21", "Trades mit direction=PLATFORM_SELLS → nur Verkäufe", lambda: cl.trades(d, direction="PLATFORM_SELLS"),
          [_err(200)], extra=_only("direction", "PLATFORM_SELLS"))
    c.run("FIL-22", "Trades mit positionId → nur diese Position", lambda: cl.trades(d, positionId=sp.id), [_err(200)],
          extra=_only("positionId", sp.id))
    if (run.market or {}).get("resolution") == "QUARTER_HOUR" and sp.planned.interval_start:
        iv = sp.planned.interval_start
        c.run("FIL-23", "Zuteilungen mit intervalStart → nur dieses Intervall", lambda: cl.allocations(d, intervalStart=iv),
              [_err(200)], extra=_only("intervalStart", iv))
        c.run("FIL-24", "Trades mit intervalStart → nur dieses Intervall", lambda: cl.trades(d, intervalStart=iv),
              [_err(200)], extra=_only("intervalStart", iv))


def _free_slot(sim: "Simulator", run: "MarketRun", live: list["SubmittedPosition"]):
    """Ein Slot (EG + Intervall), den keine aktive Position belegt: (EG, Body) oder (None, None)."""
    d = run.delivery_date
    taken = {(p.eg.id, p.planned.interval_start) for p in live}
    if (run.market or {}).get("resolution") == "QUARTER_HOUR":
        sp = live[0]
        for t in quarter_hours(resolve_delivery_date(d)):
            if (sp.eg.id, format_utc(t)) not in taken:
                return sp.eg, {"deliveryDate": d, "intervalStart": format_utc(t), "type": "DEMAND", "quantityKwh": Decimal(1)}
        return None, None
    eg = next((e for e in sim.egs if e.ready and (e.id, None) not in taken), None)
    if eg:
        return eg, {"deliveryDate": d, "type": "DEMAND", "quantityKwh": Decimal(1)}
    return None, None


def run_late_market_checks(sim: "Simulator", run: "MarketRun") -> list[dict]:
    """closesAt ist erreicht, der Betreiber hat den Markt aber noch nicht geschlossen (Status OPEN): Der Server muss
    neue, geänderte und stornierte Positionen trotzdem mit 409 MARKET_CLOSED ablehnen."""
    c = Checker(sim, f"late:{run.delivery_date}")
    live = [sp for sp in run.positions if sp.id and not sp.cancelled and sp.eg.ready]
    if not live:
        c.skip("CLOSES-*", "Checks nach closesAt bei Status OPEN", "keine eigenen Positionen")
        return c.results
    sp = live[0]
    cl = sp.eg.client
    submitter, free = _free_slot(sim, run, live)
    if free:
        c.run("CLOSES-01", "Neue Position nach closesAt (Markt noch OPEN) → 409 MARKET_CLOSED",
              lambda: submitter.client.submit_position(free), [_err(409, "MARKET_CLOSED")],
              on_unexpected_success=_cleanup(sim, submitter.client))
    else:
        c.skip("CLOSES-01", "Neue Position nach closesAt", "kein freier Slot")
    upd = {"quantityKwh": sp.quantity}
    if sp.price_expectation is not None:
        upd["priceExpectationEurPerKwh"] = sp.price_expectation
    c.run("CLOSES-02", "Position ändern nach closesAt (Markt noch OPEN) → 409 MARKET_CLOSED",
          lambda: cl.update_position(sp.id, upd), [_err(409, "MARKET_CLOSED")])

    def mark_cancelled(r: ApiResponse) -> None:
        sp.cancelled, sp.status = True, "CANCELLED"
    c.run("CLOSES-03", "Position stornieren nach closesAt (Markt noch OPEN) → 409 MARKET_CLOSED",
          lambda: cl.cancel_position(sp.id), [_err(409, "MARKET_CLOSED")], on_unexpected_success=mark_cancelled)
    return c.results


def _inactive_checks(c: Checker, ids: str, label: str, client: ApiClient) -> None:
    """Zugang einer nicht aktiven EG (PENDING/SUSPENDED): 403 vor 400 und 404, auch für Positions-Endpoints."""
    rnd = str(uuid.uuid4())
    nae = [_err(403, "ENERGY_COMMUNITY_NOT_ACTIVE")]
    c.run(f"{ids}b", f"{label}: Position einreichen → 403 ENERGY_COMMUNITY_NOT_ACTIVE",
          lambda: client.submit_position({"deliveryDate": "2099-12-31", "type": "DEMAND", "quantityKwh": Decimal(1)}), nae)
    c.run(f"{ids}c", f"{label}: Position ändern → 403 ENERGY_COMMUNITY_NOT_ACTIVE",
          lambda: client.update_position(rnd, {"quantityKwh": Decimal(1)}), nae)
    c.run(f"{ids}d", f"{label}: Position stornieren → 403 ENERGY_COMMUNITY_NOT_ACTIVE", lambda: client.cancel_position(rnd), nae)
    c.run(f"{ids}e", f"{label}: Positionen auflisten → 403 ENERGY_COMMUNITY_NOT_ACTIVE", lambda: client.list_positions(), nae)
    c.run(f"{ids}f", f"{label}: Märkte auflisten → 403 ENERGY_COMMUNITY_NOT_ACTIVE", lambda: client.list_markets(), nae)


# =====================================================================================================================
def run_closed_market_checks(sim: "Simulator", run: "MarketRun") -> list[dict]:
    c = Checker(sim, f"closed:{run.delivery_date}")
    d = run.delivery_date
    live = [sp for sp in run.positions if sp.id and not sp.cancelled]
    if not live:
        c.skip("CLOSED-*", "Checks nach Marktschluss", "keine eigenen Positionen")
        return c.results
    sp = live[0]
    cl = sp.eg.client

    # neue Position in einem freien Slot, damit 409 eindeutig MARKET_CLOSED sein muss
    submitter, free_body = _free_slot(sim, run, live)
    if free_body and submitter:
        c.run("CLOSED-01", "Neue Position nach Marktschluss → 409 MARKET_CLOSED",
              lambda: submitter.client.submit_position(free_body), [_err(409, "MARKET_CLOSED")],
              on_unexpected_success=_cleanup(sim, submitter.client))
    else:
        c.run("CLOSED-01", "Neue Position nach Marktschluss → 409 MARKET_CLOSED",
              lambda: cl.submit_position(sp.planned.body(d)), [_err(409, "MARKET_CLOSED", "POSITION_ALREADY_EXISTS")])

    upd = {"quantityKwh": sp.quantity}
    if sp.price_expectation is not None:
        upd["priceExpectationEurPerKwh"] = sp.price_expectation
    c.run("CLOSED-02", "Position nach Marktschluss ändern → 409", lambda: cl.update_position(sp.id, upd),
          [_err(409, "MARKET_CLOSED", "POSITION_NOT_MODIFIABLE")])
    c.run("CLOSED-03", "Position nach Marktschluss stornieren → 409", lambda: cl.cancel_position(sp.id),
          [_err(409, "MARKET_CLOSED", "POSITION_NOT_MODIFIABLE")])
    c.run("CLOSED-04", "EG schließt Markt → 403", lambda: cl.close_market(d), [_err(403, "FORBIDDEN")])
    run_replay_after_close_check(c, live, d)
    if run.final_status == "SETTLED":
        c.run("CLOSED-05", "Position nach Settlement ist PROCESSED", lambda: cl.get_position(sp.id), [_err(200)],
              extra=lambda r: None if (r.body or {}).get("status") == "PROCESSED"
              else f"Status {(r.body or {}).get('status')}")
        c.run("CLOSED-07", "EG ruft eigene Zuteilungen ab → 200", lambda: cl.allocations(d), [_err(200)])
        _filter_checks_settled(c, run, sp)
    return c.results


# =====================================================================================================================
def run_account_checks(sim: "Simulator") -> list[dict]:
    """Login/Logout mit den Zugangsdaten der EGs und optional Registrierungs-Checks."""
    c = Checker(sim, "konto")
    anon = sim.base
    rnd = uuid.uuid4().hex[:8]
    c.run("AUTH-04", "Login unbekannte E-Mail → 401 INVALID_CREDENTIALS",
          lambda: anon.login(f"unbekannt-{rnd}@egsim.example", "falsch-123456"), [_err(401, "INVALID_CREDENTIALS")])
    c.run("AUTH-05", "Login ohne Passwort → 400 VALIDATION_FAILED",
          lambda: anon.call("POST", "/auth/login", auth=False, json_body={"email": "a@egsim.example"}),
          [_err(400, "VALIDATION_FAILED")])

    eg = next((e for e in sim.egs if e.ready and e.cfg.email and e.cfg.password), None)
    if eg:
        email, pw = eg.cfg.email, eg.cfg.password
        c.run("AUTH-06", "Login mit falschem Passwort → 401 INVALID_CREDENTIALS", lambda: anon.login(email, pw + "x"),
              [_err(401, "INVALID_CREDENTIALS")])

        def login_ok(r: ApiResponse) -> str | None:
            b = r.body if isinstance(r.body, dict) else {}
            if b.get("tokenType") != "Bearer" or not b.get("token"):
                return "token/tokenType fehlen"
            if b.get("role") != "ENERGY_COMMUNITY" or (b.get("energyCommunity") or {}).get("id") != eg.id:
                return "role oder energyCommunity falsch"
            if not isinstance(b.get("expiresIn"), int) or b["expiresIn"] < 1:
                return "expiresIn fehlt"
            return None
        lg = c.run("AUTH-07", "Login → 200 mit Bearer-Token", lambda: anon.login(email, pw), [_err(200)], extra=login_ok)
        if lg and lg.status == 200 and isinstance(lg.body, dict) and lg.body.get("token"):
            tk = anon.with_auth(token=lg.body["token"], label=eg.key)
            c.run("AUTH-08", "GET /me mit Token → 200", tk.me, [_err(200)])
            c.run("AUTH-09", "Logout → 204", tk.logout, [_err(204)])
            c.run("AUTH-10", "Token nach Logout → 401", tk.me, [_err(401, "UNAUTHORIZED")])
    else:
        c.skip("AUTH-06", "Login-Checks", "keine EG mit E-Mail und Passwort hinterlegt")

    if sim.cfg.rate_limit_checks:
        _rate_limit_check(c, sim)
    else:
        c.skip("RATE-01", "Rate-Limit beim Login", "abgeschaltet (sperrt ggf. Logins von dieser Adresse vorübergehend)")

    if not sim.cfg.registration_checks:
        c.skip("REG-*", "Registrierungs-Checks", "abgeschaltet (legen eine PENDING-Test-EG auf der Plattform an)")
        return c.results
    email = f"egsim-regtest-{sim.run_id}@egsim.example"
    code = f"egsim-regtest-{rnd}"
    c.run("REG-01", "Registrierung mit ungültiger E-Mail → 400 VALIDATION_FAILED",
          lambda: anon.register("keine-email", "egsim-x-" + rnd, "EGSim Test"), [_err(400, "VALIDATION_FAILED")])
    c.run("REG-02", "Registrierung mit Feld status → 400 VALIDATION_FAILED",
          lambda: anon.call("POST", "/energy-communities", auth=False,
                            json_body={"email": "x-" + email, "communityCode": "egsim-x2-" + rnd, "name": "EGSim",
                                       "status": "ACTIVE"}),
          [_err(400, "VALIDATION_FAILED")])
    c.run("REG-03", "Registrierung mit leerem Namen → 400 VALIDATION_FAILED",
          lambda: anon.register("y-" + email, "egsim-y-" + rnd, "   "), [_err(400, "VALIDATION_FAILED")])
    c.run("REG-04", "Name mit 121 Zeichen → 400 VALIDATION_FAILED",
          lambda: anon.register("z-" + email, "egsim-z-" + rnd, "N" * 121), [_err(400, "VALIDATION_FAILED")])
    c.run("REG-11", "Registrierung ohne communityCode → 400 VALIDATION_FAILED",
          lambda: anon.register("c1-" + email, None, "EGSim Test"), [_err(400, "VALIDATION_FAILED")])
    for cid, bad, why in (("REG-12", "EGSim_Test", "Großbuchstaben/Unterstrich"), ("REG-13", "ab", "zu kurz"),
                          ("REG-14", "egsim--doppelt", "doppelter Bindestrich"), ("REG-14b", "-egsim", "Bindestrich am Anfang"),
                          ("REG-14c", "e" * 41, "41 Zeichen")):
        c.run(cid, f"communityCode {why} → 400 VALIDATION_FAILED",
              lambda bad=bad: anon.register(f"{cid.lower()}-{email}", bad, "EGSim Test"), [_err(400, "VALIDATION_FAILED")],
              on_unexpected_success=lambda r: sim.findings.warning("check", f"Registrierung mit ungültigem communityCode "
                                                                           f"wurde angenommen ({(r.body or {}).get('id')})"))

    def pending(r: ApiResponse) -> str | None:
        b = r.body if isinstance(r.body, dict) else {}
        if b.get("status") != "PENDING":
            return f"Status {b.get('status')} statt PENDING"
        if not b.get("id") or "apiKey" in b or "password" in b:
            return "id fehlt oder Antwort enthält Zugangsdaten"
        if b.get("communityCode") != code:
            return f"communityCode {b.get('communityCode')!r} statt {code!r}"
        return None
    reg_key = str(uuid.uuid4())
    reg_body = (email, code, f"EGSim Registrierungstest {rnd}", "Von EGSim angelegt, kann gelöscht werden")
    r = c.run("REG-05", "Registrierung → 201 PENDING mit communityCode",
              lambda: anon.register(*reg_body, idempotency_key=reg_key), [_err(201)], extra=pending)
    if r and r.status == 201:
        first = r.body
        c.run("REG-09", "Registrierung mit gleichem Idempotency-Key wiederholt → dieselbe 201-Antwort",
              lambda: anon.register(*reg_body, idempotency_key=reg_key), [_err(201)],
              extra=lambda x: None if x.body == first else f"andere Antwort (id {(x.body or {}).get('id')} statt {first.get('id')})")
        c.run("REG-10", "Gleicher Idempotency-Key, anderer Inhalt → 409 IDEMPOTENCY_KEY_REUSED",
              lambda: anon.register("other-" + email, code + "-b", "Anders", idempotency_key=reg_key),
              [_err(409, "IDEMPOTENCY_KEY_REUSED")])
        c.run("REG-06", "Gleiche E-Mail erneut → 409 EMAIL_ALREADY_REGISTERED",
              lambda: anon.register(email, code + "-c", "Doppelt"), [_err(409, "EMAIL_ALREADY_REGISTERED")])
        c.run("REG-07", "Gleiche E-Mail in Großbuchstaben → 409 EMAIL_ALREADY_REGISTERED",
              lambda: anon.register(email.upper(), code + "-d", "Doppelt"), [_err(409, "EMAIL_ALREADY_REGISTERED")],
              note_on={(201, None): "E-Mail-Vergleich ist case-sensitiv"})
        c.run("REG-15", "Gleicher communityCode, andere E-Mail → 409 COMMUNITY_CODE_ALREADY_REGISTERED",
              lambda: anon.register("code-" + email, code, "Doppelt"), [_err(409, "COMMUNITY_CODE_ALREADY_REGISTERED")])
        c.run("REG-08", "Login einer PENDING-EG ohne Passwort-Vergabe → 401/403",
              lambda: anon.login(email, "egal-12345678"),
              [_err(401, "INVALID_CREDENTIALS"), _err(403, "ENERGY_COMMUNITY_NOT_ACTIVE")])
        sim.findings.info("check", f"Registrierungstest hat die PENDING-EG {email} ({code}) angelegt – Admin kann sie löschen")
    return c.results


def _rate_limit_check(c: Checker, sim: "Simulator") -> None:
    """Login mit falschem Passwort wiederholen, bis die Plattform 429 RATE_LIMITED mit Retry-After liefert."""
    anon = sim.base.with_auth()
    anon.retry_on_429 = False
    email = f"egsim-ratelimit-{uuid.uuid4().hex[:8]}@egsim.example"
    hits: list[ApiResponse] = []

    def hammer() -> ApiResponse:
        last = None
        for _ in range(40):
            last = anon.login(email, "falsches-passwort-123")
            if last.status == 429:
                break
        hits.append(last)
        return last

    def has_retry_after(r: ApiResponse) -> str | None:
        ra = r.headers.get("retry-after", "")
        return None if ra.isdigit() and int(ra) >= 1 else f"Retry-After fehlt oder ungültig ({ra!r})"
    c.run("RATE-01", "Wiederholte Fehlversuche beim Login → 429 RATE_LIMITED mit Retry-After (nach ≤ 40 Versuchen)",
          hammer, [_err(429, "RATE_LIMITED")], extra=has_retry_after)


# ---------------------------------------------------------------------------------------------------------------------
def _market_list_checks(c: Checker, run: "MarketRun", cl: ApiClient) -> None:
    """GET /markets (ab Vertrag 0.3): alle Rollen sehen dieselben Märkte, sortiert nach deliveryDate, Filter status."""
    d = run.delivery_date

    def sorted_and_contains(r: ApiResponse) -> str | None:
        rows = r.body if isinstance(r.body, list) else []
        days = [x.get("deliveryDate") for x in rows if isinstance(x, dict)]
        if days != sorted(days):
            return "nicht nach deliveryDate sortiert"
        if d not in days:
            return f"Markt {d} fehlt in der Liste"
        return None
    c.run("MKT-01", "Märkte auflisten → 200, sortiert, enthält den Markt", lambda: cl.list_markets(), [_err(200)],
          extra=sorted_and_contains)

    def only_open(r: ApiResponse) -> str | None:
        rows = r.body if isinstance(r.body, list) else []
        bad = [x.get("deliveryDate") for x in rows if x.get("status") != "OPEN"]
        if bad:
            return f"{len(bad)} Märkte mit anderem Status in der Liste"
        return None if any(x.get("deliveryDate") == d for x in rows) else f"offener Markt {d} fehlt"
    c.run("MKT-02", "Märkte mit status=OPEN → nur offene, enthält den Markt", lambda: cl.list_markets("OPEN"),
          [_err(200)], extra=only_open)
    c.run("MKT-03", "Märkte mit ungültigem status → 400 VALIDATION_FAILED", lambda: cl.list_markets("OFFEN"),
          [_err(400, "VALIDATION_FAILED")])


def _request_id_checks(c: Checker, cl: ApiClient) -> None:
    """X-Request-Id wird übernommen bzw. erzeugt und steht im Fehler-Body als requestId."""
    rnd = str(uuid.uuid4())
    mine = f"egsim-reqcheck-{rnd[:8]}"

    def echoed(r: ApiResponse) -> str | None:
        if r.headers.get("x-request-id") != mine:
            return f"Header X-Request-Id {r.headers.get('x-request-id')!r} statt {mine!r}"
        if (r.body or {}).get("requestId") != mine:
            return f"requestId im Body {(r.body or {}).get('requestId')!r} statt {mine!r}"
        return None
    c.run("REQ-01", "Mitgeschickte X-Request-Id steht in Header und Fehler-Body",
          lambda: cl.call("GET", "/positions/{id}", path_params={"id": rnd}, request_id=mine),
          [_err(404, "POSITION_NOT_FOUND")], extra=echoed)

    def generated(r: ApiResponse) -> str | None:
        rid = r.headers.get("x-request-id")
        if not rid:
            return "Server erzeugt keine X-Request-Id"
        return None if (r.body or {}).get("requestId") == rid else "requestId im Body ungleich Header"
    c.run("REQ-02", "Ohne X-Request-Id erzeugt der Server eine und nennt sie im Fehler-Body",
          lambda: cl.call("GET", "/positions/{id}", path_params={"id": rnd}, headers={"X-Request-Id": None}),
          [_err(404, "POSITION_NOT_FOUND")], extra=generated)


def _idempotency_checks(c: Checker, sim: "Simulator", run: "MarketRun", sp_a: "SubmittedPosition",
                        sp_b: "SubmittedPosition | None") -> None:
    """Idempotency-Key bei POST /positions (Vertrag 0.3, präzisiert in 0.3.1)."""
    import json as _json
    d = run.delivery_date
    cl = sp_a.eg.client
    base = sp_a.planned.body(d)

    def count_slot() -> int:
        r = cl.list_positions(deliveryDate=d, intervalStart=sp_a.planned.interval_start)
        rows = r.body if isinstance(r.body, list) else []
        return sum(1 for x in rows if x.get("status") != "CANCELLED"
                   and normalize_interval(x.get("intervalStart")) == sp_a.planned.interval_start)

    # Header fehlt / ist ungültig: 400 (Idempotenz wird vor Body-Prüfung und vor 409 geprüft)
    c.run("IDEM-01", "POST /positions ohne Idempotency-Key → 400 VALIDATION_FAILED",
          lambda: cl.call("POST", "/positions", json_body=base), [_err(400, "VALIDATION_FAILED")],
          on_unexpected_success=_cleanup(sim, cl))
    c.run("IDEM-02", "Idempotency-Key keine UUID → 400 VALIDATION_FAILED",
          lambda: cl.call("POST", "/positions", json_body=base, idempotency_key="nicht-eine-uuid"),
          [_err(400, "VALIDATION_FAILED")], on_unexpected_success=_cleanup(sim, cl))
    c.run("IDEM-09", "Nach 401 mit einem Key ist der Key nicht verbraucht (401 wird nicht gespeichert)",
          lambda: _after_401(sim, cl, base), [_err(409, "POSITION_ALREADY_EXISTS")])

    if not sp_a.idempotency_key or not sp_a.first_response:
        c.skip("IDEM-03", "Wiederholung mit gleichem Key", "Position wurde nicht per POST in diesem Lauf angelegt")
        return
    key, first = sp_a.idempotency_key, sp_a.first_response

    def same_answer(r: ApiResponse) -> str | None:
        if r.body != first:
            diff = [k for k in set(first) | set(r.body or {}) if (r.body or {}).get(k) != first.get(k)]
            return f"Antwort weicht von der ersten ab ({', '.join(sorted(diff)[:5])})"
        n = count_slot()
        return None if n == 1 else f"{n} Positionen im Slot – Wiederholung hat eine zweite angelegt"
    c.run("IDEM-03", "Wiederholung mit gleichem Key und Inhalt → dieselbe Antwort (201, gleicher Body), keine zweite Position",
          lambda: cl.submit_position(base, idempotency_key=key), [_err(201)], extra=same_answer)

    # 0.3.1: gleicher Inhalt = gleiches JSON nach dem Parsen (Feldreihenfolge, Leerzeichen, 120 vs 120.0 egal)
    def respelled() -> str:
        items = list(_json.loads(_json.dumps({k: str(v) if isinstance(v, Decimal) else v for k, v in base.items()})).items())
        parts = []
        for k, v in reversed(items):
            if k in ("quantityKwh", "priceExpectationEurPerKwh"):
                num = Decimal(v)
                places = max(1, -num.as_tuple().exponent + 1)  # eine Nachkommastelle mehr: 120 → 120.0, 0.12 → 0.120
                parts.append(f'  "{k}" :  {num:.{places}f}')
            else:
                parts.append(f'  "{k}" :  {_json.dumps(v)}')
        return "{\n" + ",\n".join(parts) + "\n}"
    c.run("IDEM-04", "Wiederholung mit anderer Schreibweise (Feldreihenfolge, 120.0 statt 120) → dieselbe Antwort",
          lambda: cl.call("POST", "/positions", raw_body=respelled(), idempotency_key=key), [_err(201)],
          extra=same_answer)

    changed = {**base, "quantityKwh": (sp_a.quantity or Decimal(1)) + Decimal(1)}

    def unchanged(r: ApiResponse) -> str | None:
        g = cl.get_position(sp_a.id)
        q = (g.body or {}).get("quantityKwh") if isinstance(g.body, dict) else None
        return None if q is not None and Decimal(str(q)) == sp_a.quantity else f"Position geändert (quantityKwh {q})"
    c.run("IDEM-05", "Gleicher Key, anderer Inhalt → 409 IDEMPOTENCY_KEY_REUSED, nichts gespeichert",
          lambda: cl.submit_position(changed, idempotency_key=key), [_err(409, "IDEMPOTENCY_KEY_REUSED")],
          on_unexpected_success=_cleanup(sim, cl), extra=unchanged)

    if sp_b:
        ocl = sp_b.eg.client

        def not_shared(r: ApiResponse) -> str | None:
            if isinstance(r.body, dict) and r.body.get("id") == first.get("id"):
                return "liefert die gespeicherte Antwort einer anderen EG (Key gilt pro Identität)"
            if r.status == 201:
                ocl.cancel_position(r.body.get("id"))
            return None
        c.run("IDEM-06", "Anderer Absender mit demselben Key → wird normal verarbeitet (Key gilt pro Identität)",
              lambda: ocl.submit_position(base, idempotency_key=key),
              [_err(201), _err(409, "POSITION_ALREADY_EXISTS")], extra=not_shared)

    eg_free, free = _free_slot(sim, run, [x for x in run.positions if x.id and not x.cancelled and x.eg.ready])
    if not free:
        c.skip("IDEM-08", "Gleichzeitige Wiederholungen", "kein freier Slot (Intervall ohne Position)")
        return
    pkey = str(uuid.uuid4())
    answers: list[ApiResponse] = []

    def parallel() -> ApiResponse:
        with ThreadPoolExecutor(4) as ex:
            answers.extend(ex.map(lambda _: eg_free.client.submit_position(free, idempotency_key=pkey), range(4)))
        return answers[0]

    def one_position(r: ApiResponse) -> str | None:
        ids = {(a.body or {}).get("id") for a in answers if a.status == 201 and isinstance(a.body, dict)}
        statuses = sorted({a.status for a in answers})
        lst = eg_free.client.list_positions(deliveryDate=d, intervalStart=free.get("intervalStart"))
        rows = [x for x in (lst.body or []) if isinstance(x, dict) and x.get("status") != "CANCELLED"
                and normalize_interval(x.get("intervalStart")) == free.get("intervalStart")]
        for x in rows:
            eg_free.client.cancel_position(x["id"])
        if statuses != [201]:
            return f"Antworten {statuses} statt viermal 201"
        if len(ids) != 1:
            return f"{len(ids)} verschiedene Positions-IDs in den Antworten"
        return None if len(rows) == 1 else f"{len(rows)} Positionen angelegt (erwartet genau eine)"
    c.run("IDEM-08", "4 gleichzeitige Requests mit gleichem Key → genau eine Position, gleiche Antworten",
          parallel, [_err(201)], extra=one_position)


def _after_401(sim: "Simulator", cl: ApiClient, base: dict) -> ApiResponse:
    key = str(uuid.uuid4())
    sim.base.with_auth(api_key="egsim-invalid-" + key).submit_position(base, idempotency_key=key)
    return cl.submit_position(base, idempotency_key=key)


def run_replay_after_close_check(c: Checker, live: list["SubmittedPosition"], d: str) -> None:
    """0.3.1: Eine Wiederholung mit gleichem Key und Inhalt bekommt die gespeicherte Antwort, auch nach Marktschluss."""
    sp = next((x for x in live if x.idempotency_key and x.first_response and x.eg.ready), None)
    if not sp:
        c.skip("IDEM-07", "Wiederholung nach Marktschluss", "keine per POST eingereichte Position")
        return
    c.run("IDEM-07", "Wiederholung nach Marktschluss mit gleichem Key → gespeicherte 201 statt 409 MARKET_CLOSED",
          lambda: sp.eg.client.submit_position(sp.planned.body(d), idempotency_key=sp.idempotency_key), [_err(201)],
          extra=lambda r: None if (r.body or {}).get("id") == sp.first_response.get("id")
          else f"andere Position {(r.body or {}).get('id')}")


# =====================================================================================================================
def run_admin_checks(sim: "Simulator") -> list[dict]:
    """Nur mit Admin-Key der Test-Plattform: Lebenszyklus einer Wegwerf-EG, Statusübergänge, Zugangsdaten, Löschen."""
    c = Checker(sim, "admin")
    adm, anon = sim.admin, sim.base
    rnd = uuid.uuid4().hex[:8]
    email = f"egsim-admintest-{rnd}@egsim.example"

    c.run("ADM-01", "Admin reicht Position ein → 403",
          lambda: adm.submit_position({"deliveryDate": "2099-12-31", "type": "DEMAND", "quantityKwh": 1}),
          [_err(403, "FORBIDDEN")], on_unexpected_success=_cleanup(sim, adm))
    c.run("ADM-02", "Markt schließen ohne Markt → 404 MARKET_NOT_FOUND", lambda: adm.close_market("2099-12-31"),
          [_err(404, "MARKET_NOT_FOUND")])
    c.run("ADM-03", "Tagesergebnis ohne Markt → 404 MARKET_NOT_FOUND", lambda: adm.market_result("2099-12-31"),
          [_err(404, "MARKET_NOT_FOUND")])
    r = c.run("ADM-04", "Wegwerf-EG registrieren → 201 PENDING",
              lambda: anon.register(email, f"egsim-admintest-{rnd}", f"EGSim Admin-Test {rnd}"),
              [_err(201)], extra=lambda r: None if (r.body or {}).get("status") == "PENDING"
              else f"Status {(r.body or {}).get('status')}")
    if not r or r.status != 201:
        c.skip("ADM-*", "weitere Admin-Checks", "Registrierung fehlgeschlagen")
        return c.results
    ec_id = r.body["id"]
    c.run("ADM-05", "Gleichen Status erneut setzen → 200", lambda: adm.set_status(ec_id, "PENDING"), [_err(200)])
    c.run("ADM-06", "Ungültiger Status → 400 VALIDATION_FAILED", lambda: adm.set_status(ec_id, "DELETED"),
          [_err(400, "VALIDATION_FAILED")])
    cred = c.run("ADM-07", "Zugangsdaten für PENDING-EG → 201", lambda: adm.issue_credentials(ec_id), [_err(201)])
    key = pw = None
    if cred and cred.status == 201 and isinstance(cred.body, dict):
        key, pw = cred.body.get("apiKey"), cred.body.get("password")
        c.run("ADM-08", "PENDING-EG mit API-Key → 403 ENERGY_COMMUNITY_NOT_ACTIVE", anon.with_auth(api_key=key).me,
              [_err(403, "ENERGY_COMMUNITY_NOT_ACTIVE")])
        c.run("ADM-09", "PENDING-EG Login → 403 ENERGY_COMMUNITY_NOT_ACTIVE", lambda: anon.login(email, pw),
              [_err(403, "ENERGY_COMMUNITY_NOT_ACTIVE")])
        _inactive_checks(c, "ADM-08", "PENDING-EG", anon.with_auth(api_key=key))
    c.run("ADM-10", "PENDING → ACTIVE → 200", lambda: adm.set_status(ec_id, "ACTIVE"), [_err(200)])
    c.run("ADM-11", "ACTIVE → PENDING → 409 INVALID_STATUS_TRANSITION", lambda: adm.set_status(ec_id, "PENDING"),
          [_err(409, "INVALID_STATUS_TRANSITION")])
    if key:
        c.run("ADM-12", "Aktive EG mit API-Key → 200", anon.with_auth(api_key=key).me, [_err(200)])
        old_token = None
        if pw:
            lg = c.run("ADM-12b", "Login der aktiven EG → 200", lambda: anon.login(email, pw), [_err(200)])
            if lg and lg.status == 200 and isinstance(lg.body, dict):
                old_token = lg.body.get("token")
        new = c.run("ADM-13", "Zugangsdaten neu vergeben → 201", lambda: adm.issue_credentials(ec_id), [_err(201)])
        c.run("ADM-14", "Alter API-Key nach Neuvergabe → 401", anon.with_auth(api_key=key).me, [_err(401, "UNAUTHORIZED")])
        if old_token:
            c.run("ADM-14b", "Altes Token nach Neuvergabe → 401", anon.with_auth(token=old_token).me,
                  [_err(401, "UNAUTHORIZED")])
        if new and new.status == 201 and isinstance(new.body, dict):
            key = new.body.get("apiKey")
    c.run("ADM-15", "ACTIVE → SUSPENDED → 200", lambda: adm.set_status(ec_id, "SUSPENDED"), [_err(200)])
    if key:
        c.run("ADM-16", "Gesperrte EG mit API-Key → 403 ENERGY_COMMUNITY_NOT_ACTIVE", anon.with_auth(api_key=key).me,
              [_err(403, "ENERGY_COMMUNITY_NOT_ACTIVE")])
        _inactive_checks(c, "ADM-16", "gesperrte EG", anon.with_auth(api_key=key))
    c.run("ADM-17", "SUSPENDED → ACTIVE → 200", lambda: adm.set_status(ec_id, "ACTIVE"), [_err(200)])
    c.run("ADM-18", "EG-Liste mit Statusfilter → nur ACTIVE", lambda: adm.list_communities("ACTIVE"), [_err(200)],
          extra=lambda r: None if all(x.get("status") == "ACTIVE" for x in (r.body or [])) else "Filter wirkt nicht")
    c.run("ADM-19", "EG ohne Positionen löschen → 204", lambda: adm.delete_community(ec_id), [_err(204)])
    c.run("ADM-20", "Gelöschte EG abrufen → 404", lambda: adm.get_community(ec_id), [_err(404, "ENERGY_COMMUNITY_NOT_FOUND")])
    used = next((e for e in sim.egs if e.ready and any(sp.id and sp.eg is e for m in sim.markets for sp in m.positions)), None)
    if used:
        c.run("ADM-21", "EG mit Positionen löschen → 409 ENERGY_COMMUNITY_IN_USE", lambda: adm.delete_community(used.id),
              [_err(409, "ENERGY_COMMUNITY_IN_USE")])
    any_pos = next((sp for m in sim.markets for sp in m.positions if sp.id), None)
    if any_pos:
        c.run("ADM-23", "Admin ruft die Position einer EG ab → 200", lambda: adm.get_position(any_pos.id), [_err(200)],
              extra=lambda r: None if (r.body or {}).get("id") == any_pos.id else "falsche Position zurückgegeben")
        c.run("ADM-24", "Admin ruft eine EG ab → 200", lambda: adm.get_community(any_pos.eg.id), [_err(200)],
              extra=lambda r: None if (r.body or {}).get("id") == any_pos.eg.id else "falsche EG zurückgegeben")
        c.run("ADM-25", "Admin filtert Positionen nach energyCommunityId → nur diese EG",
              lambda: adm.list_positions(energyCommunityId=any_pos.eg.id), [_err(200)],
              extra=_only("energyCommunityId", any_pos.eg.id, must_include=any_pos.id))
    settled = next((m for m in sim.markets if m.final_status == "SETTLED"), None)
    if settled:
        d = settled.delivery_date

        def snapshot():
            a, t = adm.allocations(d), adm.trades(d)
            if a.status != 200 or t.status != 200 or not isinstance(a.body, list) or not isinstance(t.body, list):
                return None
            return (sorted((x.get("positionId"), str(x.get("allocatedKwh"))) for x in a.body),
                    sorted((x.get("id"), str(x.get("quantityKwh")), str(x.get("amountEur")), x.get("positionId"))
                           for x in t.body))

        def unchanged(before):
            def f(r: ApiResponse) -> str | None:
                if (r.body or {}).get("status") != "SETTLED":
                    return f"Status {(r.body or {}).get('status')}"
                after = snapshot()
                if before is None or after is None:
                    return "Zuteilungen/Trades nicht abrufbar"
                if after != before:
                    return (f"Zuteilungen/Trades haben sich verändert: {len(before[1])} → {len(after[1])} Trades "
                            f"(doppeltes Settlement?)")
                return None
            return f

        before = snapshot()
        c.run("ADM-22", "Markt erneut schließen (idempotent) → 200 SETTLED, Zuteilungen und Trades unverändert",
              lambda: adm.close_market(d), [_err(200)], extra=unchanged(before))
        answers: list[ApiResponse] = []

        def parallel_close() -> ApiResponse:
            with ThreadPoolExecutor(4) as ex:
                answers.extend(ex.map(lambda _: adm.close_market(d), range(4)))
            return answers[0]

        def all_settled(r: ApiResponse) -> str | None:
            bad = [f"{x.status} {(x.body or {}).get('status') if isinstance(x.body, dict) else ''}".strip()
                   for x in answers if x.status != 200 or (x.body or {}).get("status") != "SETTLED"]
            return f"{len(bad)} von {len(answers)} Antworten nicht 200/SETTLED: {', '.join(bad)}" if bad else unchanged(before)(r)
        c.run("ADM-22c", "4 gleichzeitige close-Aufrufe auf abgewickeltem Markt → alle 200, Daten unverändert",
              parallel_close, [_err(200)], extra=all_settled)
    return c.results
