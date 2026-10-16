"""finance_iso source: offline tests on synthetic payloads and pages (no network). Names and domains are synthetic (.test)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lead_radar.programs import detect, signals as sg, sources as srcs
from lead_radar.programs.sources import finance_iso as fi

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
FIX = Path(__file__).parent / "fixtures" / "sources" / "finance_iso"
CFG = srcs.settings("finance_iso", fi, sg.load())
SIGCFG = sg.load()
POST_PHRASES = ['"now accepting new ISOs" funding', '"seeking ISOs" funding', '"ISO partners wanted" funding',
                '"brokers welcome" funding']

US_BODY = ("<p>Acme Funding provides merchant cash advance and business loans to small businesses. Same day approvals "
           "fund merchants fast. Call (212) 555-0100. 100 Main Street, New York, NY 10005. We have funded merchants "
           "across the United States for many years with in-house underwriting.</p>")
CTA_BODY = ("<h2>Become an ISO partner</h2><p>Join our ISO program and earn up to 12 points on every funded deal.</p>")
FILLER = "<p>" + ("Our operations team reviews every application carefully and quickly. " * 14) + "</p>"
MODIFIED = '<meta property="article:modified_time" content="2026-10-01">'


def page(body: str, head: str = "<title>Acme Funding</title>", extra_head: str = "") -> str:
    return f"<html><head>{head}{extra_head}</head><body>{body}</body></html>"


def judge(html, url="https://acmefunding.test/iso/", final=None, snippet="2 days ago ... Acme Funding", status=200):
    return fi.judge_page(html, status, final or url, url, snippet, 30, NOW, SIGCFG)


# contract ---------------------------------------------------------------------------------------------------------

def test_contract():
    import inspect
    assert fi.NAME == "finance_iso" and isinstance(fi.DEFAULTS, dict)
    assert {"google_queries", "post_queries", "http_cap", "max_pages", "person_posts"} <= set(fi.DEFAULTS)
    assert inspect.iscoroutinefunction(fi.collect)
    assert fi.RANK.keys() == {"company_post", "person_post", "partner_program"}
    assert all(k in sg.KINDS for k in fi.RANK)


# web pages --------------------------------------------------------------------------------------------------------

def test_buyer_page_kept_with_verbatim_cta_and_canonical_url():
    html = (FIX / "buyer_funder.html").read_text()
    hit, why = fi.judge_page(html, 200, "https://www.examplefunding.test/iso/?utm_source=ad",
                             "https://www.examplefunding.test/iso/", "3 days ago ... ISO Partner Program", 30, NOW, SIGCFG)
    assert why == "kept" and hit.kind == "partner_program"
    assert hit.domain == "examplefunding.test" and hit.url == "https://www.examplefunding.test/iso/"
    assert hit.date == "2026-10-07"
    assert hit.quote == "Become an ISO partner and earn 12 points on every funded deal."
    assert len(hit.quote) <= 400


def test_nav_only_cta_is_dropped():
    html = (FIX / "nav_only_funder.html").read_text()
    hit, why = fi.judge_page(html, 200, "https://acmecapital.test/", "https://acmecapital.test/", "2 days ago", 30, NOW,
                             SIGCFG)
    assert hit is None and why == "no recruiting copy in body"


def test_footer_restricted_industry_list_does_not_drop_a_funder():
    html = page(CTA_BODY + US_BODY + "<footer>We do not fund gambling, adult or crypto businesses.</footer>")
    hit, why = judge(html)
    assert why == "kept" and hit.domain == "acmefunding.test"


def test_nested_chrome_is_removed_without_error():
    html = page("<header><div><nav><ul><li>ISO partners</li></ul></nav></div></header>" + CTA_BODY + US_BODY)
    hit, why = judge(html)
    assert why == "kept" and "ISO partners" not in hit.quote


def test_program_link_on_the_landing_page_is_followed_once():
    landing = page("<p>" + ("Acme Funding provides business loans for owners. " * 6) + "</p>"
                   "<main><a href='/partners/'>Partner with us</a></main>")
    partner = page(CTA_BODY + US_BODY, extra_head=MODIFIED)
    pages = {"https://acmefunding.test/": (landing, 200, None),
             "https://acmefunding.test/partners/": (partner, 200, None)}
    sc = FakeSC({"/v1/google/search": lambda p: {"results": [
        {"title": "Acme Funding", "url": "https://acmefunding.test/", "description": ""}]}})
    cands = run(FakeCtx(sc, pages))
    assert len(cands) == 1
    assert cands[0].signal.url == "https://acmefunding.test/partners/" and cands[0].domain == "acmefunding.test"


def test_login_link_is_not_followed_when_a_program_link_exists():
    landing = page("<p>" + ("Acme Funding provides business loans for owners. " * 6) + "</p>"
                   "<main><a href='/broker-login'>Broker login</a> <a href='/partners/'>Partner with us</a></main>")
    pages = {"https://acmefunding.test/": (landing, 200, None),
             "https://acmefunding.test/broker-login": (page("<p>Sign in to your account.</p>"), 200, None),
             "https://acmefunding.test/partners/": (page(CTA_BODY + US_BODY, extra_head=MODIFIED), 200, None)}
    sc = FakeSC({"/v1/google/search": lambda p: {"results": [
        {"title": "Acme Funding", "url": "https://acmefunding.test/", "description": ""}]}})
    ctx = FakeCtx(sc, pages)
    cands = run(ctx)
    assert "https://acmefunding.test/broker-login" not in ctx.fetched
    assert [c.signal.url for c in cands] == ["https://acmefunding.test/partners/"]


def test_teaser_on_a_non_program_page_is_replaced_by_its_program_page():
    about = page("<h2>About us</h2><p>Acme Funding provides merchant cash advance and business loans to owners across "
                 "the United States in New York, NY 10005. We fund owners for many years.</p>"
                 "<p><a href='/partners/'>Partner with us</a> Learn about our ISO program and the support for "
                 "financing partners.</p>")
    pages = {"https://acmefunding.test/about-us/": (about, 200, None),
             "https://acmefunding.test/partners/": (page(CTA_BODY + US_BODY, extra_head=MODIFIED), 200, None)}
    sc = FakeSC({"/v1/google/search": lambda p: {"results": [
        {"title": "About", "url": "https://acmefunding.test/about-us/", "description": "2 days ago ... About"}]}})
    cands = run(FakeCtx(sc, pages))
    assert [c.signal.url for c in cands] == ["https://acmefunding.test/partners/"]


def test_teaser_is_kept_as_it_is_when_the_program_page_is_undated():
    about = page("<h2>About us</h2><p>Acme Funding provides merchant cash advance and business loans to owners across "
                 "the United States in New York, NY 10005. We fund owners for many years.</p>"
                 "<p><a href='/partners/'>Partner with us</a> Learn about our ISO program and the support for "
                 "financing partners.</p>")
    pages = {"https://acmefunding.test/about-us/": (about, 200, None),
             "https://acmefunding.test/partners/": (page(CTA_BODY + US_BODY), 200, None)}
    sc = FakeSC({"/v1/google/search": lambda p: {"results": [
        {"title": "About", "url": "https://acmefunding.test/about-us/", "description": "2 days ago ... About"}]}})
    cands = run(FakeCtx(sc, pages))
    assert [c.signal.url for c in cands] == ["https://acmefunding.test/about-us/"]


@pytest.mark.parametrize("html,final_url,result_url,why", [
    (page(CTA_BODY + US_BODY, head="<title>Acme is for sale | HugeDomains</title>"),
     "https://acmefunding.test/iso/", "https://acmefunding.test/iso/", "parked domain"),
    (page(CTA_BODY + US_BODY, head="<title>Just a moment...</title>"),
     "https://acmefunding.test/iso/", "https://acmefunding.test/iso/", "blocked or thin page"),
    (page("<p>ISO program. Business loans.</p>"),
     "https://acmefunding.test/iso/", "https://acmefunding.test/iso/", "blocked or thin page"),
    (page(CTA_BODY + US_BODY),
     "https://elsewhere.test/", "https://acmefunding.test/iso/", "redirected off host"),
    (page(CTA_BODY + US_BODY),
     "https://acmefunding.test/blog/how-to-become-an-iso", "https://acmefunding.test/blog/how-to-become-an-iso",
     "article or directory page"),
    (page(CTA_BODY + US_BODY),
     "https://acmefunding.test/lenders/acme-capital", "https://acmefunding.test/lenders/acme-capital",
     "article or directory page"),
    (page(CTA_BODY + US_BODY, head="<title>Example Partner Funding | ISO</title>"),
     "https://acmefunding.test/iso/", "https://acmefunding.test/iso/", "do not prospect"),
])
def test_unsuitable_pages_are_dropped(html, final_url, result_url, why):
    got_hit, got = fi.judge_page(html, 200, final_url, result_url, "", 30, NOW, SIGCFG)
    assert got_hit is None and got == why


def test_http_error_and_status_are_dropped():
    assert judge(page(CTA_BODY + US_BODY), status=404)[1] == "http error"


def test_no_funding_term_is_dropped():
    html = page(CTA_BODY + "<p>" + ("We are a friendly team that loves helping small companies grow. " * 5) + "</p>")
    assert judge(html)[1] == "no funding term"


def test_payments_iso_needs_a_funding_term_near_the_cta():
    payments_only = page("<p>Become an ISO partner for merchant services and card processing.</p>" + FILLER +
                         "<p>Acme Funding provides business loans for owners.</p>")
    assert judge(payments_only)[1] == "payments ISO without funding term"
    both = page("<p>Join our ISO program for merchant cash advance and merchant services.</p>" + US_BODY)
    assert judge(both)[1] == "kept"


def test_cpa_referral_only_is_dropped():
    html = page("<p>Join our referral partner program for CPA and accountant referrals and earn commissions.</p>" +
                FILLER + "<p>Acme Funding provides business loans for owners.</p>")
    assert judge(html)[1] == "CPA referral only"


def test_non_us_only_is_dropped_and_us_plus_canada_is_kept():
    canada_only = page(CTA_BODY + "<p>Acme Funding is based in Toronto, Ontario and funds deals across Canada for "
                                  "business loans and merchant cash advance. Call +44 20 7946 0000 for details.</p>")
    assert judge(canada_only)[1] == "not US"
    both = page(CTA_BODY + "<p>Acme Funding provides business loans from Toronto, Ontario and New York, NY 10005 "
                           "for merchant cash advance clients.</p>")
    assert judge(both)[1] == "kept"


def test_non_us_city_beside_a_us_state_needs_linkedin_to_confirm_us():
    html = page(CTA_BODY + "<p>Acme Funding provides business loans and merchant cash advance from its Toronto, Ontario "
                           "office and its Dallas, Texas office. Owners across the country trust our team for funding.</p>")
    hit, why = judge(html)
    assert why == "kept" and hit.us_seen is False
    assert fi._candidate(hit).require_us is True


def test_snippet_dates_and_page_modified_dates():
    assert fi._snippet_date("3 days ago ... ISO", NOW) == "2026-10-07"
    assert fi._snippet_date("Sep 21, 2026 — Join our ISO program", NOW) == "2026-09-21"
    assert fi._snippet_date("Join our ISO program for merchant cash advance", NOW) is None
    stale = judge(page(CTA_BODY + US_BODY), snippet="Jul 1, 2026 — Join our ISO program")
    assert stale[1] == "stale snippet date"
    modified = page(CTA_BODY + US_BODY, extra_head='<meta property="article:modified_time" content="2026-10-05T09:00:00Z">')
    assert judge(modified, snippet="")[0].date == "2026-10-05"
    old = page(CTA_BODY + US_BODY, extra_head='<meta property="article:modified_time" content="2024-02-01">')
    assert judge(old, snippet="") == (None, "undated (no date in window)")


def test_undated_page_is_dropped():
    assert judge(page(CTA_BODY + US_BODY), snippet="") == (None, "undated (no date in window)")


def test_three_day_window_keeps_only_dated_evidence_inside_it():
    html = page(CTA_BODY + US_BODY)
    assert fi.judge_page(html, 200, "https://acmefunding.test/iso/", "https://acmefunding.test/iso/", "",
                         3, NOW, SIGCFG) == (None, "undated (no date in window)")
    recent = fi.judge_page(html, 200, "https://acmefunding.test/iso/", "https://acmefunding.test/iso/",
                           "2 days ago ... Acme Funding", 3, NOW, SIGCFG)
    assert recent[1] == "kept" and recent[0].date == "2026-10-08"
    stale = fi.judge_page(html, 200, "https://acmefunding.test/iso/", "https://acmefunding.test/iso/",
                          "Sep 25, 2026 — Join our ISO program", 3, NOW, SIGCFG)
    assert stale[1] == "stale snippet date"


def test_json_ld_date_comes_only_from_a_top_level_webpage_or_organization():
    nested_article = page(CTA_BODY + US_BODY, extra_head='<script type="application/ld+json">'
                          '{"@type": "Article", "dateModified": "2026-10-06"}</script>')
    assert judge(nested_article, snippet="") == (None, "undated (no date in window)")
    webpage = page(CTA_BODY + US_BODY, extra_head='<script type="application/ld+json">'
                   '{"@type": "WebPage", "dateModified": "2026-10-06T08:00:00+00:00"}</script>')
    assert judge(webpage, snippet="")[0].date == "2026-10-06"
    graph = page(CTA_BODY + US_BODY, extra_head='<script type="application/ld+json">{"@graph": ['
                 '{"@type": "Article", "dateModified": "2026-01-01"}, '
                 '{"@type": "WebPage", "dateModified": "2026-10-04"}]}</script>')
    assert judge(graph, snippet="")[0].date == "2026-10-04"


def test_cta_quote_skips_page_chrome_and_language_switchers():
    html = page('<div class="lang">Skip to content English Español 中文 한국어 347-442-7999</div>'
                '<div><p>Join the Example broker and ISO network.</p></div>' + US_BODY)
    hit, why = judge(html)
    assert why == "kept" and hit.quote == "Join the Example broker and ISO network."
    same_block = page("<p>Skip to content ISO &amp; Broker Partners Join the Example broker and ISO network.</p>"
                      + US_BODY)
    quote = judge(same_block)[0].quote
    assert "Skip to content" not in quote and "Join the Example broker and ISO network." in quote


def test_brand_adjective_on_the_sites_own_page_is_not_a_third_party_program():
    html = page("<p>Join our Premier ISO Partner Program and earn 12 points on every funded deal.</p>" + US_BODY)
    assert judge(html)[1] == "kept"


def test_snippet_dates_are_used_for_the_date_window_only():
    assert judge(page(CTA_BODY + US_BODY), snippet="Sep 21, 2026 — Join our ISO program")[0].date == "2026-09-21"


# LinkedIn posts ---------------------------------------------------------------------------------------------------

def post(desc, author_url, author_name="Pat Example", date="2026-10-05T14:00:00.000Z",
         url="https://www.linkedin.com/posts/pat-example_iso-activity-7512981802798465025-AbCd"):
    return {"url": url, "datePublished": date, "description": desc,
            "author": {"name": author_name, "url": author_url}}


COMPANY = "https://www.linkedin.com/company/test-advance/"
PERSON = "https://www.linkedin.com/in/pat-example"


def test_company_post_becomes_a_company_post_with_a_canonical_url():
    desc = "Now accepting new ISOs. Our merchant cash advance partners get same day funding. ISO partnerships are open."
    hit, why = fi.judge_post(post(desc, COMPANY, author_name="Test Advance"), 30, NOW, SIGCFG)
    assert why == "kept" and hit.kind == "company_post"
    assert hit.linkedin_url == "https://www.linkedin.com/company/test-advance"
    assert hit.url == "https://www.linkedin.com/feed/update/urn:li:activity:7512981802798465025/"
    assert "Now accepting new ISOs" in hit.quote and hit.date == "2026-10-05"


def test_raw_post_slug_never_reaches_the_signal_url():
    hit, _ = fi.judge_post(post("We are now accepting ISOs for merchant cash advance deals. Apply at samplefinancial.com",
                                PERSON), 30, NOW, SIGCFG)
    assert "pat-example" not in hit.url and "urn:li:activity:7512981802798465025" in hit.url


def test_person_post_kept_only_when_it_names_the_funder_in_its_call_sentence():
    with_domain = fi.judge_post(post("We are now accepting new ISOs for merchant cash advance deals. "
                                     "Apply at samplefinancial.com", PERSON), 30, NOW, SIGCFG)[0]
    assert with_domain.kind == "person_post" and with_domain.domain == "samplefinancial.com"
    assert with_domain.person_name == "Pat Example" and with_domain.person_url == PERSON
    with_name = fi.judge_post(post("We are now accepting new ISOs for merchant cash advance deals at Sample Funding Group.",
                                   PERSON), 30, NOW, SIGCFG)[0]
    assert with_name.company_name == "Sample Funding Group" and with_name.domain is None
    no_funder = fi.judge_post(post("We are now accepting new ISOs for merchant cash advance deals.", PERSON), 30, NOW, SIGCFG)
    assert no_funder == (None, "person post names no funder")


def test_person_post_needs_the_funder_in_the_same_sentence_as_the_call_to_join():
    away = fi.judge_post(post("We are now accepting new ISOs for merchant cash advance deals. "
                              "Our blog at samplefinancial.com covers rates.", PERSON), 30, NOW, SIGCFG)
    assert away == (None, "person post names no funder")
    no_ownership = fi.judge_post(post("We have a great team. Now accepting new ISOs for merchant cash advance deals. "
                                      "Apply at samplefinancial.com", PERSON), 30, NOW, SIGCFG)
    assert no_ownership == (None, "person post without first-person ownership")


@pytest.mark.parametrize("desc,author,why", [
    ("Use my link to join our ISO program for merchant cash advance deals. samplefinancial.com", PERSON, "individual_affiliate"),
    ("Now accepting ISOs: our CRM for brokers and merchant cash advance funding.", COMPANY, "vendor (leads, data, CRM or software)"),
    ("Now accepting ISOs. ISO 9001 certified merchant cash advance partners.", COMPANY, "ISO certification collision"),
    ("Live MCA leads for brokers. Now accepting ISOs for merchant cash advance.", COMPANY, "vendor (leads, data, CRM or software)"),
    ("Now accepting ISOs for merchant cash advance and debt settlement services.", COMPANY, "settlement or defence firm"),
    ("Our affiliate program pays creators. Business loans for merchants.", COMPANY, "no recruiting copy"),
    ("We just shipped a new feature. Business loans for merchants.", COMPANY, "no recruiting copy"),
])
def test_post_filters(desc, author, why):
    assert fi.judge_post(post(desc, author), 30, NOW, SIGCFG) == (None, why)


def test_stale_post_is_dropped_and_undated_post_is_kept():
    stale = fi.judge_post(post("Now accepting ISOs for merchant cash advance.", COMPANY, date="2026-07-01T00:00:00Z"),
                          30, NOW, SIGCFG)
    assert stale == (None, "stale post")
    undated = fi.judge_post(dict(post("Now accepting ISOs for merchant cash advance.", COMPANY, date=None),
                                 url="https://www.linkedin.com/feed/update/urn:li:share:1/"), 30, NOW, SIGCFG)
    assert undated[1] == "no activity id"  # a post without an activity id is never guessed
    dated_by_id = fi.judge_post(post("Now accepting ISOs for merchant cash advance.", COMPANY, date=None), 30, NOW, SIGCFG)
    assert dated_by_id[1] == "kept" and dated_by_id[0].date == "2026-10-05"  # decoded from the activity id, not invented


def test_null_post_fields_do_not_crash_the_judge():
    null_urn = {"url": "https://www.linkedin.com/posts/x_activity-7512981802798465025-AbCd", "activityUrn": None,
                "description": "Now accepting new ISOs for merchant cash advance deals at Test Advance.",
                "author": {"name": "Pat Example", "url": COMPANY}}
    hit, why = fi.judge_post(null_urn, 30, NOW, SIGCFG)
    assert why == "kept" and hit.date == "2026-10-05"
    assert fi.judge_post({"url": None, "activityUrn": None, "description": "x", "author": {"url": COMPANY}},
                         30, NOW, SIGCFG) == (None, "no activity id")
    urn_only = {"activityUrn": "urn:li:activity:7512981802798465025", "url": None,
                "description": "Now accepting new ISOs for merchant cash advance deals.", "author": {"url": COMPANY}}
    assert fi.judge_post(urn_only, 30, NOW, SIGCFG)[0].date == "2026-10-05"


def test_identity_drop_keeps_clean_program_copy_and_drops_each_seller_case():
    assert fi.identity_drop("Become an ISO partner for merchant cash advance", "", SIGCFG) is None
    assert fi.identity_drop("We help businesses get funded with business loans", "", SIGCFG) is None
    assert fi.identity_drop("Credit repair and funding readiness coaching for business loans", "", SIGCFG) == \
        "consultant or credit repair"
    assert fi.identity_drop("Use my code SAVE10 for merchant cash advance", "", SIGCFG) is not None


# collect(): fakes and drivers -------------------------------------------------------------------------------------

class FakeMeter:
    def __init__(self):
        self.n = 0

    def total_credits(self):
        return self.n


class FakeSC:
    """Routes by path; each call costs one credit. Raises OutOfCredits after `fail_after` calls."""

    def __init__(self, routes, fail_after=None):
        self.routes, self.calls, self.meter, self.exhausted, self.fail_after = routes, [], FakeMeter(), False, fail_after

    async def get(self, path, params=None, source="x"):
        self.calls.append((path, dict(params or {})))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            self.exhausted = True
            raise sg.OutOfCredits(f"{path}: out of credits")
        self.meter.n += 1
        if path not in self.routes:
            raise sg.SkipItem(path)
        return self.routes[path](dict(params or {}))


class FakeCtx(srcs.SourceContext):
    def __init__(self, sc, pages, limit=20, http_cap=60, days=30, cfg=None):
        self.lines = []
        self.pages = pages
        self.fetched = []
        super().__init__(sc=sc, http=None, budget=sg.Budget(sc, limit),
                         cfg=cfg or dict(CFG, post_queries=POST_PHRASES), signals_cfg=SIGCFG, days=days,
                         now=NOW, say=self.lines.append, http_cap=http_cap)

    async def get(self, url, role="page"):
        self.fetched.append(url)
        if self.http_used >= self.http_cap:
            return None
        self.http_used += 1
        spec = self.pages.get(url)
        if spec is None:
            return None
        html, status, final = spec
        return detect.Page(url, final or url, status, html, role)


def run(ctx):
    import asyncio
    return asyncio.run(fi.collect(ctx))


RESULTS = [
    {"title": "ISO Program | Example Funding", "url": "https://www.examplefunding.test/iso/?utm_medium=cpc",
     "description": "Sep 21, 2026 — Join our ISO program for merchant cash advance brokers."},
    {"title": "Example Funding ISO", "url": "https://examplefunding.test/iso-partners/", "description": ""},
    {"title": "Acme on LinkedIn", "url": "https://www.linkedin.com/company/acme", "description": "ISO program"},
    {"title": "Facebook group", "url": "https://www.facebook.com/groups/isos", "description": "ISO partners"},
    {"title": "Live MCA Leads", "url": "https://leadsvendor.example.com/", "description": "Live MCA leads for brokers"},
    {"title": "Old ISO page", "url": "https://oldfunder.test/iso/", "description": "Jul 1, 2026 — ISO program"},
]
PAGES = {
    "https://www.examplefunding.test/iso/": (page(CTA_BODY + US_BODY, head="<title>ISO Program | Example Funding</title>"),
                                             200, "https://www.examplefunding.test/iso/"),
    "https://examplefunding.test/iso-partners/": (page(CTA_BODY + US_BODY), 200, "https://examplefunding.test/iso-partners/"),
    "https://oldfunder.test/iso/": (page(CTA_BODY + US_BODY, extra_head=MODIFIED), 200, "https://oldfunder.test/iso/"),
}
POSTS = [
    post("Now accepting new ISOs. Our merchant cash advance partners get same day funding.", COMPANY,
         author_name="Test Advance"),
    post("We are now accepting new ISOs for merchant cash advance deals at Sample Funding Group.", PERSON),
    post("Now accepting ISOs for merchant cash advance deals.", PERSON),  # no named funder, no ownership: dropped
]


def routes(results=RESULTS, posts=POSTS):
    return {"/v1/google/search": lambda p: {"results": results, "credits_charged": 1},
            "/v1/linkedin/search/posts": lambda p: {"posts": posts, "credits_charged": 1}}


def test_collect_end_to_end_keeps_buyers_and_drops_noise():
    sc = FakeSC(routes())
    ctx = FakeCtx(sc, PAGES)
    cands = run(ctx)
    domains = [x.domain for x in cands if x.domain]
    lis = [x.linkedin_url for x in cands if x.linkedin_url]
    assert "examplefunding.test" in domains
    assert "Sample Funding Group" in [x.company_name for x in cands]
    assert lis == ["https://www.linkedin.com/company/test-advance"]
    assert all(x.list == "finance" for x in cands)
    kinds = sorted(x.signal.kind for x in cands)
    assert kinds == ["company_post", "partner_program", "person_post"]
    assert "https://www.facebook.com/groups/isos" not in ctx.fetched
    assert "https://www.linkedin.com/company/acme" not in ctx.fetched
    assert "https://leadsvendor.example.com/" not in ctx.fetched
    assert "https://oldfunder.test/iso/" not in ctx.fetched  # stale snippet is screened out before any fetch


def test_one_candidate_per_domain_and_company():
    dup = RESULTS[:2] + [dict(RESULTS[0], url="https://www.examplefunding.test/iso/?ref=dup")]
    cands = run(FakeCtx(FakeSC(routes(results=dup, posts=[])), PAGES))
    assert sum(1 for x in cands if x.domain == "examplefunding.test") == 1


def test_person_name_never_reaches_the_progress_lines():
    ctx = FakeCtx(FakeSC(routes()), PAGES)
    run(ctx)
    text = "\n".join(ctx.lines)
    assert "Pat Example" not in text and "Sample Funding Group" not in text and "Test Advance" not in text
    assert ctx.lines and ctx.lines[0].startswith("finance_iso: ")


def test_zero_budget_makes_no_search_calls():
    sc = FakeSC(routes())
    cands = run(FakeCtx(sc, PAGES, limit=0))
    assert sc.calls == [] and cands == []


def test_out_of_credits_keeps_what_was_already_found():
    sc = FakeSC(routes(), fail_after=8)  # all 8 Google queries succeed, the first LinkedIn query fails
    ctx = FakeCtx(sc, PAGES)
    cands = run(ctx)
    assert any(x.signal.kind == "partner_program" for x in cands)
    assert not any(x.signal.kind in ("company_post", "person_post") for x in cands)
    assert any("out of credits" in line for line in ctx.lines)


def test_an_unexpected_error_in_the_post_phase_keeps_the_web_hits(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(fi, "judge_post", boom)
    ctx = FakeCtx(FakeSC(routes()), PAGES)
    cands = run(ctx)
    assert any(x.signal.kind == "partner_program" for x in cands)
    assert any("error: RuntimeError" in line for line in ctx.lines)


def test_a_malformed_post_does_not_discard_the_other_posts():
    bad = {"activityUrn": None, "url": None, "author": None, "description": None}
    ctx = FakeCtx(FakeSC(routes(posts=[bad] + POSTS)), PAGES)
    cands = run(ctx)
    assert "https://www.linkedin.com/company/test-advance" in [x.linkedin_url for x in cands]
    assert any(x.signal.kind == "partner_program" for x in cands)


def test_person_posts_can_be_switched_off_by_config():
    ctx = FakeCtx(FakeSC(routes()), PAGES, cfg=dict(CFG, post_queries=POST_PHRASES, person_posts=False))
    cands = run(ctx)
    assert "person_post" not in [x.signal.kind for x in cands]
    assert "company_post" in [x.signal.kind for x in cands]


def test_http_cap_limits_page_fetches():
    ctx = FakeCtx(FakeSC(routes(posts=[])), PAGES, http_cap=1)
    cands = run(ctx)
    assert ctx.http_used == 1 and len(cands) <= 1


def test_queries_carry_the_date_window_and_exclusions():
    sc = FakeSC(routes(posts=[]))
    run(FakeCtx(sc, PAGES, days=30))
    google = [p for path, p in sc.calls if path == "/v1/google/search"]
    assert len(google) == len(CFG["google_queries"]) == 8
    assert all(p["date_posted"] == "last-month" for p in google)
    assert all("-site:linkedin.com" in p["query"] for p in google)


# tuning regressions (each rule was added after a live-sample false positive) --------------------------------------

def test_third_party_program_is_not_the_sites_own():
    html = page("<p>Foo Referral Partner Program gives professionals a way to introduce qualified borrowers.</p>"
                + US_BODY, head="<title>Partner Opportunities - Sample Enterprises</title>")
    assert judge(html, url="https://samplefirm.test/partners/")[1] == "third-party program"


def test_brand_in_the_title_is_the_sites_own():
    html = page("<p>Acme Capital Referral Partner Program pays referral partners on every funded deal.</p>" + US_BODY,
                head="<title>Acme Capital | Partners</title>")
    assert judge(html, url="https://acmecapital.test/partners/")[1] == "kept"


def test_non_program_page_must_speak_for_its_own_company():
    html = page("<p>Join the network of brokers for merchant cash advance deals across the country.</p>" + US_BODY)
    assert judge(html, url="https://acmefunding.test/services/")[1] == "not the company's own recruiting copy"
    assert judge(html, url="https://acmefunding.test/partners/")[1] == "kept"


def test_subscription_offer_is_not_partner_recruiting():
    html = page("<p>Choose your level. Level 1 $125 /month. Broker program resources and a training library.</p>"
                "<p>Merchant cash advance and business loans for owners across the United States, New York, NY 10005.</p>"
                + FILLER)
    assert judge(html, url="https://acmemortgage.test/membership/")[1] == "subscription or membership offer"


def test_lead_generation_company_is_a_vendor():
    assert fi.identity_drop("An advisory and lead-generation front door for equipment financing", "", SIGCFG, full=False) \
        == "vendor (leads, data, CRM or software)"


def test_tool_pages_are_directories_not_partner_pages():
    html = page(CTA_BODY + US_BODY)
    assert judge(html, url="https://levr.example.com/tools/commission-forecaster/")[1] == "article or directory page"


def test_second_page_of_a_domain_is_tried_when_the_first_is_not_a_program():
    sc = FakeSC({"/v1/google/search": lambda p: {"results": [
        {"title": "Acme products", "url": "https://acmefunding.test/products/x", "description": ""},
        {"title": "Acme partners", "url": "https://acmefunding.test/partners/", "description": ""}]}})
    pages = {"https://acmefunding.test/products/x": (page("<p>" + "Acme makes merchant cash advance products. " * 8
                                                          + "</p>"), 200, None),
             "https://acmefunding.test/partners/": (page(CTA_BODY + US_BODY, extra_head=MODIFIED), 200, None)}
    cands = run(FakeCtx(sc, pages, limit=20))
    assert [c.signal.url for c in cands if c.signal.kind == "partner_program"] == ["https://acmefunding.test/partners/"]


# follow-link dating and the US gate ---------------------------------------------------------------------------------

def test_followed_page_is_dated_only_by_its_own_date():
    landing = page("<p>" + ("Acme Funding provides business loans for owners. " * 6) + "</p>"
                   "<main><a href='/partners/'>Partner with us</a></main>")
    sc = FakeSC({"/v1/google/search": lambda p: {"results": [
        {"title": "Acme Funding", "url": "https://acmefunding.test/", "description": "3 days ago ... Acme Funding"}]}})
    dated = {"https://acmefunding.test/": (landing, 200, None),
             "https://acmefunding.test/partners/": (page(CTA_BODY + US_BODY, extra_head=MODIFIED), 200, None)}
    cands = run(FakeCtx(sc, dated))
    assert len(cands) == 1 and cands[0].signal.url == "https://acmefunding.test/partners/"
    assert cands[0].signal.signal_date == "2026-10-01"  # the landing page's snippet date is never borrowed
    undated = {"https://acmefunding.test/": (landing, 200, None),
               "https://acmefunding.test/partners/": (page(CTA_BODY + US_BODY), 200, None)}
    assert run(FakeCtx(sc, undated)) == []


def test_page_without_us_evidence_needs_linkedin_to_confirm_us():
    no_us = page(CTA_BODY + "<p>" + ("Acme Funding provides merchant cash advance and business loans to owners. " * 4)
                 + "</p>")
    hit, why = judge(no_us)
    assert why == "kept" and hit.us_seen is False
    assert fi._candidate(hit).require_us is True


def test_page_with_a_ny_address_is_us_evidenced():
    hit, why = judge(page(CTA_BODY + US_BODY))  # 100 Main Street, New York, NY 10005
    assert why == "kept" and hit.us_seen is True
    assert fi._candidate(hit).require_us is False


def test_phone_numbers_and_country_selectors_are_not_us_evidence():
    phone_only = page(CTA_BODY + "<p>" + ("Acme Funding provides merchant cash advance and business loans to owners. " * 4)
                      + "Call (786) 372-0744 for details.</p>")
    assert judge(phone_only)[0].us_seen is False
    selector = page(CTA_BODY + "<p>Merchant cash advance and business loans. Choose your country: United States, Canada, "
                               "Mexico, United Kingdom. " + "Acme funds owners for many years. " * 4 + "</p>")
    assert judge(selector)[0].us_seen is False
    assert fi._candidate(judge(selector)[0]).require_us is True


def test_the_call_to_join_beats_a_descriptive_sentence_with_the_same_kind_of_term():
    html = page('<div><p>Our remittance programs serve brokers and independent sales organizations for business loans.</p>'
                '<p>Join the Example broker and ISO network.</p></div>' + US_BODY)
    assert judge(html)[0].quote == "Join the Example broker and ISO network."


def test_a_heading_is_quoted_only_when_no_body_block_carries_the_match():
    html = page('<h1>ISO Partner Program</h1><p>Our merchant cash advance program funds owners fast.</p>' + US_BODY)
    assert judge(html)[0].quote == "ISO Partner Program"


def test_require_date_off_accepts_an_undated_page_inside_the_search_window():
    html = page(CTA_BODY + US_BODY)
    hit, why = fi.judge_page(html, 200, "https://acmefunding.test/iso/", "https://acmefunding.test/iso/", "", 3, NOW,
                             SIGCFG, require_date=False)
    assert why == "kept" and hit.date is None
