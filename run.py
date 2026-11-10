#!/usr/bin/env python3
"""One-command launcher for the F1 live tracker.

    python run.py            # asks: replay or live
    python run.py replay     # watch the saved 2026 São Paulo GP
    python run.py live       # connect to the live F1 timing feed

First run creates a .venv and installs the dependencies by itself.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VPY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_env() -> None:
    """Create .venv + install requirements once, then continue running inside it."""
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10 or newer is required (you have %s)." % sys.version.split()[0])
    if Path(sys.prefix).resolve() == VENV.resolve():
        return
    stamp = VENV / ".deps-installed"
    req = ROOT / "requirements.txt"
    if not VPY.exists():
        print("First run: creating a private Python environment (.venv) ...")
        venv.create(VENV, with_pip=True)
    if not stamp.exists() or stamp.stat().st_mtime < req.stat().st_mtime:
        print("Installing dependencies (one-off, about a minute) ...")
        subprocess.check_call([str(VPY), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", str(req)])
        stamp.write_text("ok")
    sys.exit(subprocess.call([str(VPY), str(Path(__file__).resolve()), *sys.argv[1:]]))


def free_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    sys.exit("No free port found near %d. Close other programs or pass --port." % start)


def choose() -> str:
    print("\nF1 Live Tracker\n")
    print("  1) Replay - pick any race from 2024 onwards (or the saved 2026 São Paulo GP, works offline)")
    print("  2) Live   - connect to the live F1 timing feed (only shows cars while a session is running)\n")
    while True:
        try:
            pick = input("Choose 1 or 2: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            sys.exit(0)
        if pick in ("1", "r", "replay"):
            return "replay"
        if pick in ("2", "l", "live"):
            return "live"


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description="F1 live tracker launcher")
    ap.add_argument("mode", nargs="?", choices=["replay", "live"])
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = ap.parse_args()

    ensure_env()
    mode = args.mode or choose()
    port = free_port(args.port)
    cmd = [sys.executable, "-m", "tracker"]
    if mode == "replay":
        cmd += ["serve", "--port", str(port), "--speed", "10"]
    else:
        cmd += ["live", "--port", str(port)]

    url = f"http://localhost:{port}"
    proc = subprocess.Popen(cmd, cwd=ROOT)
    try:
        for _ in range(120):                      # wait until the page answers
            if proc.poll() is not None:
                sys.exit(proc.returncode)
            try:
                urllib.request.urlopen(url, timeout=1).close()
                break
            except OSError:
                time.sleep(0.5)
        print(f"\n  >>> Open {url} <<<   (Ctrl+C to stop)\n")
        if mode == "replay":
            print("  Replay: pick a race in the page. Speed buttons / Space to pause once it plays.")
        else:
            print("  Live: cars appear when a session is running. No session now? Try the replay.")
        if not args.no_browser:
            webbrowser.open(url)
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
