"""Lead Radar v2 data model (spec §4). Plain data; no I/O."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Platform = Literal[
    "reddit", "youtube", "tiktok", "instagram", "threads",
    "linkedin", "facebook", "google", "skool", "x",
    "onlinejobs", "weworkremotely", "freelancer", "indeed",
]
API_PLATFORMS: tuple[str, ...] = (
    "reddit", "youtube", "tiktok", "instagram", "threads", "linkedin", "facebook", "google",
)
BROWSER_PLATFORMS: tuple[str, ...] = ("skool", "facebook", "x", "linkedin")
# Job boards: their posts are listings with their own queue and classifier (never the M3 social cascade).
LISTING_PLATFORMS: tuple[str, ...] = ("onlinejobs", "weworkremotely", "freelancer", "indeed")

Role = Literal["buyer", "seller", "other"]
Tier = Literal["high", "medium", "low", "none"]
Urgency = Literal["now", "soon", "someday", "unknown"]
DecidedBy = Literal["rules", "jev", "jev_heuristic", "reasoning"]
LeadStatus = Literal["new", "approved", "contacted", "ignored"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def post_id(platform: str, canonical_url: str) -> str:
    return hashlib.sha1(f"{platform}{canonical_url}".encode()).hexdigest()


class Radar(BaseModel):
    id: str
    name: str
    query: str
    offer_context: str | None = None
    platforms: list[str] = Field(default_factory=lambda: ["all"])
    lookback_days: int = Field(default=14, ge=1, le=365)
    intent_brief_id: str | None = None
    schedule: str | None = None
    active: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class IntentBrief(BaseModel):
    id: str
    radar_id: str
    offer: str
    buyer_profile: str
    need_statements: list[str] = Field(min_length=1)
    high_examples: list[str]
    medium_examples: list[str]
    low_examples: list[str]
    seller_signals: list[str] = Field(min_length=5)
    terms: dict[str, list[str]]
    communities: dict[str, list[str]] = Field(default_factory=dict)
    negative_terms: list[str] = Field(default_factory=list)
    proposed_terms: dict[str, list[str]] = Field(default_factory=dict)
    version: int = 1

    @field_validator("terms")
    @classmethod
    def _max_terms(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for platform, terms in v.items():
            if len(terms) > 8:
                raise ValueError(f"at most 8 search terms per platform ({platform} has {len(terms)})")
        return v


class Post(BaseModel):
    id: str
    platform: str
    url: str
    parent_url: str | None = None
    kind: Literal["post", "comment", "reply", "video", "job"]
    author_handle: str | None = None
    author_url: str | None = None
    author_name: str | None = None
    text: str
    title: str | None = None
    posted_at: datetime | None = None
    engagement: dict = Field(default_factory=dict)
    community: str | None = None
    source_term: str
    run_id: str
    simhash: int = 0
    raw: dict = Field(default_factory=dict)
    parent_text: str | None = None  # comments: the thing they replied to (context for the classifier)


class Classification(BaseModel):
    post_id: str
    radar_id: str
    role: Role
    tier: Tier
    need_summary: str
    evidence_quote: str
    urgency: Urgency
    confidence: float = Field(ge=0, le=1)
    decided_by: DecidedBy
    freshness_downgrade: bool = False
    contract_version: str
    created_at: datetime = Field(default_factory=utcnow)


class Lead(BaseModel):
    id: str
    radar_id: str
    display_name: str | None = None
    handles: dict[str, str] = Field(default_factory=dict)
    profile_urls: dict[str, str] = Field(default_factory=dict)
    post_ids: list[str]
    best_tier: str
    score: float
    need_summary: str
    evidence_quote: str
    suggested_opener: str = ""
    bio: str | None = None
    website: str | None = None
    status: LeadStatus = "new"
    first_seen: datetime
    last_seen: datetime


class SourceStats(BaseModel):
    requests: int = 0
    credits: int = 0
    posts: int = 0
    errors: int = 0
    skipped: int = 0  # 403/404 items: skipped and logged, not a source failure
    error_messages: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # expected, non-failure conditions (e.g. nothing configured)
    cost_usd: float = 0.0  # non-credit spend for this source (Data Box job_board)


class Run(BaseModel):
    id: str
    radar_id: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    per_source: dict = Field(default_factory=dict)
    credits_used: int = 0
    browser_minutes: float = 0.0
    jev_calls: int = 0
    reasoning_tokens_in: int = 0
    reasoning_tokens_out: int = 0
    cost_usd: float = 0.0
    counts: dict = Field(default_factory=dict)
    qc_disagreement: float | None = None
    errors: list[str] = Field(default_factory=list)
