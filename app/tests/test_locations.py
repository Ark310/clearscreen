"""Where ClearScreen looks for its folders, and the SSMS button's connection details."""
import pytest

from clearscreen import config
from clearscreen.admin import ops


def test_data_dir_prefers_env_then_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "registry_value", lambda name: r"D:\ClearScreen\Data" if name == "DataDir" else None)
    monkeypatch.setenv("CLS_DATA_DIR", str(tmp_path))
    assert config.data_dir() == tmp_path
    monkeypatch.delenv("CLS_DATA_DIR")
    assert str(config.data_dir()) == r"D:\ClearScreen\Data"
    monkeypatch.setattr(config, "registry_value", lambda name: None)
    assert config.data_dir().name == "ClearScreen"                 # ProgramData fallback


@pytest.mark.parametrize("cs,server,db", [
    ("Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=ClearScreen;Trusted_Connection=yes", "localhost", "ClearScreen"),
    ("Data Source=SQLHOST\\INST;Initial Catalog=CLS_Prod;Integrated Security=true", "SQLHOST\\INST", "CLS_Prod"),
    ("server = myhost ; database = x", "myhost", "x"),
])
def test_connection_string_parts(cs, server, db):
    assert ops._cs_part(cs, "Server", "Data Source") == server
    assert ops._cs_part(cs, "Database", "Initial Catalog") == db


def test_open_ssms_without_ssms(monkeypatch):
    monkeypatch.setattr(ops, "ssms_path", lambda: None)
    with pytest.raises(FileNotFoundError, match="not installed"):
        ops.open_ssms("Server=localhost;Database=ClearScreen")


def test_open_ssms_arguments(monkeypatch, tmp_path):
    exe = tmp_path / "Ssms.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(ops, "ssms_path", lambda: exe)
    seen = {}
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: seen.setdefault("args", args))
    assert ops.open_ssms("Server=localhost;Database=ClearScreen;Trusted_Connection=yes") == "localhost / ClearScreen"
    assert seen["args"] == [str(exe), "-S", "localhost", "-d", "ClearScreen", "-C", "-nosplash"]
