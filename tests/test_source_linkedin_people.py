"""linkedin_people source: person posts about their own program, text gates, company anchors, budget and privacy.
Offline only (fake ScrapeCreators and fake homepages). Every name here is synthetic."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lead_radar.programs import detect, signals as sg, sources as srcs
from lead_radar.programs.sources import common
from lead_radar.programs.sources import linkedin_people as lp

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
FIX = Path(__file__).parent / "fixtures" / "sources" / "linkedin_people"
CFG = sg.load()
AFFIL_PHRASE = '"our affiliate program"'


class FakeMeter:
    def __init__(self):
        self.n = 0

    def total_credits(self):
        return self.n


class FakeSC:
    """ScrapeCreators stand-in. A query with no entry is a 404 (SkipItem, no credit). Search pages are indexed by cursor."""

    def __init__(self, search=None, google=None, raise_on_google=None):
        self.search_pages = search or {}
        self.google = google or {}
        self.raise_on_google = raise_on_google
        self.meter, self.exhausted, self.calls = FakeMeter(), False, []

    async def get(self, path, params=None, source=""):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == lp.SEARCH:
            pages = self.search_pages.get(params["query"]) or []
            i = int(params.get("cursor") or 0)
            if i >= len(pages):
                raise sg.SkipItem("404")
            self.meter.n += 1
            page = dict(pages[i])
            page["cursor"] = str(i + 1) if i + 1 < len(pages) else None
            return page
        if path == lp.GOOGLE:
            if self.raise_on_google is not None:
                raise self.raise_on_google
            results = self.google.get(params["query"])
            if results is None:
                raise sg.SkipItem("404")
            self.meter.n += 1
            return {"results": results}
        raise AssertionError(path)


def make_ctx(sc, limit=100, pages=None, lines=None, cfg=None, days=30):
    pages = pages or {}

    class Ctx(srcs.SourceContext):
        async def get(self, url, role="page"):
            if self.http_used >= self.http_cap:
                return None
            self.http_used += 1
            return pages.get(url)

    say = (lambda m: lines.append(m)) if lines is not None else (lambda m: None)
    return Ctx(sc=sc, http=None, budget=sg.Budget(sc, limit), cfg=dict(lp.DEFAULTS, **(cfg or {})),
               signals_cfg=CFG, days=days, now=NOW, say=say)


def home(url, html="", final_url=None, status=200):
    return detect.Page(url, final_url or url, status, html, "home")


def post(aid, text, name="Pat Example", slug="pat-example", date="2026-10-05", url=None):
    return {"url": url or f"https://www.linkedin.com/posts/{slug}_activity-{aid}-AbCd",
            "activityUrn": f"urn:li:activity:{aid}", "description": text, "datePublished": date,
            "author": {"name": name, "url": f"https://www.linkedin.com/in/{slug}", "followers": 1, "image": ""}}


def run(coro):
    return asyncio.run(coro)


# registry and defaults -------------------------------------------------------------------------------------------

def test_module_follows_the_source_contract():
    import inspect
    assert srcs.REGISTRY["linkedin_people"] == "linkedin_people"
    m = srcs.load("linkedin_people")
    assert m.NAME == "linkedin_people" and isinstance(m.DEFAULTS, dict) and inspect.iscoroutinefunction(m.collect)
    assert m.DEFAULTS["max_author_lookups"] == 12
    for q in m.DEFAULTS["queries"]:
        assert q["list"] in ("affiliate", "finance") and q["pages"] >= 1 and q["q"]


# text gates ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,reason", [
    ("We help brands launch their affiliate program. Book a free call.", "seller"),
    ("As a consultant I build affiliate programs for our clients.", "seller"),
    ("Our agency just launched our affiliate program for a DTC brand.", "seller"),
    ("Recruiting for a client: we are hiring an affiliate manager.", "recruiter"),
    ("Join my team and earn passive income from home with our affiliate program.", "mlm"),
    ("Our sportsbook affiliate program pays revshare to partners.", "vertical"),
    ("We just launched our affiliate program for our Dubai team.", "non_us"),
    ("We just launched our affiliate program paying £50 per sale.", "non_us"),
    ("We are proud to join the AWS Partner program as an official partner.", "third_party"),
    ("Congrats to Acme on launching their affiliate program!", "third_party"),
    ("I just joined Acme's affiliate program and earn commission on my links.", "individual_affiliate"),
    ("Which affiliate programs should I join for beginners?", "seeker"),
    ("5 tips to recruit affiliates for your ecommerce brand.", "advice"),
    ("Affiliate programs are a great way to grow any brand.", "not_own_program"),
    ("New job: Affiliate Manager. View the job on our job board.", "repost"),
    ("We're hiring a partner manager. Our program pays a 20% commission per sale.", "hiring_not_program"),
    ("We just launched our campus ambassador program with rev share perks.", "ambassador_noncommission"),
    ("We're now accepting new ISO 27001 audit clients.", "iso_standard"),
])
def test_each_gate_drops_its_case(text, reason):
    lst, got = lp._screen(text, "affiliate", CFG)
    assert got == reason


@pytest.mark.parametrize("text,query_list,expected_list", [
    ("We just launched our affiliate program: 30% recurring commission.", "affiliate", "affiliate"),
    ("We're hiring an affiliate manager to grow our partner program.", "affiliate", "affiliate"),
    ("We're now accepting new ISOs for our merchant cash advance program.", "affiliate", "finance"),
    ("We're now accepting new ISOs for our merchant cash advance program.", "finance", "finance"),
])
def test_buyer_posts_pass_and_route_funders_to_the_finance_list(text, query_list, expected_list):
    lst, reason = lp._screen(text, query_list, CFG)
    assert reason is None and lst == expected_list


def test_finance_post_without_funding_context_is_dropped():
    assert lp._screen("We're now accepting new ISOs for our team.", "finance", CFG) == ("finance", "not_funding")


def test_domain_helpers_drop_freemail_and_tools():
    assert [d for d in common.domains_in_text("reach pat@gmail.com or book at apollo.io/x") if lp._usable_domain(
        d, CFG, set())] == []
    assert lp._usable_domain("acme-shop.com", CFG, set())


# quote --------------------------------------------------------------------------------------------------------------

def test_quote_is_one_clean_sentence_with_no_names():
    text = "Great week all round. We just launched our affiliate program: 30% recurring commission. Thanks Pat Example!"
    assert lp._program_sentence(text, ["Pat", "Example"]) == "We just launched our affiliate program: 30% recurring commission."
    assert lp._program_sentence("Pat here: we just launched our affiliate program. Pat Example joins.",
                                ["Pat", "Example"]) is None
    assert lp._program_sentence("Shoutout to our affiliates: we just launched our affiliate program.", []) is None


# company anchors ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("title,desc,expected", [
    ("Pat Example - Founder at Acme | LinkedIn", "Founder @ Acme. · Experience: Acme", "Acme"),
    ("Pat Example - Partnerships Manager @ Acme Labs - LinkedIn", "Partnerships Manager @ Acme Labs", "Acme Labs"),
    ("Pat Example - Acme - LinkedIn", "Experience: Acme · Location: Ohio", None),  # no owner role: never anchors
    ("Pat Example - Acme - LinkedIn", "Location: Ohio", None),
    ("Pat Example - LinkedIn", "Founder @ Acme. · Location: Ohio", "Acme"),
    ("Pat Example - Affiliate Consultant at Other Co | LinkedIn", "Consultant @ Other Co.", None),
    ("Pat Example - Founder | We help brands grow | LinkedIn", "We help brands grow", None),
    ("Pat Example - Founder at Talent Hub Staffing | LinkedIn", "", None),
])
def test_google_title_and_description_parse(title, desc, expected):
    assert lp._company_from_result(title, desc, "Pat Example", CFG) == expected


def test_profile_results_must_match_slug_and_name_and_agree_on_one_company():
    res = [{"url": "https://www.linkedin.com/in/pat-example", "title": "Pat Example - Founder at Acme | LinkedIn",
            "description": "Founder @ Acme."},
           {"url": "https://www.linkedin.com/in/pat-example-other", "title": "Pat Example - Founder at Beta | LinkedIn",
            "description": ""},
           {"url": "https://www.linkedin.com/in/sam-sample", "title": "Sam Sample - Founder at Gamma | LinkedIn",
            "description": ""}]
    assert lp._company_from_results(res, "pat-example", "Pat Example", CFG) == "Acme"
    assert lp._company_from_results(res[:1] + [dict(res[0], title="Pat Example - Founder at Beta | LinkedIn")],
                                    "pat-example", "Pat Example", CFG) is None  # two companies: ambiguous
    assert lp._company_from_results([res[2]], "pat-example", "Pat Example", CFG) is None  # other person's profile


def test_brand_homepage_must_name_the_domain_label_and_not_be_a_seller():
    good = home("https://acme-shop.com/", "<title>Acme Shop | Handmade candles</title>")
    assert lp._brand_from_page(good, "acme-shop.com", CFG) == "Acme Shop"
    assert lp._brand_from_page(home("https://acme-shop.com/", "<title>Nothing Here | Ohio</title>"),
                               "acme-shop.com", CFG) is None
    assert lp._brand_from_page(home("https://acme-shop.com/", "<title>Acme Shop | Affiliate marketing agency</title>"),
                               "acme-shop.com", CFG) is None
    assert lp._brand_from_page(home("https://acme-shop.com/", "<title>Acme Shop</title>",
                                    final_url="https://marketplace.example/acme"), "acme-shop.com", CFG) is None
    assert lp._brand_from_page(None, "acme-shop.com", CFG) is None


@pytest.mark.parametrize("text,name", [
    ("Founder at Acme here: we just launched our affiliate program.", "Acme"),
    ("We at Brightside Goods just launched our affiliate program.", "Brightside Goods"),
    ("Our founder at Scale says we just launched our affiliate program.", None),
    ("Acme's affiliate program pays 30% commission on every sale.", None),  # possessive is not ownership
    ("We're thrilled by the response to our affiliate program.", None),
    ("Founder at Launch Week says we just launched our affiliate program.", None),
])
def test_text_named_company_needs_first_person_framing(text, name):
    assert lp._text_company(text, CFG) == name


# collect: fixtures, budget, privacy --------------------------------------------------------------------------------

def test_fixture_run_keeps_the_buyer_and_anchors_it_on_its_own_homepage():
    data = json.loads((FIX / "search_affiliate.json").read_text())
    sc = FakeSC(search={AFFIL_PHRASE: [data]})
    lines = []
    pages = {"https://acme-shop.com/": home("https://acme-shop.com/",
                                            (FIX / "homepage_acme_shop.html").read_text())}
    out = run(lp.collect(make_ctx(sc, limit=30, pages=pages, lines=lines)))
    assert len(out) == 1
    c = out[0]
    assert (c.list, c.company_name, c.domain, c.linkedin_url, c.require_us) == \
        ("affiliate", "Acme Shop", "acme-shop.com", None, True)
    s = c.signal
    assert s.kind == "person_post" and s.platform == "linkedin" and s.signal_date == "2026-10-05"
    assert s.url == "https://www.linkedin.com/feed/update/urn:li:activity:7511000000000000001"
    assert s.quote == "We just launched our affiliate program: 30% recurring commission on every sale."
    assert s.person_name == "Pat Example" and s.person_url == "https://www.linkedin.com/in/pat-example"
    assert sc.meter.n == 1 and not [p for p, _ in sc.calls if p == lp.GOOGLE]
    joined = " ".join(lines).lower()
    assert "pat" not in joined and "example" not in joined and "sam sample" not in joined


def test_text_named_company_needs_no_profile_lookup():
    sc = FakeSC(search={AFFIL_PHRASE: [{"posts": [post(
        "7511000000000000010", "Founder at Acme here: we just launched our affiliate program with 25% commission.")]}]})
    out = run(lp.collect(make_ctx(sc, limit=30)))
    assert [(c.company_name, c.domain) for c in out] == [("Acme", None)]
    assert not [p for p, _ in sc.calls if p == lp.GOOGLE]


def test_profile_slug_lookup_resolves_an_unanchored_buyer():
    google = json.loads((FIX / "google_profile_pat_example.json").read_text())["results"]
    sc = FakeSC(search={AFFIL_PHRASE: [{"posts": [post(
        "7511000000000000011", "We just launched our affiliate program: 30% recurring commission.")]}]},
                google={"site:linkedin.com/in/pat-example": google})
    out = run(lp.collect(make_ctx(sc, limit=30)))
    assert [(c.company_name, c.domain) for c in out] == [("Acme", None)]
    assert [p for p, _ in sc.calls if p == lp.GOOGLE] == [lp.GOOGLE]


def test_company_pages_and_nameless_posts_never_become_candidates():
    company_post = post("7511000000000000012", "We just launched our affiliate program: 30% commission.",
                        slug="acme-careers", name="Acme Careers")
    company_post["author"]["url"] = "https://www.linkedin.com/company/acme-careers"
    sc = FakeSC(search={AFFIL_PHRASE: [{"posts": [company_post]}]})
    assert run(lp.collect(make_ctx(sc, limit=30))) == []
    assert sc.calls and not [p for p, _ in sc.calls if p == lp.GOOGLE]


def test_old_posts_are_dropped_and_dates_decode_from_the_activity_id():
    day = datetime(2026, 10, 5, tzinfo=timezone.utc)
    aid = str((int(day.timestamp() * 1000) << 22) + 12345)
    fresh = post(aid, "Founder at Acme here: we just launched our affiliate program.", date=None)
    stale = post("7511000000000000013", "Founder at Beta here: we just launched our affiliate program.",
                 name="Lee Sample", slug="lee-sample", date="2026-07-01")
    sc = FakeSC(search={AFFIL_PHRASE: [{"posts": [fresh, stale]}]})
    out = run(lp.collect(make_ctx(sc, limit=30)))
    assert [c.company_name for c in out] == ["Acme"] and out[0].signal.signal_date == "2026-10-05"


def test_canonical_url_drops_the_author_handle_and_tracking():
    p = post("7511000000000000014", "Founder at Acme here: we just launched our affiliate program.",
             url="https://www.linkedin.com/posts/pat-example_activity-7511000000000000014-AbCd?utm_source=share")
    sc = FakeSC(search={AFFIL_PHRASE: [{"posts": [p]}]})
    out = run(lp.collect(make_ctx(sc, limit=30)))
    assert out[0].signal.url == "https://www.linkedin.com/feed/update/urn:li:activity:7511000000000000014"
    assert "pat-example" not in out[0].signal.url


def test_the_same_company_twice_becomes_one_candidate():
    a = post("7511000000000000015", "Founder at Acme here: we just launched our affiliate program.")
    b = post("7511000000000000016", "Founder at Acme here: our affiliate program now pays 30% commission.")
    sc = FakeSC(search={AFFIL_PHRASE: [{"posts": [a, b]}]})
    assert [c.company_name for c in run(lp.collect(make_ctx(sc, limit=30)))] == ["Acme"]


def test_a_stopped_budget_makes_no_search_calls():
    sc = FakeSC(search={AFFIL_PHRASE: [{"posts": []}]})
    assert run(lp.collect(make_ctx(sc, limit=0))) == [] and sc.calls == []


def test_search_pages_respect_the_run_budget():
    data = {"posts": [post("7511000000000000017", "Founder at Acme here: we just launched our affiliate program.")]}
    sc = FakeSC(search={AFFIL_PHRASE: [data], '"join our affiliate program"': [data]})
    run(lp.collect(make_ctx(sc, limit=1)))
    assert sc.meter.n == 1


def test_lookup_cap_stops_profile_lookups():
    sc = FakeSC(search={AFFIL_PHRASE: [{"posts": [post(
        "7511000000000000018", "We just launched our affiliate program: 30% recurring commission.")]}]},
                google={"site:linkedin.com/in/pat-example": json.loads(
                    (FIX / "google_profile_pat_example.json").read_text())["results"]})
    out = run(lp.collect(make_ctx(sc, limit=30, cfg={"max_author_lookups": 0})))
    assert out == [] and not [p for p, _ in sc.calls if p == lp.GOOGLE]


def test_out_of_credits_mid_run_returns_what_was_resolved():
    a = post("7511000000000000019", "Founder at Acme here: we just launched our affiliate program.")
    b = post("7511000000000000020", "We just launched our affiliate program: 20% commission.",
             name="Lee Sample", slug="lee-sample")
    sc = FakeSC(search={AFFIL_PHRASE: [{"posts": [a, b]}]}, raise_on_google=sg.OutOfCredits("402"))
    out = run(lp.collect(make_ctx(sc, limit=30)))
    assert [c.company_name for c in out] == ["Acme"]


def test_finance_post_routes_to_the_finance_list_end_to_end():
    p = post("7511000000000000021", "We're now accepting new ISOs for our merchant cash advance program.",
             name="Robin Sample", slug="robin-sample")
    sc = FakeSC(search={'"now accepting new ISOs"': [{"posts": [p]}]},
                google={"site:linkedin.com/in/robin-sample": [{
                    "url": "https://www.linkedin.com/in/robin-sample", "title": "Robin Sample - Owner at Funder Co | LinkedIn",
                    "description": "Owner @ Funder Co."}]})
    out = run(lp.collect(make_ctx(sc, limit=30)))
    assert [(c.list, c.company_name) for c in out] == [("finance", "Funder Co")]


def test_candidates_carry_no_person_data_outside_the_signal():
    p = post("7511000000000000022", "Founder at Acme here: we just launched our affiliate program.")
    out = run(lp.collect(make_ctx(FakeSC(search={AFFIL_PHRASE: [{"posts": [p]}]}), limit=30)))
    c = out[0]
    assert c.linkedin_url is None and c.domain is None
    assert "Pat" not in c.company_name and "Example" not in c.company_name


# regression: review findings on the first version (every case here failed on that version) ------------------------

@pytest.mark.parametrize("text", [
    "TikTok Shop's affiliate program is now open to all US sellers. Join now and earn commission.",
    "Amazon's affiliate program pays 4% commission; join today.",
])
def test_third_party_brand_possessive_is_not_our_program(text):
    assert lp._screen(text, "affiliate", CFG)[1] is not None
    assert lp._text_company(text, CFG) is None


@pytest.mark.parametrize("text", [
    "We're recruiting brokers, join our team. Competitive base pay and commission.",
    "We just launched our affiliate program for mortgage brokers, 20% commission per funded loan.",
    "We just launched our affiliate program for insurance brokers, 25% commission.",
])
def test_broker_and_iso_wording_routes_to_finance_and_needs_a_funder(text):
    assert lp._screen(text, "affiliate", CFG) == ("finance", "not_funding")


def test_funder_posts_that_name_off_topic_verticals_are_dropped():
    assert lp._screen("We're now accepting new ISOs for our business loans, also mortgages.", "finance", CFG) == \
        ("finance", "non_funding_vertical")


@pytest.mark.parametrize("text,reason", [
    ("I grew our affiliate program at Acme 3x; now open to new opportunities.", "seeker_post"),
    ("We ran our affiliate program for 3 years. I'm looking for my next affiliate manager role.", "seeker_post"),
    ("I grew our affiliate program to 500 partners and I am open to new roles.", "seeker_post"),
    ("We just launched our affiliate program for my side project, 25% recurring commission.", "side_project"),
])
def test_seekers_and_side_projects_are_dropped(text, reason):
    assert lp._screen(text, "affiliate", CFG) == ("affiliate", reason)


@pytest.mark.parametrize("title,desc", [
    ("Pat Example - Software Engineer at Acme | LinkedIn", "Software Engineer @ Acme"),
    ("Pat Example - Director of Engineering at Acme - LinkedIn", "Director of Engineering @ Acme"),
    ("Pat Example - Acme - LinkedIn", "Experience: Acme · Location: Ohio"),
    ("Pat Example - Operator at Acme | LinkedIn", "Operator @ Acme"),
])
def test_profile_anchor_needs_an_owner_role(title, desc):
    assert lp._company_from_result(title, desc, "Pat Example", CFG) is None


def test_owner_roles_still_anchor():
    assert lp._company_from_result("Pat Example - Head of Growth at Acme | LinkedIn", "Head of Growth @ Acme",
                                   "Pat Example", CFG) == "Acme"
    assert lp._company_from_result("Pat Example - Affiliate Manager at Acme | LinkedIn", "", "Pat Example", CFG) == "Acme"


@pytest.mark.parametrize("domain,title", [("shopfast.io", "Shop | Fast checkout"),
                                          ("partnerspro.com", "Partners"), ("getlemon.com", "Lemon")])
def test_generic_homepage_titles_do_not_name_the_brand(domain, title):
    assert lp._brand_from_page(home(f"https://{domain}/", f"<title>{title}</title>"), domain, CFG) is None


def test_truncated_headline_company_needs_the_experience_line():
    title = "Pat Example - Co-Founder at Acme Gro… | LinkedIn"
    assert lp._company_from_result(title, "Growth bio", "Pat Example", CFG) is None
    assert lp._company_from_result(title, "Co-Founder @ Acme Growth", "Pat Example", CFG) == "Acme Growth"


def test_advisor_bio_wording_is_rejected():
    assert lp._company_from_result("Pat Example - Founder at Acme | LinkedIn",
                                   "Founder @ Acme. I taught founders who grow brands.", "Pat Example", CFG) is None


def test_third_party_name_in_the_quote_is_never_stored():
    assert lp._program_sentence("Welcome Pat Example as the new lead of our affiliate program.", []) is None
    assert lp._unknown_runs("Lee Sample just joined our affiliate program with 30% commission.") == ["Lee Sample"]
    assert lp._unknown_runs("Join Our Affiliate Program today: 30% recurring commission.") == []
    p = post("7511000000000000030",
             "Founder at Acme here: Lee Sample just joined our affiliate program with 30% commission.")
    out = run(lp.collect(make_ctx(FakeSC(search={AFFIL_PHRASE: [{"posts": [p]}]}), limit=30)))
    assert out == []


def test_untied_domain_does_not_anchor_the_brand():
    p = post("7511000000000000031", "We just launched our affiliate program for our customers. Shop at acme-shop.com today.")
    pages = {"https://acme-shop.com/": home("https://acme-shop.com/", "<title>Acme Shop | Candles</title>")}
    ctx = make_ctx(FakeSC(search={AFFIL_PHRASE: [{"posts": [p]}]}), limit=30, pages=pages)
    assert run(lp.collect(ctx)) == [] and ctx.http_used == 0


def _malformed_posts():
    good = "Founder at Acme here: we just launched our affiliate program."
    cases = []
    c = post("7511000000000000040", good)
    c.pop("datePublished"), c.update(activityUrn=None, url=None)
    cases.append(c)
    c = post("7511000000000000040", good)
    c["author"] = "Pat Example"
    cases.append(c)
    c = post("7511000000000000040", good)
    c["author"]["url"] = ["https://www.linkedin.com/in/pat-example"]
    cases.append(c)
    c = post("7511000000000000040", good)
    c["description"] = [good]
    cases.append(c)
    c = post("7511000000000000040", good)
    c["datePublished"] = ["2026-10-05"]
    cases.append(c)
    cases += [None, "junk"]
    return cases


@pytest.mark.parametrize("bad", _malformed_posts())
def test_malformed_posts_are_skipped_not_fatal(bad):
    good = post("7511000000000000041", "Founder at Acme here: we just launched our affiliate program.")
    sc = FakeSC(search={AFFIL_PHRASE: [{"posts": [bad, good]}]})
    out = run(lp.collect(make_ctx(sc, limit=30)))
    assert len(out) == 1 and out[0].company_name == "Acme"  # the malformed post is skipped or survives as a real post


def test_search_budget_keeps_credits_for_profile_lookups():
    buyer = post("7511000000000000050", "We just launched our affiliate program: 30% recurring commission.")
    others = [post(f"75110000000000000{51 + i}", "Affiliate programs are a great way to grow any brand.",
                   name="Lee Sample", slug="lee-sample") for i in range(2)]
    google = json.loads((FIX / "google_profile_pat_example.json").read_text())["results"]
    sc = FakeSC(search={AFFIL_PHRASE: [{"posts": [buyer]}],
                        '"join our affiliate program"': [{"posts": [others[0]]}],
                        '"launched our affiliate program"': [{"posts": [others[1]]}]},
                google={"site:linkedin.com/in/pat-example": google})
    out = run(lp.collect(make_ctx(sc, limit=2)))
    assert [c.company_name for c in out] == ["Acme"]
    assert sc.meter.n == 2  # one search and one profile lookup: the run's 2 credits, none left unspent
