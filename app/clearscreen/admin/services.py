# -*- coding: utf-8 -*-
"""The three Windows services ClearScreen installs, and how to control them (sc.exe)."""
import subprocess
import time

# (service name, what it is), in start order
SERVICES = [
    ("ClearScreenEmbed", "Embedding service (bge-m3 on CPU, port 11434, local only)"),
    ("ClearScreenApi", "ClearScreen API (port 8000, local only)"),
    ("ClearScreenHttps", "HTTPS front door (Caddy, port 443, public)"),
]
NAMES = [s for s, _ in SERVICES]

_NO_WINDOW = 0x08000000                       # CREATE_NO_WINDOW: no console flashes from the GUI


def _sc(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True, creationflags=_NO_WINDOW)


def status(name: str) -> str:
    """RUNNING, STOPPED, START_PENDING, STOP_PENDING ... or NOT_INSTALLED."""
    out = _sc("query", name)
    if out.returncode == 1060:
        return "NOT_INSTALLED"
    for line in out.stdout.splitlines():
        if "STATE" in line:
            return line.split()[-1]
    return "UNKNOWN"


def _wait(name: str, want: str, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if status(name) == want:
            return True
        time.sleep(0.5)
    return status(name) == want


def start(name: str, timeout: float = 60) -> str:
    if status(name) != "RUNNING":
        _sc("start", name)
        _wait(name, "RUNNING", timeout)
    return status(name)


def stop(name: str, timeout: float = 60) -> str:
    if status(name) not in ("STOPPED", "NOT_INSTALLED"):
        _sc("stop", name)
        _wait(name, "STOPPED", timeout)
    return status(name)


def restart(name: str) -> str:
    stop(name)
    return start(name)


def start_all() -> dict:
    return {n: start(n) for n in NAMES}


def stop_all() -> dict:
    return {n: stop(n) for n in reversed(NAMES)}


def restart_all() -> dict:
    stop_all()
    return start_all()
