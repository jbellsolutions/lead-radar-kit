"""Contract loading and validation for lead-intent.v1 and intent-brief.v1.

The validator is the last line of defense against a model inventing evidence: an
evidence_quote must be a substring of post_text after case-folding and whitespace
normalization, and must be at most 30 words.
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import jsonschema

from .config import REPO_ROOT

CONTRACTS_DIR = REPO_ROOT / "contracts"
LEAD_INTENT = "lead-intent.v1"
INTENT_BRIEF = "intent-brief.v1"
MAX_EVIDENCE_WORDS = 30

_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


class ContractError(ValueError):
    pass


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    return json.loads((CONTRACTS_DIR / f"{name}.json").read_text())


def contract_version(name: str = LEAD_INTENT) -> str:
    c = load(name)
    return f"{c['name']}.v{c['version']}"


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return _WS.sub(" ", text).strip().casefold()


def is_verbatim(quote: str, source: str) -> bool:
    q = normalize(quote)
    return bool(q) and q in normalize(source)


def validate_lead_intent(output: dict, post_text: str) -> dict:
    """Validate a classifier output against lead-intent.v1. Returns the output unchanged or raises ContractError."""
    try:
        jsonschema.validate(output, load(LEAD_INTENT)["output_schema"])
    except jsonschema.ValidationError as exc:
        raise ContractError(f"schema: {exc.message}") from None
    if output["role"] in ("seller", "other") and output["tier"] != "none":
        raise ContractError(f"role {output['role']} requires tier none, got {output['tier']}")
    quote = output["evidence_quote"]
    if output["tier"] != "none" and not quote.strip():
        raise ContractError("tier above none requires an evidence_quote")
    if quote.strip():
        if len(quote.split()) > MAX_EVIDENCE_WORDS:
            raise ContractError(f"evidence_quote exceeds {MAX_EVIDENCE_WORDS} words")
        if not is_verbatim(quote, post_text):
            raise ContractError("evidence_quote is not a verbatim substring of post_text")
    return output


def validate_intent_brief(brief: dict) -> dict:
    try:
        jsonschema.validate(brief, load(INTENT_BRIEF)["output_schema"])
    except jsonschema.ValidationError as exc:
        raise ContractError(f"schema: {exc.message}") from None
    return brief
