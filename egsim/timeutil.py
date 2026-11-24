"""Zeitfunktionen nach Datenvertrag: Liefertag in Europe/Vienna, Intervalle in UTC."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

VIENNA = ZoneInfo("Europe/Vienna")
QUARTER = timedelta(minutes=15)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INTERVAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:(00|15|30|45):00(\.0+)?Z$")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_vienna(now: datetime | None = None) -> date:
    return (now or now_utc()).astimezone(VIENNA).date()


def resolve_delivery_date(spec: str, today: date | None = None) -> date:
    """'today', 'tomorrow', '+N'/'-N' (Tage ab heute) oder 'YYYY-MM-DD'. Seit Vertrag 0.3 sind auch historische
    Liefertage handelbar, wenn der Betreiber einen Markt dafür anlegt."""
    today = today or today_vienna()
    s = str(spec).strip().lower()
    if s == "today":
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    if re.fullmatch(r"[+-]\d+", s):
        return today + timedelta(days=int(s))
    if _ISO_DATE.match(s):
        return date.fromisoformat(s)
    raise ValueError(f"Ungültiger Liefertag '{spec}' (erlaubt: today, tomorrow, +N, -N, YYYY-MM-DD, open)")


def day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0, 0), VIENNA).astimezone(timezone.utc)
    end = datetime.combine(d + timedelta(days=1), time(0, 0), VIENNA).astimezone(timezone.utc)
    return start, end


def quarter_hours(d: date) -> list[datetime]:
    """Alle Viertelstunden-Starts (UTC) des Liefertags. 96, an Umstellungstagen 92 bzw. 100."""
    start, end = day_bounds_utc(d)
    out, t = [], start
    while t < end:
        out.append(t)
        t += QUARTER
    return out


def format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    v = value.strip()
    if v.endswith("Z") or v.endswith("z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_interval(value: str | None) -> str | None:
    """Vergleichbare Form eines intervalStart (z. B. '.000Z' -> 'Z')."""
    if value is None:
        return None
    return format_utc(parse_ts(value))


def is_interval_format(value: str) -> bool:
    return bool(_INTERVAL.match(value))


def is_iso_date(value: str) -> bool:
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def local_hour(dt: datetime) -> float:
    """Uhrzeit in Wien als Dezimalstunde (für Lastprofile)."""
    loc = dt.astimezone(VIENNA)
    return loc.hour + loc.minute / 60
