from __future__ import annotations

from .. import normalize as n
from .base import SourceAdapter


class TikTokAdapter(SourceAdapter):
    """/v1/tiktok/search/keyword, then /v1/tiktok/video/comments on the top N videos."""
    platform = "tiktok"

    async def _collect(self, brief):
        videos = []

        async def search(term):
            cursor = None
            for _ in range(self.ctx.max_pages):
                data = await self.call("/v1/tiktok/search/keyword", {"query": term.lstrip("#"), "date_posted": self.window("tiktok"),
                                                                     "sort_by": "date-posted", "cursor": cursor, "trim": "true"})
                if not data:
                    return
                for item in data.get("search_item_list") or []:
                    v = self.add(n.tiktok_video(item, run_id=self.ctx.run_id, term=term))
                    if v:
                        videos.append(v)
                nxt = data.get("cursor")
                if not nxt or nxt == cursor or not data.get("search_item_list"):
                    return
                cursor = nxt

        await self.run_all((search(t) for t in self.terms(brief)))

        async def comments(video):
            data = await self.call("/v1/tiktok/video/comments", {"url": video.url, "trim": "true"})
            for item in (data or {}).get("comments") or []:
                self.add(n.tiktok_comment(item, video=video, run_id=self.ctx.run_id, term=video.source_term))

        await self.run_all((comments(v) for v in self.top_for_comments(videos)))
