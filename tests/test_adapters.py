"""M2: adapters, normalizers, client error policy and dedupe, all offline against recorded fixtures."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from lead_radar import normalize as n
from lead_radar.adapters import API_ADAPTERS, AdapterContext
from lead_radar.dedupe import dedupe, hamming, simhash
from lead_radar.expander import load_brief_file
from lead_radar.models import Post
from lead_radar.scrapecreators import OutOfCredits, ScrapeCreators, SkipItem, SourceError

from sc_mock import FixtureTransport, load

BRIEF = Path(__file__).parents[1] / "config" / "briefs" / "virtual-assistant.json"


def run(coro):
    return asyncio.run(coro)


def brief():
    return load_brief_file(BRIEF, "r1")


def ctx(**kw):
    return AdapterContext(run_id="run-1", **kw)


async def _collect(platform, transport=None):
    t = transport or FixtureTransport()
    async with ScrapeCreators("k", transport=t, backoff=0) as sc:
        return await API_ADAPTERS[platform](sc, ctx()).collect(brief()), t


# --- every API source yields normalized posts ------------------------------

@pytest.mark.parametrize("platform", sorted(API_ADAPTERS))
def test_adapter_returns_normalized_posts(platform):
    res, t = run(_collect(platform))
    assert res.platform == platform
    assert res.stats.posts == len(res.posts) > 0, res.stats
    assert res.stats.requests == len(t.calls)
    assert res.stats.credits >= 1
    assert res.stats.errors == 0, res.stats.error_messages
    for p in res.posts:
        assert isinstance(p, Post) and p.text and p.url.startswith("https://")
        assert p.run_id == "run-1" and p.source_term
        assert p.id == n.post_id(p.platform, p.url)


def test_comment_sources_carry_parent_context():
    for platform in ("youtube", "tiktok", "instagram"):
        res, _ = run(_collect(platform))
        comments = [p for p in res.posts if p.kind in ("comment", "reply")]
        assert comments, platform
        assert all(c.parent_url and c.parent_text for c in comments), platform


def test_api_key_only_in_header_never_in_url():
    _, t = run(_collect("reddit"))
    assert all(r.headers["x-api-key"] == "k" and "k" not in r.url.params.values() for r in t.calls)


def test_instagram_never_requests_replies():
    _, t = run(_collect("instagram"))
    assert not any("include_replies" in r.url.params for r in t.calls)


def test_facebook_without_groups_reports_error_not_silence():
    b = brief().model_copy(update={"communities": {}})

    async def go():
        async with ScrapeCreators("k", transport=FixtureTransport()) as sc:
            return await API_ADAPTERS["facebook"](sc, ctx()).collect(b)
    res = run(go())
    assert res.stats.errors == 0 and "no facebook groups" in res.stats.notes[0]


# --- normalizer specifics ---------------------------------------------------

def test_reddit_post_normalization():
    item = load("reddit_search")["posts"][0]
    p = n.reddit_post(item, run_id="r", term="t")
    assert p.url.startswith("https://www.reddit.com/r/") and p.author_handle and p.posted_at and p.community
    assert p.author_url == f"https://www.reddit.com/user/{p.author_handle}"


def test_tiktok_handles_both_item_shapes():
    item = load("tiktok_search_keyword")["search_item_list"][0]
    flat = n.tiktok_video(item, run_id="r", term="t")
    wrapped = n.tiktok_video({"aweme_info": item}, run_id="r", term="t")
    assert flat.id == wrapped.id and "/video/" in flat.url


def test_google_results_keep_site_platform_and_snippet_date():
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    item = {"title": "Need a VA", "url": "https://www.reddit.com/r/x/comments/abc/need_a_va/?utm_source=g",
            "description": "6 days ago ... I need a virtual assistant"}
    p = n.google_result(item, run_id="r", term="t", now=now)
    assert p.platform == "reddit" and p.raw["via"] == "google" and "utm_source" not in p.url
    assert p.posted_at == now - timedelta(days=6)
    x = n.google_result({"title": "t", "url": "https://twitter.com/someone/status/1", "description": "Apr 3, 2026 ..."},
                        run_id="r", term="t")
    assert x.platform == "x" and x.author_handle == "someone" and x.posted_at.date().isoformat() == "2026-04-03"


def test_canonical_url():
    assert n.canonical_url("http://m.facebook.com/groups/1/?fbclid=zz#frag") == "https://facebook.com/groups/1"
    assert n.canonical_url("https://www.youtube.com/watch?v=abc&si=x") == "https://www.youtube.com/watch?v=abc"
    assert n.canonical_url("https://reddit.com/r/a/comments/b/") == "https://www.reddit.com/r/a/comments/b"


def test_youtube_creator_comment_flagged():
    video = n.youtube_video(load("youtube_search")["videos"][0], run_id="r", term="t")
    c = n.youtube_comment({"id": "c1", "content": "thanks all", "author": {"name": "x", "isCreator": True}},
                          video=video, run_id="r", term="t")
    assert c.raw["is_creator"] is True


# --- client error policy ------------------------------------------------------

def _client(status, body=None):
    t = FixtureTransport(overrides={"/v1/threads/search": httpx.Response(status, json=body or {"success": False})})
    return ScrapeCreators("secret-key", transport=t, backoff=0, retries_on_5xx=2), t


def test_402_raises_out_of_credits():
    sc, _ = _client(402)
    with pytest.raises(OutOfCredits):
        run(sc.get("/v1/threads/search", {"query": "x"}))


def test_403_404_skip():
    for status in (403, 404):
        sc, _ = _client(status)
        with pytest.raises(SkipItem):
            run(sc.get("/v1/threads/search", {"query": "x"}))


def test_500_retried_twice_then_error():
    sc, t = _client(500)
    with pytest.raises(SourceError, match="after 3 attempts"):
        run(sc.get("/v1/threads/search", {"query": "x"}, source="threads"))
    assert len(t.calls) == 3 and sc.meter.requests["threads"] == 3


def test_errors_never_contain_the_key():
    t = FixtureTransport(overrides={"/v1/threads/search": httpx.Response(400, text="bad key secret-key")})
    sc = ScrapeCreators("secret-key", transport=t, backoff=0)
    with pytest.raises(SourceError) as exc:
        run(sc.get("/v1/threads/search", {"query": "x"}))
    assert "secret-key" not in str(exc.value)


def test_out_of_credits_propagates_through_adapter():
    t = FixtureTransport(overrides={"/v1/threads/search": httpx.Response(402, json={})})
    with pytest.raises(OutOfCredits):
        run(_collect("threads", t))


def test_concurrency_capped():
    peak = 0
    active = 0

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal peak, active
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, json={"success": True, "credits_charged": 1})

    async def go():
        async with ScrapeCreators("k", transport=Slow(), concurrency=10) as sc:
            await asyncio.gather(*(sc.get("/x", {}) for _ in range(40)))
    run(go())
    assert peak == 10


# --- dedupe ---------------------------------------------------------------------

def _p(i, text, author="a", platform="reddit"):
    return n.make_post(platform=platform, url=f"https://e.com/{i}", kind="post", text=text, run_id="r",
                       source_term="t", author_handle=author)


BASE = "Anyone know a good virtual assistant for inbox and scheduling? My studio is drowning in email every day."
LONG = ("Hey everyone, I run a small marketing agency with four clients and I am completely buried in admin work. "
        "Inbox, calendar, invoicing, chasing approvals, the whole thing. I think I need a virtual assistant for about "
        "fifteen hours a week but I have never hired one before. Where do you find good ones, what do you pay, and how "
        "do you onboard them without spending more time training than you save? Any agencies you would recommend?")


def test_dedupe_exact_and_cross_post_same_author():
    xpost = "ANYONE know a good virtual assistant for inbox & scheduling!! My studio is drowning in email every day https://t.co/x"
    a, b, c = _p(1, BASE), _p(2, xpost), _p(1, BASE)
    d = _p(3, xpost, author="someone-else")          # same text, different author: not merged
    e = _p(4, "I'm a VA with open spots for inbox and calendar management, DM me for rates today.")
    assert hamming(a.simhash, b.simhash) <= 3
    kept, dropped = dedupe([a, b, c, d, e])
    assert [p.id for p in kept] == [a.id, d.id, e.id]
    reasons = [r for _, r in dropped]
    assert reasons == [f"near_duplicate_of:{a.id}", "duplicate_id"]


def test_dedupe_long_cross_post_with_sign_off():
    a, b = _p(1, LONG), _p(2, LONG + " Thanks in advance!")
    kept, dropped = dedupe([a, b])
    assert [p.id for p in kept] == [a.id] and dropped[0][1] == f"near_duplicate_of:{a.id}"


def test_dedupe_keeps_distinct_posts_by_same_author():
    other = "We are a two person bakery and I keep missing supplier emails. Thinking about getting help, maybe not."
    kept, _ = dedupe([_p(1, LONG), _p(2, other)])
    assert len(kept) == 2


def test_dedupe_uses_prior_runs():
    old, new = _p(1, BASE), _p(9, BASE.upper() + " https://example.com")
    kept, dropped = dedupe([new], lambda platform, h: [(old.id, old.simhash)])
    assert not kept and dropped[0][1] == f"near_duplicate_of:{old.id}"


def test_simhash_fits_sqlite_integer():
    for text in ("a b c", "virtual assistant needed now please", "x" * 500):
        assert -(1 << 63) <= simhash(text) < (1 << 63)


def test_templated_campaign_needs_three_authors_and_length():
    from lead_radar.dedupe import templated_campaigns
    spam = ("HIRING GLOBALLY fully remote. I'm looking for a Personal Assistant for a fully remote position. Also hiring "
            "document typing, logo design and retyping. Beginners welcome, weekly pay, send a message to apply now.")
    a = _p(1, spam, author="x1")
    b = _p(2, "Malaysia " + spam, author="x2")
    c = _p(3, spam + " https://t.co/zz", author="x3")
    solo = _p(4, spam, author="x1")   # same author again: still 3 authors total, all flagged
    short = [_p(10 + i, "Is this still available? I'm interested", author=f"s{i}") for i in range(5)]
    real = _p(20, LONG, author="buyer")
    flagged = templated_campaigns([a, b, c, solo, *short, real])
    assert {a.id, b.id, c.id, solo.id} <= flagged
    assert not flagged & {p.id for p in short} and real.id not in flagged
    assert templated_campaigns([a, b]) == set()   # two authors is not a campaign
