"""Record one live ScrapeCreators response per endpoint used by Lead Radar v2.

Raw responses are written to ~/.lead-radar/recordings/, outside the repo. Trimmed, pseudonymized
copies go to tests/fixtures/sc/<name>.json for offline normalizer tests; CI never calls live APIs.
Post text, ids and links in those copies are still real: run scripts/scrub_fixtures.py tests/fixtures/sc before
committing them.

Cost: roughly one credit per endpoint recorded, about 20 in total.
Usage: .venv/bin/python scripts/radar_record_fixtures.py [--query "virtual assistant"]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lead_radar import config  # noqa: E402
from lead_radar.scrapecreators import ScrapeCreators, ScrapeCreatorsError  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "sc"
PII_KEYS = {
    "author", "username", "user_name", "name", "full_name", "fullname", "nickname", "unique_id", "uniqueid",
    "handle", "screen_name", "display_name", "displayname", "author_name", "authorname", "owner_username",
    "email", "phone", "author_fullname", "channel_name", "channelname", "channel_handle", "first_name",
    "last_name", "headline",
}
PII_URL_KEYS = {"profile_url", "author_url", "authorurl", "profileurl", "linkedinurl", "avatar", "avatar_url",
                "profile_pic_url", "profile_picture", "thumbnail_url", "avatar_thumb", "profile_image_url"}
PERSON_CONTAINERS = {"author", "user", "owner", "channel", "from", "profile", "creator", "poster"}
MAX_ITEMS = 5


_SALT = secrets.token_hex(16)  # random per run, never saved: a placeholder can't be checked against a guessed name


def _pseudo(value: str) -> str:
    return "user_" + hashlib.sha1((_SALT + value).encode()).hexdigest()[:8]


def redact(doc):
    originals: dict[str, str] = {}

    def walk(node, person=False):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                lk = k.lower()
                if lk in PERSON_CONTAINERS and isinstance(v, dict):
                    out[k] = walk(v, person=True)
                elif person and isinstance(v, str) and v and lk not in {"id", "pk", "uid", "__typename", "type"}:
                    if v.startswith("http"):
                        out[k] = "https://example.invalid/" + _pseudo(v)
                    else:
                        originals[v] = _pseudo(v)
                        out[k] = originals[v]
                elif isinstance(v, str) and v and lk in PII_KEYS:
                    originals[v] = _pseudo(v)
                    out[k] = originals[v]
                elif isinstance(v, str) and lk in PII_URL_KEYS:
                    out[k] = "https://example.invalid/" + _pseudo(v) if v else v
                else:
                    out[k] = walk(v, person)
            return out
        if isinstance(node, list):
            return [walk(x, person) for x in node[:MAX_ITEMS]]
        return node

    first = walk(doc)
    # Second pass: scrub any leaked originals (e.g. @handle inside a URL), longest first.
    pairs = sorted(((o, p) for o, p in originals.items() if len(o) >= 3), key=lambda x: -len(x[0]))

    def scrub(node):
        if isinstance(node, dict):
            return {k: scrub(v) for k, v in node.items()}
        if isinstance(node, list):
            return [scrub(x) for x in node]
        if isinstance(node, str):
            for o, p in pairs:
                if o in node:
                    node = node.replace(o, p)
        return node

    return scrub(first)


def first_url(items, pattern: str):
    rx = re.compile(pattern)
    for it in items or []:
        for key in ("url", "link", "permalink", "postUrl", "post_url"):
            v = it.get(key) if isinstance(it, dict) else None
            if isinstance(v, str) and rx.search(v):
                return v if v.startswith("http") else "https://www.reddit.com" + v
    return None


def rebuild_from_recordings() -> None:
    """Re-derive committed fixtures from raw recordings (no API calls)."""
    rec_dir = config.load().state_dir / "recordings"
    for raw in sorted(rec_dir.glob("*.json")):
        (FIXTURE_DIR / raw.name).write_text(json.dumps(redact(json.loads(raw.read_text())), indent=1, ensure_ascii=False))
        print("rebuilt", raw.name)


async def main(query: str) -> int:
    s = config.load()
    rec_dir = s.state_dir / "recordings"
    rec_dir.mkdir(parents=True, exist_ok=True)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, str] = {}

    async with ScrapeCreators(s.require("SCRAPECREATORS_API_KEY")) as sc:
        before = await sc.credit_balance()

        async def rec(name, path, params):
            try:
                data = await sc.get(path, params, source=name)
            except ScrapeCreatorsError as exc:
                results[name] = f"ERROR {exc}"
                return {}
            (rec_dir / f"{name}.json").write_text(json.dumps(data, indent=1))
            (FIXTURE_DIR / f"{name}.json").write_text(json.dumps(redact(data), indent=1, ensure_ascii=False))
            results[name] = f"ok credits={data.get('credits_charged')}"
            return data

        reddit = await rec("reddit_search", "/v1/reddit/search", {"query": query, "sort": "new", "timeframe": "week"})
        await rec("reddit_search_comments", "/v1/reddit/search", {"query": query, "filter": "comments", "sort": "new", "timeframe": "week"})
        await rec("reddit_subreddit_search", "/v1/reddit/subreddit/search", {"subreddit": "smallbusiness", "query": query, "sort": "new", "timeframe": "month"})
        url = first_url(reddit.get("posts"), r"/comments/")
        if url:
            await rec("reddit_post_comments", "/v1/reddit/post/comments", {"url": url, "trim": "true"})

        yt = await rec("youtube_search", "/v1/youtube/search", {"query": query, "uploadDate": "this_week", "includeExtras": "true"})
        vid = next((v.get("url") for v in yt.get("videos", []) if v.get("url")), None)
        if vid:
            await rec("youtube_video_comments", "/v1/youtube/video/comments", {"url": vid, "order": "newest"})

        tt = await rec("tiktok_search_keyword", "/v1/tiktok/search/keyword", {"query": query, "date_posted": "this-week", "trim": "true"})
        item = next(iter(tt.get("search_item_list") or []), None)
        if item:
            aw = item.get("aweme_info", item)
            share = aw.get("share_url") or aw.get("url")
            if not share and aw.get("aweme_id"):
                share = f"https://www.tiktok.com/@{(aw.get('author') or {}).get('unique_id', 'x')}/video/{aw['aweme_id']}"
            if share:
                await rec("tiktok_video_comments", "/v1/tiktok/video/comments", {"url": share, "trim": "true"})

        ig = await rec("instagram_reels_search", "/v2/instagram/reels/search", {"query": query, "date_posted": "last-week"})
        await rec("instagram_hashtag_search", "/v1/instagram/search/hashtag", {"hashtag": "virtualassistant"})
        reel = next((r.get("url") for r in ig.get("reels", []) if r.get("url")), None)
        if reel:
            await rec("instagram_post_comments", "/v2/instagram/post/comments", {"url": reel})

        await rec("threads_search", "/v1/threads/search", {"query": query, "trim": "true"})

        li = await rec("linkedin_search_posts", "/v1/linkedin/search/posts", {"query": f"looking for a {query}", "date_posted": "last-week"})
        lurl = next((p.get("url") for p in li.get("posts", []) if p.get("url")), None)
        if lurl:
            await rec("linkedin_post", "/v1/linkedin/post", {"url": lurl})

        g = await rec("google_search", "/v1/google/search", {"query": f'site:reddit.com "need a {query}"', "date_posted": "last-week"})
        gfb = await rec("google_search_fb_groups", "/v1/google/search", {"query": f'site:facebook.com/groups "{query}"'})
        group = None
        for r in (gfb.get("results") or []):
            m = re.match(r"(https://(?:www\.)?facebook\.com/groups/[^/?#]+)", r.get("url", ""))
            if m:
                group = m.group(1)
                break
        if group:
            fb = await rec("facebook_group_posts", "/v1/facebook/group/posts", {"url": group, "sort_by": "CHRONOLOGICAL"})
            p = next(iter(fb.get("posts") or []), None)
            if p:
                params = {"feedback_id": p.get("feedback_id")} if p.get("feedback_id") else {"url": p.get("url")}
                await rec("facebook_post_comments", "/v1/facebook/post/comments", params)

        after = await sc.credit_balance()
        meter = sc.meter

    for k, v in results.items():
        print(f"{k:28s} {v}")
    print(f"credits: before={before} after={after} spent={before - after} metered={meter.total_credits()}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="virtual assistant")
    ap.add_argument("--rebuild", action="store_true", help="re-redact from ~/.lead-radar/recordings; no API calls")
    a = ap.parse_args()
    if a.rebuild:
        rebuild_from_recordings()
        sys.exit(0)
    sys.exit(asyncio.run(main(a.query)))
