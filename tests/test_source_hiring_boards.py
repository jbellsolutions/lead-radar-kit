"""hiring_boards source: offline tests. Synthetic fixtures under tests/fixtures/sources/hiring_boards; no network, no
ScrapeCreators calls (FakeSC). Companies, funders and names here are invented, and no person name is ever printed."""
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import pytest

from lead_radar.programs import detect, signals as sg, sources as srcs
from lead_radar.programs.sources import common, hiring_boards as hb
from lead_radar.scrapecreators import OutOfCredits

FIX = Path(__file__).parent / "fixtures" / "sources" / "hiring_boards"
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
BASE = "https://builtin.com"
TIB = "https://www.timeinbusiness.com"
TIB_INDEX = f"{TIB}/jobs"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ACME_COMPANY = f"{BASE}/company/acme-health"


def read(name: str) -> str:
    return (FIX / name).read_text()


def serp(name: str) -> dict:
    return json.loads((FIX / name).read_text())


# fakes -------------------------------------------------------------------------------------------------------------

class FakeMeter:
    def __init__(self):
        self.n = 0

    def total_credits(self):
        return self.n


class FakeSC:
    """Stands in for ScrapeCreators: answers /v1/google/search from `serp` (query substring -> payload)."""

    def __init__(self, serp_payloads=None):
        self.serp = serp_payloads or {}
        self.meter = FakeMeter()
        self.exhausted = False
        self.calls = []

    async def get(self, path, params=None, source=""):
        self.calls.append((path, dict(params or {})))
        self.meter.n += 1
        query = (params or {}).get("query", "")
        for key, payload in self.serp.items():
            if key in query:
                return payload
        return {"results": [], "credits_charged": 1}


class OutSC(FakeSC):
    """Every call answers 402: ScrapeCreators raises OutOfCredits, which sg.sc_get does not catch."""

    async def get(self, path, params=None, source=""):
        self.calls.append((path, dict(params or {})))
        raise OutOfCredits("402: out of credits")


class Ctx(srcs.SourceContext):
    """SourceContext whose page fetches are served from a dict {url: (status, html)}; no network. A value that is a
    list answers in turn (the last entry repeats), e.g. [(429, ""), (200, html)] for a retry."""

    def __init__(self, pages, sc=None, budget_limit=100, cfg=None, days=30, http_cap=500):
        sc = sc or FakeSC()
        self.said = []
        self.pages = {k: list(v) if isinstance(v, list) else v for k, v in pages.items()}
        self.fetched = []
        super().__init__(sc=sc, http=None, budget=sg.Budget(sc, budget_limit), cfg=cfg or dict(hb.DEFAULTS),
                         signals_cfg=sg.load(), days=days, now=NOW, say=self.said.append, http_cap=http_cap)

    async def get(self, url, role="page"):
        self.fetched.append(url)
        if self.http_used >= self.http_cap or url not in self.pages:
            return None
        self.http_used += 1
        value = self.pages[url]
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        status, html = value
        return detect.Page(url, url, status, html, role)


BUILTIN_TERMS = [f"{BASE}/jobs?search={quote_plus(t)}" for t in hb.DEFAULTS["builtin_terms"]]


def builtin_pages(terms: int | None = None) -> dict:
    """The Built In search page for the first `terms` search terms (all five when None). The synthetic page answers
    every term, so a run over all five repeats each card five times."""
    urls = BUILTIN_TERMS if terms is None else BUILTIN_TERMS[:terms]
    pages = {url: (200, read("builtin_search.html")) for url in urls}
    pages[ACME_COMPANY] = (200, read("builtin_company_acme_health.html"))
    return pages


HOMES = {
    "https://acmehealth.com/": (200, "<html><head><title>Acme Health | Care plans for families</title></head>"
                                     "<body>Care plans.</body></html>"),
    "https://pinelaneco.com/": (200, "<html><head><title>Pine Lane Co | Home goods</title></head><body>Shop.</body>"
                                     "</html>"),
    "https://summitrobotics.com/": (401, "<html><head><title>401 Authorization Required</title></head></html>"),
    "https://platinumcommerce.com/": (200, "<html><head><title>Platinum Commerce | Live-commerce accelerator</title>"
                                           "</head><body>We help brands launch on TikTok Shop.</body></html>"),
    "https://ironwoodfunding.com/": (200, "<html><head><title>Ironwood Funding | Business funding</title></head>"
                                          "<body>Merchant cash advance and business funding for small businesses."
                                          "</body></html>"),
    "https://pearlcapital.com/": (200, "<html><head><title>Pearl Capital</title></head><body>Hard-money real estate "
                                       "loans for investors.</body></html>"),
}


def tib_pages() -> dict:
    pages = {TIB_INDEX: (200, read("tib_index.html"))}
    pages[f"{TIB}/jobs/iso-relationship-manager-ironwood-funding"] = (200, read("tib_detail_ironwood.html"))
    pages[f"{TIB}/jobs/sales-manager-iso-division-pearl-capital"] = (200, read("tib_detail_pearl.html"))
    pages[f"{TIB}/jobs/mca-broker-remote-stateline-funding"] = (200, read("tib_detail_stateline.html"))
    pages[f"{TIB}/jobs/iso-coordinator-anonymous-funder"] = (200, read("tib_detail_anonymous.html"))
    return pages


# registry and contract ----------------------------------------------------------------------------------------------

def test_module_follows_the_source_contract():
    import inspect
    mod = srcs.load("hiring_boards")
    assert mod.NAME == "hiring_boards" == srcs.REGISTRY["hiring_boards"]
    assert isinstance(mod.DEFAULTS, dict) and inspect.iscoroutinefunction(mod.collect)
    assert mod.DEFAULTS["max_google_queries"] == 6 and len(mod.DEFAULTS["builtin_terms"]) == 5
    assert len(mod.DEFAULTS["google_queries"]) >= mod.DEFAULTS["max_google_queries"]


# dates, locations and roles -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,iso", [
    ("9 Days Ago", "2026-10-01"), ("Yesterday", "2026-10-09"), ("2 Weeks Ago", "2026-09-26"),
    ("One Month Ago", None), ("Oct 3, 2026", "2026-10-03"), ("", None),
])
def test_card_dates_follow_relative_date(text, iso):
    assert common.relative_date(text, NOW) == iso


@pytest.mark.parametrize("text,ok", [
    ("Denver, CO, USA", True), ("United States", True), ("Remote (US)", True), ("Beverly Hills, CA 90210", True),
    ("Remote, U.S. preferred", True), ("Toronto, ON, Canada", False), ("Taguig", False),
    ("Santa Ana, Provincia de San José", False), ("2 Locations", False), ("London, UK", False), ("Remote", False),
    ("", False),
])
def test_us_location(text, ok):
    assert hb.us_location(text) is ok


@pytest.mark.parametrize("text,strict,ok", [
    ("Bogota, CO", True, False),      # a state code in a foreign city: Built In must carry a US marker
    ("Bogota, CO", False, True),      # Time in Business and Indeed text may use the state fallback (LinkedIn confirms)
    ("Denver, CO, USA", True, True),
    ("Remote (US)", True, True),
])
def test_builtin_location_is_strict_about_state_codes(text, strict, ok):
    assert hb.us_location(text, strict=strict) is ok


@pytest.mark.parametrize("title,text,ok", [
    ("Affiliate Marketing Manager", "", True), ("Referral Program Manager", "", True),
    ("Head of Affiliates", "", True), ("Affiliate Marketing Manager", "", True),
    ("Partner Program Manager", "", True),
    ("Channel Partner Manager", "", False),                                   # a channel title alone is not a program
    ("Channel Partner Manager", "Recruit and enable resellers for our partner program.", True),
    ("Partner & Channel Development Manager", "", False),
    ("Partner Marketing Manager", "Drive co-marketing with partners", False),
    ("Partner Marketing Manager", "We run our partner program for resellers", True),
    ("Partner Marketing Manager", "Recruit referral partners for our referral program.", True),
    ("Director of Partnerships", "Lead strategic partnerships with enterprise partners.", False),
    ("Partner Marketing Manager", "Marketing channels and campaigns across regions.", False),
    ("Regional Sales & Channel Partner Manager", "We run our partner program.", False),   # sales role
    ("Value-Added Reseller Partner Manager", "Recruit affiliates for our partner program.", False),  # VAR
    ("Strategic Advisory Partner Manager (GSI)", "Our partner program for system integrators.", False),
    ("Account Executive, Affiliate Partnerships", "Run our affiliate program.", False),
    ("Affiliate Marketing Consultant", "", False), ("Affiliate Writer", "", False),
])
def test_program_roles_keep_affiliate_titles_and_need_program_evidence_for_partner_titles(title, text, ok):
    assert hb.role_ok(title, text) is ok


@pytest.mark.parametrize("title,body,ok", [
    ("ISO Relationship Manager", "", True), ("Sales Manager, ISO Division", "", True),
    ("Account Executive - Broker Partnerships", "", True),
    ("MCA Broker - Remote", "Close merchant cash advance deals.", False),
    ("MCA Broker - Remote", "Recruit independent brokers at a 70-85% split.", True),
])
def test_iso_and_broker_roles(title, body, ok):
    assert hb.tib_role_ok(title, body) is ok


@pytest.mark.parametrize("title,company,text,reason", [
    ("Affiliate Marketing Manager", "bet365", "Manage affiliate marketing for sports betting.", "excluded vertical"),
    ("Affiliate Manager", "PartnerStack", "Run our affiliate program.", "affiliate software or network"),
    ("Affiliate Manager", "Lakeside Digital Agency", "Run our affiliate program.", "recruiter or agency"),
    ("Affiliate Manager", "Acme", "We help brands grow their affiliate program.", "text: seller"),
    ("Affiliate Manager", "Acme", "Run our affiliate program for crypto traders.", "text: vertical"),
    ("Affiliate Consultant", "Acme", "Run our affiliate program.", "seller or agency"),
    ("Affiliate Manager", "Acme", "Work with our agency clients on affiliate programs.", "seller or agency"),
    ("Affiliate Manager", "Acme", "Work from home and join my team.", "mlm or commission-only pitch"),
    ("Affiliate Manager", "Acme", "Network marketing company. Join my team and build your downline.",
     "mlm or commission-only pitch"),
    ("Affiliate Manager", "Acme", "Commission only, no cap on earnings.", "mlm or commission-only pitch"),
    ("Affiliate Manager", "Acme", "Run our affiliate program. Commission-only pay.", "mlm or commission-only pitch"),
    ("Affiliate Manager", "Acme", "Run our affiliate program. Work from home, remote.", None),
    ("Affiliate Manager", "Acme", "Run our affiliate program.", None),
])
def test_drop_reason(title, company, text, reason):
    assert hb.drop_reason(title, company, text, sg.load(), "affiliate") == reason


def test_finance_lead_vendors_are_dropped_on_the_finance_list():
    cfg = sg.load()
    assert hb.drop_reason("ISO Relationship Manager", "Ironwood Funding", "Lead generation for lenders.", cfg,
                          "finance") == "finance lead vendor"
    assert hb.drop_reason("ISO Relationship Manager", "Ironwood Funding", "Lead generation for lenders.", cfg,
                          "affiliate") is None


def test_funder_iso_postings_may_say_commission():
    cfg = sg.load()
    assert hb.drop_reason("ISO Relationship Manager", "Ironwood Funding",
                          "Commission-only ISO broker, 70% split on funded deals.", cfg, "finance") is None


def test_seller_homepage_phrases_match_sellers_and_not_brands_that_serve_agencies():
    assert hb.SELLER_HOME.search("Helping brands acquire customers at scale.")
    assert hb.SELLER_HOME.search("We are an award-winning agency.")
    assert hb.SELLER_HOME.search("The TikTok Shop Live-Commerce Accelerator")
    assert hb.SELLER_HOME.search("We help brands launch on TikTok Shop.")
    # a software brand whose customers are agencies is not a seller (the live HighLevel homepage has this wording)
    assert hb.SELLER_HOME.search("A platform for marketing agency owners to run their client CRM.") is None
    assert hb.SELLER_HOME.search("Care plans for families.") is None


# Built In -----------------------------------------------------------------------------------------------------------

def test_builtin_cards_carry_company_slug_date_location_and_description():
    jobs = hb.parse_builtin(read("builtin_search.html"), BASE, NOW)
    by = {j.url.rsplit("/", 1)[-1]: j for j in jobs}
    assert len(jobs) == 15
    j = by["1001"]
    assert j.url == f"{BASE}/job/sr-affiliate-marketing-manager-remote/1001"
    assert (j.company, j.slug, j.date) == ("Acme Health", "acme-health", "2026-10-01")
    assert j.location == "United States" and j.role and "end to end" in j.text
    assert by["1008"].text.startswith("Own affiliate recruiting")  # no collapsed card text: JSON-LD fallback
    assert by["1009"].date is None  # "One Month Ago" is not a dated card
    assert by["1014"].date == "2026-10-10"  # "Reposted 12 Hours Ago"
    assert by["1010"].location == "2 Locations" and by["1003"].location == "Denver, CO, USA"
    assert all(not j.url.count("?") for j in jobs)  # canonical posting URLs


def test_builtin_company_page_gives_the_declared_website():
    assert hb.builtin_company_site(read("builtin_company_acme_health.html")) == "acmehealth.com"
    assert hb.builtin_company_site("<a href='https://form.example.test/x'>x</a>") == ""


def test_builtin_gather_keeps_buyers_and_drops_each_card_once():
    # one search term, so each of the 15 synthetic cards is read once; the other four terms have no page
    ctx = Ctx(builtin_pages(terms=1), budget_limit=0)
    jobs, drops = asyncio.run(hb.gather(ctx))
    assert {j.company: j.title for j in jobs} == {
        "Acme Health": "Affiliate Program Manager", "Pine Lane Co": "Affiliate Manager",
        "Summit Robotics": "Channel Partner Manager", "Kestrel Capital": "Referral Program Manager"}
    assert dict(drops) == {
        "builtin page unavailable": 4,          # four search terms have no page in this run
        "credit budget spent": 1,               # budget 0: the Google leg makes no call
        "excluded vertical": 1,                 # bet365
        "text: seller": 1,                      # Lakeside Digital Agency ("an affiliate agency")
        "not US": 2,                            # Toronto card and the two-location card
        "undated": 1,                           # Riverbend: "One Month Ago"
        "affiliate software or network": 1,     # PartnerStack
        "not a program role": 3,                # Northwind, the consultant title, Juniper partnerships
        "seller or agency (industry tag)": 1,   # Brightline Consulting
        "company already listed": 1,            # Acme's older posting
    }


def test_builtin_repeated_terms_dedupe_on_company_and_title():
    # the synthetic page answers all five terms, so every kept card repeats four times
    ctx = Ctx(builtin_pages(), budget_limit=0)
    jobs, drops = asyncio.run(hb.gather(ctx))
    assert len(jobs) == 4
    assert drops["duplicate posting"] == 20 and drops["excluded vertical"] == 5 and drops["not US"] == 10


def test_builtin_resolution_confirms_the_homepage_or_drops_the_domain():
    pages = {**builtin_pages(), **HOMES}
    ctx = Ctx(pages, budget_limit=0)
    jobs, _ = asyncio.run(hb.gather(ctx))
    cands, _ = asyncio.run(hb.resolve(ctx, jobs))
    doms = {c.company_name: c.domain for c in cands}
    assert doms["Acme Health"] == "acmehealth.com"  # declared on Built In; homepage names the brand
    assert doms["Pine Lane Co"] == "pinelaneco.com"  # slug guess; homepage title names the company
    assert doms["Summit Robotics"] is None  # slug guess answers 401: rejected
    assert doms["Kestrel Capital"] is None  # nothing answers: no domain, LinkedIn name route only


def test_builtin_company_page_429_is_retried_and_the_declared_site_kept():
    pages = {**builtin_pages(terms=1), **HOMES}
    pages[ACME_COMPANY] = [(429, ""), (200, read("builtin_company_acme_health.html"))]
    ctx = Ctx(pages, budget_limit=0, cfg=dict(hb.DEFAULTS, retry_pause=0))
    jobs, _ = asyncio.run(hb.gather(ctx))
    cands, _ = asyncio.run(hb.resolve(ctx, jobs))
    assert ctx.fetched.count(ACME_COMPANY) == 2
    assert {c.company_name: c.domain for c in cands}["Acme Health"] == "acmehealth.com"


def test_builtin_company_page_that_keeps_answering_429_is_not_cached():
    pages = {**builtin_pages(terms=1), **HOMES}
    pages[ACME_COMPANY] = [(429, "")]
    ctx = Ctx(pages, budget_limit=0, cfg=dict(hb.DEFAULTS, retry_pause=0))
    jobs, _ = asyncio.run(hb.gather(ctx))
    cache: dict = {}
    job = next(j for j in jobs if j.company == "Acme Health")
    assert asyncio.run(hb._builtin_site(ctx, cache, BASE, job.slug)) == ""
    assert ("company", job.slug) not in cache
    assert ctx.fetched.count(ACME_COMPANY) == 2  # the first answer and its one retry, and nothing is cached


def test_guessed_domain_needs_the_whole_company_name_on_the_homepage():
    assert hb._brand_on_home("<title>Pine Valley Dental</title>", "Pine", guessed=True) is False
    assert hb._brand_on_home("<title>Pine | Dental</title>", "Pine", guessed=True) is True
    assert hb._brand_on_home("<title>Pine Valley Dental</title>", "Pine", guessed=False) is True


def test_affiliate_homepage_that_sells_to_brands_drops_the_candidate_even_without_a_brand_match():
    pages = {"https://excelimpact.com/": (200, "<html><head><title>Excel</title></head><body>Helping brands acquire "
                                                 "customers at scale.</body></html>")}
    ctx = Ctx(pages)
    job = hb.Job(lst="affiliate", platform="indeed", url="https://www.indeed.com/viewjob?jk=ccc333", title="Affiliate Manager",
                 company="Excel Impact", date="2026-09-18", location="Remote, USA", text="", role=True)
    assert asyncio.run(hb._domain(ctx, job, {}, BASE)) == ("", "homepage reads as a seller")


def test_non_us_company_suffix_is_dropped_unless_the_posting_is_us_marked():
    ctx = Ctx({})
    base = dict(lst="affiliate", platform="indeed", url="https://www.indeed.com/viewjob?jk=ddd444",
                title="Affiliate Manager", company="Usercentrics GmbH", date="2026-10-08",
                text="Run our affiliate program.", role=True)
    assert hb._screen(hb.Job(**dict(base, location="Austin, TX")), ctx) == "non-US company name"  # state code only
    assert hb._screen(hb.Job(**dict(base, location="New York, NY, USA")), ctx) is None


def test_builtin_card_outside_the_us_is_dropped_even_with_a_state_code():
    ctx = Ctx({})
    job = hb.Job(lst="affiliate", platform="builtin", url=f"{BASE}/job/x/1", title="Affiliate Manager",
                 company="Bogota Co", date="2026-10-08", location="Bogota, CO", text="Run our affiliate program.",
                 role=True)
    assert hb._screen(job, ctx) == "not US"


# Time in Business -------------------------------------------------------------------------------------------------------

def test_tib_index_lists_job_rows_without_the_post_link():
    rows = hb.parse_tib_index(read("tib_index.html"), TIB)
    assert len(rows) == 5 and all(url.startswith(f"{TIB}/jobs/") for url, _ in rows)
    assert not any(url.endswith("/jobs/post") for url, _ in rows)


def test_tib_detail_reads_employer_date_location_and_declared_site():
    j = hb.parse_tib_detail(read("tib_detail_ironwood.html"), f"{TIB}/jobs/iso-relationship-manager-ironwood-funding",
                            "ISO Relationship Manager", NOW)
    assert (j.company, j.date, j.website, j.lst, j.role) == ("Ironwood Funding", "2026-10-03", "ironwoodfunding.com",
                                                             "finance", True)
    assert "Remote (US)" in j.location and hb.us_location(j.location)
    anon = hb.parse_tib_detail(read("tib_detail_anonymous.html"), f"{TIB}/jobs/iso-coordinator-anonymous-funder",
                               "ISO Coordinator", NOW)
    assert anon.company == ""


def test_hard_money_homepage_is_not_a_business_funder():
    hard_money = ("https://ironwoodfunding.com/", (200, "<html><head><title>Ironwood Funding | Hard money</title></head>"
                                                       "<body>Hard money real estate loans and working capital.</body></html>"))
    ctx = Ctx({**tib_pages(), **HOMES, hard_money[0]: hard_money[1]}, budget_limit=0)
    jobs, _ = asyncio.run(hb.gather(ctx))
    cands, rdrops = asyncio.run(hb.resolve(ctx, jobs))
    assert [c.company_name for c in cands] == []  # Ironwood's confirmed homepage is hard money: dropped
    assert rdrops["homepage is not a business funder"] == 2


def test_tib_gather_and_funder_confirmation():
    pages = {**tib_pages(), **HOMES}
    ctx = Ctx(pages, budget_limit=0)
    jobs, drops = asyncio.run(hb.gather(ctx))
    assert [j.company for j in jobs] == ["Ironwood Funding", "Pearl Capital Group"]
    assert dict(drops) == {
        "builtin page unavailable": 5,   # no Built In pages in this run
        "credit budget spent": 1,
        "not a program role": 1,         # MCA broker with no broker-recruiting language
        "no named company": 1,           # anonymous row
    }
    cands, rdrops = asyncio.run(hb.resolve(ctx, jobs))
    doms = {c.company_name: (c.domain, c.list) for c in cands}
    assert doms == {"Ironwood Funding": ("ironwoodfunding.com", "finance")}
    assert rdrops["homepage is not a business funder"] == 1  # Pearl: homepage reads as hard-money real estate


def test_a_state_only_posting_needs_linkedin_to_confirm_us_and_a_us_marked_one_does_not():
    ctx = Ctx({})
    jobs = [
        hb.Job(lst="affiliate", platform="upwork", url="https://www.upwork.com/freelance-jobs/apply/x_~1/",
               title="Affiliate Manager", company="Horizon Digital Co", date="2026-09-28", location=None,
               text="Run our affiliate program.", role=True),
        hb.Job(lst="finance", platform="timeinbusiness", url=f"{TIB}/jobs/iso-x-1", title="ISO Manager",
               company="Denver Capital", date="2026-10-02", location="Denver, CO", text="ISO partners.", role=True,
               finance_gate=True),
        hb.Job(lst="finance", platform="timeinbusiness", url=f"{TIB}/jobs/iso-x-2", title="ISO Manager",
               company="Remote Ventures", date="2026-10-02", location="Remote (US)", text="ISO partners.", role=True,
               finance_gate=True),
    ]
    cands, _ = asyncio.run(hb.resolve(ctx, jobs))
    assert [c.require_us for c in cands] == [True, True, False]


# Google SERP: Indeed and Upwork ----------------------------------------------------------------------------------

def test_indeed_snippets_keep_named_us_buyers_only():
    items = serp("serp_indeed.json")["results"]
    parsed = [hb.parse_serp_item("indeed", it, NOW) for it in items]
    assert parsed[5] is None and parsed[6] is None  # non-US Indeed host; aggregator
    buyer = parsed[0]
    assert (buyer.title, buyer.company, buyer.date) == ("Affiliate Manager", "Platinum Commerce, LLC", "2026-09-16")
    assert buyer.url == "https://www.indeed.com/viewjob?jk=aaa111bbb222" and hb.us_location(buyer.location)
    assert not hb.us_location(parsed[2].location) and parsed[2].company == "BiGDUG"  # remote, no US marker
    assert parsed[4].company == "321 The Agency"
    assert parsed[1].company == "" and parsed[3].company == ""  # no named company in the snippet


def test_upwork_snippets_keep_only_named_clients():
    items = serp("serp_upwork.json")["results"]
    parsed = [hb.parse_serp_item("upwork", it, NOW) for it in items]
    horizon = parsed[0]
    assert horizon.company == "Horizon Digital Co" and horizon.date == "2026-09-28" and horizon.location is None
    assert horizon.url == "https://www.upwork.com/freelance-jobs/apply/Freelance-Affiliate-Recruiter_~0221001/"
    assert parsed[1].company == "" and parsed[2].company == ""  # generic brand phrase; nav noise only
    assert not parsed[3].role  # consultant title


def test_serp_queries_are_date_windowed_capped_and_budgeted():
    sc = FakeSC({"site:indeed.com": serp("serp_indeed.json"), "site:upwork.com": serp("serp_upwork.json")})
    ctx = Ctx(dict(HOMES), sc=sc, budget_limit=100)
    jobs, drops = asyncio.run(hb.gather(ctx))
    assert len(sc.calls) == 6
    assert all(p == "/v1/google/search" and q["date_posted"] == "last-month" for p, q in sc.calls)
    # Indeed repeats its four queries, Upwork its two. Horizon's "Commission Only, No Cap on Earnings" snippet is dropped.
    assert [(j.company, j.platform) for j in jobs] == [("Platinum Commerce, LLC", "indeed")]
    assert dict(drops) == {
        "builtin page unavailable": 5,
        "no named company": 12,               # Indeed snippets without an employer (8) and Upwork anonymous clients (4)
        "not US": 4,                          # Indeed remote posting with no US marker, four times
        "recruiter or agency": 4,             # 321 The Agency, four times
        "duplicate posting": 3,               # Platinum from the other three Indeed queries
        "mlm or commission-only pitch": 2,    # Horizon Digital, in both Upwork queries
        "not a program role": 2,              # the Upwork consultant title, twice
    }
    cands, rdrops = asyncio.run(hb.resolve(ctx, jobs))
    assert cands == [] and rdrops == {"homepage reads as a seller": 1}  # Platinum's own homepage sells to brands


def test_upwork_client_without_a_location_is_listed_only_when_linkedin_confirms_us():
    job = hb.Job(lst="affiliate", platform="upwork", url="https://www.upwork.com/freelance-jobs/apply/x_~2/",
                 title="Affiliate Manager", company="Harbor Brands", date="2026-09-28", location=None,
                 text="Run our affiliate program.", role=True)
    cands, _ = asyncio.run(hb.resolve(Ctx({}), [job]))
    assert len(cands) == 1 and cands[0].require_us is True


def test_zero_budget_makes_no_credit_call_and_free_sources_still_run():
    sc = FakeSC({"site:indeed.com": serp("serp_indeed.json")})
    ctx = Ctx(builtin_pages(), sc=sc, budget_limit=0)
    jobs, drops = asyncio.run(hb.gather(ctx))
    assert sc.calls == [] and drops["credit budget spent"] == 1
    assert jobs  # Built In still produced postings


def test_budget_stops_mid_run_and_query_cap_holds():
    sc = FakeSC({"site:indeed.com": serp("serp_indeed.json")})
    ctx = Ctx({}, sc=sc, budget_limit=2)
    asyncio.run(hb.gather(ctx))
    assert len(sc.calls) == 2  # one credit per query: the run stops at its budget
    sc2 = FakeSC()
    cfg = dict(hb.DEFAULTS, max_google_queries=3)
    asyncio.run(hb.gather(Ctx({}, sc=sc2, budget_limit=100, cfg=cfg)))
    assert len(sc2.calls) == 3


def test_out_of_credits_keeps_the_free_sources_already_read():
    pages = {**builtin_pages(), **tib_pages(), **HOMES}
    sc = OutSC()
    ctx = Ctx(pages, sc=sc, budget_limit=15)
    cands = asyncio.run(hb.collect(ctx))
    assert len(sc.calls) == 1  # the first Google query raised; the loop stopped
    assert {c.company_name for c in cands} == {"Acme Health", "Pine Lane Co", "Summit Robotics", "Kestrel Capital",
                                               "Ironwood Funding"}
    assert "out of credits" in ctx.said[0]


def test_http_cap_exhaustion_drops_pages_without_failing_the_run():
    ctx = Ctx(builtin_pages(), budget_limit=0, http_cap=2)
    jobs, drops = asyncio.run(hb.gather(ctx))
    assert drops["builtin page unavailable"] == 3  # the cap allowed two of the five search pages
    assert len(jobs) == 4
    cands, _ = asyncio.run(hb.resolve(ctx, jobs))
    assert len(cands) == 4 and all(c.domain is None for c in cands)


# candidate contract and privacy ----------------------------------------------------------------------------------

def test_collect_emits_candidates_that_follow_the_signal_contract():
    pages = {**builtin_pages(), **tib_pages(), **HOMES}
    sc = FakeSC({"site:indeed.com": serp("serp_indeed.json"), "site:upwork.com": serp("serp_upwork.json")})
    ctx = Ctx(pages, sc=sc, budget_limit=15)
    cands = asyncio.run(hb.collect(ctx))
    assert cands
    assert len(cands) == len({(c.list, common.norm(c.company_name)) for c in cands})  # one per company per list
    for c in cands:
        s = c.signal
        assert c.list in ("affiliate", "finance") and isinstance(c.require_us, bool)
        assert s.kind == "job" and s.kind in sg.KINDS
        assert s.platform in sg.PLATFORM_LABEL and s.platform in ("builtin", "timeinbusiness", "indeed", "upwork")
        assert s.signal_date is None or ISO_DATE.match(s.signal_date)
        assert len(s.quote) <= 400 and " @ " in s.quote
        assert "utm_" not in s.url and (s.platform == "indeed" or "?" not in s.url)
        assert s.person_name is None and s.person_url is None
    assert len(ctx.said) == 1 and ctx.said[0].startswith("hiring_boards:")


def test_no_person_name_is_logged_or_returned():
    pages = {**tib_pages(), **HOMES}
    ctx = Ctx(pages, budget_limit=0)
    cands = asyncio.run(hb.collect(ctx))
    blob = " ".join(ctx.said) + " " + repr(cands)
    assert "Pat Example" not in blob
    assert all(c.signal.person_name is None for c in cands)
