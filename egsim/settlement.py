"""Referenzregel „Gruppe 1“ aus dem Datenvertrag 0.3.1 (Decimal, keine Floats).

Seit 0.3 legt jede Plattform ihre Preis- und Zuteilungsregel selbst fest; verbindlich sind nur die Invarianten
(siehe verify.py). Diese Datei rechnet das nicht verbindliche Referenzbeispiel der Gruppe 1 nach:
feste Preise je Markt, bei Überangebot pro rata, Abrunden auf 0,001 kWh, Rest nach createdAt, Reserve pro rata.
Die Preisvorstellung ist nur informativ und spielt keine Rolle.

Genutzt vom Verifier (Option „Referenzregel Gruppe 1“) und vom Mock-Server.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Callable, Iterable

from egsim.timeutil import normalize_interval, parse_ts

STEP = Decimal("0.001")
CENT = Decimal("0.01")
ZERO = Decimal(0)


def floor_kwh(x: Decimal) -> Decimal:
    return x.quantize(STEP, rounding=ROUND_DOWN)


def round_eur(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def D(x) -> Decimal | None:
    if x is None:
        return None
    return x if isinstance(x, Decimal) else Decimal(str(x))


@dataclass(frozen=True)
class Pos:
    id: str
    energy_community_id: str
    type: str
    quantity: Decimal
    created_at: str
    interval_start: str | None = None
    price_expectation: Decimal | None = None

    @property
    def order_key(self):
        return (parse_ts(self.created_at), self.id)

    @classmethod
    def from_api(cls, p: dict) -> "Pos":
        return cls(
            id=p["id"], energy_community_id=p["energyCommunityId"], type=p["type"],
            quantity=D(p["quantityKwh"]), created_at=p["createdAt"],
            interval_start=normalize_interval(p.get("intervalStart")),
            price_expectation=D(p.get("priceExpectationEurPerKwh")),
        )


@dataclass
class Prices:
    buy: Decimal | None
    sell: Decimal | None
    reserve: Decimal | None

    @classmethod
    def from_api(cls, p: dict) -> "Prices":
        return cls(D(p.get("buyPriceEurPerKwh")), D(p.get("sellPriceEurPerKwh")), D(p.get("reservePriceEurPerKwh")))


@dataclass
class Allocation:
    position: Pos
    allocated: Decimal
    reason: str


@dataclass
class TradeRef:
    position: Pos
    direction: str  # PLATFORM_BUYS | PLATFORM_SELLS
    from_reserve: bool
    quantity: Decimal
    price: Decimal
    amount: Decimal


@dataclass
class ReserveRef:
    status: str
    required: Decimal
    dispatched: Decimal
    price: Decimal | None
    cost: Decimal


@dataclass
class IntervalRef:
    interval_start: str | None
    total_offered: Decimal
    tradable_offered: Decimal
    total_demand: Decimal
    total_purchased: Decimal
    total_sold: Decimal
    reserve: ReserveRef
    allocations: list[Allocation] = field(default_factory=list)
    trades: list[TradeRef] = field(default_factory=list)


@dataclass
class SettlementRef:
    intervals: list[IntervalRef]
    total_offered: Decimal
    tradable_offered: Decimal
    total_demand: Decimal
    total_purchased: Decimal
    total_sold: Decimal
    reserve: ReserveRef
    purchase_cost: Decimal
    sales_revenue: Decimal
    margin: Decimal
    avg_buy: Decimal | None = None      # MarketResult.prices: Durchschnittspreise, gewichtet nach Menge
    avg_sell: Decimal | None = None
    avg_reserve: Decimal | None = None

    @property
    def allocations(self) -> list[Allocation]:
        return [a for i in self.intervals for a in i.allocations]

    @property
    def trades(self) -> list[TradeRef]:
        return [t for i in self.intervals for t in i.trades]


def pro_rata(positions: list[Pos], total: Decimal) -> dict[str, Decimal]:
    """Verteilt `total` proportional zu den Mengen. Abrunden auf 0,001 kWh, Rest in 0,001-Schritten
    nach createdAt (bei Gleichstand id)."""
    weight = sum((p.quantity for p in positions), ZERO)
    if weight == 0 or total <= 0:
        return {p.id: ZERO for p in positions}
    shares = {p.id: floor_kwh(p.quantity * total / weight) for p in positions}
    rest = total - sum(shares.values(), ZERO)
    ordered = sorted(positions, key=lambda p: p.order_key)
    i = 0
    while rest >= STEP:
        shares[ordered[i % len(ordered)].id] += STEP
        rest -= STEP
        i += 1
    return shares


def reserve_status(required: Decimal, dispatched: Decimal) -> str:
    if required == 0:
        return "NOT_NEEDED"
    if dispatched >= required:
        return "COVERED"
    if dispatched == 0:
        return "UNAVAILABLE"
    return "PARTIAL"


def settle_interval(positions: list[Pos], prices: Prices, dispatched: Decimal | Callable[[Decimal], Decimal] | None,
                    interval_start: str | None = None) -> IntervalRef:
    """`dispatched`: vom Kraftwerk gelieferte Reserve. Zahl (aus dem Plattformergebnis), Funktion required->dispatched
    (Mock) oder None (= Reserve deckt vollständig)."""
    offers = [p for p in positions if p.type == "OFFER"]
    demands = [p for p in positions if p.type == "DEMAND"]
    S = sum((p.quantity for p in offers), ZERO)
    Dm = sum((p.quantity for p in demands), ZERO)

    allocs: list[Allocation] = []
    if Dm == 0:  # ohne Nachfrage weder Ankauf noch Reserve
        allocs += [Allocation(p, ZERO, "NOT_NEEDED") for p in offers]
        purchased = ZERO
    elif S <= Dm:
        allocs += [Allocation(p, p.quantity, "FULLY_ALLOCATED") for p in offers]
        purchased = S
    else:
        shares = pro_rata(offers, Dm)
        allocs += [Allocation(p, shares[p.id], "PARTIALLY_ALLOCATED" if shares[p.id] > 0 else "NOT_ALLOCATED")
                   for p in offers]
        purchased = Dm

    required = max(Dm - S, ZERO)
    if dispatched is None:
        disp = required
    elif callable(dispatched):
        disp = dispatched(required)
    else:
        disp = dispatched
    disp = min(max(D(disp), ZERO), required)
    status = reserve_status(required, disp)

    available = purchased + disp
    if Dm > 0 and available >= Dm:
        dem_alloc = {p.id: p.quantity for p in demands}
        dem_reason = "FULLY_ALLOCATED"
    else:
        dem_alloc = pro_rata(demands, available)
        dem_reason = "RESERVE_SHORTAGE"
    allocs += [Allocation(p, dem_alloc[p.id], dem_reason) for p in demands]

    trades: list[TradeRef] = []
    for a in allocs:
        if a.position.type == "OFFER" and a.allocated > 0:
            trades.append(TradeRef(a.position, "PLATFORM_BUYS", False, a.allocated, prices.buy,
                                   round_eur(a.allocated * prices.buy)))
    reserve_share = pro_rata(demands, disp)
    for p in demands:
        total = dem_alloc[p.id]
        res = min(reserve_share[p.id], total)
        from_offers = total - res
        if from_offers > 0:
            trades.append(TradeRef(p, "PLATFORM_SELLS", False, from_offers, prices.sell, round_eur(from_offers * prices.sell)))
        if res > 0:
            trades.append(TradeRef(p, "PLATFORM_SELLS", True, res, prices.sell, round_eur(res * prices.sell)))

    res_price = prices.reserve if required > 0 else None
    res_cost = round_eur(disp * prices.reserve) if disp > 0 and prices.reserve is not None else ZERO
    return IntervalRef(
        interval_start=interval_start,
        total_offered=sum((p.quantity for p in offers), ZERO),
        tradable_offered=S,
        total_demand=Dm,
        total_purchased=sum((t.quantity for t in trades if t.direction == "PLATFORM_BUYS"), ZERO),
        total_sold=sum(dem_alloc.values(), ZERO),
        reserve=ReserveRef(status, required, disp, res_price, res_cost),
        allocations=allocs,
        trades=trades,
    )


def _aggregate_reserve(parts: list[ReserveRef], prices: Prices) -> ReserveRef:
    required = sum((r.required for r in parts), ZERO)
    disp = sum((r.dispatched for r in parts), ZERO)
    if required == 0:
        status = "NOT_NEEDED"
    elif all(r.status in ("NOT_NEEDED", "COVERED") for r in parts):
        status = "COVERED"
    elif disp == 0:
        status = "UNAVAILABLE"
    else:
        status = "PARTIAL"
    cost = round_eur(disp * prices.reserve) if disp > 0 and prices.reserve is not None else ZERO
    return ReserveRef(status, required, disp, prices.reserve if required > 0 else None, cost)


def weighted_price(trades: list[TradeRef]) -> Decimal | None:
    """Durchschnittspreis gewichtet nach Menge, auf 5 Nachkommastellen gerundet; None ohne Geschäft."""
    qty = sum((t.quantity for t in trades), ZERO)
    if qty == 0:
        return None
    return (sum((t.quantity * t.price for t in trades), ZERO) / qty).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)


def settle(positions: Iterable[Pos], prices: Prices, resolution: str,
           dispatched: dict[str | None, Decimal] | Callable[[Decimal], Decimal] | None = None) -> SettlementRef:
    """Rechnet einen Markt ab. `positions` = alle beim Marktschluss CLOSED Positionen (nach Settlement PROCESSED)."""
    groups: dict[str | None, list[Pos]] = {}
    for p in positions:
        key = p.interval_start if resolution == "QUARTER_HOUR" else None
        groups.setdefault(key, []).append(p)
    intervals = []
    for key in sorted(groups, key=lambda k: k or ""):
        disp = dispatched.get(key) if isinstance(dispatched, dict) else dispatched
        intervals.append(settle_interval(groups[key], prices, disp, key))
    trades = [t for i in intervals for t in i.trades]
    reserve = _aggregate_reserve([i.reserve for i in intervals], prices) if intervals else \
        ReserveRef("NOT_NEEDED", ZERO, ZERO, None, ZERO)
    purchase = sum((t.amount for t in trades if t.direction == "PLATFORM_BUYS"), ZERO)
    sales = sum((t.amount for t in trades if t.direction == "PLATFORM_SELLS"), ZERO)
    return SettlementRef(
        intervals=intervals,
        total_offered=sum((i.total_offered for i in intervals), ZERO),
        tradable_offered=sum((i.tradable_offered for i in intervals), ZERO),
        total_demand=sum((i.total_demand for i in intervals), ZERO),
        total_purchased=sum((i.total_purchased for i in intervals), ZERO),
        total_sold=sum((i.total_sold for i in intervals), ZERO),
        reserve=reserve,
        purchase_cost=purchase,
        sales_revenue=sales,
        margin=sales - purchase - reserve.cost,
        avg_buy=weighted_price([t for t in trades if t.direction == "PLATFORM_BUYS"]),
        avg_sell=weighted_price([t for t in trades if t.direction == "PLATFORM_SELLS"]),
        avg_reserve=reserve.price if reserve.dispatched > 0 else None,
    )
