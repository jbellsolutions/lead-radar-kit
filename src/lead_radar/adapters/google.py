from __future__ import annotations

from .. import normalize as n
from .base import SourceAdapter

DEFAULT_SITES = ("reddit.com", "quora.com", "skool.com", "linkedin.com/posts", "facebook.com/groups", "x.com")


def site_query(term: str, extra_sites: list[str] | None = None) -> str:
    """Terms already carrying a site: operator are used as-is; others are scoped to the default sites."""
    if "site:" in term:
        return term
    sites = list(DEFAULT_SITES) + [s for s in (extra_sites or []) if s not in DEFAULT_SITES]
    return f"{term} ({' OR '.join('site:' + s for s in sites)})"


class GoogleAdapter(SourceAdapter):
    """/v1/google/search with site: queries. Posts keep the platform of the site they point to (so a reddit
    URL found via Google dedupes against the reddit adapter), with raw.via = 'google'. Covers X (site:x.com)."""
    platform = "google"

    async def _collect(self, brief):
        forums = brief.communities.get("forums", [])

        async def search(term):
            q = site_query(term, forums)
            for page in range(1, self.ctx.max_pages + 1):
                data = await self.call("/v1/google/search", {"query": q, "date_posted": self.window("google"), "page": page})
                items = (data or {}).get("results") or []
                for item in items:
                    self.add(n.google_result(item, run_id=self.ctx.run_id, term=term))
                if not items or not data.get("next_page"):
                    return

        await self.run_all((search(t) for t in self.terms(brief)))
