"""Stage 0: free, deterministic drops (spec §6). Every drop records its reason.

Drops when:
- the post is older than lookback_days (an unknown date is kept);
- the text is shorter than min_text_chars;
- the language is confidently not in config (a platform language field, or mostly non-Latin script); uncertainty keeps the post;
- the text matches a seller signal or negative term AND has no question and no request verb;
- the author is the creator on a video platform: the video itself, or the creator replying under it
  (the creator's own post is context, not a lead).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..models import IntentBrief, Post

VIDEO_PLATFORMS = {"youtube", "tiktok", "instagram"}
REQUEST_VERBS = re.compile(
    r"\b(need|needs|needed|needing|looking for|look for|recommend|recommendation|recommendations|anyone know|"
    r"any(?:one|body) (?:have|got|use)|hire|hiring|help me|can (?:someone|anyone|you)|where (?:can|do|should) i|"
    r"suggest|suggestions|advice|seeking|searching for|want to find|refer|referral|who do you use|"
    r"how do (?:i|you) find)\b", re.I)
LANG_FIELDS = ("desc_language", "comment_language", "language", "lang")


def _phrase_re(phrase: str) -> re.Pattern:
    return re.compile(r"(?<![\w])" + re.escape(phrase.lower()) + r"(?![\w])")


def _language(post: Post) -> str | None:
    for k in LANG_FIELDS:
        v = post.raw.get(k)
        if isinstance(v, str) and len(v) >= 2 and v.lower() not in ("un", "und", "unknown"):
            return v.lower()
    return None


def _mostly_non_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 20:
        return False
    latin = sum(1 for c in letters if c.isascii() or "À" <= c <= "ɏ")
    return latin / len(letters) < 0.5


def check(post: Post, brief: IntentBrief, *, lookback_days: int, languages: list[str], min_chars: int = 20,
          now: datetime | None = None) -> str | None:
    """Return a drop reason, or None to keep the post."""
    now = now or datetime.now(timezone.utc)
    if post.posted_at and post.posted_at < now - timedelta(days=lookback_days):
        return "older_than_lookback"
    if len(post.text.strip()) < min_chars:
        return "too_short"
    lang = _language(post)
    if lang and not any(lang.startswith(l.lower()) for l in languages):
        return f"language:{lang}"
    if not lang and "en" in languages and _mostly_non_latin(post.text):
        return "language:non_latin"
    if post.platform in VIDEO_PLATFORMS and post.kind == "video":
        return "creator_own_post"
    if post.raw.get("is_creator") is True:
        return "creator_reply"
    text = post.text.lower()
    asks = "?" in text or REQUEST_VERBS.search(text)
    if not asks:
        for phrase in brief.seller_signals:
            if phrase.strip() and _phrase_re(phrase).search(text):
                return f"seller_signal:{phrase}"
        for phrase in brief.negative_terms:
            if phrase.strip() and _phrase_re(phrase).search(text):
                return f"negative_term:{phrase}"
    return None
