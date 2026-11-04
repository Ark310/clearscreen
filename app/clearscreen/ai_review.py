# -*- coding: utf-8 -*-
"""AI review (fresh start): does the reasoning that whitelisted a previous case still apply
to the current case? Runs on the cases Gateway 2 marked send_to_ai.

review(current, candidates) -> {"decision", "strongest_precedent", "reviews": [...]}

  * one AI call per previous case, so reasoning from different cases is never combined;
  * each case is shown with both sides -- our customer and the blocked party (read from
    Matched_Payload by the parser) -- for the previous and the current alert, plus the
    customer-vs-blocked-party relationships Python can compute (DOB, country ...);
  * Python then checks the AI's answer against those relationships (a fact the data does
    not support is corrected) and against the decision rules; every change is listed in
    python_checks, and the AI's own answer is kept in ai_decision;
  * the overall decision: a conflict between cases -> MANUAL_REVIEW_REQUIRED; otherwise the
    strongest PRIOR_ADJUDICATION_APPEARS_APPLICABLE case; else NOT_APPLICABLE if every case
    says so; else MANUAL_REVIEW_REQUIRED.

The AI is told it assesses whether prior reasoning appears applicable. POST /gateway1 turns
the overall decision into the alert's disposition (app/gateway1.py).
"""
import re
import time
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, Field
from rapidfuzz.distance import JaroWinkler

from clearscreen.similarity import (_blank, _name_key, normalize_country, normalize_dob, normalize_entity_type)
from clearscreen.scoring.ln_parser import entity_type as listing_entity_type
from clearscreen.scoring.ln_parser import parse_listing
from clearscreen import db


class AIProviderLimit(RuntimeError):
    """The AI provider refused for a usage limit (e.g. Groq's daily allowance). Not answered as
    MANUAL_REVIEW: the request fails and says so."""


def _case_id(record) -> str:
    """A whitelisted record's ID as the AI and the results show it: its Unique_ID."""
    return str(record.get("Source_ID") if record.get("Source_ID") is not None else record.get("WL_ID")).strip()


@dataclass
class AIReviewConfig:
    model: str = "gpt-oss:20b"
    think: str | bool | None = "low"
    num_ctx: int = 8192
    timeout_s: float = 180.0
    max_cases: int = 5                  # most previous cases reviewed per alert (best first)
    ollama_url: str = "http://127.0.0.1:11434"
    retries: int = 1                    # extra attempts when the reply is not valid JSON


APPLICABLE = "PRIOR_ADJUDICATION_APPEARS_APPLICABLE"
MANUAL = "MANUAL_REVIEW_REQUIRED"
NOT_APPLICABLE = "PRIOR_ADJUDICATION_NOT_APPLICABLE"
Decision = Literal["PRIOR_ADJUDICATION_APPEARS_APPLICABLE", "MANUAL_REVIEW_REQUIRED",
                   "PRIOR_ADJUDICATION_NOT_APPLICABLE"]
RationaleType = Literal[
    "DOB_MISMATCH", "COUNTRY_MISMATCH", "ADDRESS_MISMATCH", "NATIONALITY_MISMATCH", "GENDER_MISMATCH",
    "AGE_MISMATCH", "ENTITY_TYPE_MISMATCH", "NAME_VARIATION", "DIFFERENT_MIDDLE_NAME", "DIFFERENT_IDENTIFIERS",
    "COMPANY_REGISTRATION_MISMATCH", "LOCATION_MISMATCH", "OTHER_DISTINGUISHING_INFORMATION"]
Relationship = Literal["MATCH", "MISMATCH", "UNKNOWN"]
Status = Literal["STILL_APPLIES", "NO_LONGER_APPLIES", "CANNOT_DETERMINE"]


class Values(BaseModel):
    customer: str | None = None
    blocked_party: str | None = None


class Rationale(BaseModel):
    rationale_type: RationaleType
    previous_values: Values
    current_values: Values
    previous_relationship: Relationship
    current_relationship: Relationship
    rationale_status: Status


class AIReview(BaseModel):
    decision: Decision
    previous_case_id: str
    previous_adjudication_summary: str
    rationales: list[Rationale] = Field(max_length=6)
    # At most 3 short items each: the answer's length is most of the AI call's time.
    matching_facts: list[str] = Field(max_length=3)
    material_differences: list[str] = Field(max_length=3)
    missing_information: list[str] = Field(max_length=3)
    new_contradictory_information: list[str] = Field(max_length=3)
    rationale_still_applies: bool | None
    reasoning: str
    confidence: int = Field(ge=0, le=100)


SYSTEM_PROMPT = """You are evaluating whether the reasoning from a previously approved block-list whitelist adjudication appears applicable to a new screening case.

Python has already selected the historical case as a potentially relevant precedent. Similarity scores are retrieval information only and must never be treated as evidence.

Determine why the previous analyst cleared the prior case. Identify the factual relationships or distinguishing conditions that supported that decision. Then determine whether those same conditions remain true in the current case.

Do not require exact literal values to remain unchanged. For example, if the previous reasoning was COUNTRY_MISMATCH, Canada versus USA and Canada versus England may represent the same adjudication rationale because the relevant relationship remains a country mismatch.

Never assume missing data is a match. Never invent facts. Consider any new contradictory identifying information.

If a material rationale cannot be confirmed, or if the applicability of the previous reasoning is uncertain, return MANUAL_REVIEW_REQUIRED.

Your task is to assess whether prior adjudication reasoning appears applicable. You are not independently making the final compliance decision.

HOW TO WORK
1. Read the PREVIOUS analyst note and extract the actual distinguishing reasons (not a summary of the note). Normalise each to one rationale_type: DOB_MISMATCH, COUNTRY_MISMATCH, ADDRESS_MISMATCH, NATIONALITY_MISMATCH, GENDER_MISMATCH, AGE_MISMATCH, ENTITY_TYPE_MISMATCH, NAME_VARIATION, DIFFERENT_MIDDLE_NAME, DIFFERENT_IDENTIFIERS, COMPANY_REGISTRATION_MISMATCH, LOCATION_MISMATCH, OTHER_DISTINGUISHING_INFORMATION. There may be several.
2. For each rationale, give the customer and blocked-party values in the previous case and in the current case, the relationship in each (MATCH, MISMATCH, UNKNOWN), and a rationale_status:
   STILL_APPLIES - the same relationship holds now, even if the literal values changed (Canada vs USA then, Canada vs England now = still a mismatch).
   NO_LONGER_APPLIES - the distinguishing condition is gone (e.g. the customer DOB now equals the blocked-party DOB).
   CANNOT_DETERMINE - a value needed to check it is missing now. Missing is never a match.
   The "Python fact check" lines are computed from the data; use them, do not contradict them.
3. Look for NEW information in the current case that weakens the previous conclusion even if the old reasons still hold (e.g. an identical passport or ID number, an exact DOB match, the blocked party now a different entity type).
4. If the previous decision relied on several reasons and only some still apply, decide whether the remaining ones alone were sufficient; if that is unclear, MANUAL_REVIEW_REQUIRED.

DECISION (exactly one)
PRIOR_ADJUDICATION_APPEARS_APPLICABLE - the material reasoning still holds, the key relationships still exist, no new contradictory information, the needed information is available, and you can explain why.
MANUAL_REVIEW_REQUIRED - important information is missing, a rationale cannot be confirmed, some reasoning changed, new information creates uncertainty, or you are unsure. When uncertain, prefer this.
PRIOR_ADJUDICATION_NOT_APPLICABLE - a material distinguishing condition is no longer present, the facts now align with the blocked party, or the case is materially different in a way that invalidates the previous adjudication.

OUTPUT: one JSON object with exactly these keys: decision, previous_case_id (the previous case's ID, as given), previous_adjudication_summary, rationales (list of {rationale_type, previous_values {customer, blocked_party}, current_values {customer, blocked_party}, previous_relationship, current_relationship, rationale_status}), matching_facts, material_differences, missing_information, new_contradictory_information (lists of short sentences), rationale_still_applies (true, false or null when it cannot be determined), reasoning, confidence (0-100). Use null for a value that is not given.
BE BRIEF - the answer is read by Python and an analyst: previous_adjudication_summary one sentence; each list item one short sentence (at most 3 per list, only what matters); reasoning at most 2 sentences; values as short as the data allows (e.g. "1980-05-10", "Canada")."""


# --------------------------------------------------------------- case facts --
def _grab(pattern, text):
    m = re.search(pattern, text or "", re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip(" .:") if m and m.group(1).strip(" .:") else None


_STOP = r"(?=\s+(?:Reference No|Reason\s*:|Notes\s*:|Address Information|AKA Listing|Additional Information)|$)"


def blocked_party(payload: str) -> dict:
    """The blocked party as the listing gives it (parser + the labelled header fields)."""
    p = parse_listing(payload)
    return dict(
        names=[p["name"]] + p["aliases"] if p["name"] else p["aliases"],
        entity_type=normalize_entity_type(listing_entity_type(p)),
        list_reason=_grab(r"Reason\s*:\s*(.*?)" + _STOP, payload),
        block_list_id=_grab(r"Reference No\.?\s*:?\s*([\w\-/]+)", payload),
        dobs=p["dobs"], countries=list(dict.fromkeys(a[0] for a in p["addresses"] if a[0])),
        addresses=[", ".join(x for x in (city, state, country) if x) for country, state, city in p["addresses"]],
        nationality=p["nationality"], citizenship=p["citizenship"],
        gender=_grab(r"Gender\s*:\s*(\w+)", payload), identifications=p["ids"], position=p["position"],
        notes=_grab(r"Notes\s*:\s*(.*?)" + _STOP, payload))


def customer(row: dict) -> dict:
    return dict(name=row.get("Entity_Name"), entity_type=normalize_entity_type(row.get("Entity_Type")),
                dob=row.get("Entity_DOB"), country=row.get("Entity_Country"), state=row.get("State"),
                city=row.get("City"), identification=row.get("Entity_Identification"))


def _dob_rel(ours, theirs) -> Relationship:
    a = normalize_dob(ours)
    bs = [b for b in (normalize_dob(d) for d in theirs) if b]
    if a is None or not bs:
        return "UNKNOWN"
    same = lambda b: all(x is None or y is None or x == y for x, y in zip(a, b))   # noqa: E731
    return "MATCH" if any(same(b) for b in bs) else "MISMATCH"


def _set_rel(ours, theirs, norm) -> Relationship:
    if _blank(ours) or not [t for t in theirs if not _blank(t)]:
        return "UNKNOWN"
    return "MATCH" if norm(ours) in {norm(t) for t in theirs if not _blank(t)} else "MISMATCH"


def _id_key(v) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(v).upper())


def _text_key(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().casefold()


def relationships(cust: dict, bp: dict) -> dict:
    """Customer vs blocked party, computed from the data: MATCH / MISMATCH / UNKNOWN each."""
    best_name = max((JaroWinkler.normalized_similarity(_name_key(cust["name"] or ""), _name_key(n)) * 100
                     for n in bp["names"]), default=None) if cust["name"] else None
    et = ("UNKNOWN" if not cust["entity_type"] or not bp["entity_type"]
          else "MATCH" if cust["entity_type"] == bp["entity_type"] else "MISMATCH")
    return dict(
        name_similarity=round(best_name, 1) if best_name is not None else None,
        DOB=_dob_rel(cust["dob"], bp["dobs"]),
        COUNTRY=_set_rel(cust["country"], bp["countries"], normalize_country),
        NATIONALITY=_set_rel(cust["country"], bp["nationality"] + bp["citizenship"], normalize_country),
        LOCATION=_set_rel(cust["city"], [a.split(",")[0] for a in bp["addresses"]], _text_key),
        ENTITY_TYPE=et,
        IDENTIFIERS=_set_rel(cust["identification"], bp["identifications"], _id_key))


# Rationale type -> the relationships Python computes for it, first known one used: a note's
# "country mismatch" may rest on a listed nationality when the listing gives no address country.
_CHECKED = {"DOB_MISMATCH": ("DOB",), "AGE_MISMATCH": ("DOB",),
            "COUNTRY_MISMATCH": ("COUNTRY", "NATIONALITY"), "NATIONALITY_MISMATCH": ("NATIONALITY", "COUNTRY"),
            "LOCATION_MISMATCH": ("LOCATION", "COUNTRY"), "ADDRESS_MISMATCH": ("LOCATION", "COUNTRY"),
            "ENTITY_TYPE_MISMATCH": ("ENTITY_TYPE",), "DIFFERENT_IDENTIFIERS": ("IDENTIFIERS",)}


def _checked(rels: dict, keys) -> str:
    return next((rels[k] for k in keys if rels[k] != "UNKNOWN"), "UNKNOWN")
_STATUS_OF = {"MISMATCH": "STILL_APPLIES", "MATCH": "NO_LONGER_APPLIES", "UNKNOWN": "CANNOT_DETERMINE"}


# ------------------------------------------------------------------ prompt --
def _v(x):
    if x is None or x == [] or (isinstance(x, str) and _blank(x)):
        return "not given"
    return "; ".join(map(str, x)) if isinstance(x, list) else str(x)


def _case_block(title: str, row: dict, note_label: str, note) -> str:
    c, b = customer(row), blocked_party(row["Matched_Payload"])
    r = relationships(c, b)
    return "\n".join([
        title,
        "Customer (screened party):",
        f"  Name: {_v(c['name'])} | Entity type: {_v(c['entity_type'])} | DOB: {_v(c['dob'])}",
        f"  Country: {_v(c['country'])} | State/province: {_v(c['state'])} | City: {_v(c['city'])}",
        f"  Identification: {_v(c['identification'])} | Nationality: not given | Gender: not given",
        "Blocked party (list entry):",
        f"  Names: {_v(b['names'][:6])} | Entity type: {_v(b['entity_type'])}",
        f"  List / reason: {_v(b['list_reason'])} | Block-list entity ID: {_v(b['block_list_id'])}",
        f"  DOB: {_v(b['dobs'])} | Gender: {_v(b['gender'])}",
        f"  Address/location: {_v(b['addresses'][:6])}",
        f"  Nationality: {_v(b['nationality'])} | Citizenship: {_v(b['citizenship'])}",
        f"  Identification: {_v(b['identifications'])} | Position: {_v(b['position'])}",
        f"  Other: {_v(b['notes'])}",
        "Python fact check (customer vs blocked party): "
        f"name similarity {_v(r['name_similarity'])}% | DOB {r['DOB']} | country {r['COUNTRY']} | "
        f"customer country vs listed nationality/citizenship {r['NATIONALITY']} | city {r['LOCATION']} | "
        f"entity type {r['ENTITY_TYPE']} | identification {r['IDENTIFIERS']}",
        f"{note_label}: {_v(note)}",
    ])


def _retrieval_block(ret: dict) -> str:
    diffs = [f"name similarity {ret['name']['similarity']}% (below threshold)" if ret["name"].get("materially_different") else None,
             "DOB differs" if ret["dob"].get("material_difference") else None,
             "country differs" if ret["country"].get("different") else None,
             "entity type differs" if ret["entity_type"].get("different") else None]
    return "\n".join([
        "PYTHON RETRIEVAL INFORMATION (used only to select this case - NOT evidence; previous customer vs current customer)",
        f"  Note (payload) similarity: {ret['payload_similarity']} | Adjusted similarity: {ret['adjusted_similarity']}",
        f"  Name: {_v(ret['name'].get('similarity'))}% | DOB: {ret['dob'].get('previous')!s} then vs "
        f"{ret['dob'].get('current')!s} now | Country: {ret['country'].get('previous')!s} then vs "
        f"{ret['country'].get('current')!s} now | Entity type: {ret['entity_type'].get('previous')!s} then vs "
        f"{ret['entity_type'].get('current')!s} now",
        f"  Material differences identified by Python: {_v([d for d in diffs if d])}",
    ])


_REL_LABELS = [("DOB", "DOB"), ("COUNTRY", "country"), ("NATIONALITY", "customer country vs listed nationality"),
               ("LOCATION", "city"), ("ENTITY_TYPE", "entity type"), ("IDENTIFIERS", "identification")]


def _both(current: dict, previous: dict) -> tuple[dict, dict]:
    return (relationships(customer(previous), blocked_party(previous["Matched_Payload"])),
            relationships(customer(current), blocked_party(current["Matched_Payload"])))


def relationship_changes(current: dict, previous: dict) -> list[str]:
    """Customer-vs-blocked-party relationships that differ between the previous case and now."""
    then, now = _both(current, previous)
    return [f"{label}: {then[k]} in the previous case, {now[k]} now" for k, label in _REL_LABELS if then[k] != now[k]]


def contradictions(current: dict, previous: dict) -> list[str]:
    """New identifying facts Python finds that weaken any previous clearance."""
    then, now = _both(current, previous)
    out = []
    if now["IDENTIFIERS"] == "MATCH":
        out.append("The customer's identification number matches the blocked party's.")
    if now["DOB"] == "MATCH" and then["DOB"] != "MATCH":
        out.append("The customer's DOB now matches the blocked party's DOB.")
    if then["ENTITY_TYPE"] != now["ENTITY_TYPE"] and "UNKNOWN" not in (then["ENTITY_TYPE"], now["ENTITY_TYPE"]):
        out.append(f"Customer vs blocked-party entity type was {then['ENTITY_TYPE']} in the previous case and is "
                   f"{now['ENTITY_TYPE']} now.")
    return out


def build_prompt(current: dict, previous: dict, retrieval: dict) -> str:
    changes = relationship_changes(current, previous)
    found = contradictions(current, previous)
    return "\n\n".join([
        _case_block(f"PREVIOUS WHITELISTED CASE (ID {_case_id(previous)})", previous,
                    "Previous analyst note", previous.get("Analyst_Remarks")) +
        "\nPrevious whitelist rationale (separately recorded): not given",
        _case_block("CURRENT CASE", current, "Current case note", current.get("Analyst_Remarks")),
        "CHANGES SINCE THE PREVIOUS CASE (Python, customer vs blocked party): " + (_v(changes) if changes else "none")
        + "\nNEW CONTRADICTORY INFORMATION FOUND BY PYTHON: " + (_v(found) if found else "none"),
        _retrieval_block(retrieval),
        f"Return the JSON object for previous case {_case_id(previous)}.",
    ])


# ------------------------------------------------------------------ checks --
def python_checks(review: AIReview, current: dict, previous: dict) -> tuple[AIReview, list[str]]:
    """Hold the AI's answer to the computed facts and the decision rules. Returns the
    corrected review and one line per change."""
    notes = []
    if str(review.previous_case_id).strip() != _case_id(previous):
        notes.append(f"previous_case_id corrected from {review.previous_case_id} to {_case_id(previous)}")
    review.previous_case_id = _case_id(previous)
    then, now = _both(current, previous)
    kept = []
    for r in review.rationales:
        keys = _CHECKED.get(r.rationale_type)
        if not keys:
            kept.append(r)
            continue
        rel_now, rel_then = _checked(now, keys), _checked(then, keys)
        if rel_then == "MATCH":
            notes.append(f"{r.rationale_type} removed: it was a MATCH in the previous case, so it cannot have "
                         "been a reason for that clearance")
            continue
        kept.append(r)
        if r.current_relationship != rel_now:
            notes.append(f"{r.rationale_type}: current relationship is {rel_now} in the data, "
                         f"not {r.current_relationship}")
            r.current_relationship = rel_now
        if rel_then != "UNKNOWN" and r.previous_relationship != rel_then:
            notes.append(f"{r.rationale_type}: previous relationship is {rel_then} in the data, "
                         f"not {r.previous_relationship}")
            r.previous_relationship = rel_then
        status = _STATUS_OF[rel_now]
        if r.rationale_status != status:
            notes.append(f"{r.rationale_type}: status {r.rationale_status} -> {status}")
            r.rationale_status = status
    review.rationales = kept

    for c in contradictions(current, previous):
        if c not in review.new_contradictory_information:
            review.new_contradictory_information.append(c)
            notes.append(f"added new contradictory information: {c}")

    def downgrade(why):
        notes.append(f"{review.decision} -> {MANUAL}: {why}")
        review.decision = MANUAL

    if review.decision == APPLICABLE:
        statuses = [r.rationale_status for r in review.rationales]
        if not statuses:
            downgrade("no previous rationale was identified")
        elif "STILL_APPLIES" not in statuses:
            downgrade("no previous rationale still applies")
        elif "CANNOT_DETERMINE" in statuses:
            downgrade("a previous rationale cannot be confirmed with the current data")
        elif "NO_LONGER_APPLIES" in statuses:
            downgrade("part of the previous reasoning no longer applies")
        elif review.new_contradictory_information:
            downgrade("there is new contradictory information: " + " ".join(review.new_contradictory_information))
    if review.decision != APPLICABLE and review.rationale_still_applies is True:
        statuses = {r.rationale_status for r in review.rationales}
        review.rationale_still_applies = None if "CANNOT_DETERMINE" in statuses else False
    return review, notes


# -------------------------------------------------------------------- call --
def review_one(current: dict, previous: dict, retrieval: dict, chat, cfg: AIReviewConfig) -> dict:
    prompt = build_prompt(current, previous, retrieval)
    started, last_err, info = time.time(), None, {}
    for _ in range(cfg.retries + 1):
        try:
            content, info = chat(SYSTEM_PROMPT, prompt)
            review = AIReview.model_validate_json(content)
            break
        except AIProviderLimit:
            raise                                                 # the caller reports it; no answer is made up
        except Exception as e:                                    # noqa: BLE001 -- invalid JSON, timeout, ...
            last_err = e
    else:
        return dict(previous_case_id=_case_id(previous), decision=MANUAL, ai_decision=None,
                    error=f"{type(last_err).__name__}: {last_err}"[:300],
                    python_checks=["the AI call failed -> MANUAL_REVIEW_REQUIRED"], prompt=prompt,
                    seconds=round(time.time() - started, 2))
    ai_decision = review.decision
    review, notes = python_checks(review, current, previous)
    return review.model_dump() | dict(ai_decision=ai_decision, python_checks=notes, prompt=prompt,
                                      seconds=round(time.time() - started, 2), call=info)


def review(current: dict, candidates: list[dict], *, cfg: AIReviewConfig | None = None, chat=None) -> dict:
    """current: the alert (wl_db.to_alert; Analyst_Remarks is the current case note, if any).
    candidates: [{"previous": a whitelisted record (wl_db.to_alert), "retrieval": its Gateway 2 result}],
    best first.
    Each previous case is reviewed on its own."""
    cfg = cfg or AIReviewConfig()
    chat = chat or ollama_chat(cfg)
    reviews = [review_one(current, c["previous"], c["retrieval"], chat, cfg) for c in candidates[:cfg.max_cases]]
    decisions = {r["decision"] for r in reviews}
    applicable = sorted((r for r in reviews if r["decision"] == APPLICABLE),
                        key=lambda r: r.get("confidence") or 0, reverse=True)
    if not reviews:
        overall, why = MANUAL, "No previous case was reviewed."
    elif APPLICABLE in decisions and NOT_APPLICABLE in decisions:
        overall, why = MANUAL, ("Previous cases disagree: at least one appears applicable and at least one "
                                "does not, so a human decides.")
    elif applicable:
        overall, why = APPLICABLE, f"Previous case {applicable[0]['previous_case_id']} appears applicable."
    elif decisions == {NOT_APPLICABLE}:
        overall, why = NOT_APPLICABLE, "No previous case's reasoning applies to this alert."
    else:
        overall, why = MANUAL, "No previous case's reasoning could be confirmed for this alert."
    return dict(decision=overall, explanation=why,
                strongest_precedent=applicable[0]["previous_case_id"] if overall == APPLICABLE else None,
                reviewed=len(reviews), skipped=max(0, len(candidates) - cfg.max_cases), reviews=reviews)


def load_candidates(con, g2_candidates: list[dict], client_id) -> list[dict]:
    """Gateway 2's send_to_ai cases with their WL_Records rows - only the client's own."""
    found = {a["WL_ID"]: a for a in db.alerts_by_keys(con, client_id, [g["WL_ID"] for g in g2_candidates])}
    return [dict(previous=found[g["WL_ID"]], retrieval=g) for g in g2_candidates if g["WL_ID"] in found]
