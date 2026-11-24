"""Ende-zu-Ende: Simulator (ohne Admin) gegen den Mock – korrekt und mit eingebauten Fehlern."""
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from decimal import Decimal

import pytest

from egsim.config import default_config_dict, new_community_dict, parse_config
from egsim.mockserver import ApiErr, MockPlatform, _check_uuid, start_server
from egsim.simulator import Simulator
from egsim.timeutil import now_utc, resolve_delivery_date

FLOWS = [
    {"generation": {"profile": "solar", "dailyKwh": [40, 80]}, "priceExpectation": {"probability": 0.7, "range": [0.08, 0.12]}},
    {"generation": {"profile": "wind", "dailyKwh": [20, 60]}},
    {"consumption": {"profile": "household", "dailyKwh": [80, 140]}},
    {"consumption": {"profile": "business", "dailyKwh": [50, 90]}},
]


def operator_closes(monkeypatch, platform):
    """Betreiber schließt den Markt genau dann, wenn der Simulator auf das Settlement wartet (statt fester Timer)."""
    orig = Simulator._wait_for_settlement

    def wait(self, run, reader):
        platform.close_market(run.delivery_date)
        return orig(self, run, reader)
    monkeypatch.setattr(Simulator, "_wait_for_settlement", wait)


def _setup(platform, resolution="DAY", closes_in=timedelta(hours=1), n=4, auth="apiKey", **run):
    d = resolve_delivery_date("tomorrow").isoformat()
    platform.add_market(d, resolution, closes_at=now_utc() + closes_in)
    srv = start_server(platform)
    raw = default_config_dict()
    raw["platform"]["baseUrl"] = f"http://127.0.0.1:{srv.server_address[1]}"
    raw["run"].update({"seed": 1, "deliveryDates": [d], "reportDir": None, **run})
    raw["behaviour"].update({"updateProbability": 0.3, "cancelProbability": 0.3, "quarterHour": {"intervals": 12}})
    raw["communities"] = []
    for i in range(n):
        eg = platform.add_demo_community(f"EG {i}")
        raw["communities"].append(new_community_dict(eg["name"], email=eg["email"], apiKey=eg["apiKey"],
                                                     password=eg["password"], auth=auth, **FLOWS[i % len(FLOWS)]))
    return srv, raw, d


@pytest.mark.parametrize("resolution", ["DAY", "QUARTER_HOUR"])
def test_korrekte_plattform_ohne_befunde(resolution, monkeypatch):
    p = MockPlatform()
    srv, raw, d = _setup(p, resolution, allParticipantsSimulated=True)
    try:
        # Betreiber schließt den Markt, während der Simulator wartet
        raw_cfg = parse_config({**raw, "run": {**raw["run"], "waitForSettlementMinutes": 1}})
        operator_closes(monkeypatch, p)
        rep = Simulator(raw_cfg).run()
    finally:
        srv.shutdown()
    assert rep["totals"]["errors"] == 0, [f for f in rep["findings"] if f["severity"] != "info"]
    assert rep["totals"]["checksFailed"] == 0
    assert rep["totals"]["checksPassed"] > 60
    m = rep["markets"][0]
    assert m["marketStatusAtEnd"] == "SETTLED"
    assert m["verification"]["status"] == "OK" and m["verification"]["mode"] == "full"


def test_login_per_bearer_und_reserve_knapp():
    p = MockPlatform(reserve_capacity=Decimal("1"))
    srv, raw, d = _setup(p, auth="bearer", allParticipantsSimulated=True)
    try:
        Simulator(parse_config(raw)).run()          # einreichen
        p.close_market(d)
        rep = Simulator(parse_config(raw)).verify_only()  # später nur prüfen
    finally:
        srv.shutdown()
    assert rep["totals"]["errors"] == 0, [f for f in rep["findings"] if f["severity"] != "info"]
    assert rep["markets"][0]["verification"]["status"] == "OK"


def test_ohne_zugangsdaten_und_nicht_freigeschaltet():
    p = MockPlatform()
    srv, raw, d = _setup(p, n=1)
    pending = p.add_demo_community("Pending")
    p.ecs[pending["id"]]["status"] = "PENDING"
    raw["communities"].append(new_community_dict("Ohne Key", consumption={"profile": "flat", "dailyKwh": 5}))
    raw["communities"].append(new_community_dict("Pending", apiKey=pending["apiKey"], consumption={"profile": "flat", "dailyKwh": 5}))
    try:
        rep = Simulator(parse_config(raw)).run()
    finally:
        srv.shutdown()
    status = {c["name"]: c for c in rep["communities"]}
    assert status["Ohne Key"]["problem"] == "keine Zugangsdaten"
    assert "ENERGY_COMMUNITY_NOT_ACTIVE" in status["Pending"]["problem"]
    assert status["EG 0"]["ready"]


class KaputtePlattform(MockPlatform):
    """Typische Fehler: keine Eigentümerprüfung bei Positionen, falsche Zuteilungsmenge."""

    def _load_position(self, role, ec, raw_id):
        p = self.positions.get(_check_uuid(raw_id))
        if not p:
            raise ApiErr("POSITION_NOT_FOUND")
        return p

    def _settle(self, d):
        super()._settle(d)
        for a in self.results[d]["allocations"]:
            if a["allocatedKwh"] > 0:
                a["allocatedKwh"] = a["allocatedKwh"] + Decimal("0.5")
                break


def test_kaputte_plattform_wird_erkannt(monkeypatch):
    p = KaputtePlattform()
    srv, raw, d = _setup(p)
    try:
        operator_closes(monkeypatch, p)
        rep = Simulator(parse_config({**raw, "run": {**raw["run"], "waitForSettlementMinutes": 1}})).run()
    finally:
        srv.shutdown()
    failed = {c["id"] for c in rep["checks"] if c["result"] == "FAIL"}
    assert {"TEN-03", "TEN-04"} <= failed
    assert rep["markets"][0]["verification"]["status"] == "MISMATCH"


def test_ui_api_lauf(tmp_path):
    from http.server import ThreadingHTTPServer

    from egsim.ui.server import UiState, make_handler
    p = MockPlatform()
    srv, raw, d = _setup(p, n=2)
    state = UiState(tmp_path / "egsim-ui.json")
    ui = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ui.server_address[1]}"

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method, headers={"Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    try:
        assert "EGSim" in urllib.request.urlopen(base + "/").read().decode()
        assert call("PUT", "/api/config", raw)["configError"] is None
        assert (tmp_path / "egsim-ui.json").exists()
        uid = raw["communities"][0]["uid"]
        assert call("POST", f"/api/communities/{uid}/test", {})["ok"] is True
        r = call("POST", "/api/manual", {"uid": uid, "action": "market", "params": {"date": d}})
        assert r["status"] == 200 and r["response"]["deliveryDate"] == d
        call("POST", "/api/run", {"kind": "run"})
        for _ in range(100):
            run = call("GET", "/api/run?logSince=0&reqSince=0")
            if run["status"] != "running":
                break
            time.sleep(0.2)
        assert run["status"] == "done" and run["report"]["totals"]["errors"] == 0
        assert run["requests"] and any("angemeldet" in line["text"] for line in run["logs"])
    finally:
        ui.shutdown()
        srv.shutdown()


# ---------------------------------------------------------------- Admin-Key, automatische Einrichtung, Szenarien
def _admin_setup(platform, resolution="QUARTER_HOUR", **run):
    d = resolve_delivery_date("tomorrow").isoformat()
    platform.add_market(d, resolution, closes_at=now_utc() + timedelta(hours=1))
    srv = start_server(platform)
    raw = default_config_dict()
    raw["platform"].update(baseUrl=f"http://127.0.0.1:{srv.server_address[1]}", adminApiKey="admin-key")
    raw["run"].update({"seed": 3, "deliveryDates": [d], "reportDir": None, "closeMarket": True, **run})
    raw["communities"] = [new_community_dict(f"Auto {i}", **FLOWS[i]) for i in range(3)]  # ohne Zugangsdaten
    return srv, raw, d


def test_automatische_einrichtung_admin_checks_und_volle_pruefung():
    p = MockPlatform()
    srv, raw, d = _admin_setup(p)
    try:
        cfg = parse_config(raw)
        rep = Simulator(cfg).run()
    finally:
        srv.shutdown()
    assert rep["totals"]["errors"] == 0, [f for f in rep["findings"] if f["severity"] != "info"]
    assert sorted(rep["provisioned"]) == ["Auto 0", "Auto 1", "Auto 2"]
    assert all(c.api_key and c.energy_community_id for c in cfg.communities)  # für die UI zurückgeschrieben
    assert any(c["id"].startswith("ADM-") and c["result"] == "PASS" for c in rep["checks"])
    assert rep["markets"][0]["verification"]["mode"] == "admin"
    assert rep["markets"][0]["verification"]["status"] == "OK"


@pytest.mark.parametrize("rule", ["invariants", "gruppe1"])
def test_alle_szenarien_bestehen_gegen_mock(rule):
    from egsim.scenarios import CATALOG
    p = MockPlatform()
    srv, raw, d = _admin_setup(p, scenarios=[s.id for s in CATALOG], allocationRule=rule)
    raw["communities"] = []
    try:
        rep = Simulator(parse_config(raw)).run_scenarios()
    finally:
        srv.shutdown()
    res = {s["id"]: s for s in rep["scenarios"]}
    assert set(res) == {s.id for s in CATALOG}
    assert all(s["result"] == "PASS" for s in res.values()), {k: v["messages"] for k, v in res.items() if v["result"] != "PASS"}
    assert rep["totals"]["errors"] == 0


def test_szenarien_im_tagesmarkt_und_ohne_admin(monkeypatch):
    p = MockPlatform()
    srv, raw, d = _setup(p, "DAY", n=3, scenarios=["SZ-05", "SZ-01", "SZ-12"], waitForSettlementMinutes=1)
    try:
        operator_closes(monkeypatch, p)
        rep = Simulator(parse_config(raw)).run_scenarios()
    finally:
        srv.shutdown()
    res = {s["id"]: s["result"] for s in rep["scenarios"]}
    assert res == {"SZ-05": "PASS", "SZ-01": "SKIP", "SZ-12": "SKIP"}  # ein Szenario je Tagesmarkt, SZ-12 braucht Admin


class FalscherRest(MockPlatform):
    """Verteilt den Rundungsrest an das zuletzt eingereichte statt an das erste Angebot."""

    def _settle(self, d):
        super()._settle(d)
        by_iv = {}
        for a in self.results[d]["allocations"]:
            if a["reason"] == "PARTIALLY_ALLOCATED":
                by_iv.setdefault(a["intervalStart"], []).append(a)
        for allocs in by_iv.values():
            hi = [a for a in allocs if a["allocatedKwh"] == max(x["allocatedKwh"] for x in allocs)]
            lo = [a for a in allocs if a["allocatedKwh"] == min(x["allocatedKwh"] for x in allocs)]
            if hi and lo and hi[0] is not lo[-1]:
                hi[0]["allocatedKwh"], lo[-1]["allocatedKwh"] = lo[-1]["allocatedKwh"], hi[0]["allocatedKwh"]


def test_szenario_erkennt_falschen_rundungsrest():
    p = FalscherRest()
    srv, raw, d = _admin_setup(p, scenarios=["SZ-04", "SZ-01"], allocationRule="gruppe1")
    raw["communities"] = []
    try:
        rep = Simulator(parse_config(raw)).run_scenarios()
    finally:
        srv.shutdown()
    res = {s["id"]: s for s in rep["scenarios"]}
    assert res["SZ-01"]["result"] == "PASS"
    assert res["SZ-04"]["result"] == "FAIL"
    assert any("3.334" in m for m in res["SZ-04"]["messages"])


def test_ui_passwortschutz(tmp_path):
    from http.server import ThreadingHTTPServer

    from egsim.ui.server import UiState, make_handler
    ui = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(UiState(tmp_path / "c.json"), "geheim"))
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ui.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/state")
        assert e.value.code == 401
        assert urllib.request.urlopen(base + "/healthz").status == 200
        import base64
        req = urllib.request.Request(base + "/api/state",
                                     headers={"Authorization": "Basic " + base64.b64encode(b"egsim:geheim").decode()})
        assert urllib.request.urlopen(req).status == 200
    finally:
        ui.shutdown()


# --- Vertrag 0.2, korrigierte Fassung vom 19.11.2026 -------------------------------------------------------------

class BedarfTeilweiseGeaendert(MockPlatform):
    """Meldet bei PUT eines Bedarfs mit Preisvorstellung 400, speichert die neue Menge aber trotzdem."""

    def h_put_position(self, raw_id, q, h, raw):
        role, ec, _ = self._auth(h)
        p = self.positions.get(raw_id)
        b = json.loads(raw) if raw else {}
        if p and p["type"] == "DEMAND" and b.get("priceExpectationEurPerKwh") is not None:
            p["quantityKwh"] = Decimal(str(b["quantityKwh"]))
            raise ApiErr("VALIDATION_FAILED")
        return super().h_put_position(raw_id, q, h, raw)


def test_bedarf_mit_preis_bleibt_unveraendert():
    p = BedarfTeilweiseGeaendert()
    srv, raw, d = _setup(p)
    try:
        rep = Simulator(parse_config(raw)).run()
    finally:
        srv.shutdown()
    pos08 = next(c for c in rep["checks"] if c["id"] == "POS-08")
    assert pos08["result"] == "FAIL" and "Position geändert" in (pos08.get("note") or "")


def test_bedarf_mit_preis_korrekt_abgelehnt():
    p = MockPlatform()
    srv, raw, d = _setup(p)
    try:
        rep = Simulator(parse_config(raw)).run()
    finally:
        srv.shutdown()
    assert next(c for c in rep["checks"] if c["id"] == "POS-08")["result"] == "PASS"


def test_marktpreis_null_wird_erkannt():
    p = MockPlatform()
    srv, raw, d = _setup(p, n=1)
    p.markets[d]["prices"]["reservePriceEurPerKwh"] = None
    try:
        rep = Simulator(parse_config(raw)).run()
    finally:
        srv.shutdown()
    msgs = [f["message"] for f in rep["findings"] if f["severity"] == "error"]
    assert any("reservePriceEurPerKwh" in m for m in msgs), msgs
