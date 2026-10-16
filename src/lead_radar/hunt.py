"""One radar run: brief -> adapters (concurrent) -> normalize -> dedupe -> store -> classification queue.

Deterministic code only. Classification (M3) consumes the queue; `dry_run` fetches and counts without writing.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import expander
from .adapters import API_ADAPTERS, LISTING_ADAPTERS, AdapterContext
from .config import Settings
from .dedupe import dedupe
from .db import Store
from .models import API_PLATFORMS, BROWSER_PLATFORMS, LISTING_PLATFORMS, IntentBrief, Radar, Run, utcnow
from .scrapecreators import OutOfCredits, ScrapeCreators, ScrapeCreatorsError


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "radar"


def resolve_platforms(spec: str | list[str]) -> list[str]:
    items = spec if isinstance(spec, list) else [s.strip() for s in spec.split(",") if s.strip()]
    out: list[str] = []
    for s in items:
        if s == "all":
            out += list(API_PLATFORMS) + list(LISTING_PLATFORMS) + [p for p in BROWSER_PLATFORMS if p not in API_PLATFORMS]
        elif s == "api":
            out += list(API_PLATFORMS)
        elif s == "listings":
            out += list(LISTING_PLATFORMS)
        else:
            out.append(s)
    return list(dict.fromkeys(out))


@dataclass
class HuntResult:
    run: Run
    radar: Radar
    brief: IntentBrief
    dry_run: bool
    queued: list[str] = field(default_factory=list)


async def get_brief(settings: Settings, store: Store, radar: Radar, brief_path: Path | None,
                    refresh: bool = False) -> tuple[IntentBrief, int, int]:
    """Returns (brief, tokens_in, tokens_out). Cached per radar; regenerated only on refresh."""
    if radar.intent_brief_id and not refresh and not brief_path:
        cached = store.get_brief(radar.intent_brief_id)
        if cached:
            return cached, 0, 0
    prev = store.get_brief(radar.intent_brief_id) if radar.intent_brief_id else None
    version = (prev.version + 1) if prev else 1
    if brief_path:
        loaded = expander.load_brief_file(brief_path, radar.id, version)
        same = {"id", "version"}
        if prev and prev.model_dump(exclude=same) == loaded.model_dump(exclude=same):
            return prev, 0, 0  # unchanged file: keep the cached version
        return loaded, 0, 0
    brief, completion = await expander.expand(settings, radar.id, radar.query, radar.offer_context, version)
    return brief, completion.tokens_in, completion.tokens_out


async def hunt(settings: Settings, store: Store, query: str, *, platforms: str | list[str] = "all",
               days: int | None = None, dry_run: bool = False, brief_path: Path | None = None,
               radar_name: str | None = None, offer_context: str | None = None,
               sc: ScrapeCreators | None = None, classify: bool = True, jev=None, use_jev: bool = True,
               s2_transport=None, listing_transport=None, listing_sleep=None, databox=None) -> HuntResult:
    name = radar_name or slug(query)
    radar = store.get_radar(name) or Radar(id=uuid.uuid4().hex[:12], name=name, query=query, offer_context=offer_context)
    radar.platforms = resolve_platforms(platforms)
    if days:
        radar.lookback_days = days

    brief, tin, tout = await get_brief(settings, store, radar, brief_path)
    run = Run(id=uuid.uuid4().hex[:12], radar_id=radar.id, reasoning_tokens_in=tin, reasoning_tokens_out=tout)
    if not dry_run:
        if radar.intent_brief_id != brief.id:
            store.save_radar(radar)  # FK target for the brief
            store.save_brief(brief)
            radar.intent_brief_id = brief.id
        store.save_radar(radar)
        store.save_run(run)

    d = settings.get("defaults", {})
    own_box = None
    ctx = AdapterContext(run_id=run.id, lookback_days=radar.lookback_days, max_terms=d.get("max_terms_per_platform", 8),
                         max_pages=d.get("max_pages_per_term", 2), comments_top_n=d.get("comments_top_n", 5),
                         communities=settings.get("communities", {}) or {},
                         listings=settings.get("listings", {}) or {}, listing_transport=listing_transport,
                         listing_sleep=listing_sleep, databox=databox,
                         seen=(lambda ids: store.seen_for_radar(radar.id, ids)) if radar.intent_brief_id else None)
    if "indeed" in radar.platforms:
        import os
        daily = float(os.environ.get("RADAR_LISTINGS_DAILY_USD") or (settings.get("listings.indeed.daily_usd") or 2))
        ctx.listings = {**ctx.listings, "indeed": {**(ctx.listings.get("indeed") or {}),
                                                   "budget_left_usd": max(0.0, daily - store.source_spend_today("indeed"))}}
        if ctx.databox is None:
            from .databox import DataBox
            ctx.databox = own_box = DataBox.from_settings(settings)
    api = [p for p in radar.platforms if p in API_ADAPTERS]
    boards = [p for p in radar.platforms if p in LISTING_ADAPTERS]
    browser_only = [p for p in radar.platforms if p not in API_ADAPTERS and p not in LISTING_ADAPTERS]
    for p in browser_only:
        run.per_source[p] = {"requests": 0, "credits": 0, "posts": 0, "errors": 1,
                             "error_messages": ["browser lane not built yet (M5)"]}
        run.errors.append(f"{p}: browser lane not built yet (M5)")

    own_client = sc is None
    if own_client:
        sc = ScrapeCreators(settings.require("SCRAPECREATORS_API_KEY"),
                            base_url=settings.get("scrapecreators.base_url", "https://api.scrapecreators.com"),
                            concurrency=d.get("concurrency", 10),
                            timeout=settings.get("scrapecreators.timeout_seconds", 60),
                            retries_on_5xx=settings.get("scrapecreators.retries_on_5xx", 2))
    posts = []
    try:
        balance_before = await _balance(sc, run)
        adapters = [API_ADAPTERS[p](sc, ctx) for p in api] + [LISTING_ADAPTERS[p](sc, ctx) for p in boards]
        results = await asyncio.gather(*(a.collect(brief) for a in adapters), return_exceptions=True)
        for adapter, res in zip(adapters, results):
            if isinstance(res, OutOfCredits):
                run.errors.append(f"{adapter.platform}: {res} — run stopped")
                res = None
            elif isinstance(res, BaseException):
                run.errors.append(f"{adapter.platform}: {type(res).__name__}: {res}")
                res = None
            if res is None:
                run.per_source[adapter.platform] = {"requests": sc.meter.requests[adapter.platform],
                                                    "credits": sc.meter.credits[adapter.platform], "posts": 0,
                                                    "errors": 1, "error_messages": run.errors[-1:]}
                continue
            run.per_source[adapter.platform] = res.stats.model_dump()
            posts += res.posts
            for msg in res.stats.error_messages:
                run.errors.append(f"{adapter.platform}: {msg}")
        balance_after = await _balance(sc, run)
    finally:
        if own_client:
            await sc.aclose()
        if own_box:
            await own_box.aclose()

    run.credits_used = sc.meter.total_credits()
    run.counts["listings_usd"] = round(sum(sc.meter.usd.values()), 6)
    kept, dropped = dedupe(posts, None if dry_run else store.author_simhashes)
    seen = store.seen_for_radar(radar.id, [p.id for p in kept]) if radar.intent_brief_id else set()
    fresh = [p for p in kept if p.id not in seen]
    fresh_listings = [p for p in fresh if p.kind == "job"]
    fresh = [p for p in fresh if p.kind != "job"]
    run.counts = {**run.counts, "fetched": len(posts), "duplicates_dropped": len(dropped), "unique": len(kept),
                  "already_seen": len(kept) - len(fresh) - len(fresh_listings),
                  "to_classify": len(fresh), "listings_fetched": sum(1 for p in posts if p.kind == "job"),
                  "listings_new": len(fresh_listings)}
    if balance_before is not None and balance_after is not None:
        run.counts["credit_balance_before"] = balance_before
        run.counts["credit_balance_after"] = balance_after

    queued: list[str] = []
    if not dry_run:
        new_to_store = 0
        with store.tx():
            for p in kept:
                new_to_store += store.upsert_post(p)
        run.counts["new_to_store"] = new_to_store
        run.counts["queued"] = store.enqueue([p.id for p in fresh], radar.id, run.id)
        run.counts["listings_queued"] = store.enqueue_listings([p.id for p in fresh_listings], radar.id, run.id)
        queued = [p.id for p in fresh]
        if classify and boards:
            from .classify.listings import classify_listings
            await classify_listings(settings, store, radar, brief, run, transport=s2_transport)
        if classify:
            from .classify.cascade import classify_pending
            await classify_pending(settings, store, radar, brief, run, jev=jev, use_jev=use_jev,
                                   s2_transport=s2_transport)
            run.credits_used += int(run.counts.get("google_verify_credits") or 0)
    run.cost_usd = run_cost(settings, run)
    run.finished_at = utcnow()
    if not dry_run:
        store.save_run(run)
    return HuntResult(run=run, radar=radar, brief=brief, dry_run=dry_run, queued=queued)


def run_cost(settings: Settings, run: Run) -> float:
    """Metered cost, report-only: ScrapeCreators credits + reasoning tokens + Jev calls (when priced)."""
    credits = run.credits_used * float(settings.get("scrapecreators.credit_usd", 0) or 0)
    s2 = float(run.counts.get("stage2_cost_usd") or 0)
    jev = float(run.counts.get("jev_cost_usd") or 0)
    listings = float(run.counts.get("listing_cost_usd") or 0) + float(run.counts.get("listings_usd") or 0)
    return round(credits + s2 + jev + listings, 6)


async def _balance(sc: ScrapeCreators, run: Run) -> int | None:
    if sc.exhausted:
        return 0  # known: out of credits; the stop is already recorded in run.errors
    try:
        return await sc.credit_balance()
    except OutOfCredits:
        raise
    except ScrapeCreatorsError as exc:
        run.errors.append(f"credit balance check failed: {exc}")
        return None
