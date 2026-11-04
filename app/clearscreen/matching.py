# -*- coding: utf-8 -*-
"""The matching flow of POST /match/groq, for one alert of one client. Only that client's
whitelist records are ever used.

  1. Exact repeat: a whitelisted record of the client with the same Screened_Fields,
     Screened_Values and Matched_Details (case and spacing ignored)            -> CLEAR
  2. No listing details ("Entity Details NOT AVAILABLE") and not a repeat     -> REVIEW
  3. Match score (our customer vs the listing, scoring/comparison.py) below 0.80 -> CLEAR
  4. Similar-case search (similarity.py): no whitelisted record of the client reaches
     80% adjusted similarity                                                   -> REVIEW
  5. AI review (ai_review.py) of the most similar records, each with its
     Analyst_Allow_Reasoning, held to Python's fact checks:
       prior whitelist still applies -> CLEAR · manual review -> REVIEW · no longer applies -> FLAG

A provider's usage limit (ai_review.AIProviderLimit) is not turned into a disposition: it
propagates to the endpoint (GROQ_LIMIT_REACHED).
"""
import re
import time

from clearscreen import ai_review, db, similarity
from clearscreen.scoring.comparison import MATCH_THRESHOLD, build_comparison_trace
from clearscreen.scoring.ln_parser import parse_listing
from clearscreen.models import MatchResult
from clearscreen.scoring.case import auto_clear_confidence, explain_score, parse_case
from clearscreen.scoring.models import CaseInput

REPEAT_CONFIDENCE = 0.99

# Our customer's fields -> the field name the score reads (scoring/comparison._FIELD_ALIASES).
SCORE_FIELDS = [("Entity_Name", "Name"), ("Entity_Country", "Country"), ("State", "State"),
                ("City", "City"), ("Entity_DOB", "DOB"), ("Entity_Identification", "ID Number")]

_FROM_AI = {ai_review.APPLICABLE: ("CLEAR", "prior_adjudication_applicable"),
            ai_review.MANUAL: ("REVIEW", "manual_review_required"),
            ai_review.NOT_APPLICABLE: ("FLAG", "prior_adjudication_not_applicable")}


def exact_repeats(con, alert: dict) -> list[str]:
    """Unique_IDs of the client's records with the same Screened_Fields, Screened_Values and
    Matched_Details as the alert (case and spacing ignored) - an indexed fingerprint lookup."""
    return db.exact_repeats(con, alert["ClientID"], alert.get("Screened_Fields"), alert.get("Screened_Values"),
                            alert.get("Matched_Payload"))


def as_case(alert: dict) -> CaseInput:
    """The alert as the scorer's input. Only filled fields are sent, joined by '|', so a blank
    field or a comma inside a value cannot shift the others."""
    present = [(label, alert[col].strip()) for col, label in SCORE_FIELDS if alert.get(col) and alert[col].strip()]
    listed = parse_listing(alert["Matched_Payload"])["name"] or None
    return CaseInput(id=alert.get("Source_ID"), search_fields=",".join(label for label, _ in present),
                     search_fields_value=" | ".join(v for _, v in present), xml_entity_type=alert.get("Entity_Type"),
                     xml_entity_name=listed, xml_listing=alert["Matched_Payload"])


def _reasoning_of(con, client_id, unique_id):
    rec = db.get_record(con, client_id, unique_id) if unique_id else None
    return rec["Analyst_Allow_Reasoning"] if rec else None


def match_alert(request: dict, con, *, chat, model: str, sim_cfg=None, ai_cfg=None, embed=None) -> MatchResult:
    """request: a MatchRequest as a dict (the client's column names)."""
    started = time.time()
    alert = db.to_alert(request)
    calls = []

    def timed_chat(system, user):
        content, info = chat(system, user)
        calls.append(info)
        return content, info

    def result(disposition, rule, confidence, explanation, **kw):
        ai_s = sum(c.get("total_s") or 0 for c in calls)
        return MatchResult(
            ClientID=alert["ClientID"], Unique_ID=alert["Source_ID"], disposition=disposition, rule=rule,
            confidence=round(confidence, 2), model=model, explanation=explanation, ai_calls=len(calls),
            seconds=round(time.time() - started, 2), ai_seconds=round(ai_s, 2),
            waited_seconds=round(sum(c.get("waited_s") or 0 for c in calls), 1),
            cost_usd=round(sum(c.get("cost_usd") or 0 for c in calls), 5), **kw)

    repeats = exact_repeats(con, alert)
    if repeats:
        return result("CLEAR", "exact_repeat_whitelisted", REPEAT_CONFIDENCE,
                      f"Exact repeat of whitelisted record(s) {', '.join(repeats)} of client {alert['ClientID']}.",
                      repeat_of=repeats, precedent=repeats[0],
                      precedent_reasoning=_reasoning_of(con, alert["ClientID"], repeats[0]))
    if db.no_listing_details(alert["Matched_Payload"]):
        return result("REVIEW", "no_listing_details", 0.0,
                      "LexisNexis gave no details of what was matched, and it is not an exact repeat of a "
                      "whitelisted record - a human reviews.")
    case = as_case(alert)
    trace = build_comparison_trace(case, parse_case(case))
    score = trace.deterministic_score
    if score < MATCH_THRESHOLD:
        return result("CLEAR", "score_below_threshold", auto_clear_confidence(score), explain_score(trace),
                      score=score, comparison_trace=trace)
    lead = f"Score {score:.2f} is at or above {MATCH_THRESHOLD:.2f}."
    try:
        similar = similarity.find_candidates(alert, con, sim_cfg, embed=embed)
    except Exception as e:                                        # noqa: BLE001
        return result("REVIEW", "similar_search_error", 0.0,
                      f"{lead} The similar-case search failed ({type(e).__name__}: {e}); a human reviews."[:500],
                      score=score, comparison_trace=trace)
    if not similar["send_to_ai"]:
        return result("REVIEW", "no_similar_whitelisted", 0.0,
                      f"{lead} No whitelisted record of client {alert['ClientID']} reached "
                      f"{similar['threshold']:.0f}% adjusted similarity ({similar['compared']} compared); a human reviews.",
                      score=score, comparison_trace=trace, similar=similar)
    candidates = ai_review.load_candidates(con, similar["candidates"], alert["ClientID"])
    review = ai_review.review(alert, candidates, cfg=ai_cfg, chat=timed_chat)       # AIProviderLimit propagates
    disposition, rule = _FROM_AI[review["decision"]]
    for r in review["reviews"]:
        r.pop("prompt", None)
        r["Analyst_Allow_Reasoning"] = _reasoning_of(con, alert["ClientID"], r["previous_case_id"])
    if disposition == "CLEAR":
        best = next(r for r in review["reviews"] if r["previous_case_id"] == review["strongest_precedent"])
        confidence = best["confidence"] / 100
        why = f"The reasoning that whitelisted record {best['previous_case_id']} still applies: {best['reasoning']}"
    else:
        confidence = max((r.get("confidence") or 0 for r in review["reviews"]), default=0) / 100
        why = review["explanation"] + "".join(
            f" Record {r['previous_case_id']}: {r['decision']}"
            + (f" ({'; '.join(r['python_checks'])})" if r.get("python_checks") else "")
            + (f" - {r['reasoning']}" if r.get("reasoning") else "") + "." for r in review["reviews"])
    return result(disposition, rule, confidence, f"{lead} {why}", score=score, comparison_trace=trace,
                  similar=similar, ai_review=review, precedent=review["strongest_precedent"],
                  precedent_reasoning=_reasoning_of(con, alert["ClientID"], review["strongest_precedent"]))
