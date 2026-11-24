"""Ergebnisprüfung nach dem Settlement (Datenvertrag 0.3.1).

Seit 0.3 legt jede Plattform ihre Preis- und Zuteilungsregel selbst fest. Verbindlich sind nur die Invarianten aus
dem Abschnitt „Handelslogik“. Geprüft wird deshalb in Stufen:

1. **EG-Sicht** (immer): Mandantentrennung, Positionsstatus, je Position genau eine Zuteilung, Menge und `reason`
   passen zusammen, Trades ergeben die Zuteilung, Beträge = Menge × Preis (kaufmännisch gerundet), keine 0-kWh-Trades,
   und was sich über die simulierten EGs hinweg schon folgern lässt (Überangebot ↔ Reserve schließen sich aus).
2. **Invarianten über den ganzen Markt**, wenn EGSim alle Positionen kennt (Admin-Key oder „Nur simulierte EGs am
   Markt“): Ankauf = min(S, D), Reserve = max(D − S, 0), Summen, Bedarfe gedeckt bzw. RESERVE_SHORTAGE.
   Mit Admin-Key zusätzlich das Tagesergebnis /result: Summen, Reserve, Beträge, Marge, Durchschnittspreise.
3. **Referenzregel Gruppe 1** (Option `allocationRule = gruppe1`): exakte Nachrechnung mit festen Preisen,
   pro rata und Rundungsrest nach createdAt.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from egsim.settlement import STEP, ZERO, D, Pos, Prices, reserve_status, round_eur, settle
from egsim.timeutil import normalize_interval, parse_ts

if TYPE_CHECKING:
    from egsim.simulator import MarketRun, Simulator

log = logging.getLogger("egsim.verify")
OFFER_REASONS = {"FULLY_ALLOCATED", "PARTIALLY_ALLOCATED", "NOT_ALLOCATED", "NOT_NEEDED"}
DEMAND_REASONS = {"FULLY_ALLOCATED", "RESERVE_SHORTAGE"}
PRICE_STEP = Decimal("0.00001")


class Diff:
    def __init__(self, sim: "Simulator", delivery: str) -> None:
        self.sim = sim
        self.delivery = delivery
        self.count = 0
        self.compared = 0
        self.market_final: dict | None = None  # GET /markets/{d} nach dem Settlement

    def num(self, where: str, field: str, expected: Any, actual: Any, tol: Decimal = ZERO) -> None:
        self.compared += 1
        try:
            e = D(expected)
            a = None if actual is None or isinstance(actual, bool) else D(actual)
        except ArithmeticError:
            self.fail(where, f"{field}: keine Zahl ({actual!r})")
            return
        if e is None and a is None:
            return
        if e is None or a is None or abs(e - a) > tol:
            self.fail(where, f"{field}: erwartet {e}, Plattform {a}")

    def eq(self, where: str, field: str, expected: Any, actual: Any) -> None:
        self.compared += 1
        if expected != actual:
            self.fail(where, f"{field}: erwartet {expected}, Plattform {actual}")

    def true(self, where: str, cond: bool, msg: str) -> None:
        self.compared += 1
        if not cond:
            self.fail(where, msg)

    def fail(self, where: str, msg: str) -> None:
        self.count += 1
        if self.count <= 200:
            self.sim.findings.error("verify", f"{self.delivery} {where}: {msg}")
        elif self.count == 201:
            self.sim.findings.error("verify", f"{self.delivery}: weitere Abweichungen unterdrückt")


def _list(sim: "Simulator", resp, what: str) -> list | None:
    if resp.status != 200 or not isinstance(resp.body, list):
        sim.findings.error("verify", f"{what}: HTTP {resp.status} {resp.body if isinstance(resp.body, dict) else ''}",
                           resp.short())
        return None
    return resp.body


def _decimals(v) -> int:
    exp = D(v).as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def _q(v) -> Decimal:
    try:
        return D(v) if v is not None and not isinstance(v, bool) else ZERO
    except ArithmeticError:
        return ZERO


def _check_rejection_reason(diff: Diff, where: str, p: dict) -> None:
    """rejectionReason ist nur bei REJECTED gesetzt, und zwar mit dem Grund ENERGY_COMMUNITY_NOT_ACTIVE."""
    reason = p.get("rejectionReason")
    if p.get("status") == "REJECTED":
        diff.true(where, reason == "ENERGY_COMMUNITY_NOT_ACTIVE",
                  f"Status REJECTED, rejectionReason {reason!r} (erwartet ENERGY_COMMUNITY_NOT_ACTIVE)")
    else:
        diff.true(where, reason is None, f"rejectionReason {reason!r} gesetzt, obwohl der Status {p.get('status')} ist")


def _check_market_state(sim: "Simulator", run: "MarketRun", diff: Diff) -> dict | None:
    """Marktzustand nach dem Settlement: Zeitstempel gesetzt, kein failureReason, Auflösung unverändert."""
    reader = sim.reader() or sim.admin
    if reader is None:
        return None
    r = reader.get_market(run.delivery_date)
    if r.status != 200 or not isinstance(r.body, dict):
        diff.fail("Markt nach Settlement", f"GET /markets/{run.delivery_date}: HTTP {r.status}")
        return None
    fin, start, w = r.body, run.market or {}, "Markt nach Settlement"
    diff.eq(w, "status", "SETTLED", fin.get("status"))
    diff.true(w, fin.get("closedAt") is not None, "closedAt fehlt bei Status SETTLED")
    diff.true(w, fin.get("settledAt") is not None, "settledAt fehlt bei Status SETTLED")
    diff.true(w, fin.get("failureReason") is None, f"failureReason ist gesetzt ({fin.get('failureReason')!r}), "
                                                   "gilt aber nur bei FAILED")
    try:
        if fin.get("closedAt") and fin.get("settledAt"):
            diff.true(w, parse_ts(fin["closedAt"]) <= parse_ts(fin["settledAt"]), "closedAt liegt nach settledAt")
    except (TypeError, ValueError, AttributeError):
        pass  # ungültige Zeitstempel meldet die Schema-Prüfung
    if start.get("resolution") is not None:  # Vertrag 0.3: Auflösung bleibt ab Marktöffnung unverändert
        diff.eq(w, "resolution", start.get("resolution"), fin.get("resolution"))
    sp, fp = start.get("prices") or {}, fin.get("prices") or {}
    changed = [f for f in ("buyPriceEurPerKwh", "sellPriceEurPerKwh", "reservePriceEurPerKwh")
               if f in sp and _q(sp.get(f)) != _q(fp.get(f))]
    if changed:  # nur eine Indikation (Vertrag 0.3), darf sich ändern
        sim.findings.info("verify", f"{run.delivery_date}: Preisindikation hat sich seit dem Start geändert "
                                    f"({', '.join(changed)}) – zulässig, verbindlich sind die Trade-Preise")
    return fin


# ======================================================================================================================
def verify_market(sim: "Simulator", run: "MarketRun") -> dict:
    d = run.delivery_date
    diff = Diff(sim, d)
    tol_k, tol_e = sim.cfg.tolerance_kwh, sim.cfg.tolerance_eur
    gruppe1 = sim.cfg.allocation_rule == "gruppe1"
    market = run.market or {}
    resolution = market.get("resolution")
    indication = Prices.from_api(market.get("prices") or {}) if market.get("prices") else None
    iv_key = (lambda x: normalize_interval(x)) if resolution == "QUARTER_HOUR" else (lambda x: None)

    all_pos: list[dict] = []
    all_alloc: dict[str, dict] = {}
    all_trades: list[dict] = []
    by_status: dict[str, int] = defaultdict(int)
    tracked = {sp.id: sp for sp in run.positions if sp.id}
    diff.market_final = _check_market_state(sim, run, diff)
    off_indication = 0

    for eg in sim.egs:
        if not eg.ready:
            continue
        who = f"[{eg.key}]"
        pos = _list(sim, eg.client.list_positions(deliveryDate=d), f"{who} GET /positions")
        alloc = _list(sim, eg.client.allocations(d), f"{who} GET /allocations")
        trades = _list(sim, eg.client.trades(d), f"{who} GET /trades")
        if pos is None or alloc is None or trades is None:
            diff.count += 1
            continue
        foreign = [x for x in pos + alloc + trades if x.get("energyCommunityId") != eg.id]
        diff.true(f"{who} Mandantentrennung", not foreign, f"{len(foreign)} fremde Datensätze sichtbar")

        own = {p["id"]: p for p in pos if p.get("energyCommunityId") == eg.id}
        for p in own.values():
            by_status[p.get("status")] += 1
            diff.true(f"{who} Position {p['id']}", p.get("status") not in ("OPEN", "CLOSED"),
                      f"Status {p.get('status')} nach SETTLED (erwartet PROCESSED/CANCELLED/REJECTED)")
            _check_rejection_reason(diff, f"{who} Position {p['id']}", p)
            sp = tracked.get(p["id"])
            if sp:
                diff.eq(f"{who} Position {p['id']}", "status", "CANCELLED" if sp.cancelled else "PROCESSED", p.get("status"))
                if not sp.cancelled:
                    diff.num(f"{who} Position {p['id']}", "quantityKwh", sp.quantity, p.get("quantityKwh"))
        for sp in tracked.values():
            if sp.eg is eg and sp.id not in own:
                diff.fail(f"{who} Position {sp.id}", "eigene Position fehlt in GET /positions")

        alloc_by_pos: dict[str, list[dict]] = defaultdict(list)
        for a in alloc:
            alloc_by_pos[a.get("positionId")].append(a)
        trades_by_pos: dict[str, list[dict]] = defaultdict(list)
        seen = set()
        for t in trades:
            w = f"{who} Trade {t.get('id')}"
            diff.true(w, t.get("id") not in seen, "id doppelt")
            seen.add(t.get("id"))
            diff.true(w, t.get("positionId") in own, "positionId gehört zu keiner eigenen Position")
            trades_by_pos[t.get("positionId")].append(t)
            q, pr, amt = D(t.get("quantityKwh")), D(t.get("priceEurPerKwh")), D(t.get("amountEur"))
            diff.true(w, q is not None and q > 0, "quantityKwh <= 0 (Mengen von 0 kWh erzeugen keinen Trade)")
            if None not in (q, pr, amt):  # Invariante 7
                diff.true(w, abs(round_eur(q * pr) - amt) <= tol_e,
                          f"amountEur {amt} ≠ round(quantityKwh × priceEurPerKwh) = {round_eur(q * pr)}")
                diff.true(w, _decimals(amt) <= 2, f"amountEur {amt} hat mehr als 2 Nachkommastellen")
            diff.eq(w, "deliveryDate", d, t.get("deliveryDate"))
            direction = t.get("direction")
            if direction not in ("PLATFORM_BUYS", "PLATFORM_SELLS"):
                diff.fail(w, f"direction {direction} unbekannt")
                continue
            if direction == "PLATFORM_BUYS":
                diff.true(w, not t.get("fromReserve"), "fromReserve=true bei PLATFORM_BUYS")
            ref_price = (indication.buy if direction == "PLATFORM_BUYS" else indication.sell) if indication else None
            if gruppe1 and ref_price is not None:  # Gruppe 1: feste Preise, die ab Öffnung in Market.prices stehen
                diff.num(w, f"priceEurPerKwh ({'Ankauf' if direction == 'PLATFORM_BUYS' else 'Verkauf'}, Gruppe 1)",
                         ref_price, pr)
            elif ref_price is not None and pr is not None and pr != ref_price:
                off_indication += 1

        for pid, p in own.items():
            w = f"{who} Position {pid} ({p.get('type')})"
            al = alloc_by_pos.get(pid, [])
            tr = trades_by_pos.get(pid, [])
            if p.get("status") != "PROCESSED":  # Invariante 1: nur CLOSED/PROCESSED zählt
                diff.true(w, not al and not tr, f"Status {p.get('status')}, hat aber Zuteilung/Trades")
                continue
            if len(al) != 1:
                diff.fail(w, f"{len(al)} Zuteilungen (erwartet genau 1, auch mit 0 kWh)")
                continue
            a = al[0]
            all_alloc[pid] = a
            all_pos.append(p)
            all_trades += tr
            req, got = D(p.get("quantityKwh")), D(a.get("allocatedKwh"))
            diff.num(w, "requestedKwh", req, a.get("requestedKwh"))
            diff.eq(w, "type", p.get("type"), a.get("type"))
            diff.eq(w, "intervalStart", normalize_interval(p.get("intervalStart")), normalize_interval(a.get("intervalStart")))
            diff.eq(w, "deliveryDate", d, a.get("deliveryDate"))
            if got is None:
                diff.fail(w, "allocatedKwh fehlt")
                continue
            diff.true(w, ZERO <= got <= req, f"allocatedKwh {got} außerhalb 0…{req} (Invariante 2)")
            diff.true(w, _decimals(got) <= 3, f"allocatedKwh {got} hat mehr als 3 Nachkommastellen")
            reason = a.get("reason")
            if p.get("type") == "OFFER":
                diff.true(w, reason in OFFER_REASONS, f"reason {reason} unzulässig für OFFER")
                if reason == "FULLY_ALLOCATED":
                    diff.true(w, got == req, f"FULLY_ALLOCATED, aber {got} von {req}")
                elif reason == "PARTIALLY_ALLOCATED":
                    diff.true(w, ZERO < got < req, f"PARTIALLY_ALLOCATED, aber {got} von {req}")
                elif reason in ("NOT_ALLOCATED", "NOT_NEEDED"):
                    diff.true(w, got == 0, f"{reason}, aber {got} kWh")
                wrong = [t for t in tr if t.get("direction") != "PLATFORM_BUYS"]
                diff.true(w, not wrong, "OFFER hat PLATFORM_SELLS-Trades")
                buys = [t for t in tr if t.get("direction") == "PLATFORM_BUYS"]
                if gruppe1:
                    diff.true(w, len(buys) == (1 if got > 0 else 0), f"{len(buys)} PLATFORM_BUYS-Trades (Gruppe 1: genau einer)")
            else:
                diff.true(w, reason in DEMAND_REASONS, f"reason {reason} unzulässig für DEMAND")
                if reason == "FULLY_ALLOCATED":
                    diff.true(w, got == req, f"FULLY_ALLOCATED, aber {got} von {req}")
                elif reason == "RESERVE_SHORTAGE":
                    diff.true(w, got < req, f"RESERVE_SHORTAGE, aber {got} von {req}")
                wrong = [t for t in tr if t.get("direction") != "PLATFORM_SELLS"]
                diff.true(w, not wrong, "DEMAND hat PLATFORM_BUYS-Trades")
                if gruppe1:
                    sells = [t for t in tr if t.get("direction") == "PLATFORM_SELLS"]
                    diff.true(w, sum(1 for t in sells if not t.get("fromReserve")) <= 1
                              and sum(1 for t in sells if t.get("fromReserve")) <= 1,
                              "Gruppe 1: höchstens ein Trade je Anteil (Angebote/Reserve)")
            # Handelsabschlüsse: mindestens einer bei Zuteilung > 0, keiner bei 0, Summe = Zuteilung
            total = sum((_q(t.get("quantityKwh")) for t in tr), ZERO)
            if got > 0:
                diff.true(w, len(tr) >= 1, "Zuteilung > 0, aber kein Handelsabschluss")
            else:
                diff.true(w, not tr, "Handelsabschluss trotz 0 kWh Zuteilung")
            diff.num(w, "Summe der Trade-Mengen = Zuteilung", got, total, tol_k)

    if off_indication:
        sim.findings.info("verify", f"{d}: {off_indication} Trade-Preise weichen von der Preisindikation ab – zulässig "
                                    "(verbindlich ist der Preis des Handelsabschlusses)")

    # --- Folgerungen aus den Daten der simulierten EGs (gelten unabhängig von anderen Marktteilnehmern)
    groups: dict[Any, list[dict]] = defaultdict(list)
    for p in all_pos:
        groups[iv_key(p.get("intervalStart"))].append(p)
    reserve_used: dict[Any, Decimal] = defaultdict(lambda: ZERO)
    for t in all_trades:
        if t.get("fromReserve"):
            reserve_used[iv_key(t.get("intervalStart"))] += _q(t.get("quantityKwh"))
    for key, ps in groups.items():
        w = f"Intervall {key}" if key else "Tagesmarkt"
        reason = {p["id"]: all_alloc[p["id"]].get("reason") for p in ps}
        offers = [p for p in ps if p.get("type") == "OFFER"]
        demands = [p for p in ps if p.get("type") == "DEMAND"]
        oversupply = [p for p in offers if reason[p["id"]] in ("PARTIALLY_ALLOCATED", "NOT_ALLOCATED")]
        not_needed = [p for p in offers if reason[p["id"]] == "NOT_NEEDED"]
        shortage = reserve_used[key] > 0 or any(reason[p["id"]] == "RESERVE_SHORTAGE" for p in demands)
        if not_needed:  # NOT_NEEDED heißt: kein Bedarf im Intervall
            diff.true(w, not demands, f"Angebot NOT_NEEDED (kein Bedarf), aber {len(demands)} simulierte Bedarfe im selben Intervall")
            diff.true(w, len(not_needed) == len(offers), "Angebote teils NOT_NEEDED, teils gekauft")
        if oversupply:  # Überangebot S > D: Ankauf = D, jeder Bedarf aus Angeboten gedeckt, keine Reserve (Inv. 3–5)
            diff.true(w, not shortage, "Angebote nicht voll gekauft (Überangebot) und gleichzeitig Reserve/Reserveknappheit")
            diff.true(w, all(reason[p["id"]] == "FULLY_ALLOCATED" for p in demands),
                      "Überangebot, aber nicht jeder Bedarf voll gedeckt")
        if shortage:  # D > S: jedes Angebot wird voll gekauft (Invariante 3 mit Invariante 2)
            cut = [p for p in offers if reason[p["id"]] != "FULLY_ALLOCATED"]
            diff.true(w, not cut, f"Reserve genutzt, aber {len(cut)} Angebote nicht voll gekauft (Ankauf muss min(S, D) = S sein)")
        if gruppe1 and len(oversupply) > 0:
            partial = [p for p in offers]
            ratios = [D(all_alloc[p["id"]]["allocatedKwh"]) / D(p["quantityKwh"]) for p in partial]
            tolerance = max(STEP / D(p["quantityKwh"]) for p in partial) * 2
            diff.true(w, max(ratios) - min(ratios) <= tolerance,
                      f"Gruppe 1: Überangebot nicht pro rata (Anteile {min(ratios):.4f}…{max(ratios):.4f})")

    # --- Markt vollständig bekannt: Invarianten über den ganzen Markt, Tagesergebnis, ggf. Gruppe 1
    full = None
    mode = "egView"
    if getattr(sim, "admin", None) is not None:
        full = verify_market_admin(sim, run, diff)
        mode = "admin" if full else mode
    elif sim.cfg.all_participants_simulated and all_pos:
        check_invariants(diff, "Markt", resolution, all_pos, all_alloc, all_trades, tol_k)
        full = {"invariants": "geprüft"}
        if gruppe1:
            full = _gruppe1_recalc(sim, diff, "Gruppe 1", resolution, indication, all_pos, all_alloc, all_trades,
                                   reserve_used, tol_k, tol_e) or full
        mode = "full"

    summary = {
        "status": "OK" if diff.count == 0 else "MISMATCH",
        "mode": mode,
        "rule": sim.cfg.allocation_rule,
        "comparisons": diff.compared,
        "mismatches": diff.count,
        "positionsByStatus": dict(by_status),
        "processedPositions": len(all_pos),
        "trades": len(all_trades),
        "ownPurchaseEur": str(sum((_q(t.get("amountEur")) for t in all_trades if t.get("direction") == "PLATFORM_BUYS"), ZERO)),
        "ownSalesEur": str(sum((_q(t.get("amountEur")) for t in all_trades if t.get("direction") == "PLATFORM_SELLS"), ZERO)),
        "reference": full,
    }
    log.info("Prüfung %s: %s (%d Vergleiche, %d Abweichungen)", d, summary["status"], diff.compared, diff.count)
    return summary


# ======================================================================================================================
def interval_figures(positions: list[dict], allocs: dict[str, dict], trades: list[dict]) -> dict[str, Decimal]:
    S = sum((_q(p.get("quantityKwh")) for p in positions if p.get("type") == "OFFER"), ZERO)
    Dm = sum((_q(p.get("quantityKwh")) for p in positions if p.get("type") == "DEMAND"), ZERO)
    buys = sum((_q(t.get("quantityKwh")) for t in trades if t.get("direction") == "PLATFORM_BUYS"), ZERO)
    sells = sum((_q(t.get("quantityKwh")) for t in trades if t.get("direction") == "PLATFORM_SELLS"), ZERO)
    reserve = sum((_q(t.get("quantityKwh")) for t in trades if t.get("fromReserve")), ZERO)
    sold_alloc = sum((_q(allocs[p["id"]].get("allocatedKwh")) for p in positions
                      if p.get("type") == "DEMAND" and p["id"] in allocs), ZERO)
    return {"S": S, "D": Dm, "buys": buys, "sells": sells, "reserve": reserve, "soldAlloc": sold_alloc,
            "required": max(Dm - S, ZERO)}


def check_invariants(diff: Diff, label: str, resolution: str | None, positions: list[dict], allocs: dict[str, dict],
                     trades: list[dict], tol_k: Decimal = ZERO) -> dict[Any, dict[str, Decimal]]:
    """Invarianten 3–6 je Intervall (bei DAY einmal), wenn alle PROCESSED-Positionen des Markts bekannt sind."""
    key = (lambda x: normalize_interval(x)) if resolution == "QUARTER_HOUR" else (lambda x: None)
    pos_by: dict[Any, list[dict]] = defaultdict(list)
    tr_by: dict[Any, list[dict]] = defaultdict(list)
    for p in positions:
        pos_by[key(p.get("intervalStart"))].append(p)
    for t in trades:
        tr_by[key(t.get("intervalStart"))].append(t)
    out = {}
    for k in sorted(set(pos_by) | set(tr_by), key=lambda x: x or ""):
        f = interval_figures(pos_by[k], allocs, tr_by[k])
        out[k] = f
        w = f"{label} {k}" if k else label
        diff.num(w, "Ankauf = min(S, D) (Invariante 3)", min(f["S"], f["D"]), f["buys"], tol_k)
        diff.true(w, f["reserve"] <= f["required"] + tol_k,
                  f"Reserve {f['reserve']} kWh, nötig ist höchstens max(D − S, 0) = {f['required']} kWh (Invariante 4)")
        if f["D"] == 0:
            diff.true(w, f["buys"] == 0 and f["reserve"] == 0, "ohne Nachfrage weder Ankauf noch Reserve (Invariante 4)")
        diff.num(w, "verkauft = Ankauf + Reserve", f["buys"] + f["reserve"], f["sells"], tol_k)
        diff.num(w, "Summe Verkäufe = Summe Zuteilungen der Bedarfe (Invariante 6)", f["soldAlloc"], f["sells"], tol_k)
        demands = [p for p in pos_by[k] if p.get("type") == "DEMAND"]
        if f["reserve"] >= f["required"]:  # Standardfall: jeder Bedarf voll gedeckt (Invariante 5)
            short = [p for p in demands if allocs.get(p["id"], {}).get("reason") != "FULLY_ALLOCATED"]
            diff.true(w, not short, f"Reserve reicht, aber {len(short)} Bedarfe nicht voll gedeckt (Invariante 5)")
        else:
            full = [p for p in demands if allocs.get(p["id"], {}).get("reason") == "FULLY_ALLOCATED"]
            if full and len(full) == len(demands):
                diff.fail(w, "Reserve fehlt, trotzdem alle Bedarfe FULLY_ALLOCATED (Invariante 5)")
    return out


def _reserve_expect(required: Decimal, dispatched: Decimal) -> str:
    return reserve_status(required, min(dispatched, required) if required > 0 else ZERO)


def _avg(trades: list[dict]) -> Decimal | None:
    qty = sum((_q(t.get("quantityKwh")) for t in trades), ZERO)
    if qty == 0:
        return None
    return sum((_q(t.get("quantityKwh")) * _q(t.get("priceEurPerKwh")) for t in trades), ZERO) / qty


def _cmp_price(diff: Diff, w: str, field: str, expected: Decimal | None, actual) -> None:
    """Durchschnittspreis: null genau dann, wenn es kein Geschäft gab; sonst bis auf Rundung auf 5 Stellen gleich."""
    if expected is None:
        diff.true(w, actual is None, f"{field}: ohne Geschäft in dieser Richtung muss der Preis null sein, ist {actual}")
        return
    exp5 = expected.quantize(PRICE_STEP, rounding=ROUND_HALF_UP)
    diff.num(w, f"{field} (Durchschnitt gewichtet nach Menge)", exp5, actual, PRICE_STEP)


def _cmp_result_block(diff: Diff, w: str, figures: dict[str, Decimal], block: dict, trades: list[dict], tol_k, tol_e,
                      top_level: bool) -> None:
    """Summen und Reserve eines Tages- bzw. Intervallergebnisses gegen Positionen und Trades."""
    diff.num(w, "totalOfferedKwh = S", figures["S"], block.get("totalOfferedKwh"), tol_k)
    diff.num(w, "totalDemandKwh = D", figures["D"], block.get("totalDemandKwh"), tol_k)
    diff.num(w, "totalPurchasedKwh = Summe PLATFORM_BUYS (Invariante 6)", figures["buys"], block.get("totalPurchasedKwh"), tol_k)
    diff.num(w, "totalSoldKwh = Summe PLATFORM_SELLS (Invariante 6)", figures["sells"], block.get("totalSoldKwh"), tol_k)
    res = block.get("reserve") or {}
    diff.num(w, "reserve.requiredKwh = max(D − S, 0)" + ("" if not top_level else " (bei QUARTER_HOUR Summe der Intervalle)"),
             figures["required"], res.get("requiredKwh"), tol_k)
    disp = _q(res.get("dispatchedKwh"))
    diff.true(w, disp <= figures["required"] + tol_k, f"reserve.dispatchedKwh {disp} > requiredKwh {figures['required']}")
    diff.num(w, "reserve.dispatchedKwh = Reserve-Trades", figures["reserve"], disp, tol_k)
    if not top_level or figures["required"] == 0:
        diff.eq(w, "reserve.status", _reserve_expect(figures["required"], disp), res.get("status"))
    price = D(res.get("priceEurPerKwh")) if res.get("priceEurPerKwh") is not None else None
    if price is not None:
        diff.num(w, "reserve.costEur = dispatchedKwh × Preis (Invariante 7)", round_eur(disp * price), res.get("costEur"), tol_e)
    elif disp > 0:
        diff.fail(w, "reserve.priceEurPerKwh fehlt, obwohl Reserve geliefert wurde")
    else:
        diff.num(w, "reserve.costEur ohne Reserve", ZERO, res.get("costEur"), tol_e)


def verify_market_admin(sim: "Simulator", run: "MarketRun", diff: Diff) -> dict | None:
    """Mit Admin-Key: alle Positionen, Zuteilungen und Trades laden, Invarianten prüfen und das Tagesergebnis
    /result dagegen abgleichen. Bei allocationRule=gruppe1 zusätzlich exakte Nachrechnung."""
    adm = sim.admin
    d = run.delivery_date
    tol_k, tol_e = sim.cfg.tolerance_kwh, sim.cfg.tolerance_eur
    positions = _list(sim, adm.list_positions(deliveryDate=d), "Admin: GET /positions")
    allocs = _list(sim, adm.allocations(d), "Admin: GET /allocations")
    trades = _list(sim, adm.trades(d), "Admin: GET /trades")
    rr = adm.market_result(d)
    if rr.status != 200 or not isinstance(rr.body, dict):
        sim.findings.error("verify", f"Admin: GET /result: HTTP {rr.status}", rr.short())
        return None
    if positions is None or allocs is None or trades is None:
        return None
    result = rr.body
    resolution = (run.market or {}).get("resolution")
    for p in positions:
        diff.true(f"Admin: Position {p.get('id')}", p.get("status") not in ("OPEN", "CLOSED"),
                  f"Status {p.get('status')} nach SETTLED")
        _check_rejection_reason(diff, f"Admin: Position {p.get('id')}", p)
    fin = diff.market_final or {}
    try:
        if fin.get("settledAt") and result.get("settledAt"):
            diff.eq("Admin: Tagesergebnis", "settledAt (wie im Markt)", parse_ts(fin["settledAt"]), parse_ts(result["settledAt"]))
    except (TypeError, ValueError, AttributeError):
        pass

    processed = [p for p in positions if p.get("status") == "PROCESSED"]
    plat_alloc = {a.get("positionId"): a for a in allocs}
    diff.true("Admin: Zuteilungen", len(plat_alloc) == len(allocs), "positionId doppelt")
    for p in processed:
        diff.true(f"Admin: Position {p['id']}", p["id"] in plat_alloc, "PROCESSED, aber keine Zuteilung")
    for pid in set(plat_alloc) - {p["id"] for p in processed}:
        diff.fail(f"Admin: Zuteilung {pid}", "gehört zu keiner PROCESSED-Position")
    diff.true("Admin: Trades", len({t.get("id") for t in trades}) == len(trades), "Trade-IDs nicht eindeutig")
    for t in trades:
        q, pr, amt = D(t.get("quantityKwh")), D(t.get("priceEurPerKwh")), D(t.get("amountEur"))
        if None not in (q, pr, amt):
            diff.true(f"Admin: Trade {t.get('id')}", abs(round_eur(q * pr) - amt) <= tol_e,
                      f"amountEur {amt} ≠ round({q} × {pr}) = {round_eur(q * pr)} (Invariante 7)")

    per_iv = check_invariants(diff, "Admin:", resolution, processed, plat_alloc, trades, tol_k)

    w = "Admin: Tagesergebnis"
    diff.eq(w, "status", "SETTLED", result.get("status"))
    diff.eq(w, "resolution", resolution, result.get("resolution"))
    day = interval_figures(processed, plat_alloc, trades)
    if resolution == "QUARTER_HOUR":  # Reserve gilt je Intervall, der Tag ist die Summe der Intervalle
        day["required"] = sum((f["required"] for f in per_iv.values()), ZERO)
    _cmp_result_block(diff, w, day, result, trades, tol_k, tol_e, top_level=True)
    purchase = sum((_q(t.get("amountEur")) for t in trades if t.get("direction") == "PLATFORM_BUYS"), ZERO)
    sales = sum((_q(t.get("amountEur")) for t in trades if t.get("direction") == "PLATFORM_SELLS"), ZERO)
    diff.num(w, "purchaseCostEur = Summe der Ankaufsbeträge", purchase, result.get("purchaseCostEur"), tol_e)
    diff.num(w, "salesRevenueEur = Summe der Verkaufsbeträge", sales, result.get("salesRevenueEur"), tol_e)
    res_cost = _q((result.get("reserve") or {}).get("costEur"))
    diff.num(w, "marginEur = Verkauf − Einkauf − Reserve (Invariante 8)", _q(result.get("salesRevenueEur")) -
             _q(result.get("purchaseCostEur")) - res_cost, result.get("marginEur"), tol_e)
    prices = result.get("prices") or {}
    _cmp_price(diff, w, "prices.buyPriceEurPerKwh",
               _avg([t for t in trades if t.get("direction") == "PLATFORM_BUYS"]), prices.get("buyPriceEurPerKwh"))
    _cmp_price(diff, w, "prices.sellPriceEurPerKwh",
               _avg([t for t in trades if t.get("direction") == "PLATFORM_SELLS"]), prices.get("sellPriceEurPerKwh"))
    if day["reserve"] == 0:
        diff.true(w, prices.get("reservePriceEurPerKwh") is None,
                  f"prices.reservePriceEurPerKwh ohne Reserve muss null sein, ist {prices.get('reservePriceEurPerKwh')}")
    if resolution == "QUARTER_HOUR":
        plat_iv = {normalize_interval(i.get("intervalStart")): i for i in result.get("intervals") or []}
        for k, figures in per_iv.items():
            pi = plat_iv.get(k)
            wi = f"Admin: Intervall {k}"
            if pi is None:
                diff.fail(wi, "fehlt in result.intervals")
                continue
            ivt = [t for t in trades if normalize_interval(t.get("intervalStart")) == k]
            _cmp_result_block(diff, wi, figures, pi, ivt, tol_k, tol_e, top_level=False)
        diff.num(w, "reserve.costEur = Summe der Intervalle",
                 sum((_q((i.get("reserve") or {}).get("costEur")) for i in result.get("intervals") or []), ZERO),
                 res_cost, tol_e + Decimal("0.01") * len(plat_iv))
    elif result.get("intervals"):
        diff.fail(w, "intervals ist bei DAY gesetzt")

    summary = {
        "totalOfferedKwh": str(day["S"]), "totalDemandKwh": str(day["D"]), "reserveRequiredKwh": str(day["required"]),
        "reserveDispatchedKwh": str(day["reserve"]), "purchaseCostEur": str(purchase), "salesRevenueEur": str(sales),
        "reserveCostEur": str(res_cost), "marginEur": str(_q(result.get("marginEur"))),
        "ruleVersion": result.get("ruleVersion"),
    }
    if sim.cfg.allocation_rule == "gruppe1":
        indication = Prices.from_api((run.market or {}).get("prices") or {}) if (run.market or {}).get("prices") else None
        if resolution == "QUARTER_HOUR":
            dispatched = {normalize_interval(i.get("intervalStart")): _q((i.get("reserve") or {}).get("dispatchedKwh"))
                          for i in result.get("intervals") or []}
        else:
            dispatched = {None: _q((result.get("reserve") or {}).get("dispatchedKwh"))}
        ref = _gruppe1_recalc(sim, diff, "Admin Gruppe 1", resolution, indication, processed, plat_alloc, trades,
                              dispatched, tol_k, tol_e)
        if ref:
            summary.update({k: v for k, v in ref.items() if k not in summary or k == "marginEur"})
    return summary


def _gruppe1_recalc(sim, diff: Diff, label: str, resolution, indication: Prices | None, positions: list[dict],
                    allocs: dict[str, dict], trades: list[dict], dispatched, tol_k, tol_e) -> dict | None:
    """Exakte Nachrechnung nach dem Referenzbeispiel Gruppe 1 (feste Preise aus Market.prices)."""
    if indication is None or None in (indication.buy, indication.sell, indication.reserve):
        sim.findings.info("verify", f"{diff.delivery}: {label}: Markt ohne vollständige Preisindikation – "
                                    "exakte Nachrechnung nach Gruppe 1 nicht möglich")
        return None
    if not isinstance(dispatched, dict):
        dispatched = dict(dispatched)
    if resolution != "QUARTER_HOUR":
        dispatched = {None: dispatched.get(None, ZERO)}
    ref = settle([Pos.from_api(p) for p in positions], indication, resolution, dispatched)
    for ra in ref.allocations:
        pa = allocs.get(ra.position.id, {})
        w = f"{label}: Position {ra.position.id}"
        diff.num(w, "allocatedKwh", ra.allocated, pa.get("allocatedKwh"), tol_k)
        diff.eq(w, "reason", ra.reason, pa.get("reason"))
    plat: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        plat[(t.get("positionId"), t.get("direction"), bool(t.get("fromReserve")))].append(t)
    refk = {(t.position.id, t.direction, t.from_reserve): t for t in ref.trades}
    for k, rt in refk.items():
        pts = plat.get(k, [])
        w = f"{label}: Trade {k[0]} {k[1]}{' (Reserve)' if k[2] else ''}"
        if not pts:
            diff.fail(w, f"fehlt (erwartet {rt.quantity} kWh)")
            continue
        diff.true(w, len(pts) == 1, f"{len(pts)} Trades statt einem")
        diff.num(w, "quantityKwh", rt.quantity, pts[0].get("quantityKwh"), tol_k)
        diff.num(w, "priceEurPerKwh", rt.price, pts[0].get("priceEurPerKwh"))
        diff.num(w, "amountEur", rt.amount, pts[0].get("amountEur"), tol_e)
    for k in set(plat) - set(refk):
        diff.fail(f"{label}: Trade {k[0]} {k[1]}", f"unerwartet ({plat[k][0].get('quantityKwh')} kWh)")
    return {
        "totalOfferedKwh": str(ref.total_offered), "totalDemandKwh": str(ref.total_demand),
        "reserveRequiredKwh": str(ref.reserve.required), "reserveDispatchedKwh": str(ref.reserve.dispatched),
        "purchaseCostEur": str(ref.purchase_cost), "salesRevenueEur": str(ref.sales_revenue),
        "reserveCostEur": str(ref.reserve.cost), "marginEur": str(ref.margin),
    }
