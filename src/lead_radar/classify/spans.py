"""Exact evidence spans, ported from the old radar's evidence_candidates().

Code cuts the post into exact spans and Jev *chooses* one, so evidence is verbatim by construction and Jev
never writes text. Spans are capped at 30 words to satisfy lead-intent.v1.
"""
from __future__ import annotations

import re

MAX_CANDIDATES = 8
MAX_WORDS = 30
_SENTENCE = re.compile(r"[^\n.!?]+(?:[.!?]+|$)|[^\n]+$")


def _clip(text: str) -> str:
    words = text.split()
    if len(words) <= MAX_WORDS:
        return text
    # Keep an exact prefix of the source: cut at the end of the 30th word.
    m = re.match(r"\s*(?:\S+\s+){%d}\S+" % (MAX_WORDS - 1), text)
    return m.group(0).strip() if m else text


def candidates(text: str) -> list[dict]:
    """Up to 8 non-overlapping {id, text} spans (sentences), each an exact substring of `text`.

    Overlapping candidates (e.g. the whole post plus its sentences) split Jev's probability mass between
    near-identical options, so candidates are sentences only; a single-sentence post is one candidate.
    """
    result: list[dict] = []
    whole = text.strip()
    for m in _SENTENCE.finditer(text):
        s = _clip(m.group(0).strip())
        if s and s not in {r["text"] for r in result} and len(s.split()) >= 2:
            result.append({"id": f"span_{len(result)}", "text": s})
        if len(result) == MAX_CANDIDATES:
            break
    if not result and whole:
        result.append({"id": "span_0", "text": _clip(whole)})
    return result
