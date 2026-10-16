"""Raw ScrapeCreators payloads -> Post. Pure functions; each takes one raw item and returns a Post or None.

Shapes were verified against live responses recorded on 2026-09-28 (tests/fixtures/sc/).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .dedupe import simhash
from .models import Post, post_id

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid",
                   "igsh", "igshid", "si", "ref", "ref_src", "_rdr", "rdt", "share_id", "is_from_webapp",
                   "sender_device", "_t", "_r", "mibextid", "rcm", "__cft__[0]", "__tn__"}
RAW_KEEP = 2000  # characters of raw payload kept per post (trimmed original)


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("m.").removeprefix("old.")
    if host in ("reddit.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"):
        host = {"reddit.com": "www.reddit.com"}.get(host, "x.com")
    path = re.sub(r"/+$", "", parts.path) or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if k not in TRACKING_PARAMS))
    return urlunsplit(("https", host, path, query, ""))


def _dt(value) -> datetime | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        v = float(value)
        return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=timezone.utc)
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)  # +0000 -> +00:00
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


_REL = re.compile(r"^(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.I)
_ABS = re.compile(r"^([A-Z][a-z]{2}) (\d{1,2}), (\d{4})")


def snippet_date(text: str, now: datetime | None = None) -> datetime | None:
    """Google snippets start with '6 days ago ...' or 'Apr 3, 2026 ...'."""
    now = now or datetime.now(timezone.utc)
    if m := _REL.match(text or ""):
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}[unit] * n
        return now - timedelta(days=days)
    if m := _ABS.match(text or ""):
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _clean(text) -> str:
    if text in (None, "None"):
        return ""
    return str(text).strip()


def _trim(raw: dict) -> dict:
    """Keep only scalar fields (no media blobs), capped in size."""
    out, size = {}, 0
    for k, v in raw.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            size += len(str(v))
            if size > RAW_KEEP:
                break
            out[k] = v
    return out


def make_post(*, platform: str, url: str, kind: str, text: str, run_id: str, source_term: str,
              author_handle=None, author_url=None, author_name=None, title=None, posted_at=None,
              engagement=None, community=None, parent_url=None, parent_text=None, raw=None) -> Post | None:
    text = _clean(text)
    if not url or not text:
        return None
    canon = canonical_url(url)
    return Post(
        id=post_id(platform, canon), platform=platform, url=canon, kind=kind, text=text,
        title=_clean(title) or None, author_handle=_clean(author_handle) or None,
        author_url=_clean(author_url) or None, author_name=_clean(author_name) or None,
        posted_at=posted_at, engagement={k: v for k, v in (engagement or {}).items() if v is not None},
        community=_clean(community) or None, parent_url=canonical_url(parent_url) if parent_url else None,
        parent_text=(_clean(parent_text)[:600] or None) if parent_text else None,
        source_term=source_term, run_id=run_id, simhash=simhash(text), raw=_trim(raw or {}),
    )


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# --- reddit -------------------------------------------------------------------

def _reddit_url(item: dict) -> str | None:
    permalink = item.get("permalink")
    if isinstance(permalink, str) and permalink.startswith("/"):
        return "https://www.reddit.com" + permalink
    return item.get("url")


def reddit_post(item: dict, *, run_id: str, term: str) -> Post | None:
    author = item.get("author")
    title, body = _clean(item.get("title")), _clean(item.get("selftext"))
    return make_post(
        platform="reddit", url=_reddit_url(item), kind="post", text=f"{title}\n\n{body}".strip(), title=title,
        author_handle=author, author_url=f"https://www.reddit.com/user/{author}" if author else None,
        posted_at=_dt(item.get("created_utc") or item.get("created_at_iso")),
        engagement={"score": _int(item.get("score")), "comments": _int(item.get("num_comments"))},
        community=item.get("subreddit") if isinstance(item.get("subreddit"), str) else None,
        run_id=run_id, source_term=term, raw=item)


def reddit_comment(item: dict, *, run_id: str, term: str, parent_url=None, parent_text=None) -> Post | None:
    author = item.get("author")
    post = item.get("post") if isinstance(item.get("post"), dict) else {}
    sub = item.get("subreddit")
    community = sub.get("name") if isinstance(sub, dict) else sub
    purl = parent_url or (("https://www.reddit.com" + post["permalink"]) if post.get("permalink") else post.get("url"))
    return make_post(
        platform="reddit", url=_reddit_url(item), kind="reply" if item.get("is_reply_to_comment") else "comment",
        text=item.get("body"), author_handle=author,
        author_url=f"https://www.reddit.com/user/{author}" if author else None,
        posted_at=_dt(item.get("created_at_iso") or item.get("created_utc") or item.get("created_at")),
        engagement={"score": _int(item.get("votes", item.get("score")))}, community=community,
        parent_url=purl, parent_text=parent_text or post.get("title"), run_id=run_id, source_term=term, raw=item)


def reddit_comment_tree(comments: list, *, run_id: str, term: str, parent_url: str, parent_text: str) -> list[Post]:
    out: list[Post] = []

    def walk(nodes, depth):
        for c in nodes or []:
            if not isinstance(c, dict):
                continue
            p = reddit_comment(c, run_id=run_id, term=term, parent_url=parent_url, parent_text=parent_text)
            if p:
                if depth:
                    p.kind = "reply"
                out.append(p)
            replies = c.get("replies")
            if isinstance(replies, dict):
                replies = replies.get("items") or replies.get("comments") or []
            walk(replies, depth + 1)

    walk(comments, 0)
    return out


# --- youtube ------------------------------------------------------------------

def youtube_video(item: dict, *, run_id: str, term: str) -> Post | None:
    ch = item.get("channel") or {}
    handle = ch.get("handle")
    return make_post(
        platform="youtube", url=item.get("url"), kind="video",
        text=f"{_clean(item.get('title'))}\n\n{_clean(item.get('description'))}".strip(), title=item.get("title"),
        author_handle=handle or ch.get("id"), author_name=ch.get("title"),
        author_url=f"https://www.youtube.com/{handle}" if handle else None,
        posted_at=_dt(item.get("publishedTime") or item.get("publishDate")),
        engagement={"views": _int(item.get("viewCountInt")), "likes": _int(item.get("likeCountInt")),
                    "comments": _int(item.get("commentCountInt"))},
        run_id=run_id, source_term=term, raw=item)


def youtube_comment(item: dict, *, video: Post, run_id: str, term: str) -> Post | None:
    a = item.get("author") or {}
    vurl = video.url
    return make_post(
        platform="youtube", url=f"{vurl}&lc={item.get('id')}" if "?" in vurl else f"{vurl}?lc={item.get('id')}",
        kind="reply" if _int(item.get("replyLevel")) else "comment", text=item.get("content"),
        author_handle=a.get("channelId") or a.get("name"), author_name=a.get("name"), author_url=a.get("channelUrl"),
        posted_at=_dt(item.get("publishedTime")),
        engagement={"likes": _int((item.get("engagement") or {}).get("likes")),
                    "replies": _int((item.get("engagement") or {}).get("replies"))},
        parent_url=vurl, parent_text=video.title or video.text, run_id=run_id, source_term=term,
        raw={**item, "is_creator": bool(a.get("isCreator"))})


# --- tiktok -------------------------------------------------------------------

def tiktok_video(item: dict, *, run_id: str, term: str) -> Post | None:
    aw = item.get("aweme_info") if isinstance(item.get("aweme_info"), dict) else item
    author = aw.get("author") or {}
    uid, vid = author.get("unique_id"), aw.get("aweme_id")
    if not vid:
        return None
    stats = aw.get("statistics") or {}
    return make_post(
        platform="tiktok", url=f"https://www.tiktok.com/@{uid or '_'}/video/{vid}", kind="video", text=aw.get("desc"),
        author_handle=uid, author_name=author.get("nickname"),
        author_url=f"https://www.tiktok.com/@{uid}" if uid else None, posted_at=_dt(aw.get("create_time")),
        engagement={"views": _int(stats.get("play_count")), "likes": _int(stats.get("digg_count")),
                    "comments": _int(stats.get("comment_count"))},
        run_id=run_id, source_term=term, raw=aw)


def tiktok_comment(item: dict, *, video: Post, run_id: str, term: str) -> Post | None:
    user = item.get("user") or {}
    uid = user.get("unique_id")
    cid = item.get("cid") or item.get("id") or f"{uid}-{item.get('create_time')}"
    return make_post(
        platform="tiktok", url=f"{video.url}?comment={cid}", kind="comment", text=item.get("text"),
        author_handle=uid, author_name=user.get("nickname"), author_url=f"https://www.tiktok.com/@{uid}" if uid else None,
        posted_at=_dt(item.get("create_time")), engagement={"likes": _int(item.get("digg_count"))},
        parent_url=video.url, parent_text=video.text, run_id=run_id, source_term=term,
        raw={**item, "is_creator": bool(uid and uid == video.author_handle)})


# --- instagram ----------------------------------------------------------------

def instagram_media(item: dict, *, run_id: str, term: str) -> Post | None:
    owner = item.get("owner") or {}
    user = owner.get("username")
    return make_post(
        platform="instagram", url=item.get("url"), kind="video" if item.get("is_video") else "post",
        text=item.get("caption"), author_handle=user, author_name=owner.get("full_name"),
        author_url=f"https://www.instagram.com/{user}/" if user else None, posted_at=_dt(item.get("taken_at")),
        engagement={"likes": _int(item.get("like_count")), "comments": _int(item.get("comment_count")),
                    "views": _int(item.get("video_view_count"))},
        run_id=run_id, source_term=term, raw=item)


def instagram_comment(item: dict, *, media: Post, run_id: str, term: str) -> Post | None:
    user = (item.get("user") or item.get("owner") or {}).get("username")
    return make_post(
        platform="instagram", url=f"{media.url}?comment_id={item.get('id')}", kind="comment", text=item.get("text"),
        author_handle=user, author_url=f"https://www.instagram.com/{user}/" if user else None,
        posted_at=_dt(item.get("created_at")), engagement={"likes": _int(item.get("comment_like_count", item.get("like_count")))},
        parent_url=media.url, parent_text=media.text, run_id=run_id, source_term=term,
        raw={**item, "is_creator": bool(user and user == media.author_handle)})


# --- threads ------------------------------------------------------------------

def threads_post(item: dict, *, run_id: str, term: str) -> Post | None:
    user = item.get("user") or {}
    cap = item.get("caption")
    text = cap.get("text") if isinstance(cap, dict) else cap
    name = user.get("username")
    return make_post(
        platform="threads", url=item.get("url") or (f"https://www.threads.com/@{name}/post/{item.get('code')}" if name else None),
        kind="post", text=text, author_handle=name, author_name=user.get("full_name"),
        author_url=f"https://www.threads.com/@{name}" if name else None, posted_at=_dt(item.get("taken_at")),
        engagement={"likes": _int(item.get("like_count"))}, run_id=run_id, source_term=term, raw=item)


# --- linkedin -----------------------------------------------------------------

def _li_handle(url: str | None) -> str | None:
    if url and (m := re.search(r"linkedin\.com/(?:in|company)/([^/?#]+)", url)):
        return m.group(1)
    return None


def linkedin_post(item: dict, *, run_id: str, term: str) -> Post | None:
    a = item.get("author") or {}
    return make_post(
        platform="linkedin", url=item.get("url"), kind="post", text=item.get("description"),
        author_handle=_li_handle(a.get("url")) or a.get("name"), author_name=a.get("name"), author_url=a.get("url"),
        posted_at=_dt(item.get("datePublished")),
        engagement={"likes": _int(item.get("likeCount")), "comments": _int(item.get("commentCount"))},
        run_id=run_id, source_term=term, raw=item)


def linkedin_comment(item: dict, *, post: Post, run_id: str, term: str, index: int) -> Post | None:
    a = item.get("author")
    name = a.get("name") if isinstance(a, dict) else a
    purl = item.get("linkedinUrl") or (a.get("url") if isinstance(a, dict) else None)
    return make_post(
        platform="linkedin", url=f"{post.url}?comment={index}-{_li_handle(purl) or name}", kind="comment",
        text=item.get("text"), author_handle=_li_handle(purl) or name, author_name=name, author_url=purl,
        parent_url=post.url, parent_text=post.text, run_id=run_id, source_term=term, raw=item)


# --- facebook -----------------------------------------------------------------

def facebook_post(item: dict, *, run_id: str, term: str, group_url: str) -> Post | None:
    a = item.get("author") or {}
    return make_post(
        platform="facebook", url=item.get("url"), kind="post", text=item.get("text"),
        author_handle=a.get("id") or a.get("name"), author_name=a.get("name"),
        author_url=f"https://www.facebook.com/{a['id']}" if a.get("id") else None,
        posted_at=_dt(item.get("publishTime") or item.get("creation_time")),
        engagement={"reactions": _int(item.get("reactionCount")), "comments": _int(item.get("commentCount"))},
        community=group_url, run_id=run_id, source_term=term, raw=item)


def facebook_comment(item: dict, *, post: Post, run_id: str, term: str) -> Post | None:
    a = item.get("author") or {}
    return make_post(
        platform="facebook", url=item.get("url") or f"{post.url}?comment_id={item.get('id')}", kind="comment",
        text=item.get("text") or item.get("message"), author_handle=a.get("id") or a.get("name"),
        author_name=a.get("name"), author_url=a.get("url") or (f"https://www.facebook.com/{a['id']}" if a.get("id") else None),
        posted_at=_dt(item.get("created_time") or item.get("publishTime") or item.get("created_at")),
        engagement={"reactions": _int(item.get("reactionCount") or item.get("reaction_count"))},
        community=post.community, parent_url=post.url, parent_text=post.text, run_id=run_id, source_term=term, raw=item)


# --- google -------------------------------------------------------------------

SITE_PLATFORMS = [("reddit.com", "reddit"), ("x.com", "x"), ("twitter.com", "x"), ("skool.com", "skool"),
                  ("linkedin.com", "linkedin"), ("facebook.com", "facebook"), ("quora.com", "quora")]


def site_platform(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    for domain, platform in SITE_PLATFORMS:
        if host == domain or host.endswith("." + domain):
            return platform
    return "web"


def google_result(item: dict, *, run_id: str, term: str, now: datetime | None = None) -> Post | None:
    url = item.get("url")
    if not url:
        return None
    desc = _clean(item.get("description"))
    platform = site_platform(url)
    handle = None
    if platform == "x" and (m := re.search(r"x\.com/([^/?#]+)/status", canonical_url(url))):
        handle = m.group(1)
    return make_post(
        platform=platform, url=url, kind="post", text=f"{_clean(item.get('title'))}\n\n{desc}".strip(),
        title=item.get("title"), author_handle=handle, author_url=f"https://x.com/{handle}" if handle else None,
        posted_at=snippet_date(desc, now), community=urlsplit(url).netloc.lower(), run_id=run_id,
        source_term=term, raw={**item, "via": "google"})
