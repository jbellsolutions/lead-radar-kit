"""Reddit program-lane source: offline tests (fake ScrapeCreators client, fake homepage fetches). Synthetic data only;
author names are invented ("Pat Example") and never reach a Candidate or a progress line."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from lead_radar.programs import detect, signals as sg, sources as srcs
from lead_radar.programs.sources import reddit as rd
from lead_radar.scrapecreators import OutOfCredits

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
AUTHOR = "Pat Example"
USERNAME = "pat_example_42"


class FakeMeter:
    def __init__(self):
        self.n = 0

    def total_credits(self):
        return self.n


class FakeSC:
    """Routes: path -> payload, a callable(params) -> payload, or an exception to raise. Unknown paths 404."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.meter = FakeMeter()
        self.exhausted = False
        self.calls: list[tuple[str, dict]] = []

    async def get(self, path, params, source=""):
        self.calls.append((path, {k: v for k, v in (params or {}).items() if v is not None}))
        route = self.routes.get(path)
        if route is None:
            raise sg.SkipItem(f"{path}: 404")
        result = route(params) if callable(route) else route
        if isinstance(result, BaseException):
            raise result
        self.meter.n += 1  # one credit per call, as the live API charges
        return result

    def paths(self):
        return [p for p, _ in self.calls]


def epoch(days_ago: float) -> int:
    return int((NOW - timedelta(days=days_ago)).timestamp())


def post(pid="abc123", sub="SaaS", title="We just launched our affiliate program", selftext=None, days_ago=2,
         author=AUTHOR, num_comments=0, link=None):
    return {"id": f"t3_{pid}", "author": author, "subreddit": sub, "title": title, "selftext": selftext,
            "created_utc": epoch(days_ago),
            "created_at_iso": (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "num_comments": num_comments,
            "url": link or f"https://www.reddit.com/r/{sub}/comments/{pid}/some_slug/",
            "permalink": f"/r/{sub}/comments/{pid}/some_slug/"}


def page(host: str, html: str, final: str | None = None, status: int = 200) -> detect.Page:
    return detect.Page(url=f"https://{host}/", final_url=final or f"https://{host}/", status=status, html=html,
                       role="home")


def make_ctx(sc, pages=None, budget=15, days=30, cfg=None):
    conf = dict(rd.DEFAULTS)
    conf.update(cfg or {})
    say: list[str] = []
    ctx = srcs.SourceContext(sc=sc, http=None, budget=sg.Budget(sc, budget), cfg=conf, signals_cfg={}, days=days,
                             now=NOW, say=say.append)
    pages = pages or {}

    async def fake_get(url, role="page"):
        host = detect.bare_host(url)
        ctx.http_used += 1
        return pages.get(host)
    ctx.get = fake_get
    ctx.say_lines = say
    return ctx


def search_route(*posts, after=None):
    return lambda params: {"success": True, "credits_charged": 1, "posts": list(posts), "after": after}


ACME_PAGE = page("acme.io", "<html><head><title>Acme | Invoicing for plumbers</title></head></html>")
BODY_ACME = ("We built Acme for plumbers. Our app is at acme.io. We just launched our affiliate program: 30% recurring, "
             "60-day cookie. How do we recruit our first affiliates?")


def run(ctx):
    return asyncio.run(rd.collect(ctx))


# contract -------------------------------------------------------------------------------------------------------------

def test_contract():
    import inspect
    assert rd.NAME == "reddit" and isinstance(rd.DEFAULTS, dict) and inspect.iscoroutinefunction(rd.collect)
    assert all(isinstance(q, list) and len(q) == 2 for q in rd.DEFAULTS["searches"])
    assert rd.DEFAULTS["max_comment_reads"] == 5


# buyer kept and resolved --------------------------------------------------------------------------------------------

def test_buyer_is_kept_and_resolved_to_its_domain():
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=BODY_ACME, title="We just launched our affiliate program"))})
    ctx = make_ctx(sc, pages={"acme.io": ACME_PAGE}, budget=3)
    cands = run(ctx)
    assert len(cands) == 1
    c = cands[0]
    assert c.list == "affiliate" and c.domain == "acme.io" and c.linkedin_url is None and c.require_us is True
    assert c.company_name == "Acme"
    s = c.signal
    assert s.kind == "community_post" and s.platform == "reddit"
    assert s.url == "https://www.reddit.com/r/SaaS/comments/abc123/"
    assert s.signal_date == (NOW - timedelta(days=2)).date().isoformat()
    assert s.person_name is None and s.person_url is None
    assert s.quote == "We just launched our affiliate program"
    assert sg.is_program_post({"kind": "community_post", "quote": s.quote})


def test_search_params_follow_the_documented_names():
    sc = FakeSC({"/v1/reddit/search": search_route(), "/v1/reddit/subreddit/search": search_route()})
    run(make_ctx(sc, budget=4, cfg={"searches": [["", '"our affiliate program"'], ["startups", "affiliate program"]]}))
    (p1, q1), (p2, q2) = sc.calls
    assert p1 == "/v1/reddit/search" and q1 == {"query": '"our affiliate program"', "filter": "posts", "sort": "new",
                                                "timeframe": "month"}
    assert p2 == "/v1/reddit/subreddit/search" and q2["subreddit"] == "startups" and q2["sort"] == "new"


# claims -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,domain,expected", [
    ("our app acme.io", "acme.io", True),
    ("My site: acme.io", "acme.io", True),
    ("we built acme.io", "acme.io", True),
    ("I'm the founder of acme.io", "acme.io", True),
    ("acme.io is mine", "acme.io", True),
    ("Our product is at https://acme.io/affiliates", "acme.io", True),
    ("Our app is at acme.io", "acme.io", True),
    ("shop.acme.io is our store", "acme.io", True),
    ("we pay affiliates via stripe.com", "stripe.com", False),
    ("our competitor acme.com has a program", "acme.com", False),
    ("I use tolt.io", "tolt.io", False),
    ("built on webflow.com", "webflow.com", False),
    ("our competitor app acme.io", "acme.io", False),
    ("we compared acme.io", "acme.io", False),
    ("like acme.io", "acme.io", False),
    ("our favourite tool: acme.io", "acme.io", False),
])
def test_own_claim_forms(text, domain, expected):
    assert rd._own_claim(text, domain) is expected


def test_brand_claim_needs_a_first_person_sentence_without_competitor_words():
    assert rd._brand_claim("I'm the developer behind Sidcraft Page Builder.", "Sidcraft Page Builder")
    assert not rd._brand_claim("Sidcraft Page Builder is a competitor to ours.", "Sidcraft Page Builder")
    assert not rd._brand_claim("Acme (acme.io) is a competitor. We launched our affiliate program.", "Acme")


def test_page_brands_skip_generic_titles():
    html = "<title>Affiliate Program | Acme</title>"
    assert rd._page_brands(html) == ["Acme"]
    assert "Acme" in rd._page_brands("<title>Acme | Invoicing for plumbers</title>")


# resolution rejects -------------------------------------------------------------------------------------------------

def test_parked_domain_is_rejected():
    parked = page("acme.io", "<title>acme.io is for sale | HugeDomains</title>")
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=BODY_ACME))})
    assert run(make_ctx(sc, pages={"acme.io": parked}, budget=2)) == []


def test_redirect_onto_a_platform_host_is_rejected():
    linked = page("acme.io", "<title>Acme</title>", final="https://linktr.ee/acme")
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=BODY_ACME))})
    assert run(make_ctx(sc, pages={"acme.io": linked}, budget=2)) == []


def test_dead_homepage_is_rejected():
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=BODY_ACME))})
    assert run(make_ctx(sc, pages={"acme.io": page("acme.io", "", status=404)}, budget=2)) == []


def test_competitor_domain_is_not_anchored():
    text = ("We just launched our affiliate program for plumbers. Acme (acme.io) is a competitor that also has one, "
            "and we compared our fees with it.")
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=text))})
    assert run(make_ctx(sc, pages={"acme.io": ACME_PAGE}, budget=2)) == []


def test_directory_listing_is_not_a_company_domain():
    text = "We just launched our affiliate program. Listed on affiliationlist.com with the details."
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=text))})
    assert run(make_ctx(sc, budget=2)) == []


def test_platform_host_is_never_a_company_domain():
    assert rd._domains("I use tolt.io for our affiliate program") == []


def test_tld_gap_is_covered_for_pro_domains():
    assert rd._domains("We built Canvasly, it's at canvasly.pro for our affiliate program") == ["canvasly.pro"]
    assert rd._domains("see https://shop.acme.co.uk/affiliates") == ["acme.co.uk"]


# filters ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("title,body,sub,reason", [
    ("Which affiliate programs should I join for my blog?", "", "SaaS", "seeker"),
    ("We help brands launch their affiliate program", "", "SaaS", "seller"),
    ("Join my team and make money with our referral program", "", "SaaS", "mlm"),
    ("Our casino affiliate program pays 40%", "", "SaaS", "vertical"),
    ("Our research use only peptide affiliate program is open", "", "SaaS", "vertical"),
    ("We're hiring an offer manager to run advertiser offers against our paid traffic", "", "hiring", "traffic arbitrage"),
    ("[For Hire] Affiliate manager with five years experience", "", "forhire", "for hire"),
    ("Refer a friend and get 2 months free on our referral program", "", "SaaS", "consumer referral or mlm"),
    ("Quick survey on referral programs", "We launched our affiliate program", "SaaS", "research or poll"),
    ("I'm an affiliate marketer looking for good programs", "", "SaaS", "affiliate-side"),
    ("We are hiring an affiliate manager in Austin", "", "startups", "hire without own program"),
    ("We made a launch bundle that helps prepare an affiliate program", "", "SaaS", "seller"),
    ("How's your affiliate program going?", "Wanted to know if your SaaS got an affiliate program.", "SaaS",
     "research or poll"),
    ("Does your SaaS have an affiliate program?", "", "microsaas", "research or poll"),
    ("Best affiliate program for my blog", "", "affiliatemarketing", "affiliate-side subreddit"),
    ("We launched our affiliate program", "", "SidehustlesIndia", "not US"),
])
def test_program_side_filters_drop(title, body, sub, reason):
    assert rd._drop_reason(title, body, sub, full=True) == reason


def test_a_brand_post_passes_every_filter_and_quotes_its_program_sentence():
    text = "We just launched our affiliate program for our app. 30% recurring, 60-day cookie."
    assert rd._drop_reason("We just launched our affiliate program", text, "SaaS", full=True) is None
    quote = rd._program_quote(text)
    assert quote and "launched our affiliate program" in quote and sg.is_program_post({"kind": "community_post",
                                                                                      "quote": quote})


def test_hiring_post_with_own_program_is_kept():
    title = "[Hiring] Affiliate manager for our affiliate program at Acme"
    assert rd._drop_reason(title, "We run it from Austin.", "affiliatemarketing", full=True) is None


def test_founder_recruiting_affiliates_is_kept():
    text = ("Founder here. We launched a 30% recurring affiliate program for our prayer app and need to recruit "
            "affiliates. Where do you find them?")
    assert rd._drop_reason("We launched our affiliate program", text, "indiehackers", full=True) is None
    assert rd._program_quote(text) == ("We launched a 30% recurring affiliate program for our prayer app and need to "
                                       "recruit affiliates.")


def test_recruiting_post_without_a_product_domain_is_never_guessed():
    # "Looking for affiliates" is a program sentence (config affiliate.program_post), but nothing names the product
    sc = FakeSC({"/v1/reddit/search": search_route(post(title="Looking for affiliates for my SaaS", selftext="Any tips?"))})
    ctx = make_ctx(sc, budget=3)
    assert run(ctx) == []
    assert "unresolved 1" in " ".join(ctx.say_lines)


def test_no_program_sentence_means_no_candidate():
    sc = FakeSC({"/v1/reddit/search": search_route(post(title="Weekly thread", selftext="Any tips on SEO for my SaaS?"))})
    ctx = make_ctx(sc, budget=3)
    assert run(ctx) == []


# budget and degradation ---------------------------------------------------------------------------------------------

def test_zero_budget_makes_no_scrapecreators_call():
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=BODY_ACME))})
    assert run(make_ctx(sc, budget=0)) == []
    assert sc.calls == []


def test_budget_stops_the_searches():
    sc = FakeSC({"/v1/reddit/search": search_route(), "/v1/reddit/subreddit/search": search_route()})
    run(make_ctx(sc, budget=4))  # every credit goes to searches when no post needs a read
    assert len(sc.calls) == 4


def test_out_of_credits_keeps_what_was_found():
    first = search_route(post(selftext=BODY_ACME))
    sc = FakeSC({"/v1/reddit/search": first, "/v1/reddit/subreddit/search": OutOfCredits("402")})
    cands = run(make_ctx(sc, pages={"acme.io": ACME_PAGE}, budget=15))
    assert [c.domain for c in cands] == ["acme.io"]
    assert sc.paths() == ["/v1/reddit/search", "/v1/reddit/subreddit/search"]


def test_body_is_read_only_when_the_search_result_has_none():
    a = post(pid="aaa111", title="We launched our affiliate program", selftext="We launched our affiliate program. Our app is at alpha.io.")
    b = post(pid="bbb222", title="We just opened our affiliate program", selftext=None)
    full_b = dict(b, selftext="We just opened our affiliate program. Our app is at beta.io.")
    sc = FakeSC({"/v1/reddit/search": search_route(a, b), "/v1/reddit/post": full_b})
    pages = {"alpha.io": page("alpha.io", "<title>Alpha | Tools</title>"),
             "beta.io": page("beta.io", "<title>Beta | Tools</title>")}
    cands = run(make_ctx(sc, pages=pages, budget=3))
    assert sc.paths().count("/v1/reddit/post") == 1
    assert sorted(c.domain for c in cands) == ["alpha.io", "beta.io"]


def test_same_title_and_company_are_listed_once():
    p1 = post(pid="ccc111", sub="SaaS", selftext=BODY_ACME)
    p2 = post(pid="ddd222", sub="startups", selftext=BODY_ACME)
    sc = FakeSC({"/v1/reddit/search": search_route(p1, p2)})
    assert len(run(make_ctx(sc, pages={"acme.io": ACME_PAGE}, budget=3))) == 1


def test_stale_posts_are_dropped_without_a_read():
    old = post(pid="eee333", selftext=None, days_ago=40)
    sc = FakeSC({"/v1/reddit/search": search_route(old), "/v1/reddit/post": search_route()})
    assert run(make_ctx(sc, budget=6)) == []
    assert "/v1/reddit/post" not in sc.paths()


def test_iso_date_fallback_is_used_when_epoch_is_missing():
    raw = post(pid="fff444", selftext=BODY_ACME)
    raw.pop("created_utc")
    raw.pop("created", None)
    p = rd._norm(raw)
    assert p is not None and p["iso"] == (NOW - timedelta(days=2)).date().isoformat()


def test_timeframe_follows_the_window():
    assert [rd._timeframe(d) for d in (1, 3, 30, 60)] == ["day", "week", "month", "year"]


# OP comments --------------------------------------------------------------------------------------------------------

def test_op_top_level_comment_anchors_when_the_body_does_not():
    body = "We just launched our affiliate program for plumbers. Ask me anything."
    comments = {"comments": [
        {"author": AUTHOR, "body": "Our app is at acme.io, happy to answer questions.", "parent_id": "t3_abc123",
         "depth": 0, "replies": True},
        {"author": "someone_else", "body": "try competitor.com", "parent_id": "t3_abc123", "depth": 0, "replies": True},
    ]}
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=body, num_comments=3)),
                 "/v1/reddit/post/comments": comments})
    cands = run(make_ctx(sc, pages={"acme.io": ACME_PAGE}, budget=10))
    assert [c.domain for c in cands] == ["acme.io"]
    assert "/v1/reddit/post/comments" in sc.paths()


def test_comment_reads_are_capped_at_zero():
    body = "We just launched our affiliate program for plumbers. Ask me anything."
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=body, num_comments=3)),
                 "/v1/reddit/post/comments": {"comments": []}})
    assert run(make_ctx(sc, budget=10, cfg={"max_comment_reads": 0})) == []
    assert "/v1/reddit/post/comments" not in sc.paths()


def test_non_op_comments_are_ignored():
    assert rd._op_top_level([{"author": "other", "body": "Our app is at acme.io", "parent_id": "t3_abc123", "depth": 0}],
                            AUTHOR) == []


def test_op_top_level_uses_reddit_parent_id_and_depth():
    top = {"author": AUTHOR, "body": "Our app is at acme.io", "parent_id": "t3_abc123", "depth": 0}
    assert rd._op_top_level([top], AUTHOR) == ["Our app is at acme.io"]
    assert rd._op_top_level([{"author": AUTHOR, "body": "x", "depth": 0}], AUTHOR) == ["x"]  # no parent field, depth 0


def test_op_nested_reply_never_anchors_a_domain():
    reply = {"author": AUTHOR, "body": "Our app is at acme.io", "parent_id": "t1_ed1su6t", "depth": 2}
    assert rd._op_top_level([reply], AUTHOR) == []
    body = "We just launched our affiliate program for plumbers. Ask me anything."
    comments = {"comments": [dict(reply, replies={"items": []})]}
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=body, num_comments=3)),
                 "/v1/reddit/post/comments": comments})
    assert run(make_ctx(sc, budget=10)) == []


def test_comment_without_parent_or_depth_is_not_top_level():
    assert rd._op_top_level([{"author": AUTHOR, "body": "Our app is at acme.io"}], AUTHOR) == []


def test_deleted_op_never_matches_a_deleted_commenter():
    comments = [{"author": "[deleted]", "body": "Our app is at acme.io", "parent_id": "t3_abc123", "depth": 0}]
    assert rd._op_top_level(comments, "[deleted]") == []


# privacy ------------------------------------------------------------------------------------------------------------

def test_no_author_name_reaches_progress_lines_or_candidates():
    body = BODY_ACME + " I'm Pat Example, cofounder."
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=body, author=AUTHOR)),
                 "/v1/reddit/post/comments": {"comments": [{"author": AUTHOR, "body": "Our app is at acme.io.",
                                                           "parent_id": "t3_abc123", "depth": 0}]}})
    ctx = make_ctx(sc, pages={"acme.io": ACME_PAGE}, budget=10)
    cands = run(ctx)
    text_out = " ".join(ctx.say_lines) + repr([(c.domain, c.signal.url, c.signal.person_name, c.company_name) for c in cands])
    assert AUTHOR not in text_out and USERNAME not in text_out and "pat_example" not in text_out.lower()
    assert all(c.signal.person_name is None and c.signal.person_url is None for c in cands)


def test_progress_lines_carry_counts_not_titles():
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=BODY_ACME))})
    ctx = make_ctx(sc, pages={"acme.io": ACME_PAGE}, budget=3)
    run(ctx)
    assert all("launched our affiliate program" not in line for line in ctx.say_lines)
    assert any(line.startswith("reddit:") for line in ctx.say_lines)


# helpers ------------------------------------------------------------------------------------------------------------

def test_norm_reads_the_id_and_sub_from_the_permalink():
    raw = post(pid="ggg555", sub="microsaas", selftext=BODY_ACME)
    raw["id"] = "t3_ggg555"
    p = rd._norm(raw)
    assert p["id"] == "ggg555" and p["sub"] == "microsaas"


def test_norm_rejects_a_post_without_a_usable_id_or_date():
    assert rd._norm({"id": "", "subreddit": "SaaS", "created_utc": epoch(1)}) is None
    assert rd._norm({"id": "abc123", "subreddit": "SaaS"}) is None


BODY_BETA = "We just launched our affiliate program. Our app is at beta.io."
BODY_GAMMA = "We just launched our affiliate program. Our product is at gamma.io."
BETA_PAGE = page("beta.io", "<title>Beta | Invoicing</title>")
GAMMA_PAGE = page("gamma.io", "<title>Gamma | Invoicing</title>")


# robustness (malformed records) -------------------------------------------------------------------------------------

def test_millisecond_epoch_is_read_as_seconds():
    raw = post(pid="hhh666", selftext=BODY_ACME)
    raw["created_utc"] = epoch(2) * 1000
    p = rd._norm(raw)
    assert p is not None and p["iso"] == (NOW - timedelta(days=2)).date().isoformat()


def test_out_of_range_epoch_drops_the_record_only():
    raw = post(pid="iii777", selftext=BODY_ACME)
    raw["created_utc"] = 10 ** 20
    raw["created_at_iso"] = "not a date"
    assert rd._norm(raw) is None


def test_count_fields_that_are_not_numbers_read_as_zero():
    assert rd._int("12 comments") == 0 and rd._int(None) == 0 and rd._int(float("inf")) == 0 and rd._int("7") == 7
    assert rd._norm(post(pid="jjj888", selftext=BODY_ACME, num_comments="12 comments"))["comments"] == 0


def test_one_malformed_post_does_not_discard_the_good_candidates():
    good = post(pid="good01", selftext=BODY_ACME)
    bad_date = post(pid="bad002", selftext=BODY_BETA)
    bad_date["created_utc"] = 10 ** 20
    bad_date["created_at_iso"] = "garbage"
    odd = post(pid="odd003", sub="startups", title="We just opened our affiliate program", selftext=BODY_GAMMA,
               num_comments="12 comments", author=12345)
    sc = FakeSC({"/v1/reddit/search": search_route(good, bad_date, odd)})
    pages = {"acme.io": ACME_PAGE, "beta.io": BETA_PAGE, "gamma.io": GAMMA_PAGE}
    assert sorted(c.domain for c in run(make_ctx(sc, pages=pages, budget=3))) == ["acme.io", "gamma.io"]


def test_a_post_that_raises_is_counted_and_the_rest_survive(monkeypatch):
    real = rd._program_quote

    def flaky(text, author=None):
        if "BOOM" in text:
            raise ValueError("synthetic failure")
        return real(text, author)
    monkeypatch.setattr(rd, "_program_quote", flaky)
    good = post(pid="kkk111", selftext=BODY_ACME)
    boom = post(pid="lll222", sub="startups", title="We just launched our affiliate program BOOM", selftext=BODY_GAMMA)
    sc = FakeSC({"/v1/reddit/search": search_route(good, boom)})
    ctx = make_ctx(sc, pages={"acme.io": ACME_PAGE, "gamma.io": GAMMA_PAGE}, budget=3)
    assert [c.domain for c in run(ctx)] == ["acme.io"]
    assert "bad record" in " ".join(ctx.say_lines)


# vendors ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sentence", [
    "We switched to Rewardful (rewardful.com) for our affiliate program.",
    "We moved to Rewardful (rewardful.com) to track commissions.",
    "We run our affiliate program on Rewardful (rewardful.com).",
    "Our referral program: Rewardful",
    "I'm the founder behind Rewardful, the affiliate software.",
])
def test_affiliate_software_mentions_are_never_the_company(sentence):
    assert not rd._brand_claim(sentence, "Rewardful")


def test_a_referral_program_colon_is_not_a_brand_claim():
    assert not rd._brand_claim("Our referral program: Acme", "Acme")


def test_curly_apostrophes_match_the_claim_patterns():
    assert rd._brand_claim("I’m the developer behind Sidcraft Page Builder.", "Sidcraft Page Builder")
    assert rd._own_claim("I’m the founder of acme.io", "acme.io")
    assert rd._drop_reason("I’m an affiliate marketer looking for good programs", "", "SaaS", full=True) == "affiliate-side"


def test_vendor_domain_is_rejected_even_when_its_homepage_is_live():
    text = "We just launched our affiliate program. Our app is at rewardful.com."
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext=text))})
    pages = {"rewardful.com": page("rewardful.com", "<title>Rewardful | Affiliate software</title>")}
    assert run(make_ctx(sc, pages=pages, budget=2)) == []
    sc = FakeSC({"/v1/reddit/search": search_route(post(selftext="Our referral program: rewardful.com"))})
    assert run(make_ctx(sc, pages=pages, budget=2)) == []


# cross-posts --------------------------------------------------------------------------------------------------------

def test_excluded_cross_post_does_not_hide_the_buyers_copy():
    newer = post(pid="sa1111", sub="affiliatemarketing", title="We just launched our affiliate program", selftext=BODY_ACME,
                 days_ago=1)
    older = post(pid="sb2222", sub="SaaS", title="We just launched our affiliate program", selftext=BODY_ACME, days_ago=2)
    sc = FakeSC({"/v1/reddit/search": search_route(newer, older)})
    ctx = make_ctx(sc, pages={"acme.io": ACME_PAGE}, budget=2, cfg={"searches": [["", '"our affiliate program"']]})
    assert [c.signal.url for c in run(ctx)] == ["https://www.reddit.com/r/SaaS/comments/sb2222/"]
    assert "affiliate-side subreddit" in " ".join(ctx.say_lines)


# program-side gates (sentence level) --------------------------------------------------------------------------------

@pytest.mark.parametrize("title,body,sub,reason", [
    ("We just launched our affiliate program to make money with our app", "", "SaaS", None),
    ("Our affiliate program: get paid to promote our analytics app", "", "SaaS", None),
    ("We need an affiliate program for our SaaS", "", "SaaS", None),
    ("We just launched our affiliate program for our app, and we'd love to see you join", "", "SaaS", None),
    ("Looking for an affiliate program for my website", "", "SaaS", "seeker"),
    ("Looking for an affiliate program to join for my SaaS", "", "SaaS", "seeker"),
    ("I want to join our affiliate program", "", "SaaS", "seeker"),
    ("I want to make money with affiliate programs", "", "SaaS", "affiliate-side"),
])
def test_program_sentences_pass_and_seekers_drop(title, body, sub, reason):
    assert rd._drop_reason(title, body, sub, full=True) == reason


# quote hygiene -------------------------------------------------------------------------------------------------------

def test_quote_skips_a_self_introduction_and_the_username():
    text = "I'm Pat Example, cofounder. We just launched our affiliate program for plumbers."
    assert rd._program_quote(text, AUTHOR) == "We just launched our affiliate program for plumbers."
    assert rd._program_quote("Hi, I'm Pat Example and we launched our affiliate program.", AUTHOR) is None
    assert rd._program_quote("pat_example_42 here: we launched our affiliate program.", USERNAME) is None
