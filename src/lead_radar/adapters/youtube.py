from __future__ import annotations

from .. import normalize as n
from .base import SourceAdapter


class YouTubeAdapter(SourceAdapter):
    """/v1/youtube/search, then /v1/youtube/video/comments on the top N videos. Buyers live in comments."""
    platform = "youtube"

    async def _collect(self, brief):
        videos = []

        async def search(term):
            token = None
            for _ in range(self.ctx.max_pages):
                data = await self.call("/v1/youtube/search", {"query": term, "uploadDate": self.window("youtube"),
                                                              "includeExtras": "true", "continuationToken": token})
                if not data:
                    return
                for item in (data.get("videos") or []) + (data.get("shorts") or []):
                    v = self.add(n.youtube_video(item, run_id=self.ctx.run_id, term=term))
                    if v:
                        videos.append(v)
                token = data.get("continuationToken")
                if not token:
                    return

        await self.run_all((search(t) for t in self.terms(brief)))

        async def comments(video):
            data = await self.call("/v1/youtube/video/comments", {"url": video.url, "order": "newest"})
            for item in (data or {}).get("comments") or []:
                self.add(n.youtube_comment(item, video=video, run_id=self.ctx.run_id, term=video.source_term))

        await self.run_all((comments(v) for v in self.top_for_comments(videos)))
