"""Request and response formats of the ClearScreen API."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from clearscreen.scoring.models import ComparisonTrace

_TEXT = Field(pattern=r"\S")            # required and not blank; numbers are accepted as text


class MatchRequest(BaseModel):
    """One alert to check. ClientID is required: only that client's whitelist is used."""
    model_config = ConfigDict(coerce_numbers_to_str=True)

    ClientID: str = _TEXT
    Unique_ID: str = _TEXT                       # the alert's own ID; returned with its result
    Screened_Fields: str = _TEXT                 # e.g. "Beneficiary Name,Beneficiary Country,..."
    Screened_Values: str = _TEXT                 # the matching values, comma-separated
    Database_Screened: str = _TEXT               # stored / returned only, not used in matching
    Screened_Date: str = _TEXT                   # stored / returned only, not used in matching
    Matched_Details: str = _TEXT                 # the LexisNexis listing text that was hit


class WLRecord(MatchRequest):
    """One whitelisted alert. ClientID + Unique_ID identify it; sending the pair again replaces it."""
    Analyst_Allow_Reasoning: str | None = None   # why it was whitelisted - read by the AI review


class AddResult(BaseModel):
    added: int
    replaced: int
    records: list[dict]                          # {ClientID, Unique_ID, status: added | replaced}


class MatchResult(BaseModel):
    ClientID: str
    Unique_ID: str
    disposition: Literal["CLEAR", "REVIEW", "FLAG"]
    confidence: float
    rule: Literal["exact_repeat_whitelisted", "no_listing_details", "score_below_threshold",
                  "no_similar_whitelisted", "similar_search_error", "prior_adjudication_applicable",
                  "manual_review_required", "prior_adjudication_not_applicable"]
    model: str                                   # which AI the endpoint uses (also when it was not needed)
    explanation: str
    score: float | None = None                   # the match score (customer vs listing), 0.80 threshold
    repeat_of: list[str] = []                    # Unique_IDs this alert exactly repeats
    precedent: str | None = None                 # CLEAR by the AI: the whitelisted Unique_ID relied on
    precedent_reasoning: str | None = None       # ... and its Analyst_Allow_Reasoning
    ai_calls: int = 0
    seconds: float = 0.0                         # the whole alert
    ai_seconds: float = 0.0                      # of which AI calls
    waited_seconds: float = 0.0                  # of which waiting on a provider's rate limit
    cost_usd: float = 0.0                        # AI cost at the provider's paid rates (0 for local)
    similar: dict | None = None                  # the similar-case search: candidates, near misses
    ai_review: dict | None = None                # each whitelisted case the AI judged, and Python's checks
    comparison_trace: ComparisonTrace | None = None


class SearchHit(BaseModel):
    Unique_ID: str
    similarity: float                            # 0-100, how alike the listing is to the search text
    customer: str | None = None
    Analyst_Allow_Reasoning: str | None = None


class ErrorResponse(BaseModel):
    """Every error: a stable code to act on, and a sentence to show or log."""
    error_code: str = Field(examples=["MISSING_CLIENT_ID"])
    message: str = Field(examples=["ClientID is required: whitelist records are only matched within the same client."])
    errors: list[dict] | None = None             # INVALID_REQUEST: which fields are wrong
    detail: str | None = None                    # GROQ_LIMIT_REACHED: Groq's own message
    completed: list[dict] | None = None          # GROQ_LIMIT_REACHED: results finished before the limit
    not_run: list[str] | None = None             # GROQ_LIMIT_REACHED: Unique_IDs not checked
