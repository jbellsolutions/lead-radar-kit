"""Job-listing classifier (contract listing-fit.v1). Listings never enter the M3 lead-intent cascade.

Rules first (free): too old, too short, pay-to-apply / recruit-others scams. The brief's negative terms are NOT
applied: they are written for social posts (job seekers say "job"), and every listing is a job. Everything
else gets one reasoning call (models.listings, falling back to models.reasoning). After the model:
- company_name is kept only when it appears verbatim in the listing or equals the board's own company field;
- a staffing / recruiter / relay employer is capped at fit medium (the real buyer is hidden);
- evidence_quote must be verbatim (one retry, then the verdict is fit none, confidence 0).

A listing is a lead when employer_type is direct and fit is high or medium (see `is_lead`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import jsonschema

from .. import contract, llm
from ..config import Settings
from ..db import Store
from ..models import IntentBrief, Post, Radar, Run
from .cascade import token_cost

log = logging.getLogger("lead_radar.listings")
CONTRACT = "listing-fit.v1"
SCAM = re.compile(r"\b(registration fee|pay (?:a|the) (?:fee|deposit)|buy (?:our|the) (?:course|starter kit)|"
                  r"recruit (?:\d+ )?(?:people|members|others)|unlimited earning potential|mlm|network marketing)\b", re.I)
STAFFING_HINT = re.compile(r"\b(our client|on behalf of (?:a|our) client|for (?:a|one of our) clients?|"
                           r"staffing (?:agency|firm|company)|recruitment (?:agency|firm)|we place|"
                           r"talent (?:agency|partner))\b", re.I)


def is_lead(verdict: dict) -> bool:
    return verdict.get("employer_type") == "direct" and verdict.get("fit") in ("high", "medium")


def rules(post: Post, brief: IntentBrief, *, lookback_days: int, now: datetime) -> str | None:
    if post.posted_at and post.posted_at < now - timedelta(days=lookback_days):
        return "older_than_lookback"
    if len(post.text.strip()) < 40:
        return "too_short"
    if SCAM.search(post.text):
        return "pay_to_apply_or_mlm"
    return None


def system_prompt() -> str:
    c = contract.load(CONTRACT)
    return (
        "You read one job listing for a lead radar and judge whether the business behind it could buy our offer. "
        "Output ONLY a JSON object with exactly these keys: employer_type, fit, role_title, company_name, need, "
        "evidence_quote, confidence.\n"
        f"Output spec: {json.dumps(c['output'])}\nRules:\n- " + "\n- ".join(c["rules"]) + "\n"
        "- evidence_quote: copy at most 30 consecutive words exactly from listing_text; empty string when fit is none."
    )


def board_company(post: Post) -> str:
    return str((post.raw or {}).get("company") or "").strip()


def user_payload(post: Post, brief: IntentBrief) -> str:
    return json.dumps({"offer": brief.offer, "buyer_profile": brief.buyer_profile, "board": post.platform,
                       "listing_text": post.text[:5000], "board_company": board_company(post),
                       "staffing_language_seen": bool(STAFFING_HINT.search(post.text))}, ensure_ascii=False)


def validate(out: dict, post: Post) -> dict:
    out = {k: out.get(k) for k in ("employer_type", "fit", "role_title", "company_name", "need", "evidence_quote",
                                   "confidence")}
    for k in ("role_title", "company_name", "need", "evidence_quote"):
        out[k] = re.sub(r"\s+", " ", out[k]).strip() if isinstance(out[k], str) else ("" if out[k] is None else out[k])
    if isinstance(out.get("confidence"), str):
        try:
            out["confidence"] = float(out["confidence"])
        except ValueError:
            pass
    try:
        jsonschema.validate(out, contract.load(CONTRACT)["output_schema"])
    except jsonschema.ValidationError as exc:
        raise contract.ContractError(f"schema: {exc.message}") from None
    if out["fit"] == "none":
        out["evidence_quote"] = ""
    elif not out["evidence_quote"]:
        raise contract.ContractError("fit above none requires an evidence_quote")
    if out["evidence_quote"]:
        if len(out["evidence_quote"].split()) > contract.MAX_EVIDENCE_WORDS:
            raise contract.ContractError("evidence_quote too long")
        if not contract.is_verbatim(out["evidence_quote"], post.text):
            raise contract.ContractError("evidence_quote is not verbatim")
    company = out["company_name"]
    if company and not (contract.is_verbatim(company, post.text)
                        or contract.normalize(company) == contract.normalize(board_company(post))):
        out["company_name"] = ""  # never keep a company the listing doesn't name
    if out["employer_type"] != "direct" and out["fit"] == "high":
        out["fit"] = "medium"
    return out


async def judge(settings: Settings, post: Post, brief: IntentBrief,
                transport: httpx.AsyncBaseTransport | None = None) -> tuple[dict | None, str, int, int, str]:
    """Returns (verdict or None, model, tokens_in, tokens_out, failure note)."""
    model = settings.get("models.listings") or llm.model_for(settings, "reasoning")
    tin = tout = 0
    note = ""
    for _ in range(2):
        try:
            comp = await llm.chat(settings, "reasoning", system_prompt(), user_payload(post, brief), max_tokens=500,
                                  transport=transport, model=model)
        except (RuntimeError, httpx.HTTPError) as exc:
            fallback = settings.get("models.reasoning_fallback")
            if fallback and model != fallback:
                model = fallback
                continue
            return None, model, tin, tout, f"provider:{str(exc)[:160]}"
        tin, tout = tin + comp.tokens_in, tout + comp.tokens_out
        try:
            return validate(llm.parse_json(comp.text), post), comp.model, tin, tout, ""
        except (ValueError, contract.ContractError) as exc:
            note = f"invalid:{exc}"
    return None, model, tin, tout, note


async def classify_listings(settings: Settings, store: Store, radar: Radar, brief: IntentBrief, run: Run, *,
                            transport: httpx.AsyncBaseTransport | None = None, now: datetime | None = None,
                            limit: int | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    version = contract.contract_version(CONTRACT)
    posts = [p for p in (store.get_post(i) for i in store.pending_listings(radar.id, limit)) if p]
    sem = asyncio.Semaphore(int((settings.get("listings") or {}).get("concurrency", 6)))
    fits, employers, ruled = Counter(), Counter(), Counter()
    stats = Counter()
    cost = 0.0
    tokens = [0, 0]

    def save(post: Post, v: dict, by: str, reason: str | None = None) -> None:
        store.save_listing_verdict({"post_id": post.id, "radar_id": radar.id, "run_id": run.id, "decided_by": by,
                                    "reason": reason, "contract_version": version, **v})
        store.mark_listing(post.id, radar.id, "done")
        fits[v["fit"]] += 1
        employers[v["employer_type"]] += 1

    async def one(post: Post) -> None:
        nonlocal cost
        async with sem:
            reason = rules(post, brief, lookback_days=radar.lookback_days, now=now)
            if reason:
                ruled[reason.split(":")[0]] += 1
                save(post, {"employer_type": "unknown", "fit": "none", "role_title": post.title or "",
                            "company_name": board_company(post) or None, "need": "", "evidence_quote": "",
                            "confidence": 1.0}, "rules", reason)
                return
            v, model, tin, tout, note = await judge(settings, post, brief, transport)
            tokens[0] += tin
            tokens[1] += tout
            cost += token_cost(settings, model, tin, tout)
            stats["calls"] += 1
            if v is None:
                stats["failed"] += 1
                run.errors.append(f"listing classifier gave up on {post.id}: {note}")
                v = {"employer_type": "unknown", "fit": "none", "role_title": post.title or "", "company_name": "",
                     "need": "", "evidence_quote": "", "confidence": 0.0}
            v["company_name"] = v["company_name"] or board_company(post) or None
            save(post, v, "reasoning")

    results = await asyncio.gather(*(one(p) for p in posts), return_exceptions=True)
    for p, r in zip(posts, results):
        if isinstance(r, BaseException):
            store.mark_listing(p.id, radar.id, "failed")
            stats["crashed"] += 1
            run.errors.append(f"classify listing {p.id}: {type(r).__name__}: {str(r)[:160]}")
    counts = {"listings_pending_in": len(posts), "listings_ruled_out": sum(ruled.values()),
              "listings_rule_reasons": dict(ruled), "listing_fit": dict(fits), "listing_employers": dict(employers),
              "listing_calls": stats["calls"], "listing_failed": stats["failed"] + stats["crashed"],
              "listing_leads": 0, "listing_cost_usd": round(cost, 6)}
    counts["listing_leads"] = sum(1 for v in store.listing_verdicts(radar.id) if v["run_id"] == run.id and is_lead(v))
    run.reasoning_tokens_in += tokens[0]
    run.reasoning_tokens_out += tokens[1]
    run.counts.update(counts)
    return counts
