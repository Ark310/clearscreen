"""Build docs/pdf/*.pdf from the Markdown guides: Markdown -> styled HTML -> PDF (headless Chrome).

  pip install markdown
  python tools/build_docs.py [path to chrome.exe]
"""
import re
import subprocess
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUT = DOCS / "pdf"
CHROME = sys.argv[1] if len(sys.argv) > 1 else r"C:\Program Files\Google\Chrome\Application\chrome.exe"
VERSION = (ROOT / "VERSION").read_text().strip()

CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
body { font-family: "Segoe UI", Arial, sans-serif; font-size: 10.5pt; line-height: 1.5; color: #1f2933; }
.cover { border-bottom: 3px solid #22324a; margin-bottom: 18px; padding-bottom: 10px; }
.cover .brand { font-size: 11pt; letter-spacing: .08em; color: #5f6b7a; text-transform: uppercase; }
h1 { font-size: 22pt; color: #22324a; margin: 4px 0 2px; }
.cover .meta { color: #5f6b7a; font-size: 9.5pt; }
h2 { font-size: 15pt; color: #22324a; border-bottom: 1px solid #d5dbe3; padding-bottom: 3px; margin-top: 26px;
     page-break-after: avoid; }
h3 { font-size: 12pt; color: #22324a; margin-top: 18px; page-break-after: avoid; }
p, li { orphans: 3; widows: 3; }
img { max-width: 100%; border: 1px solid #d5dbe3; border-radius: 4px; display: block; margin: 8px 0 12px;
      page-break-inside: avoid; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 14px; font-size: 9.5pt; page-break-inside: auto; }
th { background: #22324a; color: #fff; text-align: left; padding: 5px 7px; }
th code { background: rgba(255,255,255,.18); color: #fff; }
td { border: 1px solid #d5dbe3; padding: 4px 7px; vertical-align: top; }
tr:nth-child(even) td { background: #f5f7fa; }
tr { page-break-inside: avoid; }
code { font-family: Consolas, monospace; font-size: 9pt; background: #f1f3f6; padding: 1px 4px; border-radius: 3px; }
pre { background: #1f2933; color: #e8edf3; padding: 9px 11px; border-radius: 5px; white-space: pre-wrap;
      word-break: break-all; page-break-inside: avoid; }
pre code { background: none; color: inherit; padding: 0; font-size: 8.6pt; }
blockquote { border-left: 4px solid #9a6700; background: #fdf6e6; margin: 10px 0; padding: 6px 12px; }
hr { border: none; border-top: 1px solid #d5dbe3; margin: 18px 0; }
a { color: #2f5aa8; text-decoration: none; }
"""


def list_friendly(text: str) -> str:
    """Python-Markdown needs 4-space indentation inside list items and does not support fenced code
    there. The guides use GitHub style (3 spaces, fenced code in lists), so: indent 3-space
    continuation lines to 4, and turn indented fenced blocks into 8-space indented code blocks."""
    out, fence = [], None
    for line in text.split("\n"):
        if fence is not None:
            if line.strip() == "```":
                out.append("")
                fence = None
            else:
                out.append(" " * 8 + line[fence:] if line.strip() else "")
            continue
        m = re.match(r"^( {2,4})```", line)
        if m:
            fence = len(m.group(1))
            out.append("")
            continue
        if re.match(r"^ {3}\S", line):
            line = " " + line
        out.append(line)
    return "\n".join(out)


def build(md_file: Path) -> Path:
    text = list_friendly(md_file.read_text(encoding="utf-8"))
    title = re.search(r"^# (.+)$", text, re.M).group(1)
    body = re.sub(r"^# .+$", "", text, count=1, flags=re.M)
    html_body = markdown.markdown(body, extensions=["tables", "fenced_code", "sane_lists"])
    product, _, guide = title.partition(" — ")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title><style>{CSS}</style></head>
<body><div class="cover"><div class="brand">{product}</div><h1>{guide or title}</h1>
<div class="meta">Version {VERSION}</div></div>{html_body}</body></html>"""
    html_file = DOCS / f".{md_file.stem}.html"               # next to the guide, so images/ resolves
    html_file.write_text(html, encoding="utf-8")
    OUT.mkdir(exist_ok=True)
    pdf = OUT / f"ClearScreen - {guide or title}.pdf"
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", html_file.as_uri()], check=True, capture_output=True, timeout=180)
    html_file.unlink()
    return pdf


if __name__ == "__main__":
    for md in sorted(DOCS.glob("[0-9]-*.md")):
        print("built", build(md))
