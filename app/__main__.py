"""BioQL Studio's service, from the command line.

  python3 -m app serve            start it on 127.0.0.1:7810 (the native shell runs exactly this)
  python3 -m app serve --port N   another port, for a second copy or a test. It is still 127.0.0.1 only
  python3 -m app token            print the window's URL, token in the fragment
  python3 -m app who              what is listening on Studio's port, by its own /api/ping

It never binds 3000 or 5035 (BioQL) or 7790/7799/7797/7788/7791 (Agtr Desktop), and installs no LaunchAgent.
"""
from __future__ import annotations

import argparse
import os
import sys

import studio

from . import PORT
from .service import read_secret, run, who_answers

# Ports that belong to something else on this Mac. Agtr's service takes its port from the command line and binds
# whatever it is given (service.py:1487); Studio refuses these outright, so a typo cannot land on BioQL's backend.
TAKEN = {3000: "BioQL's frontend", 5035: "BioQL's Flask backend", 7790: "Agtr Desktop's window",
         7799: "Agtr's local hub", 7797: "Agtr", 7788: "Agtr", 7791: "Agtr"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m app")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve"); sv.add_argument("--port", type=int, default=PORT)
    sub.add_parser("token")
    w = sub.add_parser("who"); w.add_argument("--port", type=int, default=PORT)
    ns = ap.parse_args(argv)

    if ns.cmd == "who":
        seen = who_answers(ns.port)
        print(f"nothing answers on 127.0.0.1:{ns.port}" if seen is None else f"{ns.port}: {seen}")
        return 0
    if ns.cmd == "token":
        print(f"http://127.0.0.1:{PORT}/#t={read_secret(studio.home())}")
        return 0
    if ns.port in TAKEN:
        print(f"port {ns.port} belongs to {TAKEN[ns.port]}. BioQL Studio will not bind it.", file=sys.stderr)
        return 2
    out = run(port=ns.port)
    if out is None:
        return 1
    # The serving loop has ended and `run()` has already cleaned up. Returning here would NOT end the process:
    # `ThreadPoolExecutor` threads are not daemons, and Python joins every one of them at interpreter shutdown, so
    # a single call in flight in `fanout` or `campaign` keeps this process alive — measured 2026-11-19: a pool with
    # one 30-second task kept it up for the whole 30 seconds after SIGTERM, while `run()` had long returned. The
    # native shell quits by `terminate()` and then `waitUntilExit()` on its main thread, so that wait is the app
    # beachballing for as long as the call lasts, and BioQL's calls are allowed twelve hours. The person asked to
    # quit; the pipeline is BioQL's and keeps running there. So this process stops now, after its own cleanup.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
