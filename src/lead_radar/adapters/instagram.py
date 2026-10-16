from __future__ import annotations

from .. import normalize as n
from .base import SourceAdapter


class InstagramAdapter(SourceAdapter):
    """'#tag' terms -> /v1/instagram/search/hashtag; phrases -> /v2/instagram/reels/search.
    Then /v2/instagram/post/comments on the top N. include_replies is never set (15 credits/call)."""
    platform = "instagram"

    async def _collect(self, brief):
        media = []

        async def reels(term):
            for page in range(1, self.ctx.max_pages + 1):
                data = await self.call("/v2/instagram/reels/search",
                                       {"query": term, "date_posted": self.window("ig_reels"), "page": page})
                items = (data or {}).get("reels") or []
                for item in items:
                    m = self.add(n.instagram_media(item, run_id=self.ctx.run_id, term=term))
                    if m:
                        media.append(m)
                if not items or not data.get("next_page"):
                    return

        async def hashtag(term):
            cursor = None
            for _ in range(self.ctx.max_pages):
                data = await self.call("/v1/instagram/search/hashtag",
                                       {"hashtag": term.lstrip("#"), "date_posted": self.window("google"), "cursor": cursor})
                items = (data or {}).get("posts") or []
                for item in items:
                    m = self.add(n.instagram_media(item, run_id=self.ctx.run_id, term=term))
                    if m:
                        media.append(m)
                nxt = (data or {}).get("cursor")
                if not items or not nxt or nxt == cursor:
                    return
                cursor = nxt

        terms = self.terms(brief)
        await self.run_all((hashtag(t) if t.startswith("#") else reels(t) for t in terms))

        async def comments(m):
            data = await self.call("/v2/instagram/post/comments", {"url": m.url})
            for item in (data or {}).get("comments") or []:
                self.add(n.instagram_comment(item, media=m, run_id=self.ctx.run_id, term=m.source_term))

        await self.run_all((comments(m) for m in self.top_for_comments(media)))
