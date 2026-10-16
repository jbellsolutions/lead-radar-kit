from __future__ import annotations

from .. import normalize as n
from .base import SourceAdapter

TRUNCATION_MARKS = ("…", "...", "see more")


def looks_truncated(text: str) -> bool:
    t = text.rstrip().lower()
    return any(t.endswith(m) for m in TRUNCATION_MARKS)


class LinkedInAdapter(SourceAdapter):
    """/v1/linkedin/search/posts (Google-indexed, pages 1-11). /v1/linkedin/post fetches the full body only when
    the search snippet is truncated or the post has comments (top N). The browser lane is the M5 fallback."""
    platform = "linkedin"

    async def _collect(self, brief):
        found = []

        async def search(term):
            cursor = None
            for _ in range(self.ctx.max_pages):
                data = await self.call("/v1/linkedin/search/posts",
                                       {"query": term, "date_posted": self.window("google"), "cursor": cursor})
                items = (data or {}).get("posts") or []
                for item in items:
                    p = self.add(n.linkedin_post(item, run_id=self.ctx.run_id, term=term))
                    if p:
                        found.append(p)
                nxt = (data or {}).get("cursor")
                if not items or not nxt or nxt == cursor:
                    return
                cursor = nxt

        await self.run_all((search(t) for t in self.terms(brief)))

        follow = {p.id: p for p in self.top_for_comments(found)}
        for p in found:
            if looks_truncated(p.text) and len(follow) < self.ctx.comments_top_n * 2:
                follow.setdefault(p.id, p)

        async def detail(post):
            data = await self.call("/v1/linkedin/post", {"url": post.url})
            if not data:
                return
            body = n._clean(data.get("description"))
            if body and len(body) > len(post.text):
                post.text = body
                post.simhash = n.simhash(body)
            for i, c in enumerate(data.get("comments") or []):
                if isinstance(c, dict):
                    self.add(n.linkedin_comment(c, post=post, run_id=self.ctx.run_id, term=post.source_term, index=i))

        await self.run_all((detail(p) for p in follow.values()))
