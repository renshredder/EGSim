"""Web-UI für EGSim: Konfiguration, Läufe, Ergebnisse, Request-Log und manuelle Requests als EG.

Läuft lokal (Standard 127.0.0.1:8090) und speichert die Konfiguration in einer JSON-Datei.
Die Datei enthält Zugangsdaten der EGs – nicht einchecken.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import threading
import time
import uuid
import webbrowser
from datetime import timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from egsim import CONTRACT_VERSION, __version__
from egsim.client import RequestStats, _to_jsonable
from egsim.config import (COMMUNITY_CODE_RE, PROFILES, ConfigError, community_code_from, default_config_dict,
                          load_config_dict, new_community_dict,
                          parse_config)

log = logging.getLogger("egsim.ui")
STATIC = Path(__file__).parent / "static"


class _RunLogHandler(logging.Handler):
    def __init__(self, sink: list) -> None:
        super().__init__(logging.INFO)
        self.sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("egsim.ui") or record.name.startswith("egsim.mock"):
            return
        self.sink.append({"level": record.levelname, "text": self.format(record)})


class UiState:
    def __init__(self, config_path: Path, mock=None, mock_url: str | None = None) -> None:
        self.config_path = config_path
        self.lock = threading.RLock()
        self.mock = mock
        self.mock_url = mock_url
        self.manual_stats = RequestStats(log_size=500)
        self.run: dict | None = None
        if config_path.exists():
            try:
                self.raw = load_config_dict(config_path)
            except ConfigError as exc:
                log.warning("%s – starte mit Standardkonfiguration", exc)
                self.raw = default_config_dict()
        else:
            self.raw = default_config_dict()
            self.save()

    # ----------------------------------------------------------------- config
    def save(self) -> None:
        with self.lock:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(self.raw, indent=2, ensure_ascii=False), encoding="utf-8")

    def validate(self) -> str | None:
        try:
            parse_config(self.raw)
            return None
        except (ConfigError, ValueError, TypeError) as exc:
            return str(exc)

    def community(self, uid: str) -> dict:
        for c in self.raw.get("communities") or []:
            if c.get("uid") == uid:
                return c
        raise KeyError(uid)

    def parsed_community(self, uid: str):
        cfg = parse_config(self.raw)
        com = next((c for c in cfg.communities if c.uid == uid), None)
        if com is None:
            raise KeyError(uid)
        return cfg, com

    # -------------------------------------------------------------------- run
    def start_run(self, kind: str) -> dict:
        from egsim.simulator import Simulator
        with self.lock:
            if self.run and self.run["status"] == "running":
                raise RuntimeError("Es läuft bereits ein Lauf")
            cfg = parse_config(self.raw)
            stats = RequestStats(log_size=5000)
            stop = threading.Event()
            run = {"id": uuid.uuid4().hex[:8], "kind": kind, "status": "running", "startedAt": time.time(),
                   "finishedAt": None, "logs": [], "report": None, "reportFile": None, "error": None,
                   "stop": stop, "stats": stats}
            self.run = run

        def work():
            handler = _RunLogHandler(run["logs"])
            logger = logging.getLogger("egsim")
            logger.addHandler(handler)
            old_level = logger.level
            if logger.getEffectiveLevel() > logging.INFO:
                logger.setLevel(logging.INFO)
            try:
                sim = Simulator(cfg, stats=stats, stop=stop)
                report = {"verify": sim.verify_only, "scenarios": sim.run_scenarios}.get(kind, sim.run)()
                run["report"] = report
                run["reportFile"] = self._write_report(report, cfg.report_dir)
                self.sync_communities(cfg)
                run["status"] = "stopped" if report.get("stopped") else "done"
            except Exception as exc:  # noqa: BLE001
                log.exception("Lauf fehlgeschlagen")
                run["error"] = repr(exc)
                run["status"] = "error"
            finally:
                run["finishedAt"] = time.time()
                logger.removeHandler(handler)
                logger.setLevel(old_level)

        threading.Thread(target=work, daemon=True, name=f"egsim-run-{run['id']}").start()
        return self.run_view()

    def _write_report(self, report: dict, report_dir: str | None) -> str | None:
        if not report_dir:
            return None
        from egsim.report import write_report
        p = write_report(report, str(Path(report_dir) / "egsim-{timestamp}.json"))
        return p.name if p else None

    def sync_communities(self, cfg) -> None:
        """Vom Simulator angelegte oder geänderte Zugangsdaten (automatische Einrichtung) in die Datei übernehmen."""
        fields = {"email": "email", "api_key": "apiKey", "password": "password",
                  "energy_community_id": "energyCommunityId"}
        changed = False
        with self.lock:
            comms = self.raw.setdefault("communities", [])
            for c in cfg.communities:
                try:
                    raw = self.community(c.uid)
                except KeyError:
                    raw = new_community_dict(c.name, uid=c.uid, description=c.description or "")
                    comms.append(raw)
                    changed = True
                for attr, key in fields.items():
                    v = getattr(c, attr)
                    if v and raw.get(key) != v:
                        raw[key] = v
                        changed = True
                if c.energy_community_id and raw.get("registeredStatus") != "ACTIVE" and c.api_key:
                    raw["registeredStatus"] = "ACTIVE"
                    changed = True
            if changed:
                self.save()

    def run_view(self, log_since: int = 0, req_since: int = 0) -> dict | None:
        r = self.run
        if not r:
            return None
        return {
            "id": r["id"], "kind": r["kind"], "status": r["status"], "startedAt": r["startedAt"],
            "finishedAt": r["finishedAt"], "error": r["error"], "reportFile": r["reportFile"],
            "logs": r["logs"][log_since:], "logTotal": len(r["logs"]),
            "requests": r["stats"].log_since(req_since),
            "report": r["report"] if r["status"] != "running" else None,
            "live": _live_counts(r["stats"]),
        }


def _live_counts(stats: RequestStats) -> dict:
    total = errors = 0
    for s in stats.summary():
        total += s["requests"]
        errors += sum(v for k, v in s["statusCounts"].items() if k == "0" or int(k) >= 500)
    return {"requests": total, "serverErrors": errors}


# ======================================================================================================================
MANUAL_ACTIONS = {
    "me": ("GET", "/me"),
    "getCommunity": ("GET", "/energy-communities/{ecId}"),
    "updateCommunity": ("PUT", "/energy-communities/{ecId}"),
    "markets": ("GET", "/markets"),
    "market": ("GET", "/markets/{date}"),
    "listPositions": ("GET", "/positions"),
    "getPosition": ("GET", "/positions/{id}"),
    "submitPosition": ("POST", "/positions"),
    "updatePosition": ("PUT", "/positions/{id}"),
    "cancelPosition": ("DELETE", "/positions/{id}"),
    "allocations": ("GET", "/markets/{date}/allocations"),
    "trades": ("GET", "/markets/{date}/trades"),
}


def manual_request(state: UiState, payload: dict) -> dict:
    from egsim.simulator import eg_client
    from egsim.timeutil import resolve_delivery_date
    cfg, com = state.parsed_community(payload.get("uid", ""))
    client, login = eg_client(cfg, com, state.manual_stats)
    if client is None:
        msg = "keine Zugangsdaten" if login is None else f"Login fehlgeschlagen: {login.status} {login.code or ''}"
        return {"error": msg}
    action = payload.get("action")
    params = payload.get("params") or {}
    body = payload.get("body")
    if isinstance(body, str):
        body = body.strip()
        if body:
            try:
                body = json.loads(body, parse_float=Decimal)
            except ValueError:
                pass  # absichtlich kaputtes JSON wird als Rohtext gesendet
        else:
            body = None
    query = {k: v for k, v in (payload.get("query") or {}).items() if v not in (None, "")}
    if action == "raw":
        method = (payload.get("method") or "GET").upper()
        path = payload.get("path") or "/"
        template = path
    else:
        if action not in MANUAL_ACTIONS:
            return {"error": f"Unbekannte Aktion {action}"}
        method, template = MANUAL_ACTIONS[action]
        p = dict(params)
        if "date" in p and p["date"]:
            try:
                p["date"] = resolve_delivery_date(p["date"]).isoformat()
            except ValueError:
                pass  # ungültiges Datum bewusst durchreichen
        if "{ecId}" in template and not p.get("ecId"):
            me = client.me()
            p["ecId"] = ((me.body or {}).get("energyCommunity") or {}).get("id", "") if isinstance(me.body, dict) else ""
        try:
            path = template.format(**p)
        except KeyError as exc:
            return {"error": f"Parameter fehlt: {exc}"}
    raw_body = body if isinstance(body, str) else None
    headers = {}
    idem = (payload.get("idempotencyKey") or "").strip()
    if method == "POST" and template == "/positions" and not payload.get("noIdempotencyKey"):
        idem = idem or str(uuid.uuid4())  # Pflicht ab Vertrag 0.3; zum Testen abschaltbar
    if idem:
        headers["Idempotency-Key"] = idem
    r = client.call(method, path, json_body=None if raw_body is not None else body, raw_body=raw_body, params=query,
                    headers=headers or None)
    return {"method": method, "path": path, "query": query or None, "idempotencyKey": idem or None,
            "requestId": r.response_request_id,
            "request": _to_jsonable(body) if raw_body is None else raw_body,
            "status": r.status, "ms": round(r.elapsed_ms, 1), "response": _to_jsonable(r.body), "error": r.error}


# ======================================================================================================================
def make_handler(state: UiState, password: str | None = None, user: str = "egsim"):
    """password: optionaler Schutz per HTTP Basic Auth (z. B. auf einem VPS). /healthz bleibt offen."""
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode() if password else None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _authorized(self) -> bool:
            if not expected or urlparse(self.path).path == "/healthz":
                return True
            if hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="EGSim", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def log_message(self, fmt, *args):  # noqa: N802
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: Any = None, content_type: str = "application/json") -> None:
            if isinstance(body, (bytes, str)) and content_type != "application/json":
                data = body.encode() if isinstance(body, str) else body
            else:
                data = json.dumps(_to_jsonable(body), ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type + ("; charset=utf-8" if "text" in content_type or
                                                             "json" in content_type else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self) -> Any:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n))

        def do_GET(self):  # noqa: N802
            if not self._authorized():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                self._route("GET", u.path, q, None)
            except Exception as exc:  # noqa: BLE001
                log.exception("UI-Fehler")
                self._send(500, {"error": repr(exc)})

        def do_POST(self):  # noqa: N802
            self._with_body("POST")

        def do_PUT(self):  # noqa: N802
            self._with_body("PUT")

        def do_DELETE(self):  # noqa: N802
            self._with_body("DELETE")

        def _with_body(self, method: str) -> None:
            if not self._authorized():
                return
            u = urlparse(self.path)
            try:
                body = self._json()
            except ValueError:
                self._send(400, {"error": "Ungültiges JSON"})
                return
            try:
                self._route(method, u.path, {k: v[0] for k, v in parse_qs(u.query).items()}, body)
            except KeyError as exc:
                self._send(404, {"error": f"Nicht gefunden: {exc}"})
            except (ConfigError, ValueError, RuntimeError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                log.exception("UI-Fehler")
                self._send(500, {"error": repr(exc)})

        # -------------------------------------------------------------- routes
        def _route(self, method: str, path: str, q: dict, body: Any) -> None:
            parts = [unquote(p) for p in path.strip("/").split("/") if p]
            if method == "GET" and not parts:
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html")
                return
            if method == "GET" and parts == ["healthz"]:
                self._send(200, {"status": "UP", "version": __version__})
                return
            if not parts or parts[0] != "api":
                self._send(404, {"error": "unbekannter Pfad"})
                return
            p = parts[1:]

            if p == ["state"] and method == "GET":
                self._send(200, {
                    "version": __version__, "contractVersion": CONTRACT_VERSION,
                    "configPath": str(state.config_path), "config": state.raw, "configError": state.validate(),
                    "profiles": PROFILES, "mock": bool(state.mock), "mockUrl": state.mock_url,
                    "run": state.run_view(10**9, 10**9),
                })
            elif p == ["config"] and method == "PUT":
                if not isinstance(body, dict):
                    raise ValueError("Konfiguration muss ein Objekt sein")
                with state.lock:
                    state.raw = body
                    state.save()
                self._send(200, {"saved": True, "configError": state.validate()})
            elif p == ["scenarios"] and method == "GET":
                from egsim.scenarios import catalog_info
                self._send(200, catalog_info())
            elif p == ["config", "default-community"] and method == "GET":
                self._send(200, new_community_dict())
            elif len(p) == 3 and p[0] == "communities" and method == "POST":
                self._community_action(p[1], p[2], body or {})
            elif p == ["run"] and method == "POST":
                self._send(200, state.start_run((body or {}).get("kind", "run")))
            elif p == ["run", "stop"] and method == "POST":
                if state.run:
                    state.run["stop"].set()
                self._send(200, {"ok": True})
            elif p == ["run"] and method == "GET":
                self._send(200, state.run_view(int(q.get("logSince", 0)), int(q.get("reqSince", 0))))
            elif p == ["reports"] and method == "GET":
                self._send(200, _list_reports(state))
            elif len(p) == 2 and p[0] == "reports" and method == "GET":
                self._send(200, _read_report(state, p[1]))
            elif p == ["manual"] and method == "POST":
                self._send(200, manual_request(state, body or {}))
            elif p == ["manual", "log"] and method == "GET":
                self._send(200, state.manual_stats.log_since(int(q.get("since", 0))))
            elif p == ["plan"] and method == "GET":
                self._send(200, _plan(state, q))
            elif p and p[0] == "mock":
                self._mock(method, p[1:], body or {})
            else:
                self._send(404, {"error": "unbekannter Endpoint"})

        def _community_action(self, uid: str, action: str, body: dict) -> None:
            from egsim.simulator import register_community, test_connection
            if action == "test":
                cfg, com = state.parsed_community(uid)
                self._send(200, test_connection(cfg, com, state.manual_stats))
            elif action == "register":
                raw = state.community(uid)
                cfg = parse_config({**state.raw, "communities": []})
                email = (raw.get("email") or "").strip()
                if not email:
                    raise ValueError("Für die Registrierung eine E-Mail-Adresse eintragen")
                code = (raw.get("communityCode") or "").strip() or community_code_from(raw.get("name", ""))
                if not COMMUNITY_CODE_RE.match(code) or not 3 <= len(code) <= 40:
                    raise ValueError(f"communityCode '{code}' ungültig (Kleinbuchstaben, Ziffern, einzelne Bindestriche, "
                                     "3–40 Zeichen)")
                r = register_community(cfg.base_url, raw.get("name", "").strip(), email, code,
                                       raw.get("description") or None,
                                       timeout=cfg.timeout, verify_tls=cfg.verify_tls, stats=state.manual_stats)
                if r.status == 201 and isinstance(r.body, dict) and r.body.get("id"):
                    with state.lock:
                        raw["energyCommunityId"] = r.body["id"]
                        raw["communityCode"] = r.body.get("communityCode") or code
                        raw["registeredStatus"] = r.body.get("status")
                        state.save()
                self._send(200, {"status": r.status, "response": _to_jsonable(r.body), "error": r.error,
                                 "config": state.raw})
            elif action == "provision":
                from egsim.simulator import SimEG, Simulator
                cfg, com = state.parsed_community(uid)
                sim = Simulator(cfg, stats=state.manual_stats)
                sim.setup_admin()
                if not sim.admin:
                    err = next((f.message for f in sim.findings.items), "kein Admin-Key eingetragen")
                    raise ValueError(f"Automatische Einrichtung braucht einen gültigen Admin-Key: {err}")
                eg = SimEG(key=com.name, cfg=com)
                ok = sim.provision(eg) and sim._try_connect(eg, quiet=False)
                state.sync_communities(cfg)
                msgs = [f.message for f in sim.findings.items if f.severity in ("error", "warning")]
                self._send(200, {"ok": bool(ok), "messages": msgs, "config": state.raw})
            elif action == "approve" and state.mock:
                raw = state.community(uid)
                if not raw.get("energyCommunityId"):
                    raise ValueError("EG ist noch nicht registriert")
                creds = state.mock.approve(raw["energyCommunityId"])
                with state.lock:
                    raw["apiKey"], raw["password"], raw["registeredStatus"] = creds["apiKey"], creds["password"], "ACTIVE"
                    state.save()
                self._send(200, {"ok": True, "config": state.raw})
            else:
                self._send(404, {"error": f"Aktion {action} unbekannt"})

        def _mock(self, method: str, p: list[str], body: dict) -> None:
            if not state.mock:
                self._send(404, {"error": "UI läuft ohne Mock"})
                return
            from egsim.timeutil import now_utc, resolve_delivery_date
            m = state.mock
            if p == ["markets"] and method == "GET":
                self._send(200, [m._market_public(x) for x in m.markets.values()])
            elif p == ["markets"] and method == "POST":
                d = resolve_delivery_date(body.get("date") or "tomorrow").isoformat()
                closes = now_utc() + timedelta(minutes=float(body.get("closesInMinutes") or 60))
                mk = m.add_market(d, body.get("resolution") or "DAY", str(body.get("buy") or "0.10"),
                                  str(body.get("sell") or "0.14"), str(body.get("reserve") or "0.20"), closes,
                                  show_prices=body.get("showPrices", True) is not False)
                self._send(200, m._market_public(mk))
            elif len(p) == 3 and p[0] == "markets" and p[2] == "close" and method == "POST":
                status, mk = m.close_market(resolve_delivery_date(p[1]).isoformat())
                self._send(200, {"httpStatus": status, "market": m._market_public(mk)})
            elif p == ["reserve"] and method == "PUT":
                cap = body.get("capacityKwh")
                m.reserve_capacity = Decimal(str(cap)) if cap not in (None, "") else None
                self._send(200, {"capacityKwh": m.reserve_capacity})
            else:
                self._send(404, {"error": "unbekannter Mock-Endpoint"})

    return Handler


def _report_dir(state: UiState) -> Path | None:
    rd = (state.raw.get("run") or {}).get("reportDir", "reports")
    return Path(rd) if rd else None


def _list_reports(state: UiState) -> list[dict]:
    d = _report_dir(state)
    if not d or not d.exists():
        return []
    out = []
    for f in sorted(d.glob("egsim-*.json"), reverse=True)[:50]:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
            out.append({"file": f.name, "startedAt": r.get("startedAt"), "mode": r.get("mode"),
                        "totals": r.get("totals"), "baseUrl": r.get("baseUrl")})
        except ValueError:
            continue
    return out


def _read_report(state: UiState, name: str) -> dict:
    d = _report_dir(state)
    if not d or "/" in name or "\\" in name or not name.endswith(".json"):
        raise KeyError(name)
    f = d / name
    if not f.exists():
        raise KeyError(name)
    return json.loads(f.read_text(encoding="utf-8"))


def _plan(state: UiState, q: dict) -> list[dict]:
    import random
    from egsim.profiles import plan_positions, select_intervals
    from egsim.timeutil import quarter_hours, resolve_delivery_date
    cfg = parse_config(state.raw)
    d = resolve_delivery_date(q.get("date") or cfg.delivery_dates[0])
    res = q.get("resolution", "DAY")
    all_iv = quarter_hours(d) if res == "QUARTER_HOUR" else None
    seed = cfg.seed if cfg.seed is not None else 0
    rows = []
    for c in cfg.active_communities:
        rng = random.Random(f"{seed}:{c.name}:{d.isoformat()}")
        ivs = select_intervals(all_iv, cfg.quarter_hour_intervals, rng) if all_iv else None
        for p in plan_positions(c, res, ivs, all_iv, rng):
            rows.append({"community": c.name, **_to_jsonable(p.body(d.isoformat()))})
    return rows


# ======================================================================================================================
def start_mock(port: int = 0, demo_egs: int = 3, closes_in_minutes: float = 3.0):
    """Mock im selben Prozess, mit Demo-EGs und automatischem Marktschluss nach closesAt."""
    from egsim.mockserver import DEMO_NAMES, MockPlatform, start_server
    from egsim.timeutil import now_utc, resolve_delivery_date
    platform = MockPlatform()
    platform.add_market(resolve_delivery_date("tomorrow").isoformat(), "DAY",
                        closes_at=now_utc() + timedelta(minutes=closes_in_minutes))
    platform.add_market(resolve_delivery_date("+2").isoformat(), "QUARTER_HOUR",
                        closes_at=now_utc() + timedelta(minutes=closes_in_minutes + 5))
    egs = [platform.add_demo_community(DEMO_NAMES[i % len(DEMO_NAMES)]) for i in range(demo_egs)]
    srv = start_server(platform, "127.0.0.1", port)
    threading.Thread(target=platform.auto_settle_loop, args=(threading.Event(),), daemon=True).start()
    return platform, f"http://127.0.0.1:{srv.server_address[1]}", egs


def mock_config(url: str, egs: list[dict]) -> dict:
    flows = [
        {"generation": {"profile": "solar", "dailyKwh": [80, 150]},
         "priceExpectation": {"probability": 0.8, "range": [0.07, 0.11], "decimals": 3}},
        {"generation": {"profile": "wind", "dailyKwh": [40, 90], "noise": 0.3},
         "priceExpectation": {"probability": 1, "range": [0.09, 0.13], "decimals": 3}},
        {"consumption": {"profile": "household", "dailyKwh": [120, 200]}},
        {"consumption": {"profile": "business", "dailyKwh": [60, 120]}},
        {"generation": {"profile": "solar", "dailyKwh": [30, 70]}, "consumption": {"profile": "household", "dailyKwh": [40, 80]}},
    ]
    cfg = default_config_dict()
    cfg["platform"]["baseUrl"] = url
    cfg["platform"]["adminApiKey"] = "admin-key"
    cfg["run"]["waitForSettlementMinutes"] = 5
    cfg["run"]["allParticipantsSimulated"] = True
    cfg["run"]["closeMarket"] = True
    cfg["run"]["scenarioDates"] = ["+2"]
    from egsim.scenarios import CATALOG
    cfg["run"]["scenarios"] = [sc.id for sc in CATALOG]
    cfg["communities"] = [new_community_dict(e["name"], email=e["email"], apiKey=e["apiKey"], password=e["password"],
                                             energyCommunityId=e["id"], communityCode=e.get("communityCode", ""),
                                             **flows[i % len(flows)])
                          for i, e in enumerate(egs)]
    return cfg


def serve(args) -> None:
    mock = mock_url = None
    config_path = Path(args.config)
    if args.mock:
        mock, mock_url, egs = start_mock(demo_egs=args.demo_egs, closes_in_minutes=args.closes_in_minutes)
        config_path = Path(args.config if args.config != "egsim-ui.json" else "egsim-mock.json")
        config_path.write_text(json.dumps(mock_config(mock_url, egs), indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Mock läuft auf {mock_url} mit {len(egs)} Demo-EGs; Märkte schließen automatisch nach closesAt")
    state = UiState(config_path, mock, mock_url)
    password = os.environ.get("EGSIM_UI_PASSWORD") or None
    user = os.environ.get("EGSIM_UI_USER") or "egsim"
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(state, password, user))
    srv.daemon_threads = True
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{srv.server_address[1]}"
    print(f"EGSim-UI: {url}  (Konfiguration: {config_path.resolve()}) – Strg+C beendet")
    if password:
        print(f"UI ist passwortgeschützt (Benutzer: {user}, Passwort aus EGSIM_UI_PASSWORD)")
    elif args.host not in ("127.0.0.1", "localhost"):
        print("Achtung: Die UI ist ohne Anmeldung erreichbar und zeigt Zugangsdaten. EGSIM_UI_PASSWORD setzen!")
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
