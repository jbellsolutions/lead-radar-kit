"""Stage 2: reasoning model (spec §6). Same contract, JSON-only output, validated.

models.reasoning is tried first and models.reasoning_fallback on provider errors. One retry on invalid output;
after that the verdict is tier none with confidence 0, and the failure is logged.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from .. import contract, llm
from ..config import Settings
from ..models import IntentBrief, Post
from . import prompts

log = logging.getLogger("lead_radar.stage2")


def system_prompt() -> str:
    c = contract.load(contract.LEAD_INTENT)
    return (
        "You classify one social post for a lead radar. Output ONLY a JSON object with exactly these keys: "
        "role, tier, need_summary, evidence_quote, urgency, confidence.\n"
        f"Output spec: {json.dumps(c['output'])}\n"
        "Rules:\n- " + "\n- ".join(c["rules"]) + "\n"
        "- Job seekers are role other.\n"
        f"- {prompts.EXCLUSIONS}\n"
        "- Tier definitions: " + json.dumps(prompts.TIER_RULES) + "\n"
        "- Urgency definitions: " + json.dumps(prompts.URGENCY_RULES) + "\n"
        "- evidence_quote: copy at most 30 consecutive words exactly from post_text (never from parent_text); "
        "use an empty string when tier is none.\n"
        "- need_summary: one short line in plain words; empty string when tier is none.\n"
        f"{prompts.UNTRUSTED}"
    )


def user_payload(post: Post, brief: IntentBrief) -> str:
    return json.dumps({
        "offer": brief.offer, "buyer_profile": brief.buyer_profile, "platform": post.platform,
        "post_text": post.text[:4000], "parent_text": (post.parent_text or "")[:600] or None,
        "high_examples": brief.high_examples[:5], "medium_examples": brief.medium_examples[:5],
        "seller_signals": brief.seller_signals[:12],
    }, ensure_ascii=False)


@dataclass
class ReasoningVerdict:
    output: dict
    ok: bool
    model: str
    tokens_in: int
    tokens_out: int
    note: str = ""


def _coerce(out: dict) -> dict:
    out = {k: out.get(k) for k in ("role", "tier", "need_summary", "evidence_quote", "urgency", "confidence")}
    if out["role"] == "job_seeker":
        out["role"] = "other"
    if out["role"] in ("seller", "other"):
        out["tier"], out["evidence_quote"], out["need_summary"] = "none", "", ""
    if out["tier"] == "none":
        out["evidence_quote"] = ""
    if isinstance(out.get("confidence"), str):
        try:
            out["confidence"] = float(out["confidence"])
        except ValueError:
            pass
    for k in ("need_summary", "evidence_quote"):
        out[k] = (out[k] or "").replace("\n", " ").strip() if isinstance(out[k], (str, type(None))) else out[k]
    return out


async def classify(settings: Settings, post: Post, brief: IntentBrief,
                   transport: httpx.AsyncBaseTransport | None = None) -> ReasoningVerdict:
    system, user = system_prompt(), user_payload(post, brief)
    tin = tout = 0
    last_err = ""
    model = llm.model_for(settings, "reasoning")
    for attempt in range(2):
        try:
            comp = await llm.chat(settings, "reasoning", system, user, max_tokens=500, transport=transport, model=model)
        except (RuntimeError, httpx.HTTPError) as exc:
            fallback = settings.get("models.reasoning_fallback")
            if fallback and model != fallback:
                log.warning("stage2 %s failed (%s); falling back to %s", model, exc, fallback)
                model = fallback
                continue
            last_err = f"provider:{exc}"
            break
        tin, tout = tin + comp.tokens_in, tout + comp.tokens_out
        try:
            out = contract.validate_lead_intent(_coerce(llm.parse_json(comp.text)), post.text)
            return ReasoningVerdict(out, True, comp.model, tin, tout)
        except (ValueError, contract.ContractError) as exc:
            last_err = f"invalid:{exc}"
            continue
    log.warning("stage2 gave up on %s: %s", post.id, last_err)
    fail = {"role": "other", "tier": "none", "need_summary": "", "evidence_quote": "", "urgency": "unknown",
            "confidence": 0.0}
    return ReasoningVerdict(fail, False, model, tin, tout, note=last_err[:200])
