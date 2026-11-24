"""Referenzlogik (Referenzbeispiel Gruppe 1) gegen die Beispiele aus dem Datenvertrag 0.3.1."""
from decimal import Decimal as D

from egsim.settlement import Pos, Prices, pro_rata, settle

PRICES = Prices(D("0.10"), D("0.14"), D("0.20"))


def pos(pid, typ, qty, created, price=None, ec=None, interval=None):
    return Pos(pid, ec or "ec-" + pid, typ, D(str(qty)), created, interval, D(str(price)) if price is not None else None)


def test_beispielablauf_aus_dem_vertrag():
    # Vertrag 0.3: Sonnenfeld 100 kWh, Windpark 50 kWh zu 0,12 (über dem Ankaufspreis, trotzdem gehandelt),
    # Altstadt Bedarf 170 kWh, Reserve 20 kWh
    r = settle([
        pos("a", "OFFER", 100, "2026-11-18T09:00:00Z"),
        pos("b", "OFFER", 50, "2026-11-18T09:20:00Z", "0.12"),
        pos("c", "DEMAND", 170, "2026-11-18T09:40:00Z"),
    ], PRICES, "DAY")
    alloc = {a.position.id: (a.allocated, a.reason) for a in r.allocations}
    assert alloc == {"a": (100, "FULLY_ALLOCATED"), "b": (50, "FULLY_ALLOCATED"), "c": (170, "FULLY_ALLOCATED")}
    trades = sorted((t.position.id, t.direction, t.from_reserve, t.quantity, t.amount) for t in r.trades)
    assert trades == [("a", "PLATFORM_BUYS", False, 100, D("10.00")),
                      ("b", "PLATFORM_BUYS", False, 50, D("5.00")),
                      ("c", "PLATFORM_SELLS", False, 150, D("21.00")),
                      ("c", "PLATFORM_SELLS", True, 20, D("2.80"))]
    assert (r.total_offered, r.total_demand, r.total_purchased, r.total_sold) == (150, 170, 150, 170)
    assert (r.reserve.status, r.reserve.required, r.reserve.dispatched, r.reserve.cost) == ("COVERED", 20, 20, D("4.00"))
    assert (r.purchase_cost, r.sales_revenue, r.margin) == (D("15.00"), D("23.80"), D("4.80"))
    # MarketResult.prices: Durchschnittspreise
    assert (r.avg_buy, r.avg_sell, r.avg_reserve) == (D("0.10"), D("0.14"), D("0.20"))


def test_hohe_preisvorstellung_wird_gehandelt():
    # Preisvorstellung ist nur informativ, kein Mindestpreis
    r = settle([pos("o", "OFFER", 10, "2026-11-18T09:00:00Z", "5.00"), pos("d", "DEMAND", 10, "2026-11-18T09:01:00Z")],
               PRICES, "DAY")
    assert next(a for a in r.allocations if a.position.id == "o").reason == "FULLY_ALLOCATED"


def test_ueberangebot_not_allocated_bei_anteil_null():
    # 0,001 kWh Bedarf auf drei Angebote: nur das älteste bekommt den Rest, die anderen gehen leer aus
    r = settle([pos("a", "OFFER", 1, "2026-11-18T09:00:00Z"), pos("b", "OFFER", 1, "2026-11-18T09:01:00Z"),
                pos("c", "OFFER", 1, "2026-11-18T09:02:00Z"), pos("d", "DEMAND", "0.001", "2026-11-18T09:03:00Z")],
               PRICES, "DAY")
    alloc = {a.position.id: (a.allocated, a.reason) for a in r.allocations}
    assert alloc["a"] == (D("0.001"), "PARTIALLY_ALLOCATED")
    assert alloc["b"] == (0, "NOT_ALLOCATED") and alloc["c"] == (0, "NOT_ALLOCATED")
    assert not [t for t in r.trades if t.position.id in ("b", "c")]


def test_keine_geschaefte_durchschnittspreise_null():
    r = settle([pos("a", "OFFER", 10, "2026-11-18T09:00:00Z")], PRICES, "DAY")
    assert (r.avg_buy, r.avg_sell, r.avg_reserve) == (None, None, None)


def test_teilzuteilung_pro_rata():
    r = settle([pos("a", "OFFER", 100, "2026-11-18T09:00:00Z"), pos("b", "OFFER", 60, "2026-11-18T09:01:00Z"),
                pos("c", "DEMAND", 120, "2026-11-18T09:02:00Z")], PRICES, "DAY")
    alloc = {a.position.id: (a.allocated, a.reason) for a in r.allocations}
    assert alloc == {"a": (75, "PARTIALLY_ALLOCATED"), "b": (45, "PARTIALLY_ALLOCATED"), "c": (120, "FULLY_ALLOCATED")}
    assert r.reserve.status == "NOT_NEEDED" and r.reserve.price is None


def test_viertelstunden_beispiel_tagesergebnis():
    i1, i2 = "2026-11-19T23:00:00Z", "2026-11-20T11:15:00Z"
    r = settle([
        pos("d1", "DEMAND", "1.2", "2026-11-18T09:00:00Z", interval=i1),
        pos("o1", "OFFER", "2.5", "2026-11-18T09:00:00Z", "0.08", interval=i2),
        pos("o2", "OFFER", "1.0", "2026-11-18T09:01:00Z", "0.12", interval=i2),
        pos("d2", "DEMAND", "1.8", "2026-11-18T09:02:00Z", interval=i2),
    ], PRICES, "QUARTER_HOUR")
    assert (r.total_offered, r.total_demand) == (D("3.5"), D("3.0"))
    assert (r.total_purchased, r.total_sold) == (D("1.8"), D("3.0"))
    assert (r.reserve.required, r.reserve.dispatched, r.reserve.cost, r.reserve.status) == (D("1.2"), D("1.2"), D("0.24"), "COVERED")
    assert (r.purchase_cost, r.sales_revenue, r.margin) == (D("0.18"), D("0.42"), D("0.00"))
    iv = {i.interval_start: i for i in r.intervals}
    assert iv[i2].reserve.status == "NOT_NEEDED" and iv[i2].reserve.cost == 0
    # S = 3,5 > D = 1,8: pro rata 1,286 und 0,514 (Rest 0,001 an das ältere Angebot)
    assert {a.position.id: (a.allocated, a.reason) for a in iv[i2].allocations} == {
        "o1": (D("1.286"), "PARTIALLY_ALLOCATED"), "o2": (D("0.514"), "PARTIALLY_ALLOCATED"),
        "d2": (D("1.8"), "FULLY_ALLOCATED")}


def test_kein_bedarf_not_needed():
    r = settle([pos("a", "OFFER", 10, "2026-11-18T09:00:00Z")], PRICES, "DAY")
    assert [(a.allocated, a.reason) for a in r.allocations] == [(0, "NOT_NEEDED")]
    assert r.trades == []


def test_reserve_knapp_pro_rata_auf_bedarfe():
    r = settle([pos("o", "OFFER", 10, "2026-11-18T09:00:00Z"), pos("d1", "DEMAND", 30, "2026-11-18T09:01:00Z"),
                pos("d2", "DEMAND", 10, "2026-11-18T09:02:00Z")], PRICES, "DAY", {None: D(10)})
    alloc = {a.position.id: (a.allocated, a.reason) for a in r.allocations}
    assert alloc["d1"] == (15, "RESERVE_SHORTAGE") and alloc["d2"] == (5, "RESERVE_SHORTAGE")
    assert r.reserve.status == "PARTIAL" and r.reserve.required == 30
    sells = {(t.position.id, t.from_reserve): t.quantity for t in r.trades if t.direction == "PLATFORM_SELLS"}
    assert sells == {("d1", False): D("7.5"), ("d1", True): D("7.5"), ("d2", False): D("2.5"), ("d2", True): D("2.5")}


def test_reserve_nicht_verfuegbar():
    r = settle([pos("d", "DEMAND", 5, "2026-11-18T09:00:00Z")], PRICES, "DAY", {None: D(0)})
    assert r.reserve.status == "UNAVAILABLE"
    assert [(a.allocated, a.reason) for a in r.allocations] == [(0, "RESERVE_SHORTAGE")]
    assert r.trades == []


def test_rundungsrest_nach_created_at():
    # 3 gleiche Angebote, Bedarf 10 → je 3,333, Rest 0,001 an das älteste
    ps = [pos("x", "OFFER", 5, "2026-11-18T09:02:00Z"), pos("y", "OFFER", 5, "2026-11-18T09:00:00Z"),
          pos("z", "OFFER", 5, "2026-11-18T09:01:00Z")]
    shares = pro_rata(ps, D(10))
    assert shares == {"y": D("3.334"), "z": D("3.333"), "x": D("3.333")}
    # Gleichstand bei createdAt → nach id
    ps = [pos("b", "OFFER", 5, "2026-11-18T09:00:00Z"), pos("a", "OFFER", 5, "2026-11-18T09:00:00Z"),
          pos("c", "OFFER", 5, "2026-11-18T09:00:00Z")]
    assert pro_rata(ps, D(10))["a"] == D("3.334")


def test_betraege_kaufmaennisch_gerundet():
    # 0,125 kWh × 0,14 = 0,0175 → 0,02
    r = settle([pos("o", "OFFER", "0.125", "2026-11-18T09:00:00Z"), pos("d", "DEMAND", "0.125", "2026-11-18T09:01:00Z")],
               Prices(D("0.1"), D("0.14"), D("0.2")), "DAY")
    sell = next(t for t in r.trades if t.direction == "PLATFORM_SELLS")
    assert sell.amount == D("0.02")
    buy = next(t for t in r.trades if t.direction == "PLATFORM_BUYS")
    assert buy.amount == D("0.01")  # 0,0125 → 0,01


def test_preisvorstellung_gleich_ankaufspreis_ist_handelbar():
    r = settle([pos("o", "OFFER", 10, "2026-11-18T09:00:00Z", "0.10"), pos("d", "DEMAND", 10, "2026-11-18T09:01:00Z")],
               PRICES, "DAY")
    assert next(a for a in r.allocations if a.position.id == "o").reason == "FULLY_ALLOCATED"
