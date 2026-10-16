"""Base for job-board adapters that read a site directly (polite GET-only HTTP, robots.txt honoured)."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import httpx

from ..listings.http import Disallowed, PoliteClient
from ..models import IntentBrief
from .base import SourceAdapter


class ListingAdapter(SourceAdapter):
    min_interval = 1.0  # seconds between requests to the host (robots Crawl-delay wins when larger)

    def __init__(self, sc, ctx):
        super().__init__(sc, ctx)
        self.cfg = (ctx.listings or {}).get(self.platform, {}) or {}
        self.http = PoliteClient(sc.meter, self.platform, min_interval=float(self.cfg.get("min_interval", self.min_interval)),
                                 transport=ctx.listing_transport, sleep=ctx.listing_sleep or asyncio.sleep)

    async def collect(self, brief: IntentBrief):
        try:
            return await super().collect(brief)
        finally:
            await self.http.aclose()

    async def fetch(self, url: str) -> str | None:
        try:
            resp = await self.http.get(url)
        except Disallowed:
            self._errors.append(f"robots.txt disallows {url}")
            return None
        except httpx.HTTPError as exc:
            self._errors.append(f"GET {url}: {type(exc).__name__}: {str(exc)[:120]}")
            return None
        if resp is None:
            self._skipped += 1
            return None
        return resp.text

    def keywords(self, brief: IntentBrief) -> list[str]:
        """This board's search terms; boards without their own fall back to the OnlineJobs titles."""
        terms = self.terms(brief) or [t for t in brief.terms.get("onlinejobs", []) if t.strip()][: self.ctx.max_terms]
        return terms

    def require_keywords(self, brief: IntentBrief) -> list[str]:
        """Keywords, or [] plus a note: boards never fall back to "everything in the category"."""
        kws = self.keywords(brief)
        if not kws:
            self._notes.append(f"not searched: the brief has no {self.platform} or onlinejobs terms "
                               "(job titles); refresh the campaign plan with job boards on")
        return kws

    def tally(self, fetched: int, too_old: int = 0, off_topic: int = 0) -> None:
        """One note per run so a real zero and a broken parser look different."""
        self._notes.append(f"fetched {fetched}, dropped {too_old} outside the window, {off_topic} off-topic, "
                           f"kept {len(self.posts)}")

    def cutoff(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=self.ctx.lookback_days)

    @staticmethod
    def matches(text: str, keywords: list[str]) -> bool:
        """Local keyword filter: every word of any one keyword appears in the text (case-insensitive)."""
        low = text.lower()
        for kw in keywords:
            words = [w for w in re.findall(r"[a-z0-9+#]+", kw.lower()) if len(w) > 1]
            if words and all(w in low for w in words):
                return True
        return not keywords
