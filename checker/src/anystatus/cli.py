"""The `anystatus-checker` command: run, validate, once."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from anystatus import __version__
from anystatus.config import Config, load_config
from anystatus.document import Snapshot
from anystatus.scheduler import Scheduler, SystemClock, ThreadLauncher
from anystatus.server import ResultsServer
from anystatus.store import Store
from anystatus.table import ConfigError

log = logging.getLogger("anystatus")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="anystatus-checker", description="The MMM-AnyStatus checker.")
    parser.add_argument("--version", action="version", version=f"anystatus-checker {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every run (debug level)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the service")
    run.add_argument("config", type=Path)
    validate = sub.add_parser("validate", help="check the config and exit, listing all problems")
    validate.add_argument("config", type=Path)
    once = sub.add_parser("once", help="run checks once, print the document, exit")
    once.add_argument("config", type=Path)
    once.add_argument("--id", action="append", help="only this check (repeatable)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"{args.config}: {len(exc.problems)} problem(s):", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    for warning in config.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.command == "validate":
        print(f"{args.config}: ok, {len(config.checks)} check(s)")
        return 0
    if args.command == "once":
        return run_once(config, args.id)
    return run_service(config)


def run_once(config: Config, ids: Sequence[str] | None) -> int:
    specs = list(config.checks)
    if ids:
        unknown = [i for i in ids if i not in {s.id for s in specs}]
        if unknown:
            print(f"unknown check id: {', '.join(unknown)}", file=sys.stderr)
            return 2
        specs = [s for s in specs if s.id in ids]
    wake = threading.Event()
    clock = SystemClock()
    snapshot = Snapshot(config.name)
    scheduler = Scheduler(specs, clock=clock, launcher=ThreadLauncher(clock, wake), snapshot=snapshot, store=None, spread=0.0)
    scheduler.start()
    while not scheduler.first_runs_done():
        scheduler.tick()
        wake.wait(scheduler.next_wakeup())
        wake.clear()
    scheduler.stop()
    print(json.dumps(json.loads(snapshot.json_bytes()), indent=2, ensure_ascii=False))
    return 0


def run_service(config: Config) -> int:
    try:
        store = Store(config.state)
    except sqlite3.Error as exc:
        print(f"cannot open the memory file {config.state}: {exc}", file=sys.stderr)
        return 2
    snapshot = Snapshot(config.name)
    try:
        server = ResultsServer(config.listen[0], config.listen[1], snapshot)
    except OSError as exc:
        print(f"cannot listen on {config.listen[0]}:{config.listen[1]}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    stop = threading.Event()
    wake = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()
        wake.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    clock = SystemClock()
    scheduler = Scheduler(config.checks, clock=clock, launcher=ThreadLauncher(clock, wake), snapshot=snapshot, store=store)
    scheduler.start()
    server.start()
    log.info("%s: %d check(s), serving http://%s:%d/results", config.name, len(config.checks), *config.listen)
    try:
        scheduler.run_forever(stop, wake)
    finally:
        scheduler.stop()
        server.stop()
        store.close()
    log.info("stopped")
    return 0
