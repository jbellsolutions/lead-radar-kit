"""Stage 1: Jev via TypeSafe /v1/systemone (spec §6).

Jev only picks among code-defined options and never writes text. One call asks five choice questions:
role, tier, urgency, evidence_span (exact spans cut by code) and need (the brief's need statements + other).
`decided_by: jev` means:
- non-buyers: P(not buyer) >= accept_confidence. Per the contract ("seller or other => tier none") the role then
  fixes tier none; seller/job_seeker/other are one decision here.
- buyers: role and tier >= accept_confidence, and for tiers above none a confident evidence_span
  (1 - P(none) >= accept_confidence), which becomes the lead's verbatim quote.
need and urgency are summaries: below the threshold, need_summary falls back to the evidence span and urgency to
"unknown".
Anything else goes to Stage 2.

Request/response shape and validation are ported from system-one-workers jev_swarm/vendor/jev/model.py.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

import httpx

from .. import contract
from ..models import IntentBrief, Post
from . import prompts, spans

ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class JevError(RuntimeError):
    pass


@dataclass
class JevVerdict:
    output: dict | None          # a lead-intent.v1 output when accepted, else None
    confidence: float
    reason: str                  # accepted | low_confidence:<q> | inconsistent:<why> | need_other | invalid_response
    choices: dict
    usage: dict


def validate_choice(answer: dict, ids) -> dict:
    ids = set(ids)
    try:
        probs = answer["probabilities"]
        nums = [*probs.values(), answer["confidence"]]
        ok = (answer["choice"] in ids and set(probs) == ids
              and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in nums)
              and abs(sum(probs.values()) - 1) < 0.02
              and probs[answer["choice"]] >= max(probs.values()) - 1e-6)
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        raise JevError("invalid TypeSafe answer")
    return answer


def build_request(post: Post, brief: IntentBrief, model: str) -> tuple[dict, dict]:
    """Returns (body, lookup) where lookup maps option ids back to text."""
    cands = spans.candidates(post.text)
    needs = {f"need_{i}": s for i, s in enumerate(brief.need_statements[:24])}
    state = {
        "offer": brief.offer, "buyer_profile": brief.buyer_profile, "platform": post.platform,
        "post_text": post.text[:4000], "parent_text": (post.parent_text or "")[:600] or None,
        "high_examples": brief.high_examples[:5], "medium_examples": brief.medium_examples[:5],
        "seller_signals": brief.seller_signals[:12],
    }
    rules = prompts.UNTRUSTED + prompts.EXCLUSIONS
    questions = {
        "role": {"type": "choice", "criteria": prompts.ROLE_RULES,
                 "instructions": rules + " Decide the author's role in `post_text` before anything else."},
        "tier": {"type": "choice", "criteria": prompts.TIER_RULES,
                 "instructions": rules + " Judge intent toward `offer` for the author of `post_text`. If unsure "
                                          "between two tiers, choose the lower one."},
        "urgency": {"type": "choice", "criteria": prompts.URGENCY_RULES,
                    "instructions": rules + " Only urgency stated by the author; never infer it from recency."},
        "evidence_span": {"type": "choice",
                          "criteria": {"none": "No span shows the author's own need for the offer.",
                                       **{c["id"]: c["text"] for c in cands}},
                          "instructions": rules + " Select the exact span of `post_text` that best shows the "
                                                  "author's own need for `offer`. Select none if no span does."},
        "need": {"type": "choice", "criteria": {**needs, "other": "None of these statements fits the author's need."},
                 "instructions": rules + " Which statement best summarizes the need the author expresses?"},
    }
    return {"model": model, "state": state, "questions": questions}, {"spans": {c["id"]: c["text"] for c in cands},
                                                                       "needs": needs}


def interpret(result: dict, body: dict, lookup: dict, post: Post, accept: float) -> JevVerdict:
    usage = result.get("usage") or {}
    try:
        answers = {q: validate_choice(result["answers"].get(q, {}), spec["criteria"])
                   for q, spec in body["questions"].items()}
    except (JevError, AttributeError, KeyError):
        return JevVerdict(None, 0.0, "invalid_response", {}, usage)
    choices = {q: a["choice"] for q, a in answers.items()}
    conf = {q: min(a["confidence"], a["probabilities"][a["choice"]]) for q, a in answers.items()}
    role = {"job_seeker": "other"}.get(choices["role"], choices["role"])
    tier = choices["tier"]
    if role != "buyer":
        # Contract rule "decide role before tier; seller or other => tier none": a confident non-buyer role fixes
        # the tier, so the tier head's none/low split is irrelevant. seller/job_seeker/other are one decision
        # here ("not a buyer"), so role confidence is P(not buyer).
        tier = "none"
        conf["role"] = 1.0 - answers["role"]["probabilities"].get("buyer", 0.0)
    if choices["evidence_span"] != "none":
        # Several sentences can each evidence the same need; what must be confident is that *some* span does
        # (ported from the old radar: evidence_ok = 1 - P(none) >= threshold). The quote is the top span.
        conf["evidence_span"] = 1.0 - answers["evidence_span"]["probabilities"].get("none", 0.0)
    # Non-buyers: the role decides. Buyers: role + tier, and buyer tiers also need a confident evidence span
    # (the lead's quote). need and urgency are summaries: when unsure, need falls back to the verbatim span and
    # urgency to "unknown".
    if role != "buyer":
        required = ["role"]
    else:
        required = ["role", "tier"] + (["evidence_span"] if tier != "none" else [])
    confidence = min(conf[q] for q in required)
    low = [q for q in required if conf[q] < accept]
    if low:
        return JevVerdict(None, confidence, "low_confidence:" + ",".join(sorted(low)), choices, usage)
    if tier != "none" and choices["evidence_span"] == "none":
        return JevVerdict(None, confidence, "inconsistent:tier_without_evidence", choices, usage)
    urgency = choices["urgency"] if tier != "none" and conf["urgency"] >= accept else "unknown"
    output = {
        "role": role, "tier": tier, "urgency": urgency,
        "need_summary": _need_summary(tier, choices, conf, lookup, accept),
        "evidence_quote": lookup["spans"].get(choices["evidence_span"], "") if tier != "none" else "",
        "confidence": round(confidence, 4),
    }
    try:
        contract.validate_lead_intent(output, post.text)
    except contract.ContractError as exc:
        return JevVerdict(None, confidence, f"inconsistent:contract:{exc}", choices, usage)
    return JevVerdict(output, confidence, "accepted", choices, usage)


def _need_summary(tier: str, choices: dict, conf: dict, lookup: dict, accept: float) -> str:
    if tier == "none":
        return ""
    if choices["need"] != "other" and conf["need"] >= accept:
        return lookup["needs"][choices["need"]]
    return lookup["spans"].get(choices["evidence_span"], "")[:240]


class JevClient:
    def __init__(self, api_key: str, model: str, *, accept: float = 0.8, concurrency: int = 8,
                 transport: httpx.AsyncBaseTransport | None = None, endpoint: str = ENDPOINT):
        self.model, self.accept, self.endpoint = model, accept, endpoint
        self._sem = asyncio.Semaphore(concurrency)
        self._http = httpx.AsyncClient(timeout=60, transport=transport, headers={"Authorization": f"Bearer {api_key}"})
        self.calls = 0

    async def aclose(self):
        await self._http.aclose()

    async def _post(self, body: dict) -> dict:
        for attempt in range(3):
            async with self._sem:
                try:
                    resp = await self._http.post(self.endpoint, json=body)
                except httpx.HTTPError as exc:
                    raise JevError(f"connection failed: {type(exc).__name__}") from None
            self.calls += 1
            if resp.status_code in (429, 503, 529) and attempt < 2:
                await asyncio.sleep(0.5 * 2 ** attempt)
                continue
            if resp.is_error:
                raise JevError(f"TypeSafe HTTP {resp.status_code}")
            return resp.json()
        raise JevError("TypeSafe unavailable")

    async def classify(self, post: Post, brief: IntentBrief) -> JevVerdict:
        body, lookup = build_request(post, brief, self.model)
        try:
            result = await self._post(body)
        except JevError as exc:
            return JevVerdict(None, 0.0, f"error:{exc}", {}, {})
        return interpret(result, body, lookup, post, self.accept)
