"""Deterministic field-level comparison trace for /adjudicate/fast.

Pure functions: no LLM, no network, no I/O. Every value in the emitted trace is
computed here, so an auditor can reproduce it from the request alone. The LLM
consumes this as ground truth rather than inventing field agreement.

WHY THIS DOES NOT REUSE app/blocking.py OR ingest/parse.py
----------------------------------------------------------
Both are shared with /adjudicate and /adjudicate/hybrid, which must stay
byte-for-byte unchanged. Two defects therefore had to be fixed *here* instead of
at the source:

  1. Dotted company suffixes. blocking._tokens splits "S.A." into {s, a}, so
     "Globex Imports SA" vs "Globex Imports, S.A." reads as a company-vs-person
     conflict. canonical_tokens folds them to {sa}.
  2. Delimiters. ingest.parse_search_fields splits values on comma only; when the
     field-name count does not match it silently drops every per-field key and
     returns raw alone. _align_search_fields accepts pipe or comma and, when
     alignment is impossible, says so in the trace instead of failing quietly.

The cost is a second normalization implementation. The benefit is that /fast can
be corrected without touching the other two endpoints. Those endpoints keep both
defects; fixing them there is a separate, opt-in change.
"""
import re
import unicodedata
from datetime import datetime

from clearscreen.scoring.ln_parser import parse_listing as v2_parse_listing, rows_for as v2_rows_for, suffix as v2_suffix
from clearscreen.scoring.text import normalize_text
from clearscreen.scoring.models import (
    COMPARISON_FIELDS, BlockingCheckRow, CaseInput, ComparisonRow,
    ComparisonTrace, ListingRow, ParsedCase,
)

UNKNOWN = "unknown"

_COMPANY_MARKERS = {
    # legal-form suffixes
    "inc", "ltd", "llc", "sa", "gmbh", "corp", "corporation", "limited",
    "company", "co", "plc", "group", "holdings", "trading", "enterprises",
    "nv", "spa", "ag", "bv", "sarl", "pty", "incorporated", "llp", "lp",
    # financial-institution words: without these, "THE BANK OF PORT AURORA"
    # reads as an individual and every subsidiary match raises a false
    # entity_type_conflict that the model then cites as grounds to clear.
    "bank", "banking", "bancorp", "trust", "capital", "financial", "finance",
    "savings", "insurance", "assurance", "securities", "asset", "assets",
    "investment", "investments", "partners", "ventures", "fund", "funds",
    # corporate forms and industry words that name companies rather than people
    "aktiengesellschaft", "gesellschaft", "aktiebolag", "aandelen", "kabushiki",
    "airline", "airlines", "airways", "air", "shipping", "line", "lines",
    "logistics", "energy", "petroleum", "oil", "gas", "mining", "steel",
    "pharma", "pharmaceutical", "technologies", "technology", "systems",
    "solutions", "industries", "manufacturing", "motors", "telecom",
}

# Runs of single letters joined by periods -- "s.a.", "n.v.", "l.l.c." -- are one
# token, not several. Applied before the generic non-alphanumeric split.
_DOTTED_ACRONYM = re.compile(r"\b(?:[a-z]\.){2,}")

# Screened field-name -> canonical comparison row. Matched by substring so
# "Beneficiary Name" and "Full Name" both land on "name". Order matters: the
# first hit wins, so "address.country" must be tested before "address".
_FIELD_ALIASES = (
    ("dob", "dob"), ("date of birth", "dob"), ("birth", "dob"),
    ("country", "address.country"), ("nationality", "address.country"),
    ("city", "address.city"), ("town", "address.city"),
    ("state", "address.state"), ("province", "address.state"),
    ("address", "address"), ("street", "address"),
    ("alias", "alias"), ("aka", "alias"), ("also known", "alias"),
    ("reference", "id"), ("identifier", "id"), ("passport", "id"),
    ("id number", "id"), ("national id", "id"),
    ("name", "name"),
)

# Name/alias similarity at or above this reads as the same party. It sets the row
# LABEL and the name_overlap blocking check ONLY -- it never zeroes the score. A
# name at 0.79 still contributes 0.79, so there is no cliff at the boundary.
NAME_GATE = 0.80

# Below this, the parties are too dissimilar to be worth comparing at all: NO
# other field is evaluated. Observed defect -- "nawoon idrafiy" vs "rafiy farvi"
# (0.6316) had city, country and DOB all agreeing, which added +0.21 on top of a
# name the system had already labelled a mismatch and carried the total to 0.8416.
# Agreeing fields must not rescue a name this weak.
NAME_EVAL_FLOOR = 0.65

# deterministic_score at or above this is a match. Fields alone top out at +0.68
# (country 0.05, high-risk country 0.15, state 0.02, city 0.01, DOB 0.15, ID 0.30),
# so a name between the floor and the gate CAN reach it -- by decision, a name of
# 0.65 from a high-risk country scores 0.80. A country conflict (-0.25) can sink a
# name that cleared the gate: an identical name in a different country is 0.75.
MATCH_THRESHOLD = 0.80

# Points added on a match / subtracted on a mismatch. An `unknown` row scores 0:
# absent data is not evidence of disagreement. Name and alias are NOT here --
# they contribute their Jaro-Winkler score directly (0.00..1.00), counted once.
_POINTS = {
    # (match, mismatch)
    "entity_type":     (0.00, -0.30),   # never adds; only a conflict counts
    "address.country": (0.05, -0.25),
    "country_risk":    (0.15,  0.00),   # screened country on HIGH_RISK_COUNTRIES; never penalises
    "address.state":   (0.02, -0.02),
    "address.city":    (0.01, -0.01),
    "id":              (0.30,  0.00),   # an identifier conflict never penalises
}

# DOB is three-way rather than binary, so it cannot use the (match, mismatch)
# shape above. `partial` means the two dates are within 366 days of each other.
#
# Only an EXACT date is evidence of identity. Two dates a few months apart are
# still two different dates, so `partial` is a weak negative rather than a weak
# positive.
#
# THE WHOLE ROW SCORES ONLY BETWEEN TWO DECLARED INDIVIDUALS. A date of birth is
# a property of a person: comparing one against a company is meaningless, so it
# must move the score in neither direction. Mixed types, two entities, and any
# pair whose type could not be determined all contribute exactly 0.00 -- positive
# and negative alike. "Declared" means stated via xml_entity_type or the
# listing's "Entity Type:"; the keyword fallback is an inference, not a
# confirmation, and cannot license DOB scoring.
_DOB_POINTS = {"exact": 0.15, "partial": -0.05, "mismatch": -0.30}

# Largest absolute swing each field can produce, used only as the denominator for
# evidence_coverage -- "how much of the possible evidence was actually checkable".
# `address` (street) and `other` are reported for audit but never scored, so they
# are absent here and from _POINTS. `country_risk` is scored but deliberately
# absent: a high-risk country is not evidence about identity, so it is not
# coverage.
_MAX_IMPACT = {
    "name": 1.00,
    "dob": 0.30,
    "id": 0.30,
    "entity_type": 0.30,
    "address.country": 0.25,
    "address.state": 0.02,
    "address.city": 0.01,
}

# ingest.parse_listing only recognises fields terminated by 2+ spaces, so a listing
# written as "... Country: Costa Rica." yields countries=[] even though the value is
# right there. These re-extract the fields /fast compares, accepting a period, a
# newline, 2+ spaces, or end-of-string as the terminator. Used only as a fallback
# when parse_listing found nothing, so the production 2+ space format is unaffected.
_TOLERANT = {
    "countries": re.compile(r"Country\s*:\s*(.+?)(?:\s{2,}|\.\s|\.$|\n|$)", re.IGNORECASE),
    "akas": re.compile(r"(?:AKA|Also Known As|Full)\s*:\s*(.+?)(?:\s{2,}|\.\s|\.$|\n|$)",
                       re.IGNORECASE),
    "reference_no": re.compile(r"Reference No\.?\s*:\s*([0-9]+)", re.IGNORECASE),
    # The listing's "Address Information" block. parse_listing reads none of it,
    # so a screened city of "london" was compared against "unknown" instead of
    # against "KINGSTON UPON HULL" -- turning a decisive MISMATCH into a row that
    # was excluded from the score entirely.
    "city": re.compile(r"City\s*:\s*(.+?)(?:\s{2,}|\.\s|\.$|\n|$)", re.IGNORECASE),
    "state": re.compile(r"(?:State|Province)\s*:\s*(.+?)(?:\s{2,}|\.\s|\.$|\n|$)", re.IGNORECASE),
    "street": re.compile(r"Street\s*\d*\s*:\s*(.+?)(?:\s{2,}|\.\s|\.$|\n|$)", re.IGNORECASE),
}

_RULES = {
    "name": ("name_similarity", "ofac_name_similarity"),
    "entity_type": ("entity_type_compare", "declared_only"),
    "dob": ("date_compare_full", "date_diff_days"),
    "address": ("token_set_similarity", "token_set_ratio"),
    "address.city": ("exact_or_place_ambiguity", "exact_string"),
    "address.state": ("exact_or_place_ambiguity", "exact_string"),
    "address.country": ("country_map", "alias_normalized_membership"),
    "country_risk": ("high_risk_country_list", "normalized_list_membership"),
    "id": ("id_exact", "numeric_clean_compare"),
    "alias": ("name_similarity", "ofac_name_similarity"),
    "other": ("not_applicable", "not_applicable"),
}


# Country variants that mean the same place. Without this, "USA" vs "United
# States" scored a country mismatch on a pure formatting difference -- tied for the
# heaviest penalty in the table. ISO-2 and ISO-3 codes are included for the same
# set. Extend by adding a row; the flat lookup below is rebuilt from it.
_COUNTRY_GROUPS = (
    ("united states", ("us", "usa", "u s", "u s a", "united states",
                       "united states of america", "america")),
    ("united kingdom", ("uk", "gb", "gbr", "united kingdom", "great britain",
                        "britain")),
    ("united arab emirates", ("ae", "are", "uae", "united arab emirates")),
    ("south korea", ("kr", "kor", "south korea", "republic of korea", "rok",
                     "korea republic of")),
    ("russia", ("ru", "rus", "russia", "russian federation")),
    ("iran", ("ir", "irn", "iran", "islamic republic of iran")),
    ("canada", ("ca", "can", "canada")),
)
_COUNTRY_CANON = {v: canon for canon, variants in _COUNTRY_GROUPS for v in variants}

# High-risk third countries. A SCREENED (our side) country on this list adds the
# country_risk points; any other country adds nothing. Each row is the list name
# followed by accepted spellings of the same country. Names only -- no ISO codes,
# which are ambiguous at two letters. Deliberately separate from _COUNTRY_GROUPS,
# so the country match/mismatch comparison is unaffected. Not on the list:
# South Korea, the Republic of the Congo, Sudan.
_HIGH_RISK_GROUPS = (
    ("Afghanistan", ()),
    ("Algeria", ()),
    ("Angola", ()),
    ("Bolivia", ("Plurinational State of Bolivia", "Bolivia (Plurinational State of)")),
    ("British Virgin Islands", ("BVI", "Virgin Islands (British)")),
    ("Cameroon", ()),
    ("Côte d’Ivoire", ("Cote d'Ivoire", "Cote dIvoire", "Ivory Coast")),
    ("Democratic People's Republic of Korea (DPRK)",
     ("Democratic People's Republic of Korea", "DPRK", "North Korea", "Korea DPR",
      "Korea, Democratic People's Republic of")),
    ("Democratic Republic of the Congo",
     ("DRC", "DR Congo", "Congo (Kinshasa)", "Congo, Democratic Republic of the")),
    ("Haiti", ()),
    ("Iran", ("Islamic Republic of Iran", "Iran (Islamic Republic of)")),
    ("Kenya", ()),
    ("Laos", ("Lao PDR", "Lao People's Democratic Republic")),
    ("Lebanon", ()),
    ("Monaco", ()),
    ("Myanmar", ("Burma",)),
    ("Namibia", ()),
    ("Nepal", ()),
    ("Russian Federation", ("Russia",)),
    ("South Sudan", ()),
    ("Syria", ("Syrian Arab Republic",)),
    ("Trinidad and Tobago", ()),
    ("Vanuatu", ()),
    ("Venezuela", ("Bolivarian Republic of Venezuela", "Venezuela (Bolivarian Republic of)")),
    ("Vietnam", ("Viet Nam",)),
    ("Yemen", ()),
)

# Date layouts accepted for DOB. Order matters: DD/MM/YYYY is tried before
# MM/DD/YYYY, so an ambiguous "03/04/1980" reads as 3 April. An unambiguous
# "12/25/1980" fails the first pattern and falls through to the second.
_DOB_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
                "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y")

# Two dates this far apart or less are treated as the same year of birth --
# elapsed days, not a year-component comparison, so 1980-12-31 and 1981-01-02
# are two days apart rather than "a different year".
_DOB_WITHIN_DAYS = 366


def _fold_diacritics(text: str) -> str:
    """Bermúdez -> Bermudez. The token split below treats any non-ASCII letter as
    a delimiter, so without this 'Bermúdez' becomes {fern, ndez} and scores 0.25
    against 'Bermudez' -- under-scoring every accented name in the corpus."""
    # NFKD leaves 'ß' intact -- it is a letter, not an accented form -- so it
    # would survive to the token split and act as a delimiter, turning "Straße"
    # into {stra, e}. Map the known non-decomposing letters explicitly first.
    for src, dst in (("ß", "ss"), ("Ø", "O"), ("ø", "o"), ("Æ", "AE"), ("æ", "ae"),
                     ("Ð", "D"), ("ð", "d"), ("Þ", "TH"), ("þ", "th"), ("Ł", "L"), ("ł", "l")):
        text = text.replace(src, dst)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def canonical_tokens(text: str | None) -> set[str]:
    """Lowercase token set, diacritics folded and dotted acronyms merged."""
    folded = _fold_diacritics(text or "").lower()
    lowered = _DOTTED_ACRONYM.sub(lambda m: m.group(0).replace(".", ""), folded)
    return {t for t in re.split(r"[^a-z0-9]+", lowered) if t}


def token_set_ratio(a: str | None, b: str | None) -> float:
    """Jaccard overlap of canonical token sets. 1.0 = same tokens, 0.0 = disjoint."""
    ta, tb = canonical_tokens(a), canonical_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def jaro_winkler(a: str, b: str, prefix_weight: float = 0.05) -> float:
    """Character-level similarity, 0..1. Prefix weight reduced from the classic 0.1
    to cut prefix-driven false positives, matching the SQL screening engine."""
    a = "".join(ch for ch in _fold_diacritics(a or "").upper() if ch.isalnum())
    b = "".join(ch for ch in _fold_diacritics(b or "").upper() if ch.isalnum())
    if a == b:
        return 1.0 if a else 0.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    window = max(max(la, lb) // 2 - 1, 0)
    fa, fb = [False] * la, [False] * lb
    matches = 0
    for i in range(la):
        for j in range(max(0, i - window), min(lb, i + window + 1)):
            if not fb[j] and a[i] == b[j]:
                fa[i] = fb[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    sa = [a[i] for i in range(la) if fa[i]]
    sb = [b[j] for j in range(lb) if fb[j]]
    transpositions = sum(1 for x, y in zip(sa, sb) if x != y) // 2
    jaro = ((matches / la) + (matches / lb) + ((matches - transpositions) / matches)) / 3
    prefix = 0
    for i in range(min(4, la, lb)):
        if a[i] == b[i]:
            prefix += 1
        else:
            break
    return jaro + prefix * prefix_weight * (1.0 - jaro)


def per_token_jaro_winkler(a: str | None, b: str | None) -> float:
    """Name similarity that ignores word order. 0..1.

    Tokens are paired one-to-one, best pair first: every (screened, listed) token
    pair is scored with Jaro-Winkler, then pairs are taken highest-first, each
    token used at most once. The matched similarities are summed and divided by
    the LONGER token count.

    Why per-token: whole-string JW compared "OCAWABANTAVEN" with "BINTAVENOCAWA"
    and scored the same person 0.6467 -- below the evaluation floor -- because the
    listing was surname-first. Paired by token it reads 0.9296 in any order.

    Why the LONGER count: tokens one side does not have must cost something, or a
    person matches their own company perfectly ("rafiy farvi" vs "rafiy farvi
    trading llc" would be 1.00). Here it is 0.50.

    Why best-first rather than left-to-right: a left-to-right greedy lets an early
    token steal the partner of a later one -- "awyler" could consume "jon" before
    "jon" meets its exact twin.
    """
    ta, tb = sorted(canonical_tokens(a)), sorted(canonical_tokens(b))
    if not ta or not tb:
        return 0.0
    pairs = sorted(((jaro_winkler(u, v), i, j)
                    for i, u in enumerate(ta) for j, v in enumerate(tb)), reverse=True)
    used_a, used_b, total = set(), set(), 0.0
    for score, i, j in pairs:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        total += score
    return total / max(len(ta), len(tb))


# Credit for two tokens that sound alike (equal Soundex codes) even where their
# spelling differs -- "ban"/"bin" is 0.79 on characters. OFAC does not publish how
# its Soundex check is weighted; 0.90 is the value measured and approved.
PHONETIC_CREDIT = 0.90

_SOUNDEX_CODES = {**dict.fromkeys("BFPV", "1"), **dict.fromkeys("CGJKQSXZ", "2"),
                  **dict.fromkeys("DT", "3"), "L": "4", **dict.fromkeys("MN", "5"), "R": "6"}


def soundex(word: str | None) -> str:
    """American Soundex code, e.g. 'taven' and 'tavin' -> 'L350'. '' if no letters."""
    w = "".join(ch for ch in _fold_diacritics(word or "").upper() if ch.isalpha())
    if not w:
        return ""
    out, prev = w[0], _SOUNDEX_CODES.get(w[0], "")
    for ch in w[1:]:
        code = _SOUNDEX_CODES.get(ch, "")
        if code and code != prev:
            out += code
        if ch not in "HW":
            prev = code
    return (out + "000")[:4]


def _token_similarity(u: str, v: str) -> float:
    s = jaro_winkler(u, v)
    su = soundex(u)
    if su and su == soundex(v):
        s = max(s, PHONETIC_CREDIT)
    return s


def ofac_name_similarity(screened: str | None, listed: str | None) -> float:
    """Name similarity modelled on OFAC Sanctions List Search (FAQ 249). 0..1.

    OFAC scores a name two ways and "returns the higher of the two":
      1. the whole string, with Jaro-Winkler;
      2. split into parts, each part compared with Jaro-Winkler and Soundex,
         giving "a score for each name part entered, and a composite score".
    Here the SCREENED side is the name "entered": each screened token is paired
    best-first, one-to-one, with a listed token, and the composite is divided by
    the number of SCREENED tokens. Extra tokens on the listing -- a fuller form
    of the name such as "bin tavin ucawa bin muhammad bin akav" -- therefore do
    not count against a short screened name.

    Known consequence, accepted by decision: company words on the listing are
    ignored the same way, so "rafiy farvi" vs "rafiy farvi trading llc" scores
    1.00. The entity-type penalty is what separates a person from their company.
    """
    whole = jaro_winkler(screened or "", listed or "")
    ta, tb = sorted(canonical_tokens(screened)), sorted(canonical_tokens(listed))
    if not ta or not tb:
        return 0.0
    pairs = sorted(((_token_similarity(u, v), i, j)
                    for i, u in enumerate(ta) for j, v in enumerate(tb)), reverse=True)
    used_a, used_b, total = set(), set(), 0.0
    for score, i, j in pairs:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        total += score
    return max(whole, total / len(ta))


# Below this, two place names are confidently different (measured: London vs
# Kingston upon Hull 0.55, Cologne vs Frankfurt 0.40, Panama vs Costa Rica 0.52).
# Above it, similarity is uninformative -- Munchen/Munich scores 0.872 and so does
# Toronto/Tortona, which are different cities. No threshold separates translation
# from coincidence, so the honest answer in that band is "not verifiable", not a
# conflict. Deliberately NOT applied to country: Iran vs Iraq scores high but is a
# decisive conflict, and countries are a small closed vocabulary that wants an
# alias table instead.
_PLACE_CONFLICT_MAX = 0.65


def _norm_country(value: str | None) -> str:
    """Lowercase, diacritics folded, punctuation to single spaces. Token ORDER is
    preserved -- canonical_tokens returns a set, whose iteration order would make
    multi-word country names non-deterministic."""
    folded = _fold_diacritics(value or "").lower()
    return " ".join(t for t in re.split(r"[^a-z0-9]+", folded) if t)


def canonical_country(value: str | None) -> str:
    """Resolve a country string to its canonical name, or return it normalised
    when unknown. 'U.S.A.' -> 'united states'; 'Ruritania' -> 'ruritania'."""
    normed = _norm_country(value)
    return _COUNTRY_CANON.get(normed, normed)


# Normalised spelling -> list name. Normalising both sides folds case, accents
# and punctuation: "COTE D'IVOIRE", "Côte d’Ivoire" and "cote d ivoire" are one key.
HIGH_RISK_COUNTRIES = {_norm_country(v): name
                       for name, variants in _HIGH_RISK_GROUPS for v in (name, *variants)}


def high_risk_country(value: str | None) -> str | None:
    """The high-risk list name for a screened country, or None when it is not on
    the list. A comma-joined value is on the list when any one country is."""
    for part in [value or "", *(value or "").split(",")]:
        hit = HIGH_RISK_COUNTRIES.get(_norm_country(part))
        if hit:
            return hit
    return None


def parse_dob(value: str | None) -> "datetime.date | None":
    """Parse a date of birth, or None when it cannot be read.

    The engine previously compared DOB as raw strings, so '1965-03-12' and
    '12 Mar 1965' read as a conflict and fed a fabricated dob_conflict=FAIL to
    the model. None means 'not verifiable', which scores 0 -- never a penalty.
    """
    text = normalize_text(value or "").strip().rstrip(".")
    if not text:
        return None
    for fmt in _DOB_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _looks_company(text: str | None) -> bool:
    """Company-word test on a name. NOT used to decide entity type -- scoring uses
    declared types only (see _entity_type_labels)."""
    return bool(canonical_tokens(text) & _COMPANY_MARKERS)


_COMPANY_WORDS = {"company", "corporate", "corporation", "entity", "organisation",
                  "organization", "business", "vessel", "aircraft"}
_PERSON_WORDS = {"individual", "person", "natural", "human"}


def declared_entity_type(value: str | None) -> bool | None:
    """True=company, False=individual, None=not stated.

    A caller that knows the entity type is authoritative; keyword guessing from
    name tokens is a fallback and gets it wrong on names carrying no corporate
    word at all ("Skyvale Airlines" before 'airlines' was a marker).
    """
    tokens = canonical_tokens(value)
    if tokens & _COMPANY_WORDS:
        return True
    if tokens & _PERSON_WORDS:
        return False
    return None


def _listed_entity_type(raw_listing: str) -> bool | None:
    """Entity type as stated by the listing itself, e.g. "Entity Type: Company"."""
    m = re.search(r"Entity\s*Type\s*:\s*(.+?)(?:\s{2,}|\.\s|\.$|\n|$)",
                  raw_listing or "", re.IGNORECASE)
    return declared_entity_type(m.group(1)) if m else None


def _align_search_fields(case: CaseInput) -> tuple[dict, str | None]:
    """Map screened field names to values, accepting pipe or comma delimiters.

    Returns (mapping, misalignment_note). The note is non-None when the counts
    cannot be reconciled, which the caller surfaces in the 'other' row.
    """
    names = [normalize_text(n) for n in (case.search_fields or "").split(",") if n.strip()]
    raw = case.search_fields_value or ""
    delimiter = "|" if "|" in raw else ","
    values = [normalize_text(v) for v in raw.split(delimiter) if normalize_text(v)]

    if not names or not values:
        return {}, None
    if len(names) != len(values):
        return {}, (f"{len(names)} field name(s) but {len(values)} value(s) "
                    f"(delimiter '{delimiter}'); values could not be aligned to fields.")

    mapping: dict[str, str] = {}
    for name, value in zip(names, values):
        lowered = name.lower()
        canonical = next((c for key, c in _FIELD_ALIASES if key in lowered), "other")
        mapping.setdefault(canonical, value)
    return mapping, None


def _tolerant(raw: str, key: str) -> list[str]:
    """Re-extract a listing field, accepting period/newline/EOL terminators."""
    seen: list[str] = []
    for hit in _TOLERANT[key].findall(raw or ""):
        value = normalize_text(hit).rstrip(".").strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def _candidates(parsed: ParsedCase, raw_listing: str = "") -> dict[str, str]:
    """Values available on the listed-entity side, from the parsed listing.

    raw_listing must be the ORIGINAL xml_listing, not parsed.listing["raw"]:
    normalize_text collapses runs of whitespace, destroying the 2+ space field
    separators the tolerant patterns rely on.
    """
    listing = parsed.listing
    raw = raw_listing or listing.get("raw") or ""
    out: dict[str, str] = {}

    names = ", ".join(parsed.matched_names) or (listing.get("listed_name") or "")
    if names:
        out["name"] = names

    akas = listing.get("akas") or _tolerant(raw, "akas")
    # A "Full:"/"AKA:" hit that merely repeats the listed name is not an alias.
    akas = [a for a in akas if canonical_tokens(a) != canonical_tokens(names)] or akas
    if akas:
        out["alias"] = ", ".join(akas)

    countries = listing.get("countries") or _tolerant(raw, "countries")
    if countries:
        out["address.country"] = ", ".join(countries)

    for field, key in (("address.city", "city"), ("address.state", "state"),
                       ("address", "street")):
        values = _tolerant(raw, key)
        if values:
            out[field] = ", ".join(values)

    if listing.get("dob"):
        out["dob"] = listing["dob"]

    reference = listing.get("reference_no") or next(iter(_tolerant(raw, "reference_no")), "")
    if reference:
        out["id"] = reference
    return out


def _compare(field: str, raw_in: str, raw_cand: str) -> tuple[str, float, str]:
    """Return (result, similarity 0..1, notes) for one field pair."""
    if not raw_in or not raw_cand:
        missing = "both sides" if not raw_in and not raw_cand else ("screened side" if not raw_in else "listing side")
        return UNKNOWN, 0.0, f"No value on the {missing}; not compared."

    if field == "country_risk":
        # Reads the SCREENED country only; raw_cand is the list label. Not identity
        # evidence, so a country off the list scores 0.00, never a penalty.
        hit = high_risk_country(raw_in)
        if hit:
            return "exact", 1.0, f"Screened country {raw_in} is on the high-risk third country list ({hit})."
        return "mismatch", 0.0, (f"Screened country {raw_in} is not on the high-risk third "
                                 "country list; no points either way.")

    if field in ("name", "alias"):
        # OFAC-style name similarity (see ofac_name_similarity): the higher of
        # whole-string and per-part scoring, parts anchored on the SCREENED name,
        # with a Soundex sounds-alike credit. Word order is irrelevant and a fuller
        # listed form does not count against a short screened name.
        #
        # The listing supplies several variants (entity name, best name, full
        # name; or several AKAs) joined with commas. Matching ANY ONE of them is
        # a match, so score against the best variant rather than the whole join.
        variants = [v.strip() for v in raw_cand.split(",") if v.strip()] or [raw_cand]
        best = max(variants, key=lambda v: ofac_name_similarity(raw_in, v))
        score = ofac_name_similarity(raw_in, best)
        whole = ofac_name_similarity(raw_in, raw_cand)
        if whole > score:            # the join itself aligns better
            best, score = raw_cand, whole
        note_of = "" if best == raw_cand else f" (best variant: {best})"
        # NOTE the similarity is returned on every branch, mismatch included: the
        # gate sets the label, it does not zero the contribution.
        if score >= 1.0:
            return "exact", 1.0, f"Name similarity 1.0000 - identical after folding.{note_of}"
        if score >= NAME_GATE:
            return "partial", score, (
                f"Name similarity {score:.4f} >= {NAME_GATE:.2f} gate.{note_of}")
        return "mismatch", score, (
            f"Name similarity {score:.4f} < {NAME_GATE:.2f} gate; score still counted.{note_of}")

    if field == "address":
        # Street address is reported for audit but carries no points, so token
        # overlap is sufficient here and needs no character-level scoring.
        variants = [v.strip() for v in raw_cand.split(",") if v.strip()] or [raw_cand]
        best = max(variants, key=lambda v: token_set_ratio(raw_in, v))
        ratio = max(token_set_ratio(raw_in, best), token_set_ratio(raw_in, raw_cand))
        if ratio == 1.0:
            return "exact", 1.0, "All tokens shared after canonicalization."
        if ratio > 0.0:
            shared = ", ".join(sorted(canonical_tokens(raw_in) & canonical_tokens(best)))
            return "partial", ratio, f"Partial token overlap ({ratio:.2f}): {shared}."
        return "mismatch", 0.0, "No shared tokens."

    if field == "dob":
        screened_date, listed_date = parse_dob(raw_in), parse_dob(raw_cand)
        if screened_date is None or listed_date is None:
            side = "screened" if screened_date is None else "listing"
            return UNKNOWN, 0.0, (f"Could not parse the {side} date of birth "
                                  f"(screened: {raw_in}; listing: {raw_cand}); not compared.")
        delta = abs((screened_date - listed_date).days)
        if delta == 0:
            return "exact", 1.0, "Dates of birth are identical."
        if delta <= _DOB_WITHIN_DAYS:
            return "partial", 1.0, f"Dates of birth are {delta} days apart (within one year)."
        return "mismatch", 1.0, f"Dates of birth are {delta} days apart (more than one year)."

    if field == "id":
        a, b = re.sub(r"\D", "", raw_in), re.sub(r"\D", "", raw_cand)
        if not a or not b:
            return UNKNOWN, 0.0, "No numeric identifier on one side; not compared."
        return ("exact", 1.0, "Identifiers match.") if a == b else ("mismatch", 0.0, "Identifiers differ.")

    # address.city, address.state, address.country -> membership, not equality.
    # A listing may carry several addresses, whose values are joined with commas;
    # comparing the screened city against that whole join as one string reported
    # "Cologne" vs "Cologne, Frankfurt am Main" as a MISMATCH.
    a, b = raw_in.strip().lower(), raw_cand.strip().lower()
    if field in ("address.country", "address.city", "address.state"):
        values = [v.strip() for v in raw_cand.split(",") if v.strip()]
        if field == "address.country":
            # Alias-aware: USA / U.S.A. / United States of America all resolve to
            # one canonical name, so a formatting difference is no longer tied for
            # the heaviest penalty in the table. City and state keep plain
            # lowercase membership plus the ambiguity band below.
            listed = {canonical_country(v) for v in values}
            a = canonical_country(raw_in)
        else:
            listed = {v.lower() for v in values}
        label = {"address.country": "Country", "address.city": "City",
                 "address.state": "State"}[field]
        if a in listed:
            return "exact", 1.0, f"{label} overlap confirmed ({raw_in})."
        if field != "address.country":
            # City/state only: a place may appear in another language. Assert a
            # conflict only when the strings are confidently unrelated.
            best = max((jaro_winkler(raw_in, v) for v in values), default=0.0)
            if best > _PLACE_CONFLICT_MAX:
                return UNKNOWN, 0.0, (
                    f"{label} differs textually but is too similar to call "
                    f"(best {best:.2f}); may be the same place written differently "
                    f"(screened: {raw_in}; listing: {raw_cand}). Not counted either way.")
        return "mismatch", 0.0, f"No {label.lower()} overlap (screened: {raw_in}; listing: {raw_cand})."
    return ("exact", 1.0, "Values match.") if a == b else ("mismatch", 0.0, "Values differ.")


def _points(field: str, result: str, similarity: float,
            dob_applies: bool = True) -> float:
    """Points this row contributes to deterministic_score.

    Name and alias contribute their Jaro-Winkler score directly (0.00..1.00) on
    EVERY result including mismatch -- the 0.80 gate labels the row, it does not
    zero it. DOB is three-way and scores only between two declared individuals
    (see _DOB_POINTS). Everything else is a (match, mismatch) pair, and an
    `unknown` row always scores 0: absent data is not evidence of disagreement.
    """
    if result == UNKNOWN:
        return 0.0
    if field in ("name", "alias"):
        return round(similarity, 4)
    if field == "dob":
        # Neither direction counts unless both sides are declared individuals.
        return _DOB_POINTS.get(result, 0.0) if dob_applies else 0.0
    match_points, mismatch_points = _POINTS.get(field, (0.0, 0.0))
    return match_points if result in ("exact", "partial") else mismatch_points


def _row(field: str, raw_in: str, raw_cand: str, extra_note: str = "",
         dob_applies: bool = True) -> ComparisonRow:
    result, similarity, notes = _compare(field, raw_in, raw_cand)
    rule, method = _RULES[field]
    if field == "dob" and result != UNKNOWN and not dob_applies:
        notes += (" Not scored in either direction: a date of birth only bears on "
                  "identity between two declared individuals.")
    return ComparisonRow(
        field=field,
        input_value=raw_in or UNKNOWN,
        candidate_value=raw_cand or UNKNOWN,
        normalized_input=" ".join(sorted(canonical_tokens(raw_in))) or UNKNOWN,
        normalized_candidate=" ".join(sorted(canonical_tokens(raw_cand))) or UNKNOWN,
        rule_applied=rule,
        method=method if result != UNKNOWN else "not_applicable",
        result=result,
        score_contribution=round(_points(field, result, similarity, dob_applies), 4),
        notes=(extra_note + " " + notes).strip() if extra_note else notes,
    )


def _skipped_row(field: str, raw_in: str, raw_cand: str, name_score: float,
                 extra_note: str = "") -> ComparisonRow:
    """A row that was deliberately NOT compared because the name fell below
    NAME_EVAL_FLOOR. Emitted rather than omitted so the trace shows an auditor
    what was available and why it was not consulted."""
    rule, _method = _RULES[field]
    note = (f"Not evaluated: the name/alias score {name_score:.4f} is below the "
            f"{NAME_EVAL_FLOOR:.2f} floor, so no further fields were compared.")
    return ComparisonRow(
        field=field,
        input_value=raw_in or UNKNOWN,
        candidate_value=raw_cand or UNKNOWN,
        normalized_input=" ".join(sorted(canonical_tokens(raw_in))) or UNKNOWN,
        normalized_candidate=" ".join(sorted(canonical_tokens(raw_cand))) or UNKNOWN,
        rule_applied=rule,
        method="not_evaluated",
        result=UNKNOWN,
        score_contribution=0.0,
        notes=(extra_note + " " + note).strip() if extra_note else note,
    )


def _blocking_checks(rows: dict[str, ComparisonRow]) -> list[BlockingCheckRow]:
    """Every check reads its comparison row, so a check can never disagree with
    the row it summarises."""
    country, dob = rows["address.country"], rows["dob"]
    name, entity = rows["name"], rows["entity_type"]

    if country.result == UNKNOWN:
        country_check = ("unknown", country.notes)
    elif country.result == "exact":
        country_check = ("pass", country.notes)
    else:
        country_check = ("fail", country.notes)

    if entity.result == UNKNOWN:
        entity_check = ("unknown", "Entity type not determined on one side.")
    elif entity.result == "mismatch":
        entity_check = ("fail", f"Screened party is a {entity.input_value} but listed "
                                f"entity is a {entity.candidate_value}. {entity.notes}")
    else:
        entity_check = ("pass", f"Entity types are consistent. {entity.notes}")

    if dob.result == UNKNOWN:
        dob_check = ("unknown", dob.notes)
    elif dob.result in ("exact", "partial"):
        dob_check = ("pass", dob.notes)
    else:
        dob_check = ("fail", f"Date of birth conflict (screened: {dob.input_value}; "
                             f"listing: {dob.candidate_value}). {dob.notes}")

    # Mirrors the NAME_GATE exactly: exact/partial are at or above it, mismatch
    # is below it. The row still contributed its Jaro-Winkler score either way.
    if name.result == UNKNOWN:
        name_check = ("unknown", name.notes)
    elif name.result == "mismatch":
        name_check = ("fail", name.notes)
    else:
        name_check = ("pass", name.notes)

    specs = [
        ("country_overlap", "Country Overlap", country_check),
        ("entity_type_conflict", "Entity Type Conflict", entity_check),
        ("dob_conflict", "DOB Conflict", dob_check),
        ("name_overlap", "Name Overlap", name_check),
    ]
    return [BlockingCheckRow(check=c, display_name=d, result=r, notes=n) for c, d, (r, n) in specs]


def _type_label(value: bool | None) -> str:
    return "" if value is None else ("company" if value else "individual")


def _entity_type_labels(case: CaseInput, listed_type: str = "") -> tuple[str, str, str, bool]:
    """(screened label, listed label, source note, both declared individual).

    Only a DECLARED type is used: the caller's xml_entity_type, or the listing's
    "Entity Type: ...". A side that declares nothing is "" -- not provided -- so
    the entity row is UNKNOWN and scores 0.00. The type is never guessed from name
    keywords: the guess read "Silverbrookhamilton" (no company word) as an
    individual and charged a -0.30 conflict against a declared corporate listing.

    The fourth value gates DOB scoring, which requires both sides declared
    individual.

    listed_type is the type the v2 listing reader gives a LexisNexis listing
    (corporate / individual); when set it is the listing's type, as decided.
    """
    declared_screened = declared_entity_type(case.xml_entity_type)
    from_v2 = declared_entity_type(listed_type) if listed_type else None
    declared_listed = from_v2 if from_v2 is not None else _listed_entity_type(case.xml_listing or "")

    def _side(value: bool | None, v2: bool = False) -> str:
        if value is None:
            return "not provided"
        return f"{'v2 parser' if v2 else 'declared'} {_type_label(value)}"

    source = (f"Entity type -- screened: {_side(declared_screened)}; "
              f"listing: {_side(declared_listed, from_v2 is not None)}.")
    both_declared_individual = declared_screened is False and declared_listed is False
    return (_type_label(declared_screened), _type_label(declared_listed), source,
            both_declared_individual)


# A LexisNexis listing: a "Name:" plus one of its section headers. Anything else
# (e.g. a hand-written "Listed Name: X  Country: Y") is read the old way.
_LEXISNEXIS = re.compile(r"Address Information|AKA Listing")


def _v2_rows(case: CaseInput, parsed: ParsedCase) -> list[tuple]:
    """The listing read by the v2 parser: (name, type, country, state, city, dob, id)
    rows, every name x DOB x ID x address. A matched name on the request that the
    listing text does not carry is added as one more name. [] when the listing is
    not in LexisNexis form."""
    raw = case.xml_listing or ""
    if not (re.search(r"Name\s*:", raw) and _LEXISNEXIS.search(raw)):
        return []
    p = v2_parse_listing(raw)
    if not p["recognised"]:
        return []
    known = {n.lower() for n in [p["name"], *p["aliases"]]}
    p["aliases"] += [n for n in parsed.matched_names if n.lower() not in known]
    return v2_rows_for(p)


def build_comparison_trace(case: CaseInput, parsed: ParsedCase) -> ComparisonTrace:
    """Deterministic trace for one alert. Pure: same input -> same output.

    A LexisNexis listing is first split by the v2 parser into rows (each name or
    alias x each address x DOB x ID). Every row is scored and the highest-scoring
    row is the trace returned; listing_rows lists them all, the winner marked.
    Other listings are scored once, read the old way.
    """
    rows = _v2_rows(case, parsed)
    if not rows:
        return _trace_one(case, parsed)
    # v2 does not output the street; the street row is shown for audit only, never
    # scored, so it is still read from the listing text.
    street = ", ".join(_tolerant(case.xml_listing or "", "street"))
    traces = []
    for name, etype, country, state, city, dob, ident in rows:
        candidates = {f: v for f, v in (("name", name), ("address.country", country),
                                        ("address.state", state), ("address.city", city),
                                        ("address", street), ("dob", dob), ("id", ident)) if v}
        traces.append(_trace_one(case, parsed, candidates=candidates, listed_type=etype))
    best = max(range(len(rows)), key=lambda i: traces[i].deterministic_score)   # first on a tie
    trace = traces[best]
    trace.listing_rows = [
        ListingRow(row=v2_suffix(i), name=r[0], entity_type=r[1], country=r[2], state=r[3],
                   city=r[4], dob=r[5], id=r[6], score=traces[i].deterministic_score, best=i == best)
        for i, r in enumerate(rows)]
    return trace


def _trace_one(case: CaseInput, parsed: ParsedCase, candidates: dict[str, str] | None = None,
               listed_type: str = "") -> ComparisonTrace:
    """One scoring pass.

        deterministic_score = max(name JW, best alias JW) + sum(field points)
        range -0.88 .. +1.68            match threshold MATCH_THRESHOLD (0.80)

    candidates, when given, are the listing-side values of one v2 row; otherwise
    they are read from the listing the old way.
    """
    aligned, misalignment = _align_search_fields(case)
    if candidates is None:
        candidates = _candidates(parsed, case.xml_listing or "")

    # Without usable per-field alignment the whole screened blob is all we have;
    # use it for the name row so a name comparison is still possible.
    screened_name = aligned.get("name") or (parsed.search.get("raw", "") if not aligned else "")

    # A date of birth only bears on identity between two people. Anything less
    # than two DECLARED individuals -- an entity on either side, or a type not
    # provided -- means the DOB row scores 0.00 either way.
    et_screened, et_listed, et_note, both_individuals = _entity_type_labels(case, listed_type)

    rows: dict[str, ComparisonRow] = {}

    # Name and alias run FIRST: they decide whether anything else is evaluated.
    # An alias on the listing side is an alternate name for the same party, so it
    # is compared against the screened name -- but only when the listing actually
    # has aliases, otherwise this would invent an input_value for a row with
    # nothing to compare against.
    alias_in = aligned.get("alias", "") or (screened_name if candidates.get("alias") else "")
    rows["name"] = _row("name", screened_name, candidates.get("name", ""))
    rows["alias"] = _row("alias", alias_in, candidates.get("alias", ""))

    # Name and alias answer the same question -- "is this the same party" -- so
    # the score is max(name, alias), counted ONCE. The losing row keeps its own
    # similarity in the notes for audit but contributes nothing.
    name_row, alias_row = rows["name"], rows["alias"]
    name_score = max(name_row.score_contribution, alias_row.score_contribution)
    if alias_row.score_contribution > name_row.score_contribution:
        loser, winner = name_row, "alias"
    else:
        loser, winner = alias_row, "name"
    if loser.result != UNKNOWN:
        loser.notes += f" Not counted: the {winner} match scores at least as high."
    loser.score_contribution = 0.0

    # Below the floor the two parties are too dissimilar to be worth comparing at
    # all. Every remaining row is still EMITTED -- marked not_evaluated -- so the
    # trace shows what was available and why it was not consulted.
    evaluate_fields = name_score >= NAME_EVAL_FLOOR

    for field in COMPARISON_FIELDS:
        if field in ("name", "alias", "other"):
            continue
        if field == "entity_type":
            # Not drawn from the screened field map: the type is declared on the
            # request, stated in the listing, or inferred -- never a search field.
            raw_in, raw_cand, note = et_screened, et_listed, et_note
        elif field == "country_risk":
            # Our side's country against the high-risk list. No screened country
            # leaves both sides empty, so the row is dropped rather than shown.
            raw_in = aligned.get("address.country", "")
            raw_cand, note = ("high-risk third country list" if raw_in else ""), ""
        else:
            raw_in, raw_cand, note = aligned.get(field, ""), candidates.get(field, ""), ""
        rows[field] = (
            _row(field, raw_in, raw_cand, extra_note=note, dob_applies=both_individuals)
            if evaluate_fields else
            _skipped_row(field, raw_in, raw_cand, name_score, extra_note=note)
        )

    leftover = {k: v for k, v in aligned.items() if k not in COMPARISON_FIELDS or k == "other"}
    note = misalignment or (f"Unmapped screened fields: {', '.join(sorted(leftover))}." if leftover else "")
    rows["other"] = _row("other", "", "", extra_note=note)

    # Drop rows with no value on either side: nothing was compared, so they carry
    # no information. The 'other' row survives only when it holds a warning.
    comparisons = [
        rows[f] for f in COMPARISON_FIELDS
        if (rows[f].input_value != UNKNOWN or rows[f].candidate_value != UNKNOWN)
        or (f == "other" and note)
    ]

    # Points are absolute, so the rows add up to the headline with no rescaling --
    # the previous scheme divided by the achievable weight, which is what made
    # rows of 0.45 appear under a headline of 1.00.
    score = sum(r.score_contribution for r in comparisons)

    # Coverage still answers "how much of the possible evidence was checkable",
    # now against the largest absolute swing each field can produce. Name and
    # alias are one judgement, so they count once.
    checkable = {r.field for r in comparisons if r.result != UNKNOWN}
    if "alias" in checkable:
        checkable.discard("alias")
        checkable.add("name")
    achievable = sum(_MAX_IMPACT[f] for f in checkable if f in _MAX_IMPACT)

    return ComparisonTrace(
        blocking_checks=_blocking_checks(rows),
        comparisons=comparisons,
        deterministic_score=round(score, 4),
        evidence_coverage=round(achievable / sum(_MAX_IMPACT.values()), 4),
    )


# Verdicts that mean an analyst previously treated this entity as a real hit.
_POSITIVE_VERDICTS = {"likely_match", "pep"}

# Token overlap required before two screened parties are treated as the same
# entity. Deliberately strict: this gate overrides the model's decision, so a
# false trigger is worse than a miss. Vector distance is NOT used -- measured on
# this corpus, same-entity and unrelated distances overlap (0.42 vs 0.52), so a
# distance cutoff cannot separate them reliably.
_SAME_ENTITY_RATIO = 0.9

_SCREENED_RE = re.compile(r"Screened party:\s*(.+?)(?:\.\s|\.$|$)", re.IGNORECASE)


def screened_party_of(document: str) -> str:
    """Pull the screened-party string out of a stored precedent document."""
    m = _SCREENED_RE.search(document or "")
    return m.group(1).strip() if m else ""


def _containment(a: str, b: str) -> float:
    """Share of the SHORTER token set found in the longer one.

    Entity identity needs containment, not Jaccard: a screened party arrives as
    "ACME CORP, COSTA RICA" while the stored precedent may hold only "ACME CORP".
    Jaccard scores that 0.5 and misses the match; containment scores it 1.0.
    """
    ta, tb = canonical_tokens(a), canonical_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def same_entity_precedents(screened_name: str, retrieved) -> list:
    """Retrieved cases whose SCREENED PARTY is this same entity, whatever the
    analyst concluded. Surfaced so a re-screen of an already-adjudicated entity
    is never presented to the analyst as if it were novel."""
    if not screened_name:
        return []
    out = []
    for r in retrieved or []:
        # Compare against the first comma-segment (the name) as well as the whole
        # screened string, since stored documents append address components.
        past = screened_party_of(r.document)
        if not past:
            continue
        # Require a real overlap, not a single incidental token: a two-token
        # minimum stops "SMITH" alone from matching every Smith in the corpus.
        shared = canonical_tokens(screened_name) & canonical_tokens(past)
        candidates = [past, past.split(",")[0]]
        if len(shared) >= 2 and any(
            _containment(screened_name, c) >= _SAME_ENTITY_RATIO for c in candidates if c
        ):
            out.append(r)
    return out


def contradicting_precedents(screened_name: str, retrieved) -> list:
    """Same-entity precedents an analyst previously judged a REAL match. Their
    existence means an auto-CLEAR would silently reverse a human decision, so the
    caller escalates to REVIEW instead."""
    return [r for r in same_entity_precedents(screened_name, retrieved)
            if (r.metadata or {}).get("verdict", "") in _POSITIVE_VERDICTS]


def format_prior_history(precedents) -> str:
    """One sentence naming prior adjudications of this same entity, for the
    analyst-facing rationale. Empty when the entity is new to the corpus."""
    if not precedents:
        return ""
    parts = ", ".join(
        f"case {r.id} ({(r.metadata or {}).get('verdict', 'unknown')})" for r in precedents
    )
    return (f"NOTE: this entity already appears in the adjudication history — {parts}. "
            "Confirm the current decision is consistent with that prior outcome.")


def format_comparison_trace(trace: ComparisonTrace) -> str:
    """Render the trace for prompt injection as ground truth the LLM must not contradict."""
    checks = "\n".join(
        f"- {c.display_name}: {c.result.upper()} — {c.notes}" for c in trace.blocking_checks
    )
    rows = "\n".join(
        f"- {r.field}: {r.result.upper()} | screened={r.input_value!r} | listed={r.candidate_value!r} — {r.notes}"
        for r in trace.comparisons
    )
    return (
        "Deterministic comparison trace (computed from the alert, NOT your inference).\n"
        "Treat these as facts. A field marked UNKNOWN was not verifiable — you must NOT\n"
        "claim agreement or disagreement on it.\n\n"
        f"Blocking checks:\n{checks}\n\n"
        f"Field comparisons:\n{rows}\n\n"
        f"Deterministic match score: {trace.deterministic_score:.4f} "
        f"(range 0.00..+1.68; {MATCH_THRESHOLD:.2f} or higher reads as a match)"
    )
