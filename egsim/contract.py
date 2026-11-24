"""Prüft API-Antworten gegen die Schemas des Datenvertrags (egsim/contract.json, erzeugt aus contract/Datenvertrag.yaml).

Ein kleiner Schema-Prüfer ohne Zusatzabhängigkeit. Er kennt genau die Schlüsselwörter, die der Vertrag nutzt:
$ref, type (auch Listen wie [string, null]), enum, const, pattern, format (date, date-time, email), minLength, maxLength,
minimum, maximum, exclusiveMinimum, exclusiveMaximum, minProperties, required, properties, additionalProperties,
items, oneOf, if/then, not und die Erweiterung `x-maxDecimals` (Nachkommastellen).

Zahlen müssen als Decimal (client.py parst mit parse_float=Decimal) oder int ankommen, ein String wie "10" ist
keine Zahl. Alle Funktionen sind frei von Seiteneffekten und thread-sicher.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

_PATH = Path(__file__).with_name("contract.json")
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$", re.ASCII)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Zugangsdaten dürfen nur in diesen Antworten im Klartext stehen
SECRET_OPS = {"POST /auth/login", "POST /energy-communities/{id}/credentials"}
SECRET_KEYS = {"password", "passwordhash", "apikey", "apikeyhash", "secret", "hash", "token"}
MAX_ISSUES_PER_RESPONSE = 5


# Bekannte Fehler im Vertrag, die der Prüfer ausgleicht (sonst gäbe es Fehlalarme bei korrekten Plattformen).
# Vertrag 0.2: Summenfelder nutzten QuantityKwhNonNegative mit maximum 1.000.000 (Grenze EINER Position).
# Seit 0.3 gibt es dafür QuantityKwhTotal ohne Obergrenze; der Ausgleich greift dann nicht mehr.
KNOWN_CONTRACT_ISSUES: list[str] = []
_TOTAL_FIELDS = {
    "MarketResult": ("totalOfferedKwh", "tradableOfferedKwh", "totalDemandKwh", "totalPurchasedKwh", "totalSoldKwh"),
    "IntervalResult": ("totalOfferedKwh", "tradableOfferedKwh", "totalDemandKwh", "totalPurchasedKwh", "totalSoldKwh"),
    "Reserve": ("requiredKwh", "dispatchedKwh"),
}


def _patch(c: dict) -> dict:
    schemas = c["schemas"]
    base = schemas.get("QuantityKwhNonNegative")
    if not base:
        return c
    patched = False
    for name, fields in _TOTAL_FIELDS.items():
        for f in fields:
            prop = schemas.get(name, {}).get("properties", {}).get(f)
            if prop and prop.get("$ref") == "#/components/schemas/QuantityKwhNonNegative":
                schemas["TotalKwhNonNegative"] = {k: v for k, v in base.items() if k != "maximum"}
                prop["$ref"] = "#/components/schemas/TotalKwhNonNegative"
                patched = True
    if patched and not KNOWN_CONTRACT_ISSUES:
        KNOWN_CONTRACT_ISSUES.append("Summenfelder von MarketResult/IntervalResult/Reserve sind auf 1.000.000 kWh begrenzt "
                                     "(Grenze einer einzelnen Position) – Prüfer ignoriert diese Grenze.")
    return c


@lru_cache(maxsize=1)
def load() -> dict:
    return _patch(json.loads(_PATH.read_text(encoding="utf-8")))


def cache_clear() -> None:
    load.cache_clear()


def version() -> str:
    return load()["version"]


def operations() -> dict:
    return load()["operations"]


# ---------------------------------------------------------------------------------------------------------------------
def _typename(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float, Decimal)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "array"
    return type(v).__name__


def _is_type(v: Any, t: str) -> bool:
    if t == "null":
        return v is None
    if t == "boolean":
        return isinstance(v, bool)
    if t == "string":
        return isinstance(v, str)
    if t == "number":
        return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)
    if t == "integer":
        return (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, Decimal) and v == v.to_integral_value())
    if t == "object":
        return isinstance(v, dict)
    if t == "array":
        return isinstance(v, list)
    return True


def _decimals(v: Any) -> int:
    d = v if isinstance(v, Decimal) else Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
    exp = d.normalize().as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def _format_ok(fmt: str, v: str) -> bool:
    if fmt == "date":
        if not _DATE.match(v):
            return False
        try:
            date.fromisoformat(v)
        except ValueError:
            return False
        return True
    if fmt == "date-time":
        if not _DATE_TIME.match(v):
            return False
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return False
        return True
    if fmt == "email":
        return bool(_EMAIL.match(v)) and len(v) <= 254
    return True


def _ref(root: dict, ref: str) -> dict:
    name = ref.rsplit("/", 1)[1]
    return root["schemas"][name]


def _validate(v: Any, s: dict, root: dict, p: str, out: list[tuple[str, str]]) -> None:
    if "$ref" in s:
        _validate(v, _ref(root, s["$ref"]), root, p, out)
        return
    t = s.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(v, x) for x in types):
            out.append((p, f"erwartet {' oder '.join(types)}, erhalten {_typename(v)}"
                           + (f" {json.dumps(v, default=str)[:30]}" if isinstance(v, (str, int, Decimal)) else "")))
            return
    if "const" in s and v != s["const"]:
        out.append((p, f"erwartet {s['const']!r}"))
    if "enum" in s and v not in s["enum"]:
        out.append((p, f"{v!r} ist kein erlaubter Wert ({', '.join(map(str, s['enum']))})"))
    if "oneOf" in s:
        failed, ok = [], 0
        for alt in s["oneOf"]:
            sub: list[tuple[str, str]] = []
            _validate(v, alt, root, p, sub)
            if sub:
                failed.append(sub[0][1])
            else:
                ok += 1
        if ok == 0:
            out.append((p, "passt zu keiner erlaubten Variante: " + "; ".join(failed)))
        elif ok > 1:
            out.append((p, "passt zu mehreren Varianten"))
    if isinstance(v, str):
        if "minLength" in s and len(v) < s["minLength"]:
            out.append((p, f"kürzer als {s['minLength']} Zeichen"))
        if "maxLength" in s and len(v) > s["maxLength"]:
            out.append((p, f"länger als {s['maxLength']} Zeichen"))
        if "pattern" in s and not re.search(s["pattern"], v, re.ASCII):
            out.append((p, f"{v!r} passt nicht zum Muster {s['pattern']}"))
        if "format" in s and not _format_ok(s["format"], v):
            out.append((p, f"{v!r} ist kein gültiges Format {s['format']}"))
    elif isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        if "minimum" in s and v < s["minimum"]:
            out.append((p, f"{v} ist kleiner als {s['minimum']}"))
        if "maximum" in s and v > s["maximum"]:
            out.append((p, f"{v} ist größer als {s['maximum']}"))
        if "exclusiveMinimum" in s and v <= s["exclusiveMinimum"]:
            out.append((p, f"{v} muss größer als {s['exclusiveMinimum']} sein"))
        if "exclusiveMaximum" in s and v >= s["exclusiveMaximum"]:
            out.append((p, f"{v} muss kleiner als {s['exclusiveMaximum']} sein"))
        if "x-maxDecimals" in s and _decimals(v) > s["x-maxDecimals"]:
            out.append((p, f"{v} hat mehr als {s['x-maxDecimals']} Nachkommastellen"))
    elif isinstance(v, dict):
        if "minProperties" in s and len(v) < s["minProperties"]:
            out.append((p, f"weniger als {s['minProperties']} Felder"))
        for r in s.get("required", []):
            if r not in v:
                out.append((f"{p}/{r}", "Pflichtfeld fehlt"))
        props = s.get("properties", {})
        for k, sub in props.items():
            if k in v:
                _validate(v[k], sub, root, f"{p}/{k}", out)
        if s.get("additionalProperties") is False:
            for k in v:
                if k not in props:
                    out.append((f"{p}/{k}", "unbekanntes Feld"))
    elif isinstance(v, list):
        if "minItems" in s and len(v) < s["minItems"]:
            out.append((p, f"weniger als {s['minItems']} Einträge"))
        if "items" in s:
            for i, item in enumerate(v):
                _validate(item, s["items"], root, f"{p}/{i}", out)
    if "if" in s:
        cond: list[tuple[str, str]] = []
        _validate(v, s["if"], root, p, cond)
        if not cond and "then" in s:
            _validate(v, s["then"], root, p, out)
    if "not" in s:
        neg: list[tuple[str, str]] = []
        _validate(v, s["not"], root, p, neg)
        if not neg:
            out.append((p, "Kombination nicht erlaubt"))


def schema_errors(value: Any, schema: dict) -> list[str]:
    """Abweichungen von einem Schema des Vertrags, z. B. schema_errors(body, {"$ref": "#/components/schemas/Position"})."""
    out: list[tuple[str, str]] = []
    _validate(value, schema, load(), "", out)
    return [f"{p or '(Antwort)'}: {m}" for p, m in out]


def _secrets(node: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.lower() in SECRET_KEYS:
                found.append(f"{path}/{k}")
            found += _secrets(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _secrets(v, f"{path}/{i}")
    return found


def check_response(method: str, template: str, status: int, body: Any, content_type: str | None = None
                   ) -> list[tuple[str, str]]:
    """Prüft eine Antwort gegen den Vertrag. Ergebnis: Liste (Schwere, Meldung); leer = konform.

    `template` ist der Pfad wie im Vertrag, z. B. "/positions/{id}". Unbekannte Endpoints werden nicht geprüft.
    """
    op = operations().get(f"{method} {template}")
    if op is None or status == 0:
        return []
    issues: list[tuple[str, str]] = []
    responses = op["responses"]
    documented = str(status) in responses
    schema = responses.get(str(status))
    if not documented:
        if status not in (401, 403, 429, 500):  # diese Codes gelten laut Vertrag für alle Endpoints
            issues.append(("warning", f"Statuscode {status} ist für diesen Endpoint im Vertrag nicht vorgesehen"))
        schema = {"$ref": "#/components/schemas/Error"} if status >= 400 else None
    has_body = body not in (None, "")
    if has_body and content_type and "json" not in content_type.lower():
        issues.append(("error", f"Content-Type ist '{content_type}', erwartet application/json"))
    if has_body and isinstance(body, str):
        issues.append(("error", "Antwort ist kein gültiges JSON"))
        return issues
    if schema is None:
        if has_body and documented:
            issues.append(("error", f"Status {status} darf laut Vertrag keinen Body haben"))
        return issues
    if not has_body:
        issues.append(("error", "Antwort ohne Body, der Vertrag verlangt ein JSON-Objekt"))
        return issues
    errs = schema_errors(body, schema)
    for e in errs[:MAX_ISSUES_PER_RESPONSE]:
        issues.append(("error", e))
    if len(errs) > MAX_ISSUES_PER_RESPONSE:
        issues.append(("error", f"… und {len(errs) - MAX_ISSUES_PER_RESPONSE} weitere Abweichungen"))
    if f"{method} {template}" not in SECRET_OPS:
        for s in _secrets(body)[:3]:
            issues.append(("error", f"{s}: Zugangsdaten dürfen nur in der Antwort der Zugangsdaten-Vergabe stehen"))
    return issues


def contract_minor(v: str | None = None) -> str:
    """Haupt- und Nebenversion, z. B. '0.3' für '0.3.1'."""
    parts = (v or version()).strip().split(".")
    return ".".join(parts[:2])


def check_headers(method: str, template: str, status: int, headers: dict[str, str], body: Any,
                  sent_request_id: str | None = None) -> list[tuple[str, str]]:
    """Header-Regeln ab Vertrag 0.3: X-Request-Id in jeder Antwort (gesendete wird übernommen), Api-Version,
    requestId im Fehler-Body gleich dem Header, Retry-After bei 429. `headers` mit kleingeschriebenen Namen."""
    if status == 0 or f"{method} {template}" not in operations():
        return []
    issues: list[tuple[str, str]] = []
    h = {k.lower(): v for k, v in (headers or {}).items()}
    rid = h.get("x-request-id")
    if not rid:
        issues.append(("error", "Header X-Request-Id fehlt"))
    elif not 1 <= len(rid) <= 128:
        issues.append(("error", f"X-Request-Id hat {len(rid)} Zeichen (erlaubt 1–128)"))
    elif sent_request_id and rid != sent_request_id:
        issues.append(("error", f"X-Request-Id {rid!r} statt der mitgeschickten {sent_request_id!r}"))
    api_version = h.get("api-version")
    if not api_version:
        issues.append(("error", "Header Api-Version fehlt"))
    elif contract_minor(api_version) != contract_minor():
        issues.append(("warning", f"Api-Version {api_version}, EGSim prüft gegen Vertrag {version()}"))
    if status >= 400 and isinstance(body, dict) and rid and body.get("requestId") not in (None, rid):
        issues.append(("error", f"requestId im Fehler-Body ({body.get('requestId')!r}) ungleich Header X-Request-Id"))
    if status == 429:
        ra = h.get("retry-after", "")
        if not ra.strip().isdigit():
            issues.append(("error", f"429 ohne gültigen Header Retry-After (Sekunden), erhalten {ra!r}"))
    return issues
