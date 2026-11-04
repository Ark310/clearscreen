"""Write app/requirements.lock from a folder of downloaded wheels: every package pinned to an
exact version and SHA-256 (pip --require-hashes).

  python tools/make_lock.py <wheel folder>

Make the wheel folder with, e.g.:
  pip download --only-binary=:all: --platform win_amd64 --python-version 3.13 --implementation cp -d wheels \
      fastapi uvicorn pydantic httpx numpy==2.4.6 rapidfuzz pycountry python-dateutil ftfy openpyxl pyodbc
"""
import hashlib
import re
import sys
from pathlib import Path

HEADER = """# ClearScreen runtime dependencies for CPython 3.13 on Windows x64.
# Installed with: pip install --require-hashes --only-binary=:all: -r requirements.lock
# Every package is pinned to an exact version and SHA-256, so a changed download is refused.
# Regenerate with tools/make_lock.py.
"""


def main(folder: str) -> None:
    entries = []
    for wheel in sorted(Path(folder).glob("*.whl")):
        m = re.match(r"([A-Za-z0-9_.]+?)-(\d[^-]*)-", wheel.name)
        name, version = m.group(1).replace("_", "-").lower(), m.group(2)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        entries.append(f"{name}=={version} \\\n    --hash=sha256:{digest}\n")
    out = Path(__file__).resolve().parents[1] / "app" / "requirements.lock"
    out.write_bytes((HEADER + "".join(entries)).encode("utf-8"))      # bytes: "\n" line ends on every OS
    print(f"{len(entries)} packages -> {out}")


if __name__ == "__main__":
    main(sys.argv[1])
