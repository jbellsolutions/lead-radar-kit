"""Classifier cascade (spec §6): Stage 0 rules -> Stage 1 Jev -> Stage 2 reasoning, over a radar's pending queue.

- Without a TypeSafe key, Jev runs in heuristic mode: every post that survives Stage 0 goes to Stage 2, the run is
  flagged `jev_mode: heuristic`, and the report leads with a red warning. Stage 2 verdicts are always stored as
  decided_by "reasoning", so the "share decided by Jev" metric stays honest.
- Calibration: the first `calibration_first_n` Jev-High verdicts of a radar also go to Stage 2. Agreement is
  logged, and on disagreement the Stage 2 verdict wins.
- Freshness: a High older than `freshness_high_max_days` becomes Medium (freshness_downgrade=True).
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

from .. import contract
from ..config import Settings
from ..dedupe import templated_campaigns
from ..db import Store
from ..models import Classification, IntentBrief, Post, Radar, Run
from ..scrapecreators import ScrapeCreators
from . import stage0_rules, stage2_reasoning, verify
from .stage1_jev import JevClient

log = logging.getLogger("lead_radar.cascade")


def make_jev(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> JevClient | None:
    if not settings.has("TYPESAFE_API_KEY"):
        return None
    j = settings.get("jev", {}) or {}
    import os
    model = os.environ.get("TYPESAFE_MODEL") or j.get("model") or "jev-latest"
    return JevClient(settings.require("TYPESAFE_API_KEY"), model, accept=j.get("accept_confidence", 0.8),
                     concurrency=j.get("concurrency", 8), transport=transport,
                     endpoint=j.get("endpoint") or "https://api.typesafe.ai/v1/systemone")


def token_cost(settings: Settings, model: str, tin: int, tout: int) -> float:
    prices = (settings.get("models.prices_per_mtok") or {}).get(model) or {}
    return (tin * prices.get("in", 0) + tout * prices.get("out", 0)) / 1_000_000


async def classify_pending(settings: Settings, store: Store, radar: Radar, brief: IntentBrief, run: Run, *,
                           limit: int | None = None, jev: JevClient | None = None, use_jev: bool = True,
                           s2_transport: httpx.AsyncBaseTransport | None = None,
                           now: datetime | None = None, skip_stage0: bool = False,
                           calibrate: bool = True, sc: ScrapeCreators | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    d = settings.get("defaults", {}) or {}
    jcfg = settings.get("jev", {}) or {}
    own_jev = jev is None and use_jev
    if own_jev:
        jev = make_jev(settings)
    heuristic = jev is None
    calib_n = int(jcfg.get("calibration_first_n", 50)) if calibrate else 0
    fresh_days = int(d.get("freshness_high_max_days", 14))
    version = contract.contract_version()

    ids = store.pending(radar.id, limit)
    posts = [p for p in (store.get_post(i) for i in ids) if p]
    by_id = {p.id: p for p in store.radar_posts(radar.id)}
    by_id.update({p.id: p for p in posts})
    campaign = set() if skip_stage0 else templated_campaigns(by_id.values())
    stats = Counter()
    drop_reasons, decided, tiers, roles, jev_reasons = Counter(), Counter(), Counter(), Counter(), Counter()
    s2_cost = 0.0
    tokens = [0, 0]
    calib_lock = asyncio.Lock()
    sem = asyncio.Semaphore(int(jcfg.get("concurrency", 8)))
    own_sc = sc is None and settings.has("SCRAPECREATORS_API_KEY")
    if own_sc:
        sc = ScrapeCreators(settings.require("SCRAPECREATORS_API_KEY"),
                            base_url=settings.get("scrapecreators.base_url", "https://api.scrapecreators.com"))

    async def stage2(post: Post):
        nonlocal s2_cost
        v = await stage2_reasoning.classify(settings, post, brief, transport=s2_transport)
        tokens[0] += v.tokens_in
        tokens[1] += v.tokens_out
        s2_cost += token_cost(settings, v.model, v.tokens_in, v.tokens_out)
        stats["stage2_calls"] += 1
        if not v.ok:
            stats["stage2_failed"] += 1
            run.errors.append(f"stage2 invalid output for {post.id}: {v.note}")
        return v

    async def one(post: Post):
        async with sem:
            reason = None if skip_stage0 else stage0_rules.check(
                post, brief, lookback_days=radar.lookback_days, languages=d.get("languages", ["en"]),
                min_chars=d.get("min_text_chars", 20), now=now)
            if not reason and post.id in campaign:
                reason = "templated_campaign"
            if reason:
                store.save_drop(post.id, radar.id, run.id, reason)
                store.mark_queue(post.id, radar.id, "done")
                drop_reasons[reason.split(":")[0]] += 1
                return
            out, by = None, "reasoning"
            if not heuristic:
                jv = await jev.classify(post, brief)
                jev_reasons[jv.reason if jv.reason.startswith("low_confidence") else jv.reason.split(":")[0]] += 1
                if jv.output:
                    out, by = jv.output, "jev"
                    if out["tier"] == "high":
                        async with calib_lock:
                            check = store.calibration_count(radar.id) < calib_n
                            if check:  # reserve the slot before awaiting Stage 2
                                store.save_calibration(post.id, radar.id, run.id, "high", "pending")
                        if check:
                            v = await stage2(post)
                            store.save_calibration(post.id, radar.id, run.id, "high", v.output["tier"])
                            stats["calibration_checked"] += 1
                            if v.output["tier"] == "high":
                                stats["calibration_agreed"] += 1
                            elif v.ok:
                                out, by = v.output, "reasoning"
            if out is None:
                v = await stage2(post)
                out = v.output
            if out["tier"] == "high" and post.raw.get("via") == "google" and not post.raw.get("verified_full_post"):
                full = await verify.full_post(sc, post) if sc else None
                stats["google_verify"] += 1
                if full is None:
                    out = {**out, "tier": "medium"}  # snippet-only evidence is not enough for High
                    stats["google_unverified_capped"] += 1
                else:
                    store.update_post_content(full)
                    post = full
                    v = await stage2(post)
                    out, by = v.output, "reasoning"
                    stats["google_verified"] += 1
            downgrade = False
            if out["tier"] == "high" and post.posted_at and post.posted_at < now - timedelta(days=fresh_days):
                out = {**out, "tier": "medium"}
                downgrade = True
            c = Classification(post_id=post.id, radar_id=radar.id, decided_by=by, freshness_downgrade=downgrade,
                               contract_version=version, **out)
            store.save_classification(c)
            store.mark_queue(post.id, radar.id, "done")
            decided[by] += 1
            tiers[c.tier] += 1
            roles[c.role] += 1

    try:
        results = await asyncio.gather(*(one(p) for p in posts), return_exceptions=True)
    finally:
        if own_jev and jev:
            await jev.aclose()
        if own_sc and sc:
            await sc.aclose()
    for p, r in zip(posts, results):
        if isinstance(r, BaseException):
            store.mark_queue(p.id, radar.id, "failed")
            stats["failed"] += 1
            run.errors.append(f"classify {p.id}: {type(r).__name__}: {str(r)[:160]}")

    classified = sum(decided.values())
    jev_calls = jev.calls if jev else 0
    jev_cost_per = jcfg.get("cost_per_call_usd")
    counts = {
        "pending_in": len(posts), "stage0_dropped": sum(drop_reasons.values()), "stage0_reasons": dict(drop_reasons),
        "classified": classified, "decided_by": dict(decided),
        "jev_share": round(decided["jev"] / classified, 4) if classified else None,
        "jev_mode": "heuristic" if heuristic else "typesafe", "jev_outcomes": dict(jev_reasons),
        "high": tiers["high"], "medium": tiers["medium"], "low": tiers["low"], "none": tiers["none"],
        "sellers": roles["seller"], "buyers": roles["buyer"], "stage2_calls": stats["stage2_calls"],
        "stage2_failed": stats["stage2_failed"], "calibration_checked": stats["calibration_checked"],
        "calibration_agreed": stats["calibration_agreed"], "classify_failed": stats["failed"],
        "google_verified": stats["google_verified"], "google_unverified_capped": stats["google_unverified_capped"],
        "google_verify_credits": sc.meter.credits.get("google", 0) if sc else 0,
        "stage2_cost_usd": round(s2_cost, 6),
        "jev_cost_usd": None if jev_cost_per is None else round(jev_calls * jev_cost_per, 6),
    }
    run.jev_calls += jev_calls
    run.reasoning_tokens_in += tokens[0]
    run.reasoning_tokens_out += tokens[1]
    run.counts.update(counts)
    if heuristic:
        warning = "JEV IN HEURISTIC MODE: no TYPESAFE_API_KEY; every post went to the reasoning model"
        if warning not in run.errors:
            run.errors.insert(0, warning)
    return counts
