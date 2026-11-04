"""AI review (app/ai_review.py): one AI call per previous case, the AI's answer held to the
facts Python computes, and the overall decision across cases. The AI is faked here."""
import json

from clearscreen import ai_review
from clearscreen.ai_review import APPLICABLE, MANUAL, NOT_APPLICABLE, build_prompt, relationships, customer, blocked_party
from clearscreen.similarity import Gateway2Config, score_candidate


def listing(name="JANE DOE", city="Chicago", country="United States", dob="1955-02-01", extra="Nationality: United States"):
    return (f"Entity Name: {name} Reference No.:58219 Reason: OFAC SDN Notes: Program SDGT "
            f"Address Information {'-' * 40} Address Type: Unknown City: {city} Country: {country} "
            f"AKA Listing NOT AVAILABLE Additional Information {'-' * 40} DOB: {dob} {extra}")


PREV = dict(WL_ID=12, Source_ID="12", ClientID="c1", Entity_Name="Jane Doe", Entity_Type="Individual",
            Entity_DOB="1980-05-10", Entity_Country="Canada", State="Ontario", City="Toronto",
            Entity_Identification=None, Matched_Payload=listing(),
            Analyst_Remarks="Customer is Canadian, born 1980. Listed person is American, born 1955. Not the same.")
CUR = {k: PREV[k] for k in ("ClientID", "Entity_Name", "Entity_Type", "Entity_DOB", "Entity_Country", "State",
                            "City", "Entity_Identification")} | dict(Matched_Payload=listing(city="London", country="England",
                                                                                              dob="1962-09-17"))


def rationale(t, prev_rel="MISMATCH", cur_rel="MISMATCH", status="STILL_APPLIES"):
    return dict(rationale_type=t, previous_values={"customer": "x", "blocked_party": "y"},
                current_values={"customer": "x", "blocked_party": "z"}, previous_relationship=prev_rel,
                current_relationship=cur_rel, rationale_status=status)


def answer(decision=APPLICABLE, case_id="12", rationales=None, contradictions=(), confidence=90):
    return json.dumps(dict(
        decision=decision, previous_case_id=str(case_id), previous_adjudication_summary="DOB and country differ.",
        rationales=rationales if rationales is not None else [rationale("DOB_MISMATCH"), rationale("COUNTRY_MISMATCH")],
        matching_facts=[], material_differences=[], missing_information=[],
        new_contradictory_information=list(contradictions), rationale_still_applies=decision == APPLICABLE,
        reasoning="r", confidence=confidence))


def fake_chat(*replies):
    calls = []

    def chat(system, user):
        calls.append(user)
        return replies[min(len(calls), len(replies)) - 1], {"total_s": 0.1}
    chat.calls = calls
    return chat


def cand(prev=PREV, cur=CUR):
    return dict(previous=prev, retrieval=score_candidate(cur, prev, 92.0, Gateway2Config()))


# --- facts Python computes
def test_relationships_compare_relationship_not_literal_values():
    r = relationships(customer(CUR), blocked_party(CUR["Matched_Payload"]))
    assert (r["DOB"], r["COUNTRY"], r["ENTITY_TYPE"], r["IDENTIFIERS"]) == ("MISMATCH", "MISMATCH", "MATCH", "UNKNOWN")


def test_blocked_party_read_from_payload():
    b = blocked_party(listing(extra="Nationality: Syria Gender: Female Passport Number: AB123"))
    assert b["list_reason"] == "OFAC SDN" and b["block_list_id"] == "58219" and b["gender"] == "Female"
    assert b["nationality"] == ["Syria"] and b["identifications"] == ["AB123"] and b["dobs"] == ["1955-02-01"]


def test_prompt_has_both_cases_note_and_retrieval_disclaimer():
    p = build_prompt(CUR, PREV, cand()["retrieval"])
    assert "PREVIOUS WHITELISTED CASE (ID 12)" in p and "CURRENT CASE" in p
    assert PREV["Analyst_Remarks"] in p and "NOT evidence" in p


# --- the AI's answer
def test_applicable_kept_when_relationships_still_hold():
    out = ai_review.review(CUR, [cand()], chat=fake_chat(answer()))
    assert out["decision"] == APPLICABLE and out["strongest_precedent"] == "12"
    assert out["reviews"][0]["python_checks"] == []


def test_ai_claim_against_the_data_is_corrected_and_downgraded():
    cur = CUR | {"Entity_DOB": None}                                        # DOB missing now
    out = ai_review.review(cur, [cand(cur=cur)], chat=fake_chat(answer()))
    r = out["reviews"][0]
    dob = next(x for x in r["rationales"] if x["rationale_type"] == "DOB_MISMATCH")
    assert (dob["current_relationship"], dob["rationale_status"]) == ("UNKNOWN", "CANNOT_DETERMINE")
    assert r["ai_decision"] == APPLICABLE and r["decision"] == MANUAL and out["decision"] == MANUAL


def test_matching_id_is_new_contradiction():
    cur = CUR | {"Entity_Identification": "AB1234567",
                 "Matched_Payload": listing(extra="Nationality: United States Passport Number: AB 123 4567")}
    r = ai_review.review(cur, [cand(cur=cur)], chat=fake_chat(answer()))["reviews"][0]
    assert r["decision"] == MANUAL
    assert any("identification" in c for c in r["new_contradictory_information"])


def test_dob_now_matching_reads_no_longer_applies():
    cur = CUR | {"Matched_Payload": listing(dob="1980-05-10")}
    reply = answer(rationales=[rationale("DOB_MISMATCH")])                  # the AI wrongly says it still applies
    r = ai_review.review(cur, [cand(cur=cur)], chat=fake_chat(reply))["reviews"][0]
    assert r["rationales"][0]["rationale_status"] == "NO_LONGER_APPLIES" and r["decision"] == MANUAL


def test_rationale_that_matched_before_is_removed():
    reply = answer(rationales=[rationale("DOB_MISMATCH"), rationale("ENTITY_TYPE_MISMATCH", prev_rel="MATCH")])
    r = ai_review.review(CUR, [cand()], chat=fake_chat(reply))["reviews"][0]
    assert [x["rationale_type"] for x in r["rationales"]] == ["DOB_MISMATCH"]


def test_wrong_case_id_corrected():
    r = ai_review.review(CUR, [cand()], chat=fake_chat(answer(case_id=999)))["reviews"][0]
    assert r["previous_case_id"] == "12"


def test_invalid_reply_retried_then_manual():
    chat = fake_chat("not json", "still not json")
    r = ai_review.review(CUR, [cand()], chat=chat)["reviews"][0]
    assert r["decision"] == MANUAL and r["error"] and len(chat.calls) == 2


# --- several previous cases
def test_each_case_reviewed_in_its_own_call():
    other = PREV | {"WL_ID": 13, "Source_ID": "13"}
    chat = fake_chat(answer(), answer(case_id=13))
    out = ai_review.review(CUR, [cand(), cand(prev=other)], chat=chat)
    assert len(chat.calls) == 2 and "(ID 12)" in chat.calls[0] and "(ID 13)" not in chat.calls[0]
    assert out["decision"] == APPLICABLE


def test_conflicting_cases_go_to_manual_review():
    other = PREV | {"WL_ID": 13, "Source_ID": "13"}
    out = ai_review.review(CUR, [cand(), cand(prev=other)],
                           chat=fake_chat(answer(), answer(decision=NOT_APPLICABLE, case_id=13)))
    assert out["decision"] == MANUAL and out["strongest_precedent"] is None


def test_strongest_applicable_case_is_highest_confidence():
    other = PREV | {"WL_ID": 13, "Source_ID": "13"}
    out = ai_review.review(CUR, [cand(), cand(prev=other)],
                           chat=fake_chat(answer(confidence=70), answer(case_id=13, confidence=95)))
    assert out["strongest_precedent"] == "13"


def test_all_not_applicable():
    out = ai_review.review(CUR, [cand()], chat=fake_chat(answer(decision=NOT_APPLICABLE)))
    assert out["decision"] == NOT_APPLICABLE
