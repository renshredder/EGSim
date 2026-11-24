"""In-Memory-Mock der Handelsplattform (Datenvertrag 0.3.1) – nur zum Testen des Simulators.

Achtung: Der Mock rechnet mit derselben Referenzlogik wie der Verifier. Gegen den Mock bestätigt die Ergebnisprüfung
also nur, dass der Simulator-Ablauf funktioniert, nicht die Richtigkeit der Logik. Dafür gibt es die Unit-Tests
mit den Beispielen aus dem Vertrag.

Der Mock rechnet nach dem Referenzbeispiel von Gruppe 1 (feste Preise, pro rata bei Überangebot,
ruleVersion "gruppe1-prorata-1.0").

Nicht im Vertrag: POST /_mock/markets legt einen Markt an (das macht in echt der Betreiber).
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from egsim import CONTRACT_VERSION
from egsim.client import _to_jsonable
from egsim.settlement import Pos, Prices, settle
from egsim.timeutil import (day_bounds_utc, format_utc, is_interval_format, is_iso_date, normalize_interval, now_utc,
                            parse_ts, resolve_delivery_date, today_vienna)

log = logging.getLogger("egsim.mock")

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CODE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
RULE_VERSION = "gruppe1-prorata-1.0"
TRANSITIONS = {("PENDING", "ACTIVE"), ("PENDING", "SUSPENDED"), ("ACTIVE", "SUSPENDED"), ("SUSPENDED", "ACTIVE")}

MESSAGES = {
    "VALIDATION_FAILED": "At least one field is invalid.",
    "INVALID_ID": "The ID is not a valid UUID.",
    "INVALID_DELIVERY_DATE": "deliveryDate must be in the format YYYY-MM-DD.",
    "INVALID_INTERVAL": "intervalStart is missing, not allowed or not a quarter hour of the delivery day.",
    "INVALID_POSITION_TYPE": "type must be OFFER or DEMAND.",
    "INVALID_QUANTITY": "quantityKwh must be greater than 0 (max. 3 decimal places).",
    "INVALID_PRICE": "The price must be between 0 and 100 EUR/kWh (max. 5 decimal places).",
    "UNAUTHORIZED": "API key or token is missing, invalid or expired.",
    "INVALID_CREDENTIALS": "Email or password is wrong.",
    "FORBIDDEN": "You are not allowed to access this resource or perform this action.",
    "ENERGY_COMMUNITY_NOT_ACTIVE": "The energy community is not active (pending approval or suspended).",
    "ENERGY_COMMUNITY_NOT_FOUND": "The energy community does not exist.",
    "POSITION_NOT_FOUND": "The position does not exist.",
    "MARKET_NOT_FOUND": "No market exists for the delivery date.",
    "EMAIL_ALREADY_REGISTERED": "An energy community with this email is already registered.",
    "ENERGY_COMMUNITY_IN_USE": "The energy community cannot be deleted because it has positions or trades.",
    "INVALID_STATUS_TRANSITION": "This status change is not allowed.",
    "POSITION_ALREADY_EXISTS": "A position already exists for this energy community, delivery date and interval.",
    "POSITION_NOT_MODIFIABLE": "The position is cancelled or already processed and can no longer be changed.",
    "MARKET_CLOSED": "The market is already closed.",
    "MARKET_NOT_SETTLED": "The market result is not available yet.",
    "COMMUNITY_CODE_ALREADY_REGISTERED": "An energy community with this communityCode is already registered.",
    "IDEMPOTENCY_KEY_REUSED": "The Idempotency-Key was already used with a different request.",
    "RATE_LIMITED": "Too many requests. Try again later.",
    "INTERNAL_ERROR": "Internal error.",
}
STATUS_OF = {"VALIDATION_FAILED": 400, "INVALID_ID": 400, "INVALID_DELIVERY_DATE": 400, "INVALID_INTERVAL": 400,
             "INVALID_POSITION_TYPE": 400, "INVALID_QUANTITY": 400, "INVALID_PRICE": 400, "UNAUTHORIZED": 401,
             "INVALID_CREDENTIALS": 401, "FORBIDDEN": 403, "ENERGY_COMMUNITY_NOT_ACTIVE": 403,
             "ENERGY_COMMUNITY_NOT_FOUND": 404, "POSITION_NOT_FOUND": 404, "MARKET_NOT_FOUND": 404,
             "EMAIL_ALREADY_REGISTERED": 409, "ENERGY_COMMUNITY_IN_USE": 409, "INVALID_STATUS_TRANSITION": 409,
             "POSITION_ALREADY_EXISTS": 409, "POSITION_NOT_MODIFIABLE": 409, "MARKET_CLOSED": 409,
             "MARKET_NOT_SETTLED": 409, "COMMUNITY_CODE_ALREADY_REGISTERED": 409, "IDEMPOTENCY_KEY_REUSED": 409,
             "RATE_LIMITED": 429, "INTERNAL_ERROR": 500}


DEMO_NAMES = ["EG Sonnenfeld", "EG Windpark Nord", "EG Altstadt", "Gewerbepark Süd", "EG Dorfmitte"]


class ApiErr(Exception):
    def __init__(self, code: str, details: list[dict] | None = None, message: str | None = None,
                 retry_after: int | None = None):
        self.code, self.details = code, details
        self.status = STATUS_OF[code]
        self.message = message or MESSAGES[code]
        self.retry_after = retry_after

    def body(self) -> dict:
        b = {"code": self.code, "message": self.message}
        if self.details:
            b["details"] = self.details
        if self.retry_after is not None:
            b["_retryAfter"] = self.retry_after  # wird in respond() zum Header Retry-After
        return b


def _ts() -> str:
    return now_utc().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, Decimal)) and not isinstance(v, bool)


def _decimals(v: Any) -> int:
    exp = Decimal(v).as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def _detail(field: str, code: str, msg: str) -> dict:
    return {"field": f"/{field}", "code": code, "message": msg}


def _check_uuid(v: Any) -> str:
    if not isinstance(v, str) or not UUID_RE.match(v):
        raise ApiErr("INVALID_ID")
    return v.lower()


def _canonical(raw: bytes) -> str:
    """Request-Body als kanonisches JSON (Feldreihenfolge, Leerzeichen und 120 vs. 120.0 egal)."""
    def norm(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: norm(x) for k, x in v.items()}
        if isinstance(v, list):
            return [norm(x) for x in v]
        if isinstance(v, (int, Decimal)) and not isinstance(v, bool):
            d = Decimal(v).normalize()
            return {"$num": format(d, "f")}
        return v
    try:
        return json.dumps(norm(json.loads(raw or b"null", parse_float=Decimal)), sort_keys=True)
    except ValueError:
        return "raw:" + raw.decode("utf-8", "replace")


def _quantity_err(v: Any) -> str | None:
    if not _is_num(v):
        return "VALIDATION_FAILED"
    d = Decimal(v)
    if d <= 0 or d > 1_000_000 or _decimals(d) > 3:
        return "INVALID_QUANTITY"
    return None


def _price_err(v: Any) -> str | None:
    if not _is_num(v):
        return "VALIDATION_FAILED"
    d = Decimal(v)
    if d < 0 or d > 100 or _decimals(d) > 5:
        return "INVALID_PRICE"
    return None


class MockPlatform:
    def __init__(self, admin_api_key: str = "admin-key", admin_email: str = "admin@platform.example",
                 admin_password: str = "admin-password", reserve_capacity: Decimal | None = None,
                 settlement_delay: float = 0.0, login_rate_limit: int | None = 10, retry_after: int = 30) -> None:
        self.lock = threading.RLock()
        self.admin_key_hash = _hash(admin_api_key)
        self.admin_email = admin_email.lower()
        self.admin_pw_hash = _hash(admin_password)
        self.reserve_capacity = reserve_capacity
        self.settlement_delay = settlement_delay
        self.ecs: dict[str, dict] = {}
        self.secrets: dict[str, dict] = {}  # ec_id -> {apiKey, password} (gehasht)
        self.tokens: dict[str, tuple[str, str | None, float]] = {}
        self.positions: dict[str, dict] = {}
        self.markets: dict[str, dict] = {}
        self.results: dict[str, dict] = {}
        self.idempotency: dict[tuple, tuple[str, int, Any]] = {}  # (Identität, Key) -> (Inhalt, Status, Body)
        # Login: nach login_rate_limit Fehlversuchen je E-Mail für retry_after Sekunden gesperrt (None = aus)
        self.login_rate_limit = login_rate_limit
        self.retry_after = retry_after
        self.login_failures: dict[str, list[float]] = {}

    # ------------------------------------------------------------------ setup
    def add_market(self, d: str, resolution: str = "DAY", buy: str = "0.10", sell: str = "0.14", reserve: str = "0.20",
                   closes_at: datetime | None = None, show_prices: bool = True) -> dict:
        """show_prices=False: Markt ohne Preisindikation (Market.prices ist ab 0.3 optional); gerechnet wird intern
        trotzdem mit den Preisen."""
        closes_at = closes_at or (now_utc() + timedelta(hours=2))
        m = {
            "deliveryDate": d, "resolution": resolution, "status": "OPEN",
            "prices": {"deliveryDate": d, "buyPriceEurPerKwh": Decimal(buy), "sellPriceEurPerKwh": Decimal(sell),
                       "reservePriceEurPerKwh": Decimal(reserve)},
            "closesAt": format_utc(closes_at), "closedAt": None, "settledAt": None, "failureReason": None,
        }
        if not show_prices:
            m["_hidePrices"] = True
        with self.lock:
            self.markets[d] = m
        return m

    @staticmethod
    def _market_public(m: dict) -> dict:
        out = {k: v for k, v in m.items() if not k.startswith("_")}
        if m.get("_hidePrices"):
            out.pop("prices", None)
        return out

    # ------------------------------------------------------------------- auth
    def _auth(self, headers: dict[str, str]) -> tuple[str, dict | None, str | None]:
        key = headers.get("x-api-key")
        authz = headers.get("authorization", "")
        token = authz[7:].strip() if authz.lower().startswith("bearer ") else None
        if key:
            h = _hash(key)
            if secrets.compare_digest(h, self.admin_key_hash):
                return "ADMIN", None, None
            ec = next((self.ecs[i] for i, s in self.secrets.items() if s.get("apiKey") == h and i in self.ecs), None)
            if not ec:
                raise ApiErr("UNAUTHORIZED")
            role, ec_ = "ENERGY_COMMUNITY", ec
        elif token:
            t = self.tokens.get(token)
            if not t or t[2] < time.time():
                raise ApiErr("UNAUTHORIZED")
            if t[0] == "ADMIN":
                return "ADMIN", None, token
            ec_ = self.ecs.get(t[1])
            if not ec_:
                raise ApiErr("UNAUTHORIZED")
            role = "ENERGY_COMMUNITY"
        else:
            raise ApiErr("UNAUTHORIZED")
        if ec_["status"] != "ACTIVE":
            raise ApiErr("ENERGY_COMMUNITY_NOT_ACTIVE")
        return role, ec_, token

    @staticmethod
    def _need(role: str, *allowed: str) -> None:
        if role not in allowed:
            raise ApiErr("FORBIDDEN")

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _body(raw: bytes) -> Any:
        if not raw:
            raise ApiErr("VALIDATION_FAILED", [_detail("", "VALIDATION_FAILED", "request body is required")])
        try:
            return json.loads(raw, parse_float=Decimal)
        except ValueError:
            raise ApiErr("VALIDATION_FAILED", [_detail("", "VALIDATION_FAILED", "malformed JSON")])

    @staticmethod
    def _fields(body: Any, allowed: set[str], required: set[str]) -> None:
        if not isinstance(body, dict):
            raise ApiErr("VALIDATION_FAILED", [_detail("", "VALIDATION_FAILED", "JSON object expected")])
        details = [_detail(k, "VALIDATION_FAILED", "unknown field") for k in body if k not in allowed]
        details += [_detail(k, "VALIDATION_FAILED", "required") for k in sorted(required) if k not in body]
        if details:
            raise ApiErr("VALIDATION_FAILED", details)

    def _ec_public(self, ec: dict) -> dict:
        return {k: ec[k] for k in ("id", "email", "communityCode", "name", "description", "status", "createdAt",
                                   "updatedAt") if k in ec}

    def _market(self, d: str) -> dict:
        m = self.markets.get(d)
        if not m:
            raise ApiErr("MARKET_NOT_FOUND")
        return m

    @staticmethod
    def _market_closed(m: dict) -> bool:
        return m["status"] != "OPEN" or now_utc() >= parse_ts(m["closesAt"])

    @staticmethod
    def _check_date(v: Any) -> str:
        if not is_iso_date(v):
            raise ApiErr("INVALID_DELIVERY_DATE")
        return v

    # ---------------------------------------------------------------- routing
    ROUTES = [
        ("GET", r"/health/(live|ready)", "health"),
        ("POST", r"/auth/login", "login"),
        ("POST", r"/auth/logout", "logout"),
        ("GET", r"/me", "me"),
        ("POST", r"/energy-communities", "register"),
        ("GET", r"/energy-communities", "list_ecs"),
        ("GET", r"/energy-communities/([^/]+)", "get_ec"),
        ("PUT", r"/energy-communities/([^/]+)", "put_ec"),
        ("DELETE", r"/energy-communities/([^/]+)", "delete_ec"),
        ("POST", r"/energy-communities/([^/]+)/credentials", "credentials"),
        ("PUT", r"/energy-communities/([^/]+)/status", "set_status"),
        ("POST", r"/positions", "submit"),
        ("GET", r"/positions", "list_positions"),
        ("GET", r"/positions/([^/]+)", "get_position"),
        ("PUT", r"/positions/([^/]+)", "put_position"),
        ("DELETE", r"/positions/([^/]+)", "cancel_position"),
        ("GET", r"/markets", "list_markets"),
        ("GET", r"/markets/([^/]+)", "get_market"),
        ("POST", r"/markets/([^/]+)/close", "close"),
        ("GET", r"/markets/([^/]+)/result", "result"),
        ("GET", r"/markets/([^/]+)/allocations", "allocations"),
        ("GET", r"/markets/([^/]+)/trades", "trades"),
        ("POST", r"/_mock/markets", "mock_market"),
        ("GET", r"/_mock/markets", "mock_markets"),
        ("POST", r"/_mock/markets/([^/]+)/close", "mock_close"),
        ("POST", r"/_mock/energy-communities/([^/]+)/approve", "mock_approve"),
    ]

    def respond(self, method: str, path: str, query: dict[str, str], headers: dict[str, str],
                raw: bytes) -> tuple[int, Any, dict[str, str]]:
        """handle() plus die Header aus Vertrag 0.3: X-Request-Id, Api-Version, requestId im Fehler, Retry-After."""
        rid = headers.get("x-request-id")
        if not rid or not (1 <= len(rid) <= 128):
            rid = str(uuid.uuid4())
        status, body = self.handle(method, path, query, headers, raw)
        out = {"X-Request-Id": rid, "Api-Version": CONTRACT_VERSION}
        if status >= 400 and isinstance(body, dict):
            body = dict(body)
            ra = body.pop("_retryAfter", None)
            if ra is not None:
                out["Retry-After"] = str(ra)
            body["requestId"] = rid
        return status, body, out

    def handle(self, method: str, path: str, query: dict[str, str], headers: dict[str, str], raw: bytes) -> tuple[int, Any]:
        path = path.rstrip("/") or "/"
        known_path = False
        for m, pattern, name in self.ROUTES:
            mt = re.fullmatch(pattern, path)
            if not mt:
                continue
            known_path = True
            if m != method:
                continue
            try:
                with self.lock:
                    return getattr(self, "h_" + name)(*mt.groups(), q=query, h=headers, raw=raw)
            except ApiErr as e:
                return e.status, e.body()
            except Exception:  # noqa: BLE001
                log.exception("Fehler im Mock")
                return 500, ApiErr("INTERNAL_ERROR").body()
        return (405 if known_path else 404), {"code": "VALIDATION_FAILED", "message": f"No route for {method} {path}"}

    # ------------------------------------------------------------- idempotency
    @staticmethod
    def _idem_key(h: dict, required: bool) -> str | None:
        k = h.get("idempotency-key")
        if k is None:
            if required:
                raise ApiErr("VALIDATION_FAILED", [_detail("Idempotency-Key", "VALIDATION_FAILED", "header required")])
            return None
        if not UUID_RE.match(k):
            raise ApiErr("VALIDATION_FAILED", [_detail("Idempotency-Key", "VALIDATION_FAILED", "must be a UUID")])
        return k.lower()

    def _idempotent(self, ident: tuple, key: str | None, raw: bytes, fn) -> tuple[int, Any]:
        """Gleicher Key + gleicher Inhalt: gespeicherte Antwort; anderer Inhalt: 409 IDEMPOTENCY_KEY_REUSED.
        Gespeichert wird die erste endgültige Antwort (2xx oder 4xx)."""
        if key is None:
            return fn()
        canon = _canonical(raw)
        stored = self.idempotency.get((ident, key))
        if stored:
            if stored[0] != canon:
                raise ApiErr("IDEMPOTENCY_KEY_REUSED")
            return stored[1], copy.deepcopy(stored[2])
        try:
            status, body = fn()
        except ApiErr as e:
            if e.status < 500:
                self.idempotency[(ident, key)] = (canon, e.status, e.body())
            raise
        if status < 500:
            self.idempotency[(ident, key)] = (canon, status, copy.deepcopy(body))
        return status, body

    # -------------------------------------------------------------- endpoints
    def h_health(self, _kind, **_):
        return 200, {"status": "UP", "checks": {"database": "UP"}}

    def h_login(self, q, h, raw):
        email_guess = None
        try:
            b0 = json.loads(raw or b"{}")
            email_guess = b0.get("email", "").lower() if isinstance(b0, dict) and isinstance(b0.get("email"), str) else None
        except ValueError:
            pass
        if email_guess and self.login_rate_limit:
            now = time.time()
            fails = [t for t in self.login_failures.get(email_guess, []) if now - t < self.retry_after]
            self.login_failures[email_guess] = fails
            if len(fails) >= self.login_rate_limit:
                raise ApiErr("RATE_LIMITED", retry_after=max(1, int(self.retry_after - (now - fails[0])) + 1))
        try:
            return self._login(raw)
        except ApiErr as e:
            if e.code == "INVALID_CREDENTIALS" and email_guess:
                self.login_failures.setdefault(email_guess, []).append(time.time())
            raise

    def _login(self, raw):
        b = self._body(raw)
        self._fields(b, {"email", "password"}, {"email", "password"})
        if not isinstance(b["email"], str) or not isinstance(b["password"], str) or len(b["password"]) > 128 \
                or not b["password"]:
            raise ApiErr("VALIDATION_FAILED", [_detail("password", "VALIDATION_FAILED", "invalid")])
        email = b["email"].lower()
        if email == self.admin_email and _hash(b["password"]) == self.admin_pw_hash:
            tok = secrets.token_urlsafe(24)
            self.tokens[tok] = ("ADMIN", None, time.time() + 3600)
            return 200, {"token": tok, "tokenType": "Bearer", "expiresIn": 3600, "role": "ADMIN"}
        ec = next((e for e in self.ecs.values() if e["email"].lower() == email), None)
        if not ec or self.secrets.get(ec["id"], {}).get("password") != _hash(b["password"]):
            raise ApiErr("INVALID_CREDENTIALS")
        if ec["status"] != "ACTIVE":
            raise ApiErr("ENERGY_COMMUNITY_NOT_ACTIVE")
        tok = secrets.token_urlsafe(24)
        self.tokens[tok] = ("ENERGY_COMMUNITY", ec["id"], time.time() + 3600)
        return 200, {"token": tok, "tokenType": "Bearer", "expiresIn": 3600, "role": "ENERGY_COMMUNITY",
                     "energyCommunity": self._ec_public(ec)}

    def h_logout(self, q, h, raw):
        _, _, token = self._auth(h)
        if not token:
            raise ApiErr("UNAUTHORIZED")
        self.tokens.pop(token, None)
        return 204, None

    def h_me(self, q, h, raw):
        role, ec, _ = self._auth(h)
        return 200, {"role": role, **({"energyCommunity": self._ec_public(ec)} if ec else {})}

    def _validate_profile(self, b: dict, details: list) -> None:
        if "name" in b:
            n = b["name"]
            if not isinstance(n, str) or not (1 <= len(n) <= 120) or not n.strip():
                details.append(_detail("name", "VALIDATION_FAILED", "1-120 characters, not only whitespace"))
        if "description" in b:
            dsc = b["description"]
            if not isinstance(dsc, str) or not (1 <= len(dsc) <= 500):
                details.append(_detail("description", "VALIDATION_FAILED", "1-500 characters"))

    def h_register(self, q, h, raw):
        key = self._idem_key(h, required=False)
        return self._idempotent(("register",), key, raw, lambda: self._register(raw))

    def _register(self, raw):
        b = self._body(raw)
        self._fields(b, {"email", "communityCode", "name", "description"}, {"email", "communityCode", "name"})
        details: list = []
        if not isinstance(b["email"], str) or len(b["email"]) > 254 or not EMAIL_RE.match(b["email"]):
            details.append(_detail("email", "VALIDATION_FAILED", "invalid email"))
        code = b["communityCode"]
        if not isinstance(code, str) or not (3 <= len(code) <= 40) or not CODE_RE.match(code):
            details.append(_detail("communityCode", "VALIDATION_FAILED",
                                   "3-40 characters: lowercase letters, digits, single hyphens"))
        self._validate_profile(b, details)
        if details:
            raise ApiErr("VALIDATION_FAILED", details)
        if any(e["email"].lower() == b["email"].lower() for e in self.ecs.values()):
            raise ApiErr("EMAIL_ALREADY_REGISTERED")
        if any(e.get("communityCode") == code for e in self.ecs.values()):
            raise ApiErr("COMMUNITY_CODE_ALREADY_REGISTERED")
        ts = _ts()
        ec = {"id": str(uuid.uuid4()), "email": b["email"], "communityCode": code, "name": b["name"],
              "status": "PENDING", "createdAt": ts, "updatedAt": ts}
        if "description" in b:
            ec["description"] = b["description"]
        self.ecs[ec["id"]] = ec
        return 201, self._ec_public(ec)

    def h_list_ecs(self, q, h, raw):
        role, _, _ = self._auth(h)
        self._need(role, "ADMIN")
        st = q.get("status")
        if st is not None and st not in ("PENDING", "ACTIVE", "SUSPENDED"):
            raise ApiErr("VALIDATION_FAILED", [_detail("status", "VALIDATION_FAILED", "invalid status")])
        return 200, [self._ec_public(e) for e in self.ecs.values() if st is None or e["status"] == st]

    def _load_ec(self, role: str, ec: dict | None, raw_id: str) -> dict:
        ec_id = _check_uuid(raw_id)
        target = self.ecs.get(ec_id)
        if not target:
            raise ApiErr("ENERGY_COMMUNITY_NOT_FOUND")
        if role != "ADMIN" and target["id"] != ec["id"]:
            raise ApiErr("FORBIDDEN")
        return target

    def h_get_ec(self, raw_id, q, h, raw):
        role, ec, _ = self._auth(h)
        return 200, self._ec_public(self._load_ec(role, ec, raw_id))

    def h_put_ec(self, raw_id, q, h, raw):
        role, ec, _ = self._auth(h)
        ec_id = _check_uuid(raw_id)
        b = self._body(raw)
        self._fields(b, {"name", "description"}, set())
        details: list = []
        if not b:
            details.append(_detail("", "VALIDATION_FAILED", "at least one property"))
        self._validate_profile(b, details)
        if details:
            raise ApiErr("VALIDATION_FAILED", details)
        target = self._load_ec(role, ec, ec_id)
        target.update(b)
        target["updatedAt"] = _ts()
        return 200, self._ec_public(target)

    def h_delete_ec(self, raw_id, q, h, raw):
        role, ec, _ = self._auth(h)
        self._need(role, "ADMIN")
        target = self._load_ec(role, ec, raw_id)
        if any(p["energyCommunityId"] == target["id"] for p in self.positions.values()):
            raise ApiErr("ENERGY_COMMUNITY_IN_USE")
        del self.ecs[target["id"]]
        self.secrets.pop(target["id"], None)
        self.tokens = {k: v for k, v in self.tokens.items() if v[1] != target["id"]}
        return 204, None

    def h_credentials(self, raw_id, q, h, raw):
        role, ec, _ = self._auth(h)
        self._need(role, "ADMIN")
        target = self._load_ec(role, ec, raw_id)
        api_key, pw = "egk_" + secrets.token_urlsafe(24), secrets.token_urlsafe(12)
        self.secrets[target["id"]] = {"apiKey": _hash(api_key), "password": _hash(pw)}
        self.tokens = {k: v for k, v in self.tokens.items() if v[1] != target["id"]}
        return 201, {"apiKey": api_key, "password": pw}

    def h_set_status(self, raw_id, q, h, raw):
        role, ec, _ = self._auth(h)
        self._need(role, "ADMIN")
        ec_id = _check_uuid(raw_id)
        b = self._body(raw)
        self._fields(b, {"status"}, {"status"})
        if b["status"] not in ("PENDING", "ACTIVE", "SUSPENDED"):
            raise ApiErr("VALIDATION_FAILED", [_detail("status", "VALIDATION_FAILED", "invalid status")])
        target = self._load_ec(role, ec, ec_id)
        if target["status"] != b["status"]:
            if (target["status"], b["status"]) not in TRANSITIONS:
                raise ApiErr("INVALID_STATUS_TRANSITION")
            target["status"] = b["status"]
            target["updatedAt"] = _ts()
        return 200, self._ec_public(target)

    # -------------------------------------------------------------- positions
    def _validate_interval(self, m: dict, v: Any) -> str | None:
        if m["resolution"] == "DAY":
            if v is not None:
                raise ApiErr("INVALID_INTERVAL")
            return None
        if v is None or not isinstance(v, str) or not is_interval_format(v):
            raise ApiErr("INVALID_INTERVAL")
        start, end = day_bounds_utc(date.fromisoformat(m["deliveryDate"]))
        t = parse_ts(v)
        if not (start <= t < end):
            raise ApiErr("INVALID_INTERVAL")
        return format_utc(t)

    def h_submit(self, q, h, raw):
        role, ec, _ = self._auth(h)
        self._need(role, "ENERGY_COMMUNITY")
        key = self._idem_key(h, required=True)
        return self._idempotent(("ec", ec["id"]), key, raw, lambda: self._submit(ec, raw))

    def _submit(self, ec: dict, raw: bytes):
        b = self._body(raw)
        allowed = {"energyCommunityId", "deliveryDate", "intervalStart", "type", "quantityKwh", "priceExpectationEurPerKwh"}
        self._fields(b, allowed, {"deliveryDate", "type", "quantityKwh"})
        if "energyCommunityId" in b:
            _check_uuid(b["energyCommunityId"])
        d = self._check_date(b["deliveryDate"])
        if b["type"] not in ("OFFER", "DEMAND"):
            raise ApiErr("INVALID_POSITION_TYPE")
        qe = _quantity_err(b["quantityKwh"])
        if qe:
            raise ApiErr(qe, [_detail("quantityKwh", qe, "invalid quantity")])
        price = b.get("priceExpectationEurPerKwh")
        if "priceExpectationEurPerKwh" in b:
            if b["type"] == "DEMAND":
                raise ApiErr("VALIDATION_FAILED", [_detail("priceExpectationEurPerKwh", "VALIDATION_FAILED",
                                                           "only allowed for OFFER")])
            pe = _price_err(price)
            if pe:
                raise ApiErr(pe, [_detail("priceExpectationEurPerKwh", pe, "invalid price")])
        if "intervalStart" in b and b["intervalStart"] is not None and not (
                isinstance(b["intervalStart"], str) and is_interval_format(b["intervalStart"])):
            raise ApiErr("INVALID_INTERVAL")
        # 404
        if "energyCommunityId" in b and b["energyCommunityId"].lower() not in self.ecs:
            raise ApiErr("ENERGY_COMMUNITY_NOT_FOUND")
        m = self._market(d)
        interval = self._validate_interval(m, b.get("intervalStart"))
        # 403 eigene Ressource
        if "energyCommunityId" in b and b["energyCommunityId"].lower() != ec["id"]:
            raise ApiErr("FORBIDDEN", message="energyCommunityId does not belong to the authenticated energy community.")
        # 409 (historische Liefertage sind erlaubt, entscheidend ist nur der Markt)
        if self._market_closed(m):
            raise ApiErr("MARKET_CLOSED")
        if any(p["energyCommunityId"] == ec["id"] and p["deliveryDate"] == d and p["intervalStart"] == interval
               and p["status"] != "CANCELLED" for p in self.positions.values()):
            raise ApiErr("POSITION_ALREADY_EXISTS")
        ts = _ts()
        p = {"id": str(uuid.uuid4()), "energyCommunityId": ec["id"], "deliveryDate": d, "intervalStart": interval,
             "type": b["type"], "quantityKwh": Decimal(b["quantityKwh"]), "status": "OPEN", "rejectionReason": None,
             "createdAt": ts, "updatedAt": ts}
        if price is not None:
            p["priceExpectationEurPerKwh"] = Decimal(price)
        self.positions[p["id"]] = p
        return 201, p

    def _filter_ec(self, role: str, ec: dict | None, q: dict) -> str | None:
        f = q.get("energyCommunityId")
        if f is not None:
            f = _check_uuid(f)
        if role != "ADMIN":
            if f is not None and f != ec["id"]:
                raise ApiErr("FORBIDDEN", message="energyCommunityId does not belong to the authenticated energy community.")
            return ec["id"]
        return f

    def _filter_interval(self, q: dict) -> str | None:
        v = q.get("intervalStart")
        if v is None:
            return None
        if not is_interval_format(v):
            raise ApiErr("INVALID_INTERVAL")
        return normalize_interval(v)

    def h_list_positions(self, q, h, raw):
        role, ec, _ = self._auth(h)
        if "deliveryDate" in q:
            self._check_date(q["deliveryDate"])
        if "type" in q and q["type"] not in ("OFFER", "DEMAND"):
            raise ApiErr("INVALID_POSITION_TYPE")
        if "status" in q and q["status"] not in ("OPEN", "CANCELLED", "CLOSED", "PROCESSED", "REJECTED"):
            raise ApiErr("VALIDATION_FAILED", [_detail("status", "VALIDATION_FAILED", "invalid status")])
        iv = self._filter_interval(q)
        f = self._filter_ec(role, ec, q)
        out = [p for p in self.positions.values()
               if (f is None or p["energyCommunityId"] == f)
               and ("deliveryDate" not in q or p["deliveryDate"] == q["deliveryDate"])
               and (iv is None or p["intervalStart"] == iv)
               and ("type" not in q or p["type"] == q["type"])
               and ("status" not in q or p["status"] == q["status"])]
        return 200, sorted(out, key=lambda p: (p["createdAt"], p["id"]))

    def _load_position(self, role: str, ec: dict | None, raw_id: str) -> dict:
        pid = _check_uuid(raw_id)
        p = self.positions.get(pid)
        if not p:
            raise ApiErr("POSITION_NOT_FOUND")
        if role != "ADMIN" and p["energyCommunityId"] != ec["id"]:
            raise ApiErr("FORBIDDEN")
        return p

    def h_get_position(self, raw_id, q, h, raw):
        role, ec, _ = self._auth(h)
        return 200, self._load_position(role, ec, raw_id)

    def h_put_position(self, raw_id, q, h, raw):
        role, ec, _ = self._auth(h)
        self._need(role, "ENERGY_COMMUNITY")
        pid = _check_uuid(raw_id)
        b = self._body(raw)
        self._fields(b, {"quantityKwh", "priceExpectationEurPerKwh"}, {"quantityKwh"})
        qe = _quantity_err(b["quantityKwh"])
        if qe:
            raise ApiErr(qe, [_detail("quantityKwh", qe, "invalid quantity")])
        if "priceExpectationEurPerKwh" in b and b["priceExpectationEurPerKwh"] is not None:
            pe = _price_err(b["priceExpectationEurPerKwh"])
            if pe:
                raise ApiErr(pe, [_detail("priceExpectationEurPerKwh", pe, "invalid price")])
        p = self._load_position(role, ec, pid)
        if p["type"] == "DEMAND" and b.get("priceExpectationEurPerKwh") is not None:
            raise ApiErr("VALIDATION_FAILED", [_detail("priceExpectationEurPerKwh", "VALIDATION_FAILED",
                                                       "only allowed for OFFER")])
        if p["status"] != "OPEN":
            raise ApiErr("POSITION_NOT_MODIFIABLE")
        if self._market_closed(self._market(p["deliveryDate"])):
            raise ApiErr("MARKET_CLOSED")
        p["quantityKwh"] = Decimal(b["quantityKwh"])
        # PUT ersetzt: fehlt die Preisvorstellung, wird sie entfernt
        if b.get("priceExpectationEurPerKwh") is not None:
            p["priceExpectationEurPerKwh"] = Decimal(b["priceExpectationEurPerKwh"])
        else:
            p.pop("priceExpectationEurPerKwh", None)
        p["updatedAt"] = _ts()
        return 200, p

    def h_cancel_position(self, raw_id, q, h, raw):
        role, ec, _ = self._auth(h)
        self._need(role, "ENERGY_COMMUNITY")
        p = self._load_position(role, ec, raw_id)
        if p["status"] == "CANCELLED":  # schon storniert: 200 ohne Änderung
            return 200, p
        if p["status"] != "OPEN":
            raise ApiErr("POSITION_NOT_MODIFIABLE")
        if self._market_closed(self._market(p["deliveryDate"])):
            raise ApiErr("MARKET_CLOSED")
        p["status"] = "CANCELLED"
        p["updatedAt"] = _ts()
        return 200, p

    # ---------------------------------------------------------------- markets
    def h_list_markets(self, q, h, raw):
        self._auth(h)
        st = q.get("status")
        if st is not None and st not in ("OPEN", "CLOSED", "SETTLED", "FAILED"):
            raise ApiErr("VALIDATION_FAILED", [_detail("status", "VALIDATION_FAILED", "invalid status")])
        rows = sorted(self.markets.values(), key=lambda m: m["deliveryDate"])
        return 200, [self._market_public(m) for m in rows if st is None or m["status"] == st]

    def h_get_market(self, raw_d, q, h, raw):
        self._auth(h)
        return 200, self._market_public(self._market(self._check_date(raw_d)))

    def h_close(self, raw_d, q, h, raw):
        role, _, _ = self._auth(h)
        self._need(role, "ADMIN")
        m = self._market(self._check_date(raw_d))
        status, mk = self.close_market(m["deliveryDate"])
        return status, self._market_public(mk)

    def close_market(self, d: str) -> tuple[int, dict]:
        """Wie POST /markets/{d}/close durch den Admin (auch für Tests und die Mock-Steuerung)."""
        with self.lock:
            m = self._market(d)
            if m["status"] in ("OPEN", "FAILED"):
                if m["status"] == "OPEN":
                    ts = _ts()
                    for p in self.positions.values():
                        if p["deliveryDate"] == d and p["status"] == "OPEN":
                            owner = self.ecs.get(p["energyCommunityId"])
                            if owner and owner["status"] == "ACTIVE":
                                p["status"] = "CLOSED"
                            else:
                                p["status"], p["rejectionReason"] = "REJECTED", "ENERGY_COMMUNITY_NOT_ACTIVE"
                            p["updatedAt"] = ts
                m["status"], m["closedAt"], m["failureReason"] = "CLOSED", _ts(), None
                if self.settlement_delay > 0:
                    threading.Timer(self.settlement_delay, self._settle_locked, args=(d,)).start()
                else:
                    self._settle(d)
            return (200 if m["status"] == "SETTLED" else 202), m

    def add_demo_community(self, name: str, email: str | None = None, community_code: str | None = None) -> dict:
        """Aktive EG mit Zugangsdaten anlegen (übernimmt im Mock die Rolle des Admins)."""
        with self.lock:
            ts = _ts()
            slug = re.sub("[^a-z0-9]+", "-", name.lower()).strip("-") or "eg"
            code = community_code or slug[:40].strip("-")
            if len(code) < 3 or any(e.get("communityCode") == code for e in self.ecs.values()):
                code = f"{code[:33].strip('-')}-{uuid.uuid4().hex[:6]}"
            ec = {"id": str(uuid.uuid4()), "email": email or f"{slug}@demo.egsim.example", "communityCode": code,
                  "name": name, "description": "Demo-EG im Mock", "status": "ACTIVE", "createdAt": ts, "updatedAt": ts}
            self.ecs[ec["id"]] = ec
            api_key, pw = "egk_" + secrets.token_urlsafe(24), secrets.token_urlsafe(12)
            self.secrets[ec["id"]] = {"apiKey": _hash(api_key), "password": _hash(pw)}
            return {**ec, "apiKey": api_key, "password": pw}

    def approve(self, ec_id: str) -> dict:
        """Mock-Admin: EG freischalten und Zugangsdaten vergeben."""
        with self.lock:
            ec = self.ecs.get(ec_id)
            if not ec:
                raise ApiErr("ENERGY_COMMUNITY_NOT_FOUND")
            ec["status"], ec["updatedAt"] = "ACTIVE", _ts()
            api_key, pw = "egk_" + secrets.token_urlsafe(24), secrets.token_urlsafe(12)
            self.secrets[ec_id] = {"apiKey": _hash(api_key), "password": _hash(pw)}
            return {"apiKey": api_key, "password": pw}

    def auto_settle_loop(self, stop: threading.Event, interval: float = 2.0) -> None:
        """Betreiber spielen: Märkte nach closesAt automatisch schließen und abwickeln."""
        while not stop.wait(interval):
            for d, m in list(self.markets.items()):
                if m["status"] == "OPEN" and now_utc() >= parse_ts(m["closesAt"]):
                    log.info("Mock: schließe Markt %s (closesAt erreicht)", d)
                    self.close_market(d)

    def _settle_locked(self, d: str) -> None:
        with self.lock:
            self._settle(d)

    def _settle(self, d: str) -> None:
        m = self.markets[d]
        try:
            closed = [p for p in self.positions.values() if p["deliveryDate"] == d and p["status"] == "CLOSED"]
            prices = Prices.from_api(m["prices"])
            cap = self.reserve_capacity
            ref = settle([Pos.from_api(p) for p in closed], prices, m["resolution"],
                         (lambda req: min(req, cap)) if cap is not None else None)
        except Exception as exc:  # noqa: BLE001
            m["status"], m["failureReason"] = "FAILED", f"settlement error: {exc}"
            return
        ts = _ts()
        allocations = [{"deliveryDate": d, "intervalStart": a.position.interval_start, "positionId": a.position.id,
                        "energyCommunityId": a.position.energy_community_id, "type": a.position.type,
                        "requestedKwh": a.position.quantity, "allocatedKwh": a.allocated, "reason": a.reason}
                       for a in ref.allocations]
        trades = [{"id": str(uuid.uuid4()), "deliveryDate": d, "intervalStart": t.position.interval_start,
                   "positionId": t.position.id, "energyCommunityId": t.position.energy_community_id,
                   "direction": t.direction, "fromReserve": t.from_reserve, "quantityKwh": t.quantity,
                   "priceEurPerKwh": t.price, "amountEur": t.amount, "createdAt": ts} for t in ref.trades]

        def reserve(r):
            return {"deliveryDate": d, "status": r.status, "requiredKwh": r.required, "dispatchedKwh": r.dispatched,
                    "priceEurPerKwh": r.price, "costEur": r.cost}
        result = {"deliveryDate": d, "resolution": m["resolution"], "status": "SETTLED", "settledAt": ts,
                  "totalOfferedKwh": ref.total_offered,
                  "totalDemandKwh": ref.total_demand, "totalPurchasedKwh": ref.total_purchased,
                  "totalSoldKwh": ref.total_sold, "reserve": reserve(ref.reserve),
                  # Durchschnittspreise, gewichtet nach Menge; null ohne Geschäft in der Richtung
                  "prices": {"deliveryDate": d, "buyPriceEurPerKwh": ref.avg_buy, "sellPriceEurPerKwh": ref.avg_sell,
                             "reservePriceEurPerKwh": ref.avg_reserve},
                  "purchaseCostEur": ref.purchase_cost, "salesRevenueEur": ref.sales_revenue, "marginEur": ref.margin,
                  "ruleVersion": RULE_VERSION}
        if m["resolution"] == "QUARTER_HOUR":
            result["intervals"] = [{"intervalStart": i.interval_start, "totalOfferedKwh": i.total_offered,
                                    "totalDemandKwh": i.total_demand,
                                    "totalPurchasedKwh": i.total_purchased, "totalSoldKwh": i.total_sold,
                                    "reserve": reserve(i.reserve)} for i in ref.intervals]
        for p in closed:
            p["status"], p["updatedAt"] = "PROCESSED", ts
        self.results[d] = {"allocations": allocations, "trades": trades, "result": result}
        m["status"], m["settledAt"] = "SETTLED", ts

    def _settled(self, raw_d: str) -> tuple[str, dict]:
        d = self._check_date(raw_d)
        m = self._market(d)
        if m["status"] != "SETTLED":
            raise ApiErr("MARKET_NOT_SETTLED")
        return d, self.results[d]

    def h_result(self, raw_d, q, h, raw):
        role, _, _ = self._auth(h)
        self._need(role, "ADMIN")
        return 200, self._settled(raw_d)[1]["result"]

    def h_allocations(self, raw_d, q, h, raw):
        role, ec, _ = self._auth(h)
        self._check_date(raw_d)
        iv = self._filter_interval(q)
        f = self._filter_ec(role, ec, q)
        _, res = self._settled(raw_d)
        return 200, [a for a in res["allocations"] if (f is None or a["energyCommunityId"] == f)
                     and (iv is None or a["intervalStart"] == iv)]

    def h_trades(self, raw_d, q, h, raw):
        role, ec, _ = self._auth(h)
        self._check_date(raw_d)
        iv = self._filter_interval(q)
        if "direction" in q and q["direction"] not in ("PLATFORM_BUYS", "PLATFORM_SELLS"):
            raise ApiErr("VALIDATION_FAILED", [_detail("direction", "VALIDATION_FAILED", "invalid direction")])
        pid = _check_uuid(q["positionId"]) if "positionId" in q else None
        f = self._filter_ec(role, ec, q)
        _, res = self._settled(raw_d)
        return 200, [t for t in res["trades"] if (f is None or t["energyCommunityId"] == f)
                     and (iv is None or t["intervalStart"] == iv)
                     and ("direction" not in q or t["direction"] == q["direction"])
                     and (pid is None or t["positionId"] == pid)]

    def h_mock_markets(self, q, h, raw):
        return 200, [self._market_public(m) for m in self.markets.values()]

    def h_mock_close(self, raw_d, q, h, raw):
        status, m = self.close_market(self._check_date(raw_d))
        return status, self._market_public(m)

    def h_mock_approve(self, raw_id, q, h, raw):
        return 200, self.approve(_check_uuid(raw_id))

    def h_mock_market(self, q, h, raw):
        b = self._body(raw)
        d = resolve_delivery_date(b.get("deliveryDate", "tomorrow")).isoformat()
        closes = parse_ts(b["closesAt"]) if b.get("closesAt") else None
        m = self.add_market(d, b.get("resolution", "DAY"), str(b.get("buyPriceEurPerKwh", "0.10")),
                            str(b.get("sellPriceEurPerKwh", "0.14")), str(b.get("reservePriceEurPerKwh", "0.20")), closes,
                            show_prices=b.get("showPrices", True) is not False)
        return 201, self._market_public(m)


# ======================================================================================================================
def make_handler(platform: MockPlatform):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _do(self) -> None:
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, body, extra = platform.respond(self.command, u.path, q, headers, raw)
            data = b"" if body is None else json.dumps(_to_jsonable(body)).encode()
            self.send_response(status)
            for k, v in extra.items():
                self.send_header(k, v)
            if data:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = _do

        def log_message(self, fmt, *args):  # noqa: N802
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def parse_market_spec(spec: str) -> dict:
    parts = spec.split(":")
    d = resolve_delivery_date(parts[0]).isoformat()
    res = parts[1].upper() if len(parts) > 1 and parts[1] else "DAY"
    if res not in ("DAY", "QUARTER_HOUR"):
        raise ValueError(f"resolution {res} unbekannt")
    prices = parts[2:5] if len(parts) >= 5 else ["0.10", "0.14", "0.20"]
    return {"d": d, "resolution": res, "buy": prices[0], "sell": prices[1], "reserve": prices[2]}


def start_server(platform: MockPlatform, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(platform))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def serve(args) -> None:
    cap = Decimal(args.reserve_capacity) if args.reserve_capacity else None
    platform = MockPlatform(args.admin_api_key, args.admin_email, args.admin_password, cap, args.settlement_delay,
                            login_rate_limit=getattr(args, "login_rate_limit", 10) or None)
    closes = now_utc() + timedelta(minutes=args.closes_in_minutes)
    for spec in args.market or ["tomorrow:DAY"]:
        ms = parse_market_spec(spec)
        platform.add_market(ms["d"], ms["resolution"], ms["buy"], ms["sell"], ms["reserve"], closes)
        print(f"Markt {ms['d']} ({ms['resolution']}) Preise {ms['buy']}/{ms['sell']}/{ms['reserve']}, closesAt {format_utc(closes)}")
    for i in range(getattr(args, "demo_egs", 0) or 0):
        eg = platform.add_demo_community(DEMO_NAMES[i % len(DEMO_NAMES)] + ("" if i < len(DEMO_NAMES) else f" {i}"))
        print(f"Demo-EG {eg['name']}: id={eg['id']} email={eg['email']} apiKey={eg['apiKey']} password={eg['password']}")
    if getattr(args, "auto_settle", False):
        threading.Thread(target=platform.auto_settle_loop, args=(threading.Event(),), daemon=True).start()
        print("Märkte werden nach closesAt automatisch geschlossen und abgewickelt")
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(platform))
    print(f"EGSim-Mock läuft auf http://{args.host}:{args.port} (Admin-API-Key: {args.admin_api_key}) – Strg+C beendet")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
