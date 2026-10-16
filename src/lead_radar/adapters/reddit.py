from __future__ import annotations

from .. import normalize as n
from .base import SourceAdapter


class RedditAdapter(SourceAdapter):
    """/v1/reddit/search (posts + comments), /v1/reddit/subreddit/search per community, comments on top N posts."""
    platform = "reddit"

    async def _collect(self, brief):
        tf = self.window("reddit")
        found = []

        async def search(term, kind):
            after = None
            for _ in range(self.ctx.max_pages):
                data = await self.call("/v1/reddit/search", {"query": term, "filter": kind, "sort": "new",
                                                             "timeframe": tf, "after": after, "trim": "true"})
                if not data:
                    return
                for item in data.get(kind) or []:
                    fn = n.reddit_post if kind == "posts" else n.reddit_comment
                    p = self.add(fn(item, run_id=self.ctx.run_id, term=term))
                    if p and kind == "posts":
                        found.append(p)
                nxt = data.get("after")
                if not nxt or nxt == after:
                    return
                after = nxt

        async def subreddit(sub, term):
            data = await self.call("/v1/reddit/subreddit/search",
                                   {"subreddit": sub.removeprefix("r/"), "query": term, "sort": "new", "timeframe": tf})
            for item in (data or {}).get("posts") or []:
                p = self.add(n.reddit_post(item, run_id=self.ctx.run_id, term=term))
                if p:
                    found.append(p)

        terms = self.terms(brief)
        jobs = [search(t, "posts") for t in terms] + [search(t, "comments") for t in terms]
        jobs += [subreddit(s, t) for s in self.communities(brief) for t in terms[:2]]
        await self.run_all(jobs)

        async def comments(post):
            data = await self.call("/v1/reddit/post/comments", {"url": post.url, "trim": "true"})
            for p in n.reddit_comment_tree((data or {}).get("comments") or [], run_id=self.ctx.run_id,
                                           term=post.source_term, parent_url=post.url, parent_text=post.text):
                self.add(p)

        await self.run_all((comments(p) for p in self.top_for_comments(found)))
