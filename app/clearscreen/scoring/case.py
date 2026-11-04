"""The three helpers the matching flow uses from the proof of concept's older pipeline modules,
copied verbatim: parse_case (app/pipeline.py), auto_clear_confidence and explain_score
(app/pipeline_fastv2.py)."""
from clearscreen.scoring.comparison import MATCH_THRESHOLD, NAME_EVAL_FLOOR
from clearscreen.scoring.models import CaseInput, ParsedCase
from clearscreen.scoring.text import build_case_text, normalize_text, parse_listing, parse_search_fields


def parse_case(case: CaseInput) -> ParsedCase:
    search = parse_search_fields(case.search_fields, case.search_fields_value)
    matched = [
        n for n in (
            normalize_text(case.xml_entity_name),
            normalize_text(case.xml_best_name),
            normalize_text(case.xml_full_name),
        ) if n
    ]
    listing = parse_listing(case.xml_listing)
    query_text = build_case_text(search, matched, listing)
    return ParsedCase(
        search=search,
        matched_names=list(dict.fromkeys(matched)),
        listing=listing,
        query_text=query_text,
        xml_entity_score=case.xml_entity_score,
        xml_score=case.xml_score,
    )


_LABELS = {
    "name": "Name", "alias": "Alias", "entity_type": "Entity type",
    "address.country": "Country", "country_risk": "High-risk country",
    "address.state": "State", "address.city": "City", "dob": "DOB", "id": "ID",
}


def auto_clear_confidence(score: float) -> float:
    """How sure the deterministic clear is: 0.51 just under the threshold, rising
    to 0.99 for a pair with nothing in common."""
    return round(min(0.99, 0.50 + 0.49 * (MATCH_THRESHOLD - score) / MATCH_THRESHOLD), 2)


def explain_score(comparison) -> str:
    """Every scored field with its points, then the total against the threshold."""
    parts, drag = [], []
    for r in comparison.comparisons:
        label = _LABELS.get(r.field)
        if not label:
            continue                      # street / other: reported, never scored
        if r.method == "not_evaluated":
            parts.append(f"{label}: not evaluated (name below the {NAME_EVAL_FLOOR:.2f} floor)")
            continue
        pts = (f"{r.score_contribution:.4f}" if r.field in ("name", "alias")
               else f"{r.score_contribution:+.2f}")
        if r.field == "country_risk":
            on_list = "on the high-risk list" if r.result == "exact" else "not on the high-risk list"
            parts.append(f"{label}: {pts} ({r.input_value} {on_list})")
            continue
        parts.append(f"{label}: {pts} {r.result} "
                     f"(screened: {r.input_value}; listing: {r.candidate_value})")
        if r.field not in ("name", "alias") and r.score_contribution < 0:
            drag.append(f"{label.lower()} {r.result} {r.score_contribution:+.2f}")
    name = next((r for r in comparison.comparisons if r.field in ("name", "alias")
                 and r.score_contribution > 0), None)
    if name is None or name.score_contribution < NAME_EVAL_FLOOR:
        why = "the name is too dissimilar for any other field to be compared"
    elif drag:
        why = "the name is offset by " + ", ".join(drag)
    else:
        why = "the name similarity alone does not reach the threshold"
    return (f"Score {comparison.deterministic_score:.4f} is below the {MATCH_THRESHOLD:.2f} "
            f"match threshold: {why}. " + "; ".join(parts) + ".")
