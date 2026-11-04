import re

import ftfy

from clearscreen.scoring.ln_parser import parse_listing as v2_parse_listing

_WS = re.compile(r"\s+")
_LEXISNEXIS = re.compile(r"Address Information|AKA Listing")

# Section-header labels that can appear immediately after an empty optional
# field (e.g. "Country:    AKA Listing"). Lazy regex captures on empty
# fields can accidentally swallow these labels as if they were the field's
# value; filter them out post-capture.
_SECTION_LABELS = (
    "AKA Listing",
    "AKA",
    "Additional Information",
    "Address Information",
    "Reason",
    "Notes",
    "Position",
    "Reference No",
)


def _is_section_label(value: str) -> bool:
    """True if value is (or begins with) a known section header, not real data."""
    return any(value == label or value.startswith(label) for label in _SECTION_LABELS)


def normalize_text(s: str | None) -> str:
    """Repair encoding artifacts and collapse whitespace. None -> ''."""
    if not s:
        return ""
    fixed = ftfy.fix_text(str(s))
    return _WS.sub(" ", fixed).strip()


def parse_search_fields(fields: str | None, values: str | None) -> dict:
    """Best-effort align field names to comma-joined values.

    Always returns a normalized 'raw'. Only splits into per-field keys when
    the counts match exactly, since address values can contain commas.
    """
    names = [normalize_text(f) for f in (fields or "").split(",") if f.strip()]
    raw_vals = [normalize_text(v) for v in (values or "").split(",")]
    result = {"raw": ", ".join(raw_vals) if raw_vals != [""] else ""}
    if names and len(names) == len(raw_vals):
        for name, val in zip(names, raw_vals):
            result[name] = val
    return result


def _grab(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    if not m:
        return None
    value = normalize_text(m.group(1))
    return value or None


def parse_listing(raw: str | None) -> dict:
    """Extract structured fields from the semi-structured XML_LISTING text.

    The source uses runs of 2+ spaces as field separators.
    """
    text = raw or ""
    out: dict = {"raw": normalize_text(text)}
    out["listed_name"] = _grab(r"Name:\s*(.+?)\s{2,}", text) or _grab(r"Name:\s*(.+)", text)
    out["reference_no"] = _grab(r"Reference No\.?:\s*([0-9]+)", text)
    out["position"] = _grab(r"Position:\s*(.+?)\s{2,}", text) or _grab(r"Position:\s*(.+)", text)
    out["dob"] = _grab(r"DOB:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", text)

    countries = re.findall(r"Country:\s*([A-Za-z][A-Za-z .'-]+?)\s{2,}", text)
    out["countries"] = sorted(
        {
            normalize_text(c)
            for c in countries
            if normalize_text(c) and not _is_section_label(normalize_text(c))
        }
    )

    akas = re.findall(r"Full:\s*(.+?)\s{2,}", text)
    out["akas"] = [
        normalize_text(a)
        for a in akas
        if normalize_text(a) and not _is_section_label(normalize_text(a))
    ]

    m = re.search(
        r"Notes:(.*?)(?:Address Information|AKA Listing|Additional Information|$)",
        text,
        re.DOTALL,
    )
    out["notes"] = normalize_text(m.group(1)) if m else None

    # A LexisNexis listing: the listed name, aliases and countries are taken from the
    # v2 listing reader (app.ln_parser_v2), the same reader scoring uses, so history
    # search and scoring read a listing the same way.
    if re.search(r"Name\s*:", text) and _LEXISNEXIS.search(text):
        v2 = v2_parse_listing(text)
        if v2["recognised"]:
            out["listed_name"] = v2["name"]
            out["akas"] = v2["aliases"]
            out["countries"] = sorted({a[0] for a in v2["addresses"] if a[0]})
    return out


def build_case_text(search: dict, matched_names: list[str], listing: dict) -> str:
    """Compact representation used as embedding text and stored document."""
    parts = []
    if search.get("raw"):
        parts.append(f"Screened party: {search['raw']}.")
    names = ", ".join(n for n in dict.fromkeys(matched_names) if n)
    if names:
        parts.append(f"Matched listed entity: {names}.")
    if listing.get("akas"):
        parts.append(f"Also known as: {', '.join(listing['akas'])}.")
    if listing.get("countries"):
        parts.append(f"Listing countries: {', '.join(listing['countries'])}.")
    if listing.get("position"):
        parts.append(f"Position: {listing['position']}.")
    if listing.get("notes"):
        parts.append(f"Listing notes: {listing['notes']}")
    return normalize_text(" ".join(parts))
