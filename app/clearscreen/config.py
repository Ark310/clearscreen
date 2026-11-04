# -*- coding: utf-8 -*-
"""Where ClearScreen keeps its configuration, and the two files in it.

  %ProgramData%\\ClearScreen\\config\\settings.json   plain settings (no secrets)
  %ProgramData%\\ClearScreen\\config\\secure.dat      secrets, encrypted with Windows DPAPI
                                                      (machine scope):
        groq_api_key, sql_connection_string, clients {ClientID: {name, key_hash, key_hint,
        created_utc, rotated_utc, enabled}}

The config folder is readable only by Administrators, SYSTEM and the API service account (the
installer sets this). DPAPI ties secure.dat to this machine: copied elsewhere it cannot be read.
API keys are stored only as SHA-256 hashes; the key itself is shown once when it is generated.
Both files are re-read when they change, so the service picks up edits without a restart.

Set CLS_DATA_DIR to use another folder (tests).
"""
import ctypes
import json
import os
import tempfile
import threading
from ctypes import wintypes
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("CLS_DATA_DIR") or Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "ClearScreen")


def config_dir() -> Path:
    return data_dir() / "config"


def logs_dir() -> Path:
    return data_dir() / "logs"


# --------------------------------------------------------------- settings ---
@dataclass
class Settings:
    domain: str = "api.clearscreen.example"
    acme_email: str = ""                       # Let's Encrypt contact for the HTTPS certificate
    api_host: str = "127.0.0.1"                # the API listens only locally; HTTPS proxy faces out
    api_port: int = 8000
    embed_url: str = "http://127.0.0.1:11434"  # the local embedding service (bge-m3 on CPU)
    embed_model: str = "bge-m3"
    groq_model: str = "qwen/qwen3.8-27b"
    groq_reasoning_effort: str = "high"
    ai_max_cases: int = 5                      # most similar whitelist records the AI reviews per alert
    rate_limit_per_minute: int = 120           # requests per client per minute
    max_request_mb: int = 100                  # largest request body accepted
    max_match_batch: int = 500                 # alerts per /match request
    max_add_batch: int = 50_000                # records per /whitelist/add request
    enable_docs: bool = True                   # Swagger page at /docs
    embed_worker_seconds: int = 30             # how often new records are embedded in the background

    @classmethod
    def load(cls) -> "Settings":
        p = config_dir() / "settings.json"
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        _atomic_write(config_dir() / "settings.json", json.dumps(asdict(self), indent=2).encode("utf-8"))


# ------------------------------------------------------------------ DPAPI ---
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_CRYPTPROTECT_LOCAL_MACHINE = 0x4
_ENTROPY = b"ClearScreen secure store v1"


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def protect(data: bytes) -> bytes:
    """Encrypt with Windows DPAPI, machine scope: only this machine can decrypt."""
    out, ent = _Blob(), _blob(_ENTROPY)
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(_blob(data)), "ClearScreen", ctypes.byref(ent), None,
                                                  None, _CRYPTPROTECT_UI_FORBIDDEN | _CRYPTPROTECT_LOCAL_MACHINE,
                                                  ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def unprotect(data: bytes) -> bytes:
    out, ent = _Blob(), _blob(_ENTROPY)
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(_blob(data)), None, ctypes.byref(ent), None, None,
                                                    _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a temporary file in the same folder, then replace: never a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ----------------------------------------------------------- secure store ---
class SecureStore:
    """secure.dat, decrypted in memory and re-read when the file changes on disk."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "secure.dat"
        self._lock = threading.Lock()
        self._mtime = None
        self._data: dict = {}

    def data(self) -> dict:
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime_ns
            except FileNotFoundError:
                self._mtime, self._data = None, {}
                return self._data
            if mtime != self._mtime:
                self._data = json.loads(unprotect(self.path.read_bytes()).decode("utf-8"))
                self._mtime = mtime
            return self._data

    def update(self, change) -> dict:
        """Apply change(data) to a fresh copy and write it back encrypted. Returns the new data."""
        with self._lock:
            current = json.loads(unprotect(self.path.read_bytes()).decode("utf-8")) if self.path.exists() else {}
            change(current)
            _atomic_write(self.path, protect(json.dumps(current).encode("utf-8")))
            self._data, self._mtime = current, self.path.stat().st_mtime_ns
            return current

    # convenience
    def get(self, key, default=None):
        return self.data().get(key, default)

    def clients(self) -> dict:
        return self.data().get("clients", {})
