"""Spec §5 (google): "Fetch the full page only for High candidates."

A Google result is a title plus a snippet. The snippet can splice text from several posts, and it has no author.
Before a Google-found post may stay High, fetch the underlying post through ScrapeCreators, replace the snippet
with the real text and author, and re-judge it. If the full post can't be fetched, cap the post at Medium,
because snippet-only evidence is not enough for High.
"""
from __future__ import annotations

from ..models import Post
from ..normalize import _dt, _li_handle, simhash
from ..scrapecreators import OutOfCredits, ScrapeCreators, ScrapeCreatorsError

ENDPOINTS = {"reddit": "/v1/reddit/post", "facebook": "/v1/facebook/post", "linkedin": "/v1/linkedin/post",
             "threads": "/v1/threads/post", "x": "/v1/twitter/tweet"}


def _extract(platform: str, d: dict) -> tuple[str, str | None, str | None, object] | None:
    """-> (text, author_handle, author_name, posted_at_raw) from a full-post response."""
    if platform == "reddit":
        text = f"{d.get('title') or ''}\n\n{d.get('selftext') or ''}".strip()
        return text, d.get("author"), None, d.get("created_utc")
    if platform == "facebook":
        a = d.get("author") or {}
        return d.get("description") or "", a.get("id") or a.get("name"), a.get("name"), d.get("creation_time")
    if platform == "linkedin":
        a = d.get("author") or {}
        return d.get("description") or "", _li_handle(a.get("url")) or a.get("name"), a.get("name"), d.get("datePublished")
    if platform == "threads":
        p = d.get("post") or {}
        cap = p.get("caption")
        user = p.get("user") or {}
        return (cap.get("text") if isinstance(cap, dict) else cap) or "", user.get("username"), user.get("full_name"), p.get("taken_at")
    if platform == "x":
        legacy = d.get("legacy") or {}
        text = legacy.get("full_text") or d.get("full_text") or d.get("text") or ""
        core = (((d.get("core") or {}).get("user_results") or {}).get("result") or {})
        handle = ((core.get("legacy") or {}).get("screen_name") or (core.get("core") or {}).get("screen_name"))
        return text, handle, None, legacy.get("created_at")
    return None


async def full_post(sc: ScrapeCreators, post: Post) -> Post | None:
    """Fetch the underlying post for a Google result. Returns an updated Post, or None if unavailable."""
    ep = ENDPOINTS.get(post.platform)
    if not ep:
        return None
    try:
        data = await sc.get(ep, {"url": post.url}, source="google")
    except OutOfCredits:
        raise
    except ScrapeCreatorsError:
        return None
    got = _extract(post.platform, data or {})
    if not got or len(got[0].strip()) < 20:
        return None
    text, handle, name, when = got
    return post.model_copy(update={
        "text": text.strip(), "author_handle": handle or post.author_handle, "author_name": name or post.author_name,
        "posted_at": _dt(when) or post.posted_at, "simhash": simhash(text),
        "raw": {**post.raw, "verified_full_post": True}})
