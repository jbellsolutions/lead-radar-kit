"""Offline tests for the community source (Hacker News, Indie Hackers, Product Hunt, Shopify Community).

No network: HTTP goes through a fake client that returns real httpx.Response objects (so detect._get runs unchanged),
ScrapeCreators through a fake that records calls. Every name here is synthetic; none may reach ctx.say output or a
candidate's person fields."""
import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from lead_radar.programs import signals as sg, sources as srcs
from lead_radar.programs.sources import community as com, common as c

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def day(n: int) -> str:
    return (NOW - timedelta(days=n)).date().isoformat()


class FakeMeter:
    def __init__(self):
        self.n = 0

    def total_credits(self):
        return self.n


class FakeSC:
    """Answers Google searches by a substring of the query; anything else is a zero-result search (SkipItem)."""

    def __init__(self, answers=None):
        self.answers, self.meter, self.exhausted, self.calls = answers or {}, FakeMeter(), False, []

    async def get(self, path, params, source=""):
        self.calls.append((path, params["query"]))
        for key, data in self.answers.items():
            if key in params["query"]:
                if isinstance(data, Exception):
                    raise data
                self.meter.n += 1
                return data
        raise sg.SkipItem("no results")


class FakeHTTP:
    """Stands in for listings.http.PoliteClient: a route is (status, content-type, body, final_url). Unknown URLs
    return None, as a 404 does. Earlier keys win, so list specific routes first."""

    def __init__(self, routes=None):
        self.routes, self.calls = routes or {}, []

    async def get(self, url):
        self.calls.append(url)
        for key, (status, ctype, body, final) in self.routes.items():
            if key in url:
                return httpx.Response(status, headers={"content-type": ctype}, text=body,
                                      request=httpx.Request("GET", final or url))
        return None


def js(obj):
    return (200, "application/json", json.dumps(obj), None)


def page(html, status=200, final=None):
    return (status, "text/html; charset=utf-8", html, final)


def make_ctx(http, sc=None, *, days=30, budget=10, cfg=None, known=(), say=None):
    conf = dict(com.DEFAULTS)
    conf.update(cfg or {})
    sc = sc or FakeSC()
    return srcs.SourceContext(sc=sc, http=http, budget=sg.Budget(sc, budget), cfg=conf, signals_cfg=sg.load(),
                              days=days, now=NOW, known=(lambda d: d in known), say=say or (lambda m: None),
                              http_cap=conf["http_cap"])


def run(ctx):
    return asyncio.run(com.collect(ctx))


def hn_hit(oid, title, created, url=None, story_text=None, author="Pat Example"):
    return {"objectID": str(oid), "title": title, "url": url, "story_text": story_text, "created_at": created,
            "author": author}


ACME_HOME = ('<html><head><title>Acme | Affiliate program for SaaS</title></head><body>'
             '<footer><a href="https://www.linkedin.com/company/acme/">LinkedIn</a></footer></body></html>')
WIDGET_HOME = "<html><head><title>Widget</title></head><body>Widget</body></html>"
PARKED_HOME = "<html><head><title>This domain is for sale | Sedo</title></head><body></body></html>"
COLUMN_HOME = ('<html><head><title>Column</title></head><body>'
               '<footer><a href="https://www.linkedin.com/company/column-bank">LinkedIn</a></footer></body></html>')
WIDGETCO_HOME = ("<html><head><title>WidgetCo | Affiliate</title></head><body>"
                 '<footer><a href="https://www.linkedin.com/company/widgetco">LinkedIn</a></footer></body></html>')
PEXO_HOME = "<html><head><title>Pexo</title></head><body>Pexo</body></html>"
SHOPFIT_HOME = "<html><head><title>Shopfit</title></head><body>Shopfit</body></html>"


# contract and helpers ---------------------------------------------------------------------------------------------

def test_module_contract():
    assert com.NAME == "community" and srcs.REGISTRY["community"] == "community"
    assert {"enabled", "credits", "hn_queries", "google_queries", "max_google_queries", "max_candidates"} <= set(com.DEFAULTS)
    assert inspect.iscoroutinefunction(com.collect)
    assert int(srcs.settings("community", com, sg.load())["credits"]) > 0


def test_canonical_urls_and_shapes():
    assert com.canonical("https://www.indiehackers.com/post/launched-x-abc?utm_source=tw&ref=1") \
        == "https://www.indiehackers.com/post/launched-x-abc"
    assert com.canonical("https://www.producthunt.com/products/pexo-2/") == "https://www.producthunt.com/products/pexo-2"
    assert com.canonical("https://news.ycombinator.com/item?id=1") is None
    shop = "https://community.shopify.com/c/shopify-discussion/my-affiliate-program/td-p/1234567"
    assert com.canonical(shop) == shop
    assert com.POST_PATH["shopify"].search(urlsplit_path(shop))
    assert not com.POST_PATH["shopify"].search("/c/shopify-discussion")
    assert com.POST_PATH["producthunt"].search("/products/pexo-2")
    assert com.LISTING.search("/products/pexo-2/alternatives") and com.LISTING.search("/leaderboard/daily")
    assert not com.LISTING.search("/products/pexo-2")


def urlsplit_path(url):
    from urllib.parse import urlsplit
    return urlsplit(url).path


def test_window_maps_days_to_date_posted():
    assert [com.window(d) for d in (1, 3, 30, 60)] == ["last-day", "last-week", "last-month", "last-year"]


@pytest.mark.parametrize("desc,iso,end_is_zero", [
    ("6 days ago ... I need a tool", "2026-10-04", False),
    ("Sep 30, 2026 ... Launched", "2026-09-30", False),
    ("yesterday · we launched", "2026-10-09", False),
    ("Launched 11d ago", None, True),
    ("I went 3 days ago to a conference", None, True),
    ("a week ago we launched", None, True),
])
def test_leading_date_only(desc, iso, end_is_zero):
    got, end = com.lead_date(desc, NOW)
    assert got == iso and (end == 0) is end_is_zero


@pytest.mark.parametrize("text,reason", [
    ("We just launched our affiliate program: 30% recurring for 12 months.", None),
    ("Join our affiliate program and earn 20% on every sale.", None),
    ("Looking for ambassadors for our referral program, DM us.", None),
    ("We launched our affiliate programs for creators today.", None),  # plural and programme spellings
    ("Now accepting new ISOs for our merchant funding program.", "no program named"),
    ("I'm looking for an affiliate program to join for my newsletter.", "seeker"),
    ("Which affiliate program should I join as a beginner?", "seeker"),
    ("We help brands launch their affiliate program. Book a call.", "seller"),
    ("We launched our affiliate program using PartnerStack for payouts.", "vendor_names"),
    ("We just launched our casino affiliate program.", "vertical"),
    ("Use my code SAVE10 for our referral program", "individual_affiliate"),
    ("Acme launched an affiliate program last week.", None),  # company voice, buyer verb, same sentence
    ("Acme has an affiliate program for its partners.", "no buyer verb next to the program"),
    ("I built the tool and it has an affiliate program with 30% commission.", "no buyer verb next to the program"),
    ("Show HN: Referral program software for SaaS teams", "affiliate or referral software"),
    ("Affiliate program management software for brands", "affiliate or referral software"),
    ("How to launch an affiliate program (AffiliateKit guide)", "educational title"),
])
def test_post_gate(text, reason):
    assert com.post_gate(text, sg.load()) == reason


def test_hiring_parts_and_gate():
    cfg = sg.load()
    html = ('<p>Column ( <a href="https://column.com">https://column.com</a> ) | Affiliate Manager | '
            'San Francisco, CA | Full-time</p><p>Launch our affiliate program with us. Email jane@column.com</p>')
    name, role, line, urls = com.hiring_parts(html)
    assert name == "Column" and role == "Affiliate Manager | San Francisco, CA | Full-time"
    assert "https://column.com" in urls and "Column ( https://column.com )" in line
    body = com._clean(com.html_text(html))
    assert com.hiring_gate(name, role, line, body, cfg) is None
    assert com.hiring_gate("Partner Co", "Partnerships Manager | US",
                           "Partner Co | Partnerships Manager | US", "Own the bank partnership pipeline.", cfg) \
        == "not an affiliate or referral role"
    assert com.hiring_gate("Maple", "Affiliate Manager | Toronto, Canada", "Maple | Affiliate Manager | Toronto, Canada",
                           "Maple | Affiliate Manager | Toronto, Canada", cfg) == "not US"
    assert com.hiring_gate("Ledger", "Referral Partner Manager | NYC", "x",
                           "We fund small businesses with a merchant cash advance and ISO referral partners.",
                           cfg) == "funding context (finance lane)"
    assert com.hiring_gate("Mono", "Affiliate Manager | US", "x", "Our affiliate software company is hiring.",
                           cfg) == "vendors"
    assert com.hiring_parts("Hi there, we are hiring engineers") is None
    # employee-referral wording is not an affiliate or referral role
    assert com.hiring_gate("Acme", "Senior Engineer | Remote (US) | referral bonus",
                           "Acme | Senior Engineer | Remote (US) | referral bonus",
                           "Acme | Senior Engineer | Remote (US) | referral bonus", cfg) == "not an affiliate or referral role"
    assert com.hiring_gate("Acme", "Senior Engineer | Remote (US)", "Acme | Senior Engineer | Remote (US)",
                           "Acme | Senior Engineer | Remote (US) | we pay an employee referral bonus and refer a friend",
                           cfg) == "not an affiliate or referral role"


def test_label_linkedin_and_denied_hosts():
    assert com._label("get.snugcv.com") == "snugcv"
    assert com._label("ai.io") == ""  # too short to name a brand
    assert com._denied("github.com") and com._denied("myapp.vercel.app".split(".", 1)[1])
    assert not com._denied("snugcv.com")
    page_ok = detect_page(ACME_HOME)
    assert com._linkedin_for(page_ok, "acme.com") == "https://www.linkedin.com/company/acme"
    assert com._linkedin_for(detect_page(
        '<a href="https://www.linkedin.com/company/110250619">x</a>'), "pexo.ai") is None


def detect_page(html):
    from lead_radar.programs import detect
    return detect.Page("https://acme.com/", "https://acme.com/", 200, html, "home")


def test_html_text_keeps_paragraphs_and_entities():
    assert com._clean(com.html_text("<p>Acme &amp; Co</p><p>Second</p>")) == "Acme & Co Second"
    assert [s.strip() for s in com.html_text("<p>A</p><p>B</p>").split("\n") if s.strip()] == ["A", "B"]


# Hacker News stories ----------------------------------------------------------------------------------------------

def test_hacker_news_stories_keep_only_resolved_own_programs():
    hits = [
        hn_hit(101, "Show HN: Acme – we just launched our affiliate program for SaaS founders", f"{day(2)}T10:00:00.000Z",
               url="https://acme.com/"),
        hn_hit(102, "We launched our referral program for Widget", f"{day(3)}T10:00:00.000Z",
               url="https://github.com/acme/widget", story_text="<p>Try it at widget.io</p>"),
        hn_hit(103, "Ask HN: Which affiliate program should I join?", f"{day(1)}T10:00:00.000Z"),
        hn_hit(104, "Show HN: Parked – our affiliate program", f"{day(1)}T10:00:00.000Z", url="https://parked-acme.com/"),
        hn_hit(105, "We launched our casino affiliate program", f"{day(1)}T10:00:00.000Z", url="https://casino.example/"),
        hn_hit(106, "We launched our affiliate program", "2026-01-05T10:00:00.000Z", url="https://acme.com/"),
        hn_hit(107, "Show HN: Fakeco", f"{day(1)}T10:00:00.000Z", url="https://github.com/x/y",
               story_text="We launched our affiliate program"),
        # the domain is only mentioned in the body; the title does not name the brand, so nothing is guessed
        hn_hit(108, "Managed hosting, 60-second provisioning", f"{day(1)}T10:00:00.000Z",
               story_text="<p>We launched our affiliate program at clawhosters.com/affiliate</p>"),
    ]
    http = FakeHTTP({
        "author_whoishiring": js({"hits": []}),
        "search_by_date": js({"hits": hits}),  # the same hits for every phrase: de-duplicated by objectID
        "https://acme.com/": page(ACME_HOME),
        "https://widget.io/": page(WIDGET_HOME),
        "https://parked-acme.com/": page(PARKED_HOME),
    })
    said = []
    got = run(make_ctx(http, say=said.append))
    assert sorted(c_.domain for c_ in got) == ["acme.com", "widget.io"]
    acme = next(c_ for c_ in got if c_.domain == "acme.com")
    assert acme.linkedin_url == "https://www.linkedin.com/company/acme" and acme.require_us is True
    assert acme.list == "affiliate" and acme.signal.kind == "community_post" and acme.signal.platform == "hackernews"
    assert acme.signal.url == "https://news.ycombinator.com/item?id=101" and acme.signal.signal_date == day(2)
    assert acme.signal.person_name is None and acme.signal.person_url is None
    assert all(len(c_.signal.quote) <= 400 for c_ in got)
    assert not any("Pat Example" in s for s in said)
    assert any("tags=story" in u and "numericFilters=created_at_i%3E" in u for u in http.calls)
    assert not any("Pat Example" in str(c_.__dict__) for c_ in got)


def test_a_non_show_hn_story_cannot_anchor_a_news_site():
    """A third-person launch story that links a news site must not list the news site: the story URL is checked
    against the title, and a Show HN is the only post whose link may be taken as the maker's own site."""
    hits = [hn_hit(201, "Acme launched an affiliate program", day(2), url="https://techcrunch.com/2026/acme-affiliates"),
            hn_hit(202, "Show HN: Acme – affiliate program for SaaS", day(2), url="https://techcrunch.com/"),
            hn_hit(203, "Acme launched an affiliate program", day(2), url="https://forbes.com/sites/acme-affiliates")]
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": hits}),
                     "https://techcrunch.com/": page("<html><head><title>TechCrunch</title></head></html>")})
    assert run(make_ctx(http)) == []
    assert not any(u.startswith("https://techcrunch.com/") and "2026/acme" in u for u in http.calls)


def test_quotes_never_carry_an_email_address():
    hits = [hn_hit(301, "Show HN: Acme – we launched our affiliate program", day(2), url="https://acme.com/",
                   story_text="<p>Questions? mail pat@acme.com about the affiliate program.</p>")]
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": hits}),
                     "https://acme.com/": page(ACME_HOME)})
    got = run(make_ctx(http))
    assert got and "@" not in got[0].signal.quote


# hiring rows ------------------------------------------------------------------------------------------------------

def test_hiring_rows_keep_affiliate_roles_at_us_companies():
    comments = [
        {"objectID": 9001, "parent_id": 49922569, "created_at": f"{day(8)}T09:00:00.000Z",
         "comment_text": ('<p>Column ( <a href="https://column.com">https://column.com</a> ) | Affiliate Manager | '
                          'San Francisco, CA | Full-time</p><p>Launch our affiliate program with us. '
                          'Email jane@column.com</p>')},
        {"objectID": 9002, "parent_id": 49922569, "created_at": f"{day(8)}T09:00:00.000Z",
         "comment_text": "<p>Partner Co | Partnerships Manager | Remote, US</p><p>Own the bank partnership pipeline.</p>"},
        {"objectID": 9003, "parent_id": 49922569, "created_at": f"{day(8)}T09:00:00.000Z",
         "comment_text": "<p>Maple Labs | Affiliate Manager | Toronto, Canada</p>"},
        {"objectID": 9004, "parent_id": 49922569, "created_at": f"{day(8)}T09:00:00.000Z",
         "comment_text": ("<p>Ledger Funds | Referral Partner Manager | NYC</p><p>We fund small businesses with a merchant "
                          "cash advance and ISO referral partners.</p>")},
        {"objectID": 9005, "parent_id": 9001, "created_at": f"{day(8)}T09:00:00.000Z",
         "comment_text": "<p>Reply: thanks Column!</p>"},
        {"objectID": 9006, "parent_id": 49922569, "created_at": f"{day(8)}T09:00:00.000Z",
         "comment_text": ("<p>Reactor Labs | Head of Partnerships | Remote within USA | We are building our referral "
                          "program for creators.</p>")},
        {"objectID": 9007, "parent_id": 49922569, "created_at": f"{day(8)}T09:00:00.000Z",
         "comment_text": "<p>Hi there, we are hiring engineers</p>"},
    ]
    http = FakeHTTP({
        "author_whoishiring": js({"hits": [
            {"objectID": 49922569, "title": "Ask HN: Who is hiring? (October 2026)", "created_at": f"{day(9)}T15:00:00.000Z"},
            {"objectID": 1, "title": "Ask HN: Who wants to be hired? (October 2026)", "created_at": f"{day(9)}T15:00:00.000Z"},
        ]}),
        "story_49922569": js({"hits": comments}),
        "https://column.com/": page(COLUMN_HOME),
        "search_by_date": js({"hits": []}),
    })
    got = run(make_ctx(http))
    by = {c_.domain or c_.company_name: c_ for c_ in got}
    assert set(by) == {"column.com", "Reactor Labs"}
    column = by["column.com"]
    assert column.signal.kind == "job" and column.signal.platform == "hackernews"
    assert column.signal.url == "https://news.ycombinator.com/item?id=9001" and column.signal.signal_date == day(8)
    assert column.linkedin_url == "https://www.linkedin.com/company/column-bank"
    assert "jane@" not in column.signal.quote and column.signal.quote.startswith("Column ( https://column.com )")
    assert by["Reactor Labs"].domain is None and by["Reactor Labs"].require_us is True


# Indie Hackers, Product Hunt, Shopify Community (Google) ----------------------------------------------------------

def test_forum_and_launch_posts_resolve_only_with_own_domain():
    answers = {
        "site:indiehackers.com": {"results": [
            {"url": "https://www.indiehackers.com/post/launched-widgetco-a-tool-xyz-1?utm_source=x",
             "title": "Launched WidgetCo, a tool with a live affiliate program",
             "description": f"3 days ago ... I built WidgetCo, and we launched our affiliate program last week. "
                            f"Find us at widgetco.com."},
            {"url": "https://www.indiehackers.com/post/we-launched-referral-2",
             "title": "We launched our referral program",
             "description": "2 days ago ... We launched our referral program for our note app."},
            {"url": "https://www.indiehackers.com/post/undated-3", "title": "We launched our affiliate program",
             "description": "We launched our affiliate program at noteapp.com recently."},
            {"url": "https://www.indiehackers.com/leaderboard/best-affiliate-tools", "title": "Best affiliate tools",
             "description": "3 days ago ... We launched our affiliate program."},
        ]},
        "site:producthunt.com": {"results": [
            {"url": "https://www.producthunt.com/products/pexo-2?utm_source=x",
             "title": "Pexo - Launched our affiliate program | Product Hunt",
             "description": "5 days ago ... We launched our affiliate program for creators on Pexo."},
            {"url": "https://www.producthunt.com/posts/roadmap-x", "title": "Roadmap - we launched our referral program",
             "description": "3 days ago ... We launched our referral program."},
        ]},
        "site:community.shopify.com": {"results": [
            {"url": "https://community.shopify.com/c/shopify-discussion/we-launched-our-affiliate-program/td-p/7654321",
             "title": "We launched our affiliate program for Shopfit",
             "description": "4 days ago ... We launched our affiliate program for our store, Shopfit."},
        ]},
    }
    http = FakeHTTP({
        "author_whoishiring": js({"hits": []}),
        "search_by_date": js({"hits": []}),
        "producthunt.com/products/pexo-2": page(
            '<html><head><title>Pexo | Product Hunt</title></head><body>'
            '<a href="https://www.producthunt.com/r/ABC123?utm_campaign=x">Visit website</a>'
            '<a href="https://twitter.com/pexo">X</a></body></html>'),
        "producthunt.com/r/ABC123": page(PEXO_HOME, final="https://pexo.ai/?ref=ph"),
        "https://pexo.ai/": page(PEXO_HOME),
        "td-p/7654321": page('<html><body><a href="https://shopfit.com/">Shopfit</a>'
                             '<a href="https://shopify.dev/docs">Docs</a></body></html>'),
        "https://shopfit.com/": page(SHOPFIT_HOME),
        "https://widgetco.com/": page(WIDGETCO_HOME),
    })
    sc = FakeSC(answers)
    got = run(make_ctx(http, sc))
    assert sorted({(c_.domain, c_.signal.platform) for c_ in got}) == [
        ("pexo.ai", "producthunt"), ("shopfit.com", "shopify"), ("widgetco.com", "indiehackers")]
    widget = next(c_ for c_ in got if c_.domain == "widgetco.com")
    assert widget.signal.url == "https://www.indiehackers.com/post/launched-widgetco-a-tool-xyz-1"
    assert widget.signal.signal_date == day(3) and widget.signal.kind == "community_post"
    assert widget.linkedin_url == "https://www.linkedin.com/company/widgetco"
    assert all(c_.signal.person_name is None for c_ in got)
    # Indie Hackers bodies are never fetched; Product Hunt and Shopify pages are
    assert not any("indiehackers.com/post" in u for u in http.calls)
    assert len(sc.calls) == 6  # DEFAULTS max_google_queries


def test_budget_spent_means_no_scrapecreators_call():
    sc = FakeSC({"site:indiehackers.com": {"results": [{"url": "https://www.indiehackers.com/post/x-1",
                                                       "title": "t", "description": f"{day(1)} x"}]}})
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": []})})
    got = run(make_ctx(http, sc, budget=0))
    assert sc.calls == [] and got == []


def test_page_cap_stops_every_free_read_but_not_the_search():
    sc = FakeSC({})
    http = FakeHTTP({"search_by_date": js({"hits": [hn_hit(1, "x", day(1))]}),
                     "author_whoishiring": js({"hits": []})})
    ctx = make_ctx(http, sc, cfg={"http_cap": 0})
    assert run(ctx) == [] and http.calls == []


def test_one_stage_failing_keeps_the_others():
    hits = [hn_hit(101, "Show HN: Acme – we launched our affiliate program", day(2), url="https://acme.com/")]
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": hits}),
                     "https://acme.com/": page(ACME_HOME)})
    sc = FakeSC({"site:indiehackers.com": RuntimeError("layout changed")})
    said = []
    got = run(make_ctx(http, sc, say=said.append))
    assert [c_.domain for c_ in got] == ["acme.com"]
    assert any("google" in s or "forum" in s or "failed (RuntimeError)" in s for s in said)


def test_no_person_names_in_progress_lines():
    hits = [hn_hit(101, "Show HN: Acme – we launched our affiliate program", day(2), url="https://acme.com/",
                   author="Pat Example")]
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": hits}),
                     "https://acme.com/": page(ACME_HOME)})
    said = []
    run(make_ctx(http, say=said.append))
    assert said and not any("Pat Example" in s for s in said)


# regression cases from the adversarial review ---------------------------------------------------------------------

def test_brand_names_are_whole_words_not_substrings():
    assert com._names("Shipping labels: we launched our affiliate program", "ship") is False
    assert com._names("Ship Co launches an affiliate program", "ship") is True
    assert com._names("Widget Co launched our affiliate program", "widgetco") is True
    assert com._names("Acme launched an affiliate program", "") is False


def test_show_hn_referral_software_is_dropped_before_any_fetch():
    hits = [hn_hit(401, "Show HN: Referral program software for SaaS teams", day(1), url="https://refertool.io/")]
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": hits}),
                     "https://refertool.io/": page("<html><head><title>Refertool | Referral program software"
                                                   "</title></head></html>")})
    assert run(make_ctx(http)) == []
    assert not any("refertool.io" in u for u in http.calls)


def test_homepage_that_names_itself_a_vendor_is_rejected():
    """The post passes the gate, but the company's own title says it sells referral program software."""
    hits = [hn_hit(402, "Show HN: Refertool – we launched our referral program for teams", day(1),
                   url="https://refertool.io/")]
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": hits}),
                     "https://refertool.io/": page("<html><head><title>Refertool | Referral program software"
                                                   "</title></head></html>")})
    assert run(make_ctx(http)) == []
    assert "https://refertool.io/" in http.calls  # fetched, then rejected on its own title


def test_educational_title_from_a_vendor_blog_is_dropped():
    hits = [hn_hit(403, "How to launch an affiliate program (AffiliateKit guide)", day(1),
                   url="https://affiliatekit.io/guide")]
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": hits}),
                     "https://affiliatekit.io/": page(ACME_HOME)})
    assert run(make_ctx(http)) == []
    assert not any("affiliatekit" in u for u in http.calls)


def test_known_domain_named_only_in_the_body_is_not_attributed():
    answers = {"site:indiehackers.com": {"results": [
        {"url": "https://www.indiehackers.com/post/we-launched-our-affiliate-program-9",
         "title": "We launched our affiliate program for our Shopify app",
         "description": "2 days ago ... Works with klaviyo.com integrations and we launched our affiliate program."}]}}
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": []})})
    assert run(make_ctx(http, FakeSC(answers), known=("klaviyo.com",))) == []
    assert not any("klaviyo" in u for u in http.calls)


def test_known_domain_named_in_the_title_still_resolves_without_a_fetch():
    answers = {"site:indiehackers.com": {"results": [
        {"url": "https://www.indiehackers.com/post/widgetco-launch-4",
         "title": "WidgetCo launched our affiliate program",
         "description": "2 days ago ... We launched our affiliate program for WidgetCo users. Find us at widgetco.com."}]}}
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": []})})
    got = run(make_ctx(http, FakeSC(answers), known=("widgetco.com",), cfg={}))
    assert [c_.domain for c_ in got] == ["widgetco.com"] and got[0].linkedin_url is None
    assert not any("https://widgetco.com/" in u for u in http.calls)


def test_malformed_google_item_drops_only_itself():
    results = {"results": [
        {"url": "http://[bad-url", "title": "We launched our affiliate program", "description": "1 day ago ... x"},
        "junk",
        {"url": "https://www.indiehackers.com/post/launched-widgetco-1",
         "title": "We launched our affiliate program at WidgetCo",
         "description": "2 days ago ... we launched our affiliate program. Find us at widgetco.com."},
    ]}
    sc = FakeSC({"site:indiehackers.com": results})
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": []}),
                     "https://widgetco.com/": page(WIDGETCO_HOME)})
    said = []
    got = run(make_ctx(http, sc, say=said.append))
    assert [c_.domain for c_ in got] == ["widgetco.com"]
    assert any("malformed" in s for s in said)


def test_malformed_hacker_news_rows_drop_only_themselves():
    hits = ["junk", {"objectID": 5, "created_at": 12345, "title": "x"},
            hn_hit(601, "Show HN: Acme – we launched our affiliate program", day(1), url="https://acme.com/")]
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": hits}),
                     "https://acme.com/": page(ACME_HOME)})
    assert [c_.domain for c_ in run(make_ctx(http))] == ["acme.com"]


def test_non_object_json_is_no_rows_not_a_crash():
    http = FakeHTTP({"author_whoishiring": js([1, 2]), "search_by_date": js(["x"])})
    assert run(make_ctx(http)) == []


def test_company_only_hiring_rows_need_a_us_location():
    comments = [
        {"objectID": 9100, "parent_id": 49922569, "created_at": f"{day(8)}T09:00:00.000Z",
         "comment_text": "<p>Maplewood Labs | Affiliate Manager | Remote</p><p>Launch our affiliate program.</p>"},
        {"objectID": 9101, "parent_id": 49922569, "created_at": f"{day(8)}T09:00:00.000Z",
         "comment_text": "<p>Maplewood Labs | Affiliate Manager | Remote, USA</p><p>Launch our affiliate program.</p>"},
    ]
    http = FakeHTTP({"author_whoishiring": js({"hits": [
        {"objectID": 49922569, "title": "Ask HN: Who is hiring? (October 2026)", "created_at": f"{day(9)}T15:00:00.000Z"},
    ]}), "story_49922569": js({"hits": comments}), "search_by_date": js({"hits": []})})
    got = run(make_ctx(http))
    assert [c_.company_name for c_ in got] == ["Maplewood Labs"]
    assert got[0].signal.url.endswith("item?id=9101")


def test_one_bad_stage_does_not_crowd_out_another_under_the_cap():
    hits = [hn_hit(700 + i, f"Show HN: Acme{i} – we launched our affiliate program", day(1),
                   url=f"https://acme{i}.com/") for i in range(4)]
    routes = {"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": hits})}
    for i in range(4):
        routes[f"https://acme{i}.com/"] = page(f"<html><head><title>Acme{i} | Affiliate program</title></head></html>")
    sc = FakeSC({"site:indiehackers.com": {"results": [
        {"url": "https://www.indiehackers.com/post/widgetco-launch-4", "title": "WidgetCo launched our affiliate program",
         "description": "2 days ago ... We launched our affiliate program for WidgetCo users. Find us at widgetco.com."}]}})
    routes["https://widgetco.com/"] = page(WIDGETCO_HOME)
    got = run(make_ctx(FakeHTTP(routes), sc, cfg={"max_candidates": 2}))
    assert len(got) == 2
    assert {c_.signal.platform for c_ in got} == {"hackernews", "indiehackers"}


def test_live_verified_buyer_pattern_resolves_from_a_story_link():
    """The pattern a live 365-day run confirmed: a buyer verb with the program noun, a brand-named title, and a link
    to the company's own homepage (blink.new)."""
    hits = [hn_hit(801, "Blink is launching affiliate program with $100 per referral payout", day(2),
                   url="https://blink.new/")]
    http = FakeHTTP({"author_whoishiring": js({"hits": []}), "search_by_date": js({"hits": hits}),
                     "https://blink.new/": page("<html><head><title>Blink | Build apps with AI</title></head>"
                                                '<body><a href="https://www.linkedin.com/company/blink-new/">in</a>'
                                                "</body></html>")})
    got = run(make_ctx(http))
    assert [(c_.domain, c_.linkedin_url) for c_ in got] == [
        ("blink.new", "https://www.linkedin.com/company/blink-new")]
    assert got[0].signal.platform == "hackernews" and got[0].signal.signal_date == day(2)
