"""Program lane growth signals + finance list: parsing, exclusions, scoring, resolution, lists. Synthetic data only."""
import asyncio
import csv
from datetime import datetime, timedelta, timezone

import pytest

from lead_radar.db import Store
from lead_radar.programs import detect, growth, signals as sg

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def days_ago(n):
    return (NOW - timedelta(days=n)).date().isoformat()


# parsing -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url,title,expected", [
    ("https://jobs.lever.co/acmeco/abc", "AcmeCo - Affiliate Manager - Lever", "AcmeCo"),
    ("https://jobs.ashbyhq.com/acmeco/abc", "Senior Affiliate Manager @ AcmeCo - Jobs", "AcmeCo"),
    ("https://boards.greenhouse.io/acmeco/jobs/1", "Job Application for Partnerships Manager at AcmeCo", "AcmeCo"),
    ("https://www.linkedin.com/jobs/view/1", "AcmeCo hiring Affiliate Manager in Austin, TX | LinkedIn", "AcmeCo"),
    ("https://jobs.lever.co/acme-co/abc", "", "acme co"),
    ("https://example.com/job", "Affiliate Manager", None),
])
def test_company_from_job(url, title, expected):
    assert sg.company_from_job(url, title) == expected


def test_staff_profile_parsing_skips_sellers_and_unnamed_companies():
    assert sg.staff_from_profile("Pat Example - Affiliate Manager - AcmeCo | LinkedIn") == \
        ("Pat Example", "Affiliate Manager", "AcmeCo")
    assert sg.staff_from_profile("Pat Example - I launch and grow Affiliate Programs. Let's connect! | LinkedIn") is None
    assert sg.staff_from_profile("Pat Example - Affiliate Manager | LinkedIn") is None
    assert sg.staff_from_profile("Pat Example - Freelance Affiliate Manager - Self-employed | LinkedIn") is None
    # current Google titles: "Name - Headline - LinkedIn", company only when the headline names it
    assert sg.staff_from_profile("Pat Example - Senior Affiliate Manager at AcmeCo - LinkedIn") == \
        ("Pat Example", "Senior Affiliate Manager", "AcmeCo")
    assert sg.staff_from_profile("Pat Example - Affiliate Manager @ AcmeCo | Growth ... - LinkedIn") == \
        ("Pat Example", "Affiliate Manager", "AcmeCo")
    assert sg.staff_from_profile("Pat Example - Affiliate Marketing Manager - LinkedIn") is None
    assert sg.staff_from_profile("Pat Example - Affiliate Manager | LinkedIn") is None


def test_post_date_from_field_or_activity_id():
    assert sg.post_date({"datePublished": "2026-10-05T16:10:24.661Z"}) == "2026-10-05"
    # activity id 7512907053686902785 was published 2026-10-05 (checked against a post that has datePublished)
    assert sg.post_date({"url": "https://www.linkedin.com/posts/x_y-activity-7512907053686902785-ezrc"}) == "2026-10-05"
    assert sg.post_date({}) is None


def test_company_url_normalises_any_linkedin_company_link():
    assert sg.company_url("https://uk.linkedin.com/company/AcmeCo/about/") == "https://www.linkedin.com/company/acmeco"
    assert sg.company_url("https://www.linkedin.com/in/someone") is None


# exclusions ----------------------------------------------------------------------------------------------------

def prof(**kw):
    base = dict(linkedin_url="https://www.linkedin.com/company/acmeco", name="AcmeCo", domain="acmeco.com",
                country="US", industry="Software Development", text="AcmeCo makes invoicing software for plumbers.")
    base.update(kw)
    return sg.Profile(**base)


@pytest.mark.parametrize("kw,lst,reason", [
    ({}, "affiliate", None),
    ({"country": "CY"}, "affiliate", "not US (CY)"),
    ({"text": "The best online casino and sportsbook affiliate program"}, "affiliate", "excluded vertical"),
    ({"industry": "Staffing and Recruiting"}, "affiliate", "recruiter"),
    ({"name": "Partner Marketing Agency Co", "text": "We are an affiliate marketing agency"}, "affiliate",
     "sells affiliate/partner marketing"),
    ({"industry": "Marketing Services", "text": "Full-service affiliate marketing for DTC brands"}, "affiliate",
     "sells affiliate/partner marketing"),
    ({"industry": "Financial Services", "text": "Merchant cash advance and working capital for small businesses"},
     "finance", None),
    ({"industry": "Financial Services", "text": "MCA leads generator: fresh merchant cash advance leads daily"},
     "finance", "finance lead/software vendor"),
    ({"industry": "Financial Services", "text": "Personal wealth management"}, "finance", "not a business-funding company"),
    ({"name": "Example Partner Funding", "industry": "Financial Services", "text": "Business funding"}, "finance",
     "on the do-not-prospect list"),
    ({"industry": "Book and Periodical Publishing", "text": "News for the equipment financing industry"}, "finance",
     "not a business-funding company"),
    ({"industry": "Banking", "domain": "microloans.org", "text": "Small business loans for rural owners"}, "finance",
     "nonprofit lender"),
])
def test_exclusions(kw, lst, reason):
    assert sg.exclusion(prof(**kw), lst) == reason


@pytest.mark.parametrize("kw,reason", [
    ({"name": "Superfiliate", "domain": "superfiliate.com", "text": "Superfiliate. Creator and affiliate platform for DTC"},
     "affiliate/partner software or network"),
    ({"name": "PartnerCo", "text": "PartnerCo. The partner relationship management platform for SaaS"},
     "affiliate/partner software or network"),
    ({"name": "Fortune Media", "domain": "fortune.com", "text": "Business news"}, None),  # not "tune.com"
])
def test_affiliate_software_vendors_are_excluded(kw, reason):
    assert sg.exclusion(prof(**kw), "affiliate") == reason


def test_program_jobs_and_posts_prove_a_program_without_a_site_check():
    row = {"list": "affiliate", "status": "no_program", "size": "51-200 employees"}
    hiring = [{"kind": "job", "quote": "Senior Affiliate Marketing Manager @ AcmeCo", "signal_date": days_ago(2)}]
    bd = [{"kind": "job", "quote": "Strategic Partnerships Manager, Systems Integrators", "signal_date": days_ago(2)}]
    post = [{"kind": "company_post", "quote": "We just launched our referral program!", "signal_date": days_ago(2)}]
    vague = [{"kind": "company_post", "quote": "Great partnerships this quarter", "signal_date": days_ago(2)}]
    assert sg.lead_score(row, hiring, NOW) == 65 and sg.lead_score(row, post, NOW) == 50
    assert sg.lead_score(row, bd, NOW) == 0 and sg.lead_score(row, vague, NOW) == 0
    verified = {**row, "status": "verified"}
    assert sg.lead_score(verified, bd, NOW) == 45  # a BD hire adds a little on a verified program


def test_shortlink_websites_are_followed_to_the_company_domain(store):
    class Hop:
        async def get(self, url, **kw):
            return None

    sc = FakeSC({"https://www.linkedin.com/company/fundco": company("FundCo", "https://bit.ly/3abc")})
    res = growth.Resolver(sc, Hop(), store, sg.Budget(sc, 10), sg.load())

    async def fake_get(http, url, role):
        return detect.Page(url, "https://fundco.example/home", 200, "<p>hi</p>", role)
    orig, detect._get = detect._get, fake_get
    try:
        p = asyncio.run(res.resolve(sg.Candidate("finance", linkedin_url="https://www.linkedin.com/company/fundco")))
    finally:
        detect._get = orig
    assert p.domain == "fundco.example"


def test_program_posts_and_finance_program_quote():
    p = prof(posts=[{"url": "https://www.linkedin.com/posts/acmeco_a", "datePublished": "2026-10-01T00:00:00Z",
                     "text": "Big news: we just launched our affiliate program with 30% recurring commission."},
                    {"url": "https://www.linkedin.com/posts/acmeco_b", "text": "Happy Friday!"}])
    sigs = sg.program_posts(p, "affiliate")
    assert [(s.kind, s.signal_date) for s in sigs] == [("company_post", "2026-10-01")]
    page = detect.Page("https://fund.example/iso", "https://fund.example/iso", 200,
                       "<p>Join our ISO program and earn up to 12 points on every funded deal.</p>", "linked")
    sig = sg.finance_program([page])
    assert sig.kind == "partner_program" and "ISO program" in sig.quote


# scoring -------------------------------------------------------------------------------------------------------

def test_lead_score_rewards_fresh_hiring_staff_posts_and_small_programs():
    row = {"list": "affiliate", "status": "verified", "platforms": ["partnerstack"], "partner_count": 20,
           "size": "51-200 employees", "commission_text": "30%"}
    assert sg.lead_score({**row, "status": "no_program"}, []) == 0
    assert sg.lead_score({**row, "excluded": "recruiter"}, [{"kind": "job", "signal_date": days_ago(3)}], NOW) == 0
    base = sg.lead_score(row, [], NOW)
    hiring = sg.lead_score(row, [{"kind": "job", "quote": "Affiliate Manager", "signal_date": days_ago(3)}], NOW)
    stale = sg.lead_score(row, [{"kind": "job", "quote": "Affiliate Manager", "signal_date": days_ago(200)}], NOW)
    assert base == 55 and hiring == 85 and stale == 70


def test_finance_score_puts_iso_programs_first():
    row = {"list": "finance", "size": "11-50 employees"}
    assert sg.lead_score(row, [], NOW) == 45
    assert sg.lead_score(row, [{"kind": "partner_program"}], NOW) == 75
    assert sg.lead_score({**row, "_direct": True}, [{"kind": "partner_program"},
                         {"kind": "company_post", "signal_date": days_ago(5)}], NOW) == 95


# resolution + process + lists (fake ScrapeCreators) ----------------------------------------------------------

class FakeMeter:
    def __init__(self):
        self.n = 0

    def total_credits(self):
        return self.n


class FakeSC:
    def __init__(self, companies, searches=None):
        self.companies, self.searches, self.meter, self.exhausted, self.calls = companies, searches or {}, FakeMeter(), False, []

    async def get(self, path, params, source=""):
        self.meter.n += 1
        self.calls.append((path, params.get("url") or params.get("query")))
        if path == "/v1/linkedin/company":
            d = self.companies.get(params["url"])
            if d is None:
                raise sg.SkipItem("404")
            return d
        return {"results": self.searches.get(params["query"], [])}


class NoHTTP:
    async def get(self, url):
        return None


def company(name, website, **kw):
    return {"name": name, "website": website, "location": {"country": "US"}, "industry": "Software Development",
            "size": "51-200 employees", "description": f"{name} software", "posts": [], "similarPages": [], **kw}


@pytest.fixture
def store():
    return Store.open(":memory:")


def test_process_resolves_candidates_stores_signals_and_excludes(store):
    sc = FakeSC({
        "https://www.linkedin.com/company/acmeco": company("AcmeCo", "https://acmeco.com/", posts=[
            {"url": "https://www.linkedin.com/posts/acmeco_x", "datePublished": "2026-10-02T00:00:00Z",
             "text": "Our affiliate program now pays 25% recurring."}]),
        "https://www.linkedin.com/company/betco": company("BetCo", "https://betco.example/",
                                                          description="Online casino and sportsbook"),
    }, searches={'site:linkedin.com/company "AcmeCo"': [
        {"url": "https://www.linkedin.com/company/acmeco", "title": "AcmeCo | LinkedIn"}]})
    res = growth.Resolver(sc, NoHTTP(), store, sg.Budget(sc, 50), sg.load())
    cands = [sg.Candidate("affiliate", sg.Signal("job", "https://jobs.lever.co/acmeco/1", "Affiliate Manager", "2026-10-09"),
                          company_name="AcmeCo"),
             sg.Candidate("affiliate", sg.Signal("company_post", "https://www.linkedin.com/posts/betco_y", "join"),
                          linkedin_url="https://www.linkedin.com/company/betco"),
             sg.Candidate("affiliate", company_name="Nobody Inc")]
    rep = growth.GrowthReport()
    touched = asyncio.run(growth.process(cands, res, store, rep))
    assert touched == ["acmeco.com", "betco.example"] and rep.unresolved == 1
    assert rep.excluded == {"excluded vertical": 1}
    sigs = store.program_signals()
    assert sorted(s["kind"] for s in sigs["acmeco.com"]) == ["company_post", "job"]
    row = store.program_company("acmeco.com")
    assert row["linkedin_url"] == "https://www.linkedin.com/company/acmeco" and row["country"] == "US"
    assert store.program_company("betco.example")["excluded"] == "excluded vertical"
    # the company page is fetched once even when two candidates point at it
    assert asyncio.run(res.profile("https://www.linkedin.com/company/acmeco")) is not None
    assert [c for c in sc.calls if c[1] == "https://www.linkedin.com/company/acmeco"].__len__() == 1


def test_budget_stops_collection(store):
    sc = FakeSC({})
    bud = sg.Budget(sc, 0)
    assert asyncio.run(growth.collect_jobs(sc, bud, sg.load(), days=7)) == []
    assert sc.calls == []


def test_top_lists_and_csv(store, tmp_path):
    store.add_program_seed("acmeco.com", source="serp", ref="x", name="AcmeCo")
    store.save_program_check("acmeco.com", status="verified", platforms=["rewardful"], program_url="https://acmeco.com/affiliates",
                             commission_text="Earn 30% recurring", evidence=[], segment="saas", name="AcmeCo", score=60, error=None)
    store.save_company_profile("acmeco.com", lst="affiliate", source="signal:job", ref="x", name="AcmeCo",
                               linkedin_url="https://www.linkedin.com/company/acmeco", country="US", hq="Austin, Texas",
                               size="51-200 employees", industry="Software Development", excluded=None)
    store.add_program_signal("acmeco.com", sg.Signal("job", "https://jobs.lever.co/acmeco/1", "Affiliate Manager",
                                                      datetime.now(timezone.utc).date().isoformat()))
    store.add_program_seed("quiet.com", source="serp", ref="x", name="Quiet")  # verified program, no signal
    store.save_program_check("quiet.com", status="verified", platforms=["in-house"], program_url=None,
                             commission_text=None, evidence=[], segment="saas", name="Quiet", score=40, error=None)
    store.save_company_profile("fundco.example", lst="finance", source="finance", ref="x", name="FundCo",
                               linkedin_url="https://www.linkedin.com/company/fundco", country="US", hq="Miami, Florida",
                               size="11-50 employees", industry="Financial Services", excluded=None)
    store.add_program_signal("fundco.example", sg.Signal("partner_program", "https://fundco.example/iso",
                                                         "Join our ISO program: 10 points"))
    growth.rescore(store)
    aff = growth.top_list(store, "affiliate")
    assert [r["domain"] for r in aff] == ["acmeco.com"]  # quiet.com has no signal
    fin = growth.top_list(store, "finance")
    assert [r["domain"] for r in fin] == ["fundco.example"] and fin[0]["lead_score"] == 75
    out = tmp_path / "aff.csv"
    assert growth.export_list(store, "affiliate", out) == 1
    row = next(csv.DictReader(out.open()))
    assert row["rank"] == "1" and row["company"] == "AcmeCo" and row["why now"].startswith("hiring: Affiliate Manager")
    assert row["hq"] == "Austin, Texas"


@pytest.mark.parametrize("name,domain,expected", [
    ("Home", "languagebird.com", "Languagebird"), ("Access Denied", "rei.com", "Rei"),
    ("Careers at Chewy", "careers.chewy.com", "Chewy"), ("AcmeCo", "acmeco.com", "AcmeCo"),
    ("Connect with mental health experts who specialize in you", "rula.com", "Rula"),
    ("Web Hosting | Built to Grow", "hosting.com", "Hosting"), (None, "acme.io", "Acme"),
])
def test_clean_name(name, domain, expected):
    assert sg.clean_name(name, domain) == expected


def test_workable_job_titles_and_non_us_roles():
    assert sg.company_from_job("https://apply.workable.com/acme/j/1", "Partnerships Manager - AcmeCo - Jobs by Workable") \
        == "AcmeCo"
    assert sg.company_from_job("https://apply.workable.com/acme/j/1", "Regional Head of Affiliates - AcmeCo - Application") \
        == "AcmeCo"
    assert sg.NON_US_JOB.search("Regional Head of Affiliates, SEA") and not sg.NON_US_JOB.search("Affiliate Manager, US")


def test_agencies_are_not_on_the_affiliate_list():
    assert sg.exclusion(prof(industry="Advertising Services", text="Display ads for retailers"), "affiliate") \
        == "agency / marketing services"
