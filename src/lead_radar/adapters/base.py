"""SourceAdapter base: request counting, error policy, pagination helpers (spec §5).

Adapters are deterministic code: they search with the brief's terms and normalize results. No model calls.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..models import IntentBrief, Post, SourceStats
from ..scrapecreators import OutOfCredits, ScrapeCreators, SkipItem, SourceError

log = logging.getLogger("lead_radar.adapters")


@dataclass
class Collected:
    platform: str
    posts: list[Post]
    stats: SourceStats


@dataclass
class AdapterContext:
    run_id: str
    lookback_days: int = 14
    max_terms: int = 8
    max_pages: int = 2
    comments_top_n: int = 5
    communities: dict[str, list[str]] = field(default_factory=dict)  # human-approved, from radar.yaml
    listings: dict = field(default_factory=dict)  # config `listings:` block (per-board limits)
    listing_transport: object = None  # tests inject an httpx transport for the job-board adapters
    listing_sleep: object = None      # tests inject a fake sleep so crawl delays don't slow the suite
    databox: object = None            # DataBox client (Indeed via `job_board`); built from env when None
    seen: object = None               # callable(post_ids) -> set already handled for this radar (skip detail pages)


class SourceAdapter(ABC):
    platform: str = ""

    def __init__(self, sc: ScrapeCreators, ctx: AdapterContext):
        self.sc = sc
        self.ctx = ctx
        self.posts: list[Post] = []
        self._errors: list[str] = []
        self._notes: list[str] = []
        self._skipped = 0

    async def collect(self, brief: IntentBrief) -> Collected:
        try:
            await self._collect(brief)
        except OutOfCredits:
            raise
        except Exception as exc:  # an adapter bug must surface as a source failure, never silently
            self._errors.append(f"adapter crashed: {type(exc).__name__}: {str(exc)[:200]}")
            log.exception("%s adapter crashed", self.platform)
        m = self.sc.meter
        stats = SourceStats(requests=m.requests[self.platform], credits=m.credits[self.platform], posts=len(self.posts),
                            errors=len(self._errors), skipped=self._skipped, error_messages=self._errors[:20],
                            notes=self._notes[:20], cost_usd=round(m.usd[self.platform], 6))
        return Collected(self.platform, self.posts, stats)

    @abstractmethod
    async def _collect(self, brief: IntentBrief) -> None: ...

    async def call(self, path: str, params: dict) -> dict | None:
        try:
            return await self.sc.get(path, params, source=self.platform)
        except SkipItem as exc:
            self._skipped += 1
            log.info("%s skip: %s", self.platform, exc)
        except SourceError as exc:
            self._errors.append(str(exc))
            log.warning("%s error: %s", self.platform, exc)
        return None

    async def run_all(self, coros) -> None:
        """Await every job (no orphaned tasks), then re-raise OutOfCredits or the first other failure."""
        results = await asyncio.gather(*coros, return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        for r in errors:
            if isinstance(r, OutOfCredits):
                raise r
        if errors:
            raise errors[0]

    def add(self, post: Post | None) -> Post | None:
        if post is not None:
            self.posts.append(post)
        return post

    def terms(self, brief: IntentBrief) -> list[str]:
        return [t for t in brief.terms.get(self.platform, []) if t.strip()][: self.ctx.max_terms]

    def communities(self, brief: IntentBrief) -> list[str]:
        # Config-listed communities win; brief suggestions are used only when config lists none.
        approved = self.ctx.communities.get(self.platform) or []
        return list(approved) or list(brief.communities.get(self.platform, []))

    def top_for_comments(self, posts: list[Post]) -> list[Post]:
        ranked = [p for p in posts if (p.engagement.get("comments") or 0) > 0]
        ranked.sort(key=lambda p: p.engagement.get("comments") or 0, reverse=True)
        return ranked[: self.ctx.comments_top_n]

    # lookback -> each endpoint's date filter vocabulary -----------------------
    def window(self, vocab: str) -> str:
        d = self.ctx.lookback_days
        table = {
            "reddit": [(1, "day"), (7, "week"), (31, "month"), (365, "year")],
            "youtube": [(1, "today"), (7, "this_week"), (31, "this_month"), (365, "this_year")],
            "tiktok": [(1, "yesterday"), (7, "this-week"), (31, "this-month"), (92, "last-3-months"), (183, "last-6-months")],
            "google": [(1, "last-day"), (7, "last-week"), (31, "last-month"), (365, "last-year")],
            "ig_reels": [(7, "last-week"), (31, "last-month"), (365, "last-year")],
        }[vocab]
        for limit, value in table:
            if d <= limit:
                return value
        return table[-1][1]
