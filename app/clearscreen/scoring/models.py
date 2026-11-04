from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Disposition = Literal["FLAG", "REVIEW", "CLEAR"]
Level = Literal["High", "Medium", "Low"]
RiskCategory = Literal[
    "Sanctions/BIS/Terrorism",
    "PEP",
    "Unauthorized Bank",
    "Regulatory/SOE/FATF",
    "Adverse Media",
    "Other",
]


class CaseInput(BaseModel):
    id: str | None = None
    search_fields: str | None = None
    search_fields_value: str | None = None
    # Entity type as supplied by the caller: "Individual" or "Corporate"
    # (also accepts Company/Entity/Person/Vessel/Aircraft). When present it is
    # AUTHORITATIVE -- the engine stops inferring type from name keywords, which
    # misread "Skyvale AirlInes" as an individual and raised a false
    # entity_type_conflict that the model then cited as grounds to clear.
    xml_entity_type: str | None = None
    xml_entity_name: str | None = None
    xml_best_name: str | None = None
    xml_full_name: str | None = None
    xml_entity_score: float | None = None
    xml_score: float | None = None
    xml_listing: str | None = None


class ParsedCase(BaseModel):
    search: dict
    matched_names: list[str]
    listing: dict
    query_text: str
    xml_entity_score: float | None = None
    xml_score: float | None = None


def _as_field_names(value):
    """Coerce whatever the model emitted into a flat list of field-name strings.

    Shown the comparison-trace rows, qwen3:8b echoes that shape back -- emitting
    agreeing_fields as objects like {"field": "name", "result": "EXACT"} rather
    than ["name"]. Strict validation rejected it, the retry loop reproduced the
    same mistake, and the request failed with a 500 over a field the merged
    pipeline overwrites from the deterministic trace two lines later. These lists
    are advisory, so parse leniently rather than fail the whole adjudication.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out = []
    for item in value:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("field") or item.get("name") or item.get("field_name")
        else:
            name = None
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


class MatchQuality(BaseModel):
    level: Level
    is_match: bool
    agreeing_fields: list[str] = Field(default_factory=list)
    differing_fields: list[str] = Field(default_factory=list)
    rationale: str

    @field_validator("agreeing_fields", "differing_fields", mode="before")
    @classmethod
    def _coerce_field_names(cls, v):
        return _as_field_names(v)


class SimilarityJudgment(BaseModel):
    # The LLM often emits numeric-looking case ids as JSON integers; coerce to str.
    model_config = ConfigDict(coerce_numbers_to_str=True)
    similar_case_ids: list[str] = Field(default_factory=list)
    are_any_similar: bool
    rationale: str


class RiskClassification(BaseModel):
    category: RiskCategory
    level: Level
    rationale: str


class FinalDisposition(BaseModel):
    # The LLM often emits numeric-looking case ids as JSON integers; coerce to str.
    model_config = ConfigDict(coerce_numbers_to_str=True)
    disposition: Disposition
    confidence: float = Field(ge=0.0, le=1.0)
    cited_case_ids: list[str] = Field(default_factory=list)
    rationale: str


class NonMatchConfidence(BaseModel):
    """How sure the model is that the screened party is NOT the listed entity.

    The non-match path returns before any disposition call, so it previously had
    no model output to report and used a hardcoded constant. This tiny schema
    gives that path a real, case-specific number without pulling in the unused
    disposition/cited_case_ids fields of FinalDisposition.
    """
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class MatchAndRisk(BaseModel):
    """Combined output of the merged (2-call) pipeline's first call: match_quality
    and risk_classification produced in a single reasoning pass. Reuses the exact
    sub-schemas so validation/coercion is identical to the 4-call pipeline."""
    match: MatchQuality
    risk: RiskClassification


class SimilarityAndDisposition(BaseModel):
    """Combined output of the merged pipeline's second call: similarity judgment and
    the final disposition produced in a single reasoning pass."""
    similarity: SimilarityJudgment
    disposition: FinalDisposition


class PrecedentRef(BaseModel):
    id: str
    verdict: str
    distance: float
    snippet: str


class AdjudicationResult(BaseModel):
    id: str | None = None
    disposition: Disposition
    confidence: float
    match_quality: MatchQuality
    risk: RiskClassification | None = None
    similarity: SimilarityJudgment | None = None
    precedents: list[PrecedentRef] = Field(default_factory=list)
    cited_case_ids: list[str] = Field(default_factory=list)
    rationale: str
    trace: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _risk_required_when_matched(self):
        if self.match_quality.is_match and self.risk is None:
            raise ValueError("risk must be populated when match_quality.is_match is true")
        return self


CheckResult = Literal["pass", "fail", "unknown"]
CompareResult = Literal["exact", "partial", "mismatch", "unknown"]

# Fixed row order for ComparisonTrace.comparisons. Emitted in full on every
# response (missing data -> result="unknown") so downstream consumers can render
# a stable table instead of a variable-length list.
COMPARISON_FIELDS = (
    "name", "entity_type", "dob", "address", "address.city", "address.state",
    "address.country", "country_risk", "id", "alias", "other",
)


class BlockingCheckRow(BaseModel):
    """One deterministic hard-fact check, as surfaced in the API response.

    Tri-state on purpose: "unknown" means the check could not run (data absent
    on one side), which is materially different from "pass" (checked, no
    conflict). A boolean would collapse those two into a false assurance.
    """
    check: str
    display_name: str
    result: CheckResult
    notes: str


class ComparisonRow(BaseModel):
    """Field-level comparison between the screened party and the listed entity.

    Every value here is computed in Python from ParsedCase -- never authored by
    the LLM -- so the row is reproducible and auditable.
    """
    field: str
    input_value: str
    candidate_value: str
    normalized_input: str
    normalized_candidate: str
    rule_applied: str
    method: str
    result: CompareResult
    score_contribution: float
    notes: str


class ListingRow(BaseModel):
    """One row of a LexisNexis listing as split by the v2 parser, with its score."""
    row: str
    name: str
    entity_type: str = ""
    country: str = ""
    state: str = ""
    city: str = ""
    dob: str = ""
    id: str = ""
    score: float = 0.0
    best: bool = False


class ComparisonTrace(BaseModel):
    blocking_checks: list[BlockingCheckRow] = Field(default_factory=list)
    comparisons: list[ComparisonRow] = Field(default_factory=list)

    # How well the COMPARABLE fields agreed, 0..1. Normalised by the weight that
    # was actually achievable, so an exact match on the only available field
    # scores 1.0 rather than being penalised for fields the listing never had.
    deterministic_score: float = 0.0

    # How much of the weighted field set could be compared at all, 0..1. Read it
    # alongside the score: 1.0 score with 0.45 coverage means "everything we could
    # check matched, but we could only check 45% of the evidence base".
    evidence_coverage: float = 0.0

    # A LexisNexis listing split by the v2 parser: every row and its score. The
    # comparisons above are the best row's. Empty when the listing was read the
    # old way.
    listing_rows: list[ListingRow] = Field(default_factory=list)


class FastAdjudicationResult(AdjudicationResult):
    """Response type for /adjudicate/fast only.

    Subclassed rather than adding comparison_trace to AdjudicationResult so that
    /adjudicate and /adjudicate/batch responses stay byte-for-byte unchanged
    (they would otherwise gain a "comparison_trace": null key).
    """
    comparison_trace: ComparisonTrace = Field(default_factory=ComparisonTrace)


class FastV2AdjudicationResult(FastAdjudicationResult):
    """Response type for /adjudicate/fastv2 only (/adjudicate/fast is unchanged).

    llm_called  False when the deterministic score decided the case on its own.
    gate        What decided it: "score_below_threshold" (auto-CLEAR),
                "prior_confirmed_hit" (auto-REVIEW), or None when the LLM path ran.
    """
    llm_called: bool = True
    gate: str | None = None


class V3Judgement(BaseModel):
    """The single LLM call of /adjudicate/fastv3. The model answers two questions
    only -- the risk rating and the outcome are decided in Python from the policy."""
    model_config = ConfigDict(coerce_numbers_to_str=True)

    same_entity: bool
    same_situation_as: str | None = None     # id of a previously-cleared case, or null
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str

    @field_validator("same_situation_as", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if v in ("", "null", "none", "None") else v


class FastV3AdjudicationResult(FastV2AdjudicationResult):
    """Response type for /adjudicate/fastv3 only.

    gate          What decided it: "exact_repeat_cleared", "exact_repeat_confirmed",
                  "exact_repeat_mixed", "score_below_threshold", "prior_confirmed_hit",
                  or "ai_policy" when the LLM was called.
    risk_rating   High / Medium / Low from config/risk_sources.json.
    risk_sources  Every source type that matched, with the words that triggered it.
    rule_applied  The policy rule that produced the disposition.
    """
    risk_rating: str | None = None
    risk_sources: list[str] = Field(default_factory=list)
    rule_applied: str | None = None


class V4Judgement(BaseModel):
    """The single LLM call of /adjudicate/fastv4 on the precedent path: which past
    analyst decision fits this alert, and does the analyst's reason still hold."""
    model_config = ConfigDict(coerce_numbers_to_str=True)

    closest_case: str | None = None
    reason_applies: bool | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str

    @field_validator("closest_case", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if v in ("", "null", "none", "None") else v


class FastV4AdjudicationResult(FastV3AdjudicationResult):
    """Response type for /adjudicate/fastv4 only.

    precedent_evidence     The past decision the outcome rests on: case id, verdict,
                           how it relates (same listing / same customer), date, the
                           analyst's words, the THEN vs NOW field table, whether the
                           reason still applies and the AI's evidence. None when no
                           precedent existed (the v3 path answered).
    precedents_considered  Every past alert that was shown to the AI.
    """
    precedent_evidence: dict | None = None
    precedents_considered: list[str] = Field(default_factory=list)


class V41Judgement(BaseModel):
    """The single LLM call of /adjudicate/fastv4.1 - the question is flipped: is
    there positive evidence this alert is OUR customer?"""
    model_config = ConfigDict(coerce_numbers_to_str=True)

    evidence_ours: bool
    closest_case: str | None = None
    reason_applies: bool | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str

    @field_validator("closest_case", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if v in ("", "null", "none", "None") else v


class FastV41AdjudicationResult(FastV4AdjudicationResult):
    """Response type for /adjudicate/fastv4.1 only.

    basis          "direct" (past alerts on the same listing / for the same
                   customer), "pattern" (similar past decisions on other customers),
                   or None when the fastv3 path answered.
    evidence_ours  The AI's answer to "is there positive evidence this is our
                   customer?" (None when it was not asked).
    """
    basis: str | None = None
    evidence_ours: bool | None = None


class FastV42AdjudicationResult(FastV41AdjudicationResult):
    """Response type for /adjudicate/fastv4.2 only (same fields as fastv4.1)."""


class V43Judgement(BaseModel):
    """The single LLM call of /adjudicate/fastv4.3: based on how similar hits were
    whitelisted before, should this hit be cleared?"""
    model_config = ConfigDict(coerce_numbers_to_str=True)

    decision: Literal["CLEAR", "FLAG", "REVIEW"]
    cited_cases: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str

    @field_validator("decision", mode="before")
    @classmethod
    def _upper(cls, v):
        return str(v).strip().upper()

    @field_validator("cited_cases", mode="before")
    @classmethod
    def _as_list(cls, v):
        if v in (None, "", "null"):
            return []
        return [str(x) for x in (v if isinstance(v, list) else [v])]


class FastV43AdjudicationResult(FastV2AdjudicationResult):
    """Response type for /adjudicate/fastv4.3 only.

    Outcomes: CLEAR = safe to whitelist, FLAG = similar whitelisted pattern but not
    sure (a human confirms), REVIEW = no comparable whitelisted pattern.
    similar_whitelisted  every similar past whitelisted alert shown to the AI, with
                         its similarity on both sides and the analyst's note
    python_reasoning     what Python established (repeat, score, cases found)
    ai_reasoning         the AI's own reasoning (None when the AI was not called)
    rule_applied         the step that produced the outcome
    """
    similar_whitelisted: list[dict] = Field(default_factory=list)
    python_reasoning: str = ""
    ai_reasoning: str | None = None
    rule_applied: str | None = None


class FastV44AdjudicationResult(FastV43AdjudicationResult):
    """Response type for /adjudicate/fastv4.4 only (same fields as fastv4.3; each
    similar_whitelisted entry also says which side matched)."""


class FastV45AdjudicationResult(FastV44AdjudicationResult):
    """Response type for /adjudicate/fastv4.5 only (same fields as fastv4.4)."""


class ModelTestAdjudicationResult(FastV44AdjudicationResult):
    """Response type for /adjudicate/modeltest/<model>: fastv4.4's fields plus
    model_test - the model under test, the AI's own decision before Python's checks,
    timings and the model's thinking (see app/pipeline_model_test.py)."""
    model_test: dict = Field(default_factory=dict)
