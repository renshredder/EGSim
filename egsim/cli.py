"""Kommandozeile: egsim ui | egsim run | egsim mock | egsim plan"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys

from egsim import CONTRACT_VERSION, __version__
from egsim.config import ConfigError, load_config


def _setup_logging(verbose: int) -> None:
    level = logging.DEBUG if verbose >= 2 else logging.INFO if verbose >= 1 else logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if verbose < 2:
        logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_ui(args: argparse.Namespace) -> int:
    from egsim.ui.server import serve
    serve(args)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Headless-Lauf mit der von der UI gespeicherten Konfiguration (z. B. für CI)."""
    from pathlib import Path

    from egsim.report import format_summary, write_report
    from egsim.simulator import Simulator

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    if args.base_url:
        cfg.base_url = args.base_url
    if args.date:
        cfg.delivery_dates = args.date
    if args.no_checks:
        cfg.checks = False
    if args.seed is not None:
        cfg.seed = args.seed
    if args.wait is not None:
        cfg.wait_for_settlement_minutes = args.wait
    if args.close:
        cfg.close_market = True

    sim = Simulator(cfg)
    if args.scenarios is not None:
        if args.scenarios:
            cfg.scenarios = [x.strip() for x in args.scenarios.split(",") if x.strip()]
        report = sim.run_scenarios()
    else:
        report = sim.verify_only() if args.verify_only else sim.run()
    path = write_report(report, str(Path(cfg.report_dir) / "egsim-{timestamp}.json") if cfg.report_dir else None)
    print(format_summary(report))
    if path:
        print(f"Bericht: {path}")
    t = report["totals"]
    return 1 if cfg.fail_on_findings and (t["errors"] or t["checksFailed"]) else 0


def cmd_plan(args: argparse.Namespace) -> int:
    from egsim.profiles import plan_positions, select_intervals
    from egsim.timeutil import quarter_hours, resolve_delivery_date

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    seed = cfg.seed if cfg.seed is not None else 0
    d = resolve_delivery_date(args.date or cfg.delivery_dates[0])
    all_iv = quarter_hours(d) if args.resolution == "QUARTER_HOUR" else None
    rows = []
    for c in cfg.active_communities:
        rng = random.Random(f"{seed}:{c.name}:{d.isoformat()}")
        ivs = select_intervals(all_iv, cfg.quarter_hour_intervals, rng) if all_iv else None
        for p in plan_positions(c, args.resolution, ivs, all_iv, rng):
            rows.append({"community": c.name, **p.body(d.isoformat())})
    print(json.dumps(rows, indent=2, default=str, ensure_ascii=False))
    return 0


def cmd_mock(args: argparse.Namespace) -> int:
    from egsim.mockserver import serve
    serve(args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="egsim", description=f"Dummy-Energiegemeinschaften für die Handelsplattform-API "
                                                          f"(Datenvertrag {CONTRACT_VERSION})")
    p.add_argument("--version", action="version", version=f"egsim {__version__}")
    p.add_argument("-v", "--verbose", action="count", default=1, help="-vv: Debug inkl. Requests")
    p.add_argument("-q", "--quiet", action="store_true", help="nur Zusammenfassung")
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("ui", help="Web-UI starten (Konfiguration, Läufe, Ergebnisse)")
    u.add_argument("-c", "--config", default="egsim-ui.json", help="Konfigurationsdatei (wird angelegt)")
    u.add_argument("--host", default="127.0.0.1")
    u.add_argument("--port", type=int, default=8090)
    u.add_argument("--open", action="store_true", help="Browser öffnen")
    u.add_argument("--mock", action="store_true", help="Demo-Modus mit eingebautem Mock und Demo-EGs")
    u.add_argument("--demo-egs", type=int, default=3)
    u.add_argument("--closes-in-minutes", type=float, default=3, help="Demo: closesAt des Markts für morgen")
    u.set_defaults(func=cmd_ui)

    r = sub.add_parser("run", help="Lauf ohne UI mit gespeicherter Konfiguration (z. B. CI)")
    r.add_argument("-c", "--config", default="egsim-ui.json")
    r.add_argument("--base-url")
    r.add_argument("--date", action="append", help="Liefertag (mehrfach möglich)")
    r.add_argument("--no-checks", action="store_true")
    r.add_argument("--seed", type=int)
    r.add_argument("--wait", type=float, help="Minuten auf Settlement warten")
    r.add_argument("--verify-only", action="store_true", help="nur Ergebnisse prüfen")
    r.add_argument("--scenarios", nargs="?", const="", help="Szenarien ausführen (ohne Wert: Auswahl aus der Konfiguration, "
                                                             "sonst z. B. SZ-01,SZ-04)")
    r.add_argument("--close", action="store_true", help="Markt per Admin-Key selbst schließen")
    r.set_defaults(func=cmd_run)

    pl = sub.add_parser("plan", help="geplante Positionen anzeigen (ohne Plattform)")
    pl.add_argument("-c", "--config", default="egsim-ui.json")
    pl.add_argument("--date")
    pl.add_argument("--resolution", choices=["DAY", "QUARTER_HOUR"], default="DAY")
    pl.set_defaults(func=cmd_plan)

    m = sub.add_parser("mock", help="Mock der Plattform als eigenen Server starten")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8080)
    m.add_argument("--admin-api-key", default="admin-key")
    m.add_argument("--admin-email", default="admin@platform.example")
    m.add_argument("--admin-password", default="admin-password")
    m.add_argument("--market", action="append", help="DATUM[:DAY|QUARTER_HOUR[:ANKAUF:VERKAUF:RESERVE]] (Standard tomorrow:DAY)")
    m.add_argument("--closes-in-minutes", type=float, default=120)
    m.add_argument("--reserve-capacity", default=None, help="max. Reserve je Intervall in kWh")
    m.add_argument("--settlement-delay", type=float, default=0)
    m.add_argument("--demo-egs", type=int, default=0, help="N aktive Demo-EGs anlegen und Zugangsdaten ausgeben")
    m.add_argument("--auto-settle", action="store_true", help="Märkte nach closesAt automatisch schließen")
    m.add_argument("--login-rate-limit", type=int, default=10,
                   help="Login-Fehlversuche je E-Mail bis 429 RATE_LIMITED (0 = aus)")
    m.set_defaults(func=cmd_mock)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(0 if args.quiet else args.verbose)
    return args.func(args)
