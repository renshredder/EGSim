"""Schema-Prüfer für Antworten (egsim/contract.py) und seine Einbindung in den Simulator."""
import threading
from copy import deepcopy
from decimal import Decimal

import pytest

from egsim import contract
from egsim.config import parse_config
from egsim.mockserver import MockPlatform
from egsim.simulator import Simulator

from test_e2e_mock import _setup, operator_closes

POSITION = {
    "id": "6b1a8f3e-2d94-4a77-9c1e-0f5e8a7b1234", "energyCommunityId": "3f6c1c8e-5b1f-4c37-a8b1-1c6d9b9d2a10",
    "deliveryDate": "2026-11-20", "intervalStart": None, "type": "OFFER", "quantityKwh": Decimal("120"),
    "priceExpectationEurPerKwh": Decimal("0.09"), "status": "OPEN", "rejectionReason": None,
    "createdAt": "2026-11-18T09:00:00Z", "updatedAt": "2026-11-18T09:00:00Z",
}
ERROR = {"code": "FORBIDDEN", "message": "nope", "requestId": "req-1"}


def check(status, body, method="GET", template="/positions/{id}", ctype="application/json"):
    return contract.check_response(method, template, status, body, ctype)


def messages(issues):
    return " | ".join(m for _, m in issues)


def test_gueltige_antworten_sind_konform():
    assert check(200, POSITION) == []
    assert check(403, ERROR) == []
    assert check(204, None, "POST", "/auth/logout") == []
    # Tagesmarkt: intervalStart null, 15-Minuten-Markt: Zeitstempel
    assert check(200, {**POSITION, "intervalStart": "2026-11-20T11:15:00Z"}) == []
    # Zeitstempel mit Offset sind RFC 3339 und im Vertrag erlaubt, nur intervalStart muss UTC sein
    assert check(200, {**POSITION, "createdAt": "2026-11-18T10:00:00+01:00"}) == []


@pytest.mark.parametrize("change,expected", [
    ({"quantityKwh": "120"}, "erwartet number, erhalten string"),
    ({"quantityKwh": Decimal("0")}, "muss größer als 0 sein"),
    ({"quantityKwh": Decimal("1.2345")}, "mehr als 3 Nachkommastellen"),
    ({"quantityKwh": Decimal("1000000.001")}, "größer als 1000000"),
    ({"priceExpectationEurPerKwh": Decimal("0.123456")}, "mehr als 5 Nachkommastellen"),
    ({"priceExpectationEurPerKwh": None}, "erwartet number, erhalten null"),
    ({"type": "SELL"}, "kein erlaubter Wert"),
    ({"status": "DONE"}, "kein erlaubter Wert"),
    ({"id": "123"}, "passt nicht zum Muster"),
    ({"deliveryDate": "20.11.2026"}, "passt nicht zum Muster"),
    ({"deliveryDate": "2026-02-30"}, "kein gültiges Format date"),
    ({"intervalStart": "2026-11-20T11:07:00Z"}, "passt zu keiner erlaubten Variante"),
    ({"intervalStart": "2026-11-20T11:15:00+01:00"}, "passt zu keiner erlaubten Variante"),
    ({"createdAt": "gestern"}, "kein gültiges Format date-time"),
    ({"rejectionReason": 5}, "erwartet string oder null"),
])
def test_abweichungen_werden_erkannt(change, expected):
    issues = check(200, {**POSITION, **change})
    assert issues and issues[0][0] == "error", change
    assert expected in messages(issues), (change, issues)


def test_pflichtfeld_fehlt():
    body = deepcopy(POSITION)
    del body["createdAt"]
    del body["intervalStart"]
    msg = messages(check(200, body))
    assert "/createdAt: Pflichtfeld fehlt" in msg and "/intervalStart: Pflichtfeld fehlt" in msg


def test_fehlerformat():
    assert "kein erlaubter Wert" in messages(check(403, {"code": "NOPE", "message": "x", "requestId": "r"}))
    assert "/message: Pflichtfeld fehlt" in messages(check(403, {"code": "FORBIDDEN", "requestId": "r"}))
    assert "/requestId: Pflichtfeld fehlt" in messages(check(403, {"code": "FORBIDDEN", "message": "x"}))
    assert "/details/0/field: Pflichtfeld fehlt" in messages(
        check(400, {"code": "VALIDATION_FAILED", "message": "x", "requestId": "r", "details": [{"message": "m"}]}))
    assert check(400, {"code": "VALIDATION_FAILED", "message": "x", "requestId": "r",
                       "details": [{"field": "/quantityKwh", "code": "INVALID_QUANTITY", "message": "m"}]}) == []


def test_body_status_und_content_type():
    assert "keinen Body" in messages(check(204, {"x": 1}, "POST", "/auth/logout"))
    assert "kein gültiges JSON" in messages(check(200, "<html>Fehler</html>"))
    assert "Content-Type" in messages(check(200, POSITION, ctype="text/plain"))
    assert "ohne Body" in messages(check(200, None))
    # nicht dokumentierter Status: nur Warnung, Fehlerformat wird trotzdem geprüft
    iss = check(418, ERROR)
    assert [s for s, _ in iss] == ["warning"]
    assert check(500, {"code": "INTERNAL_ERROR", "message": "x", "requestId": "r"}) == []  # 401/403/500 gelten überall


def test_zugangsdaten_nur_in_den_vorgesehenen_antworten():
    assert "Zugangsdaten dürfen nur" in messages(check(200, {**POSITION, "passwordHash": "x"}))
    cred = {"apiKey": "k", "password": "p"}
    assert check(201, cred, "POST", "/energy-communities/{id}/credentials") == []
    login = {"token": "t", "tokenType": "Bearer", "expiresIn": 3600, "role": "ADMIN"}
    assert check(200, login, "POST", "/auth/login") == []


def test_unbekannter_endpoint_und_transportfehler_werden_nicht_geprueft():
    assert check(200, {"x": 1}, "GET", "/gibt-es-nicht") == []
    assert check(0, None) == []


def test_anfrage_schemas_if_then_und_unbekannte_felder():
    req = {"$ref": "#/components/schemas/PositionRequest"}
    ok = {"deliveryDate": "2026-11-20", "type": "OFFER", "quantityKwh": Decimal("5"), "priceExpectationEurPerKwh": Decimal("0.1")}
    assert contract.schema_errors(ok, req) == []
    assert any("Kombination" in e for e in contract.schema_errors({**ok, "type": "DEMAND"}, req))
    assert any("unbekanntes Feld" in e for e in contract.schema_errors({**ok, "foo": 1}, req))


def test_vertragsversion():
    assert contract.version() == "0.3.1"


def test_header_pruefung():
    hdr = {"x-request-id": "egsim-1", "api-version": "0.3.1"}
    assert contract.check_headers("GET", "/positions/{id}", 200, hdr, POSITION, "egsim-1") == []
    msg = messages(contract.check_headers("GET", "/positions/{id}", 200, {"api-version": "0.3.1"}, POSITION))
    assert "X-Request-Id" in msg
    msg = messages(contract.check_headers("GET", "/positions/{id}", 200, {**hdr, "x-request-id": "andere"}, POSITION,
                                          "egsim-1"))
    assert "X-Request-Id" in msg
    assert "Api-Version" in messages(contract.check_headers("GET", "/positions/{id}", 200,
                                                            {"x-request-id": "egsim-1"}, POSITION, "egsim-1"))
    # requestId im Fehler muss zum Header passen, 429 braucht Retry-After
    assert "requestId" in messages(contract.check_headers("GET", "/positions/{id}", 403, hdr,
                                                          {**ERROR, "requestId": "x"}, "egsim-1"))
    assert "Retry-After" in messages(contract.check_headers("POST", "/auth/login", 429, hdr,
                                                            {"code": "RATE_LIMITED", "message": "m", "requestId": "egsim-1"},
                                                            "egsim-1"))
    # andere Minor-Version: nur Warnung
    iss = contract.check_headers("GET", "/positions/{id}", 200, {**hdr, "api-version": "0.4.0"}, POSITION, "egsim-1")
    assert iss and all(s == "warning" for s, _ in iss)


# ---------------------------------------------------------------------------------------------------------------------
class SchemaBruch(MockPlatform):
    """Plattform mit Schemafehlern: Trade ohne createdAt, Passwort-Hash in /me, Menge als String."""

    def handle(self, method, path, query, headers, raw):
        status, body = super().handle(method, path, query, headers, raw)
        if method == "GET" and path.endswith("/trades") and isinstance(body, list) and body:
            body = [{k: v for k, v in body[0].items() if k != "createdAt"}] + body[1:]
        if method == "GET" and path == "/me" and isinstance(body, dict):
            body = {**body, "passwordHash": "abc"}
        if method == "POST" and path == "/positions" and status == 201:
            body = {**body, "quantityKwh": str(body["quantityKwh"])}
        return status, body


def test_simulator_meldet_schemafehler(monkeypatch):
    p = SchemaBruch()
    srv, raw, d = _setup(p)
    try:
        operator_closes(monkeypatch, p)
        rep = Simulator(parse_config({**raw, "run": {**raw["run"], "waitForSettlementMinutes": 1}})).run()
    finally:
        srv.shutdown()
    msgs = [f["message"] for f in rep["findings"] if f["area"] == "contract"]
    assert any("/createdAt: Pflichtfeld fehlt" in m and "/trades" in m for m in msgs), msgs
    assert any("passwordHash" in m or "Zugangsdaten dürfen nur" in m for m in msgs), msgs
    assert any("erwartet number, erhalten string" in m for m in msgs), msgs
    assert rep["totals"]["errors"] > 0 and rep["contractIssues"]


def test_schemapruefung_abschaltbar():
    p = SchemaBruch()
    srv, raw, d = _setup(p)
    raw["run"]["validateSchema"] = False
    try:
        rep = Simulator(parse_config(raw)).run()
    finally:
        srv.shutdown()
    assert not [f for f in rep["findings"] if f["area"] == "contract"]
    assert rep["contractIssues"] == []
