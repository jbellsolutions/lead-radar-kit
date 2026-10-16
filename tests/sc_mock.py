"""httpx MockTransport serving recorded ScrapeCreators fixtures (tests/fixtures/sc). No network."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

SC = Path(__file__).parent / "fixtures" / "sc"
PAGINATION = ("after", "cursor", "continuationToken")

ROUTES = {
    "/v1/reddit/subreddit/search": "reddit_subreddit_search",
    "/v1/reddit/post/comments": "reddit_post_comments",
    "/v1/youtube/search": "youtube_search",
    "/v1/youtube/video/comments": "youtube_video_comments",
    "/v1/tiktok/search/keyword": "tiktok_search_keyword",
    "/v1/tiktok/video/comments": "tiktok_video_comments",
    "/v2/instagram/reels/search": "instagram_reels_search",
    "/v1/instagram/search/hashtag": "instagram_hashtag_search",
    "/v2/instagram/post/comments": "instagram_post_comments",
    "/v1/threads/search": "threads_search",
    "/v1/linkedin/search/posts": "linkedin_search_posts",
    "/v1/linkedin/post": "linkedin_post",
    "/v1/facebook/group/posts": "facebook_group_posts",
    "/v1/facebook/post/comments": "facebook_post_comments",
    "/v1/google/search": "google_search",
}
EMPTY = {"success": True, "credits_charged": 1, "posts": [], "comments": [], "videos": [], "search_item_list": [],
         "reels": [], "results": []}


def load(name: str) -> dict:
    return json.loads((SC / f"{name}.json").read_text())


class FixtureTransport(httpx.AsyncBaseTransport):
    def __init__(self, overrides: dict[str, httpx.Response] | None = None, balance: int = 10_000):
        self.calls: list[httpx.Request] = []
        self.overrides = overrides or {}
        self.balance = balance

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path, q = request.url.path, dict(request.url.params)
        if path in self.overrides:
            return self.overrides[path]
        if path == "/v1/account/credit-balance":
            return httpx.Response(200, json={"success": True, "creditCount": self.balance})
        if any(q.get(k) for k in PAGINATION) or int(q.get("page", "1")) > 1:
            return httpx.Response(200, json=EMPTY)
        if path == "/v1/reddit/search":
            name = "reddit_search_comments" if q.get("filter") == "comments" else "reddit_search"
        else:
            name = ROUTES.get(path)
        if not name:
            return httpx.Response(404, json={"success": False})
        return httpx.Response(200, json=load(name))
