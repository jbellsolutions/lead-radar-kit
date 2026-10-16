from __future__ import annotations

from .. import normalize as n
from .base import SourceAdapter


class FacebookAdapter(SourceAdapter):
    """/v1/facebook/group/posts per group URL in communities.facebook (3 posts/page), then
    /v1/facebook/post/comments on the top N. In-group keyword search is the browser lane's job (M5)."""
    platform = "facebook"

    async def _collect(self, brief):
        groups = self.communities(brief)
        if not groups:
            self._notes.append("not searched: no facebook groups configured (config communities.facebook or the "
                               "brief's communities.facebook); Facebook groups are still reached via Google site: terms")
            return
        found = []
        # 3 posts per page, so page deeper than the keyword sources: up to max_pages * 3 pages per group.
        pages = self.ctx.max_pages * 3

        async def group(url):
            cursor = None
            for _ in range(pages):
                data = await self.call("/v1/facebook/group/posts", {"url": url, "sort_by": "CHRONOLOGICAL", "cursor": cursor})
                items = (data or {}).get("posts") or []
                for item in items:
                    p = self.add(n.facebook_post(item, run_id=self.ctx.run_id, term=f"group:{url}", group_url=url))
                    if p:
                        found.append(p)
                nxt = (data or {}).get("cursor")
                if not items or not nxt or nxt == cursor:
                    return
                cursor = nxt

        await self.run_all((group(g) for g in groups))

        async def comments(post):
            fid = post.raw.get("feedback_id")
            data = await self.call("/v1/facebook/post/comments", {"feedback_id": fid} if fid else {"url": post.url})
            for item in (data or {}).get("comments") or []:
                self.add(n.facebook_comment(item, post=post, run_id=self.ctx.run_id, term=post.source_term))

        await self.run_all((comments(p) for p in self.top_for_comments(found)))
