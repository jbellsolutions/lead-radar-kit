"""meta_ads source: offline tests on synthetic payloads shaped like the live Ad Library responses (no network).
Brands and people are invented; 'Pat Example' and 'Sam Example' are synthetic names."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lead_radar.programs import signals as sg, sources as srcs
from lead_radar.programs.sources import meta_ads as ma

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
FIX = Path(__file__).parent / "fixtures" / "sources" / "meta_ads"
AFFILIATE_QUERIES = [q for q, _ in ma.QUERIES]


class FakeMeter:
    def __init__(self):
        self.n = 0

    def total_credits(self):
        return self.n


class FakeSC:
    """Search rows for every query (or per query), company-ads rows per pageId; 1 credit per call."""

    def __init__(self, search_rows=(), company=None, per_query=None, fail_queries=()):
        self.search_rows, self.company = list(search_rows), company or {}
        self.per_query, self.fail_queries = per_query or {}, set(fail_queries)
        self.meter, self.exhausted, self.calls = FakeMeter(), False, []

    async def get(self, path, params, source=""):
        self.meter.n += 1
        self.calls.append((path, dict(params)))
        if path == ma.SEARCH_PATH:
            if params["query"] in self.fail_queries:
                raise sg.SkipItem("404")
            return {"success": True, "searchResults": self.per_query.get(params["query"], self.search_rows),
                    "cursor": ""}
        if path == ma.COMPANY_ADS_PATH:
            return {"success": True, "results": self.company.get(params["pageId"], []), "cursor": ""}
        raise sg.SkipItem("unexpected path")


def run(sc, *, days=30, budget=100, company_max=3, queries=None, http_cap=0, get=None):
    """Runs collect() offline. http_cap=0 makes ctx.get return None without touching the (absent) HTTP client; pass
    get=<async fn(url) -> Page|None> to serve landing pages from memory."""
    lines = []
    cfg = dict(ma.DEFAULTS)
    cfg["company_ads_max"] = company_max
    if queries is not None:
        cfg["queries"] = queries
    ctx = srcs.SourceContext(sc=sc, http=None, budget=sg.Budget(sc, budget), cfg=cfg, signals_cfg=sg.load(),
                             days=days, now=NOW, say=lines.append, http_cap=http_cap)
    if get is not None:
        ctx.get = get
    return asyncio.run(ma.collect(ctx)), lines, ctx


def ad(ad_id, page_id, page, link, *, day="2026-10-08", copy="", title="", desc="", caption=None,
       cats=("Sportswear",), active=True, extra=()):
    start = datetime.fromisoformat(day + "T07:00:00+00:00")
    return {"ad_archive_id": ad_id, "page_id": page_id, "page_name": page, "is_active": active,
            "page_is_deleted": False, "start_date": int(start.timestamp()),
            "start_date_string": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "snapshot": {"page_name": page, "caption": caption, "link_url": link, "link_description": desc,
                         "title": title, "body": {"text": copy}, "page_categories": list(cats),
                         "extra_links": list(extra)}}


def fixture_rows():
    return json.loads((FIX / "search_affiliate.json").read_text())["searchResults"]


def fixture_company():
    return {"5007": json.loads((FIX / "company_ads_cobalt.json").read_text())["results"]}


# registry and settings -------------------------------------------------------------------------------------------

def test_registry_and_settings():
    assert ma.NAME == "meta_ads" and srcs.REGISTRY["meta_ads"] == "meta_ads"
    assert srcs.load("meta_ads") is ma
    conf = srcs.settings("meta_ads", ma, sg.load())
    assert conf["enabled"] is True and conf["credits"] == 30
    assert len(conf["queries"]) == 13
    assert "paid_ad" in sg.KINDS and "meta_ads" in sg.PLATFORM_LABEL


# realistic payload end to end ----------------------------------------------------------------------------------

def test_realistic_payload_keeps_six_brands_and_no_person():
    sc = FakeSC(search_rows=fixture_rows(), company=fixture_company())
    cands, lines, ctx = run(sc)
    got = [(c.company_name, c.list, c.domain) for c in cands]
    assert got == [
        ("Marigold Beauty", "affiliate", "marigoldbeauty.example"),
        ("Acme Gear Co", "affiliate", "acmegear.com"),
        ("Vesper Ridge Capital", "finance", "vesperridgecap.example"),
        ("Orchid Labs", "affiliate", "orchidlabs.com"),
        ("Cobalt Goods", "affiliate", "cobaltgoods.example"),
        ("Juniper Golf", "affiliate", "junipergolf.example"),
    ]
    assert sc.meter.n == 13 + 3          # 13 phrase searches (ads deduped across phrases) + 3 company-ads pulls
    assert ctx.http_used == 0            # no page fetches at all
    assert len(lines) == 1 and "6 brands" in lines[0] and "3 company-ads pulls" in lines[0]
    output = " ".join(lines) + repr([c.company_name for c in cands]) + repr([c.signal.quote for c in cands])
    for name in ("Pat Example", "Sam Example", "synthetic.person"):
        assert name not in output


def test_candidate_fields_are_canonical():
    cands, _, _ = run(FakeSC(search_rows=fixture_rows(), company=fixture_company()))
    acme = next(c for c in cands if c.company_name == "Acme Gear Co")
    sig = acme.signal
    assert sig.kind == "paid_ad" and sig.platform == "meta_ads"
    assert sig.url == "https://www.facebook.com/ads/library/?id=9000000000000001"   # newest ad, no tracking
    assert sig.signal_date == "2026-10-08" and sig.person_name is None and sig.person_url is None
    assert sig.quote == "Join Our Affiliate Program"          # verbatim, from the link description
    assert len(sig.quote) <= 400
    assert acme.require_us is True and acme.linkedin_url is None and acme.domain == "acmegear.com"
    assert "profile" not in repr(acme)


def test_finance_brand_is_listed_as_finance():
    cands, _, _ = run(FakeSC(search_rows=fixture_rows(), company=fixture_company()))
    vesperridge = next(c for c in cands if c.company_name == "Vesper Ridge Capital")
    assert vesperridge.list == "finance" and vesperridge.signal.signal_date == "2026-10-06"


# row gate: each filter drops its case -------------------------------------------------------------------------------

@pytest.mark.parametrize("row,reason", [
    (ad("1", "1", "Inactive Gear", "https://inactivegear.example/affiliate", active=False,
        copy="Join our affiliate program"), "inactive ad"),
    (ad("2", "2", "Old Gear", "https://oldgear.example/affiliate", day="2026-07-01",
        copy="Join our affiliate program"), "outside window"),
    (ad("3", "3", "Pat Example", "https://pat.example/affiliate", cats=("Digital Creator",),
        copy="Join my affiliate program"), "person page"),
    (ad("4", "4", "Bank Rewards", "https://bank.example/refer", copy="Refer a friend and get a $20 gift card."),
     "consumer refer-a-friend"),
    (ad("5", "5", "Launch Co", "https://launch.example/", copy="Launch your own affiliate program with our software"),
     "seller"),
    (ad("6", "6", "Team Co", "https://team.example/affiliate", copy="Join my team and sell ads for passive income"),
     "mlm"),
    (ad("7", "7", "Casino Hub", "https://casino.example/affiliate", copy="Best casino affiliate program 50% revshare"),
     "vertical"),
    (ad("8", "8", "Code Shop", "https://codeshop.example/shop", copy="Use my code SAVE10 for 10% off"),
     "individual_affiliate"),
    (ad("9", "9", "Summer Shop", "https://summershop.example/collections/summer",
        copy="Summer collection drops today"), "no buyer language"),
    (ad("10", "10", "Acme Gear", "https://acmegear.example/affiliate", copy="Join our affiliate program"), None),
    (ad("11", "11", "Ambassador Co", "https://ambco.example/pages/ambassador-program",
        copy="Brand ambassador application for our apparel brand."), None),
    (ad("12", "12", "Uppro Brand", "https://af.uppromote.com/uprobrand/register", copy="Start earning."), None),
])
def test_row_gate_reasons(row, reason):
    assert ma.row_gate(row, 30, NOW, sg.load()["_rx"]) == reason


def test_window_is_capped_at_sixty_days():
    rows = [ad("1", "1", "Old Gear", "https://oldgear.example/affiliate", day="2026-07-20",
               copy="Join our affiliate program"),
            ad("2", "2", "Mid Gear", "https://midgear.example/affiliate", day="2026-08-20",
               copy="Join our affiliate program")]
    cands, lines, _ = run(FakeSC(search_rows=rows), days=90)
    assert [c.company_name for c in cands] == ["Mid Gear"]
    assert "outside window 1" in lines[0]


@pytest.mark.parametrize("row,reason", [
    (ad("1", "1", "Youth Alliance", "https://youth.example/ambassadors", copy="Apply by October 22.",
        cats=("Nonprofit organization",)), "non-commercial page"),
    (ad("2", "2", "Course Shop", "https://courses.example/iso-9001", copy="Build auditing skills.",
        cats=("Education website",)), "non-commercial page"),
    (ad("3", "3", "Playbook Co", "https://playbook.example/affiliate",
        copy="This playbook breaks down how to build a creator program."), "seller"),
    (ad("4", "4", "Creator Suite", "https://suite.example/affiliate",
        copy="Discover creators and automate outreach for your program."), "seller"),
    (ad("5", "5", "Loan Apply", "https://loans.example/apply", copy="Business financing only. Get funded today."),
     "no buyer language"),
    (ad("6", "6", "Cert Co", "https://cert.example/iso-9001-lead-auditor", copy="ISO 9001 certification."),
     "no buyer language"),
    (ad("7", "7", "Apply Brand", "https://applybrand.example/apply", copy="Join our affiliate program. Apply here."),
     None),
])
def test_row_gate_new_rules(row, reason):
    assert ma.row_gate(row, 30, NOW, sg.load()["_rx"]) == reason


def test_referral_wording_marks_a_finance_partner_program():
    rows = [ad("2", "2", "Kestrel Funding Co", "https://kestrelfunding.example/partner/",
               copy="Refer them for $10K to $500K+ in working capital. Business financing only.")]
    cands, _, _ = run(FakeSC(search_rows=rows), company_max=0)
    assert [(c.list, c.domain) for c in cands] == [("finance", "kestrelfunding.example")]


def test_funder_partner_copy_with_lenders_is_finance():
    rows = [ad("1", "1", "Lanternly Co", "https://lanternly.example/become-partner",
               copy="Become a partner. Refer a client to 150+ lenders. Referring takes a few steps.")]
    cands, _, _ = run(FakeSC(search_rows=rows), company_max=0)
    assert [(c.list, c.domain) for c in cands] == [("finance", "lanternly.example")]


def test_finance_vendor_is_dropped():
    rows = [ad("1", "1", "Lender Software Co", "https://lendertools.example/iso-partners",
               copy="Become an ISO partner: get our software for lenders with merchant cash advance leads.")]
    cands, lines, _ = run(FakeSC(search_rows=rows))
    assert cands == [] and "finance vendor 1" in lines[0]


# names and domains ----------------------------------------------------------------------------------------------

def test_person_like_name_is_dropped_even_with_a_matching_domain():
    rows = [ad("1", "1", "Sam Example", "https://www.examplestudio.com/affiliate", copy="Join my affiliate program")]
    cands, lines, _ = run(FakeSC(search_rows=rows))
    assert cands == [] and "person-like page name 1" in lines[0]
    assert "Sam" not in " ".join(lines)


@pytest.mark.parametrize("name,personal", [
    ("Pat Example", True),
    ("Mary Ann Smith", True),
    ("Fern Capital", False),
    ("Velvet Looks", False),
    ("Marigold Beauty", False),
    ("Harbor Gear Swag", False),
    ("Zephyra AI", False),
    ("Verdant Works.", False),
])
def test_person_like_name_rule(name, personal):
    assert ma.looks_personal(name) is personal


@pytest.mark.parametrize("name,root,ok", [
    ("Harbor Gear Swag", "harborgearswag.com", True),
    ("NY Yarn Sales (YARNNOW)", "yarnnow.com", True),
    ("Summit 26 Capital", "summit26cap.com", True),
    ("KEYSTONEgg", "keystone.gg", True),
    ("Wellness Retreat Co", "quietharbor.com", False),
    ("Olympian Labs", "fernbridge.com", False),
])
def test_root_must_carry_a_name_token(name, root, ok):
    assert ma.root_matches(root, name) is ok


def test_facebook_redirect_wrapper_is_unwrapped_to_the_brand():
    wrapped = ("https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.vesperridgecap.example%2Fiso-partners%3Futm_source%3Dfb"
               "&h=AT0x")
    rows = [ad("1", "1", "Vesper Ridge Capital", wrapped, copy="Become an ISO partner for merchant cash advance funding.")]
    cands, _, _ = run(FakeSC(search_rows=rows))
    assert [(c.domain, c.list) for c in cands] == [("vesperridgecap.example", "finance")]


def test_platform_only_links_use_company_ads_then_drop_when_none():
    rows = [ad("1", "5007", "Cobalt Goods", "https://www.facebook.com/cobaltgoods",
               copy="Join our affiliate program and earn commission on every order.")]
    sc = FakeSC(search_rows=rows, company=fixture_company())
    cands, _, _ = run(sc, queries=[["join our affiliate program", True]])
    assert [c.domain for c in cands] == ["cobaltgoods.example"]
    assert [p for p, _ in sc.calls].count(ma.COMPANY_ADS_PATH) == 1
    sc2 = FakeSC(search_rows=rows, company=fixture_company())
    cands2, lines2, _ = run(sc2, queries=[["join our affiliate program", True]], company_max=0)
    assert cands2 == [] and all(p != ma.COMPANY_ADS_PATH for p, _ in sc2.calls)
    assert "no brand domain 1" in lines2[0]


def test_brand_without_its_name_in_the_domain_is_not_guessed():
    rows = [ad("1", "9", "Wellness Retreat Co", "https://quietharbor.example/tickets/wellness",
               copy="Recruiting Chicago wellness creators for an affiliate program.")]
    cands, lines, _ = run(FakeSC(search_rows=rows), company_max=0)
    assert cands == [] and "no brand domain 1" in lines[0]


def test_caption_names_the_domain_when_the_landing_is_hosted():
    rows = [ad("1", "4", "Orchid Labs", "https://af.uppromote.com/orchidlabs/register", caption="orchidlabs.com",
               copy="Start earning with our referral program.")]
    cands, _, _ = run(FakeSC(search_rows=rows))
    assert [(c.company_name, c.domain) for c in cands] == [("Orchid Labs", "orchidlabs.com")]


def test_one_candidate_per_brand_newest_ad_as_signal():
    rows = [ad("1", "1", "Acme Gear Co", "https://acmegear.com/affiliate", day="2026-09-20",
               copy="Join our affiliate program."),
            ad("2", "2", "Acme Gear Shop", "https://acmegear.com/affiliates", day="2026-10-08",
               copy="Join our affiliate program for creators.")]
    cands, _, _ = run(FakeSC(search_rows=rows))
    assert len(cands) == 1 and cands[0].domain == "acmegear.com"
    assert cands[0].signal.url.endswith("id=2") and cands[0].signal.signal_date == "2026-10-08"


# budget, dedupe, errors -------------------------------------------------------------------------------------------

def test_ads_are_deduped_across_phrases():
    rows = [ad("1", "1", "Acme Gear Co", "https://acmegear.com/affiliate", copy="Join our affiliate program.")]
    cands, lines, _ = run(FakeSC(search_rows=rows))
    assert len(cands) == 1 and "1 ads" in lines[0]


def test_zero_budget_makes_no_call():
    sc = FakeSC(search_rows=fixture_rows(), company=fixture_company())
    cands, _, _ = run(sc, budget=0)
    assert cands == [] and sc.calls == []


def test_budget_stops_mid_run():
    sc = FakeSC(search_rows=fixture_rows(), company=fixture_company())
    run(sc, budget=2, company_max=0)
    assert len(sc.calls) == 2 and all(p == ma.SEARCH_PATH for p, _ in sc.calls)


def test_failed_queries_are_tolerated():
    sc = FakeSC(fail_queries=AFFILIATE_QUERIES)
    cands, lines, _ = run(sc)
    assert cands == [] and "0 ads" in lines[0]
    assert len(sc.calls) == len(AFFILIATE_QUERIES)


def test_phrase_list_matches_the_contract():
    queries = dict(ma.QUERIES)
    assert queries["join our affiliate program"] is True and queries["ISO partners"] is True
    assert "become an ISO" in queries and "referral partner program" in queries and "creator program" in queries


# quote and dates -----------------------------------------------------------------------------------------------------

def test_quote_starts_and_ends_on_word_boundaries():
    copy = ("lorem ipsum " * 60) + "join our affiliate program today " + ("dolor sit amet " * 40)
    quote = ma.excerpt(copy, ma.QUOTE_CENTER)
    assert len(quote) <= 400 and "join our affiliate program today" in quote
    assert quote.split()[0] in copy.split() and quote.split()[-1] in copy.split()
    assert not quote.startswith(" ") and not quote.endswith(" ")
    assert ma.excerpt("short text", None) == "short text"


def test_quote_is_short_and_verbatim():
    copy = ("x " * 300) + "Join our affiliate program and earn 15% commission. " + ("y" * 200)
    rows = [ad("1", "1", "Acme Gear Co", "https://acmegear.com/affiliate", copy=copy)]
    cands, _, _ = run(FakeSC(search_rows=rows), queries=[["join our affiliate program", True]])
    quote = cands[0].signal.quote
    assert 0 < len(quote) <= 400
    assert quote in " ".join(copy.split())


@pytest.mark.parametrize("row,expected", [
    ({"start_date": 1791529200}, "2026-10-09"),
    ({"start_date": "1791529200"}, "2026-10-09"),
    ({"start_date": True}, None),
    ({"start_date_string": "2026-09-30T07:00:00.000Z"}, "2026-09-30"),
    ({}, None),
])
def test_start_date(row, expected):
    assert ma.start_iso(row) == expected


def test_scrub_removes_personal_profile_fields():
    row = {"page_profile_uri": "https://www.facebook.com/synthetic.person.profile", "snapshot": {
        "page_profile_picture_url": "https://img.example/p.jpg", "link_url": "https://acmegear.com/affiliate"}}
    assert ma.scrub(row) == {"snapshot": {"link_url": "https://acmegear.com/affiliate"}}


def test_company_name_bounded_and_non_empty():
    assert ma.page_name({"page_name": "  Acme  "}) == "Acme"
    assert ma.page_name({}) == ""


# review regressions: buyer gate, malformed rows, funder routing, quote choice, vertical page, budget --------------------

@pytest.mark.parametrize("link,copy", [
    ("https://tidewell.example/join-waitlist", "Join the waitlist for our next flavour."),
    ("https://tidewell.example/affiliate-disclosure", "Shop now and save 20% this weekend."),
    ("https://tidewell.example/partners-club", "Shop the spring drop."),
    ("https://tidewell.example/creators-collection", "Summer collection drops today."),
    ("https://loanpath.example/partner/", "Business financing only. Get funded today."),
])
def test_path_alone_is_not_a_buyer_signal(link, copy):
    """A program-looking landing path with no program wording in the copy is not a buyer (review: 6 of 6 leaked)."""
    row = ad("1", "1", "Tidewell Goods", link, copy=copy)
    assert ma.row_gate(row, 30, NOW, sg.load()["_rx"]) == "no buyer language"


def test_path_with_program_copy_is_a_buyer():
    row = ad("1", "1", "Tidewell Goods", "https://tidewell.example/pages/ambassador-program",
             copy="Earn a 10% commission on every sale.")
    assert ma.row_gate(row, 30, NOW, sg.load()["_rx"]) is None


def test_no_dead_apply_clause_left():
    assert not hasattr(ma, "APPLY_PATH")


@pytest.mark.parametrize("bad", [
    {"snapshot": None},
    {"snapshot": {"link_url": "https://[bad", "body": {"text": "Join our affiliate program"}}},
    {"start_date": float("nan")},
    {"start_date": float("inf")},
    {"start_date": 10 ** 400},
    {"snapshot": {"link_url": "https://badlist.example/affiliate", "extra_links": 5, "page_categories": 5,
                  "body": {"text": "Join our affiliate program"}}},
    {"ad_archive_id": "abc"},
    {"ad_archive_id": {"nested": 1}},
])
def test_malformed_row_is_dropped_and_the_good_row_survives(bad):
    good = ad("1", "1", "Acme Gear Co", "https://acmegear.com/affiliate", copy="Join our affiliate program")
    broken = {**ad("2", "2", "Badrow Co", "https://badrow.example/affiliate", copy="Join our affiliate program"), **bad}
    cands, lines, _ = run(FakeSC(search_rows=[good, broken]))
    assert [c.domain for c in cands] == ["acmegear.com"]


def test_non_dict_row_and_list_body_are_tolerated():
    good = ad("1", "1", "Acme Gear Co", "https://acmegear.com/affiliate", copy="Join our affiliate program")
    sc = FakeSC(search_rows=[good, "not a row", None, 7])
    cands, lines, _ = run(sc)
    assert [c.domain for c in cands] == ["acmegear.com"] and "malformed row" in lines[0]


def test_list_response_and_error_body_yield_nothing():
    class ListSC(FakeSC):
        async def get(self, path, params, source=""):
            self.meter.n += 1
            return [{"searchResults": []}]

    cands, _, _ = run(ListSC())
    assert cands == []


def test_non_digit_ad_id_is_dropped():
    rows = [ad("abc", "1", "Acme Gear Co", "https://acmegear.com/affiliate", copy="Join our affiliate program")]
    cands, lines, _ = run(FakeSC(search_rows=rows))
    assert cands == [] and "bad ad id 1" in lines[0]


@pytest.mark.parametrize("row,expected", [
    ({"start_date": 1791529200000}, "2026-10-09"),
    ({"start_date": float("nan")}, None),
    ({"start_date": float("inf")}, None),
    ({"start_date": 10 ** 400}, None),
    ({"start_date": -10 ** 15}, None),
    ({"start_date_string": "2026-13-45"}, None),
])
def test_start_date_guards(row, expected):
    assert ma.start_iso(row) == expected


def test_funder_recruiting_affiliates_is_finance():
    rows = [ad("1", "1", "Bramble Capital", "https://bramblecap.example/affiliate",
               copy="Join our Affiliate Program to earn residual income on funded deals")]
    cands, _, _ = run(FakeSC(search_rows=rows), company_max=0)
    assert [(c.list, c.domain) for c in cands] == [("finance", "bramblecap.example")]


def test_signal_comes_from_the_ad_with_recruiting_copy():
    newest = ad("2", "2", "Kestrel Funding", "https://af.uppromote.com/kestrelfunding/register", day="2026-10-08",
                copy="Business financing only. No startups.")
    older = ad("1", "2", "Kestrel Funding", "https://kestrelfunding.example/partner/", day="2026-10-01",
               copy="Refer them for $10K to $500K+ in working capital. Business financing only.")
    cands, _, _ = run(FakeSC(search_rows=[newest, older]), company_max=0)
    assert len(cands) == 1
    sig = cands[0].signal
    assert sig.url.endswith("id=1") and sig.signal_date == "2026-10-01"
    assert sig.quote.startswith("Refer them") and cands[0].list == "finance"


def test_quote_and_url_come_from_the_same_ad():
    newest = ad("2", "1", "Acme Gear Co", "https://acmegear.example/affiliate", day="2026-10-08",
                copy="Earn a 10% commission on every sale.")
    older = ad("1", "1", "Acme Gear Co", "https://acmegear.example/affiliate", day="2026-10-01",
               copy="Join our affiliate program.")
    cands, _, _ = run(FakeSC(search_rows=[newest, older]), company_max=0)
    sig = cands[0].signal
    assert sig.url.endswith("id=1") and sig.quote == "Join our affiliate program."


def test_refer_does_not_match_inside_prefer_or_transfer():
    assert ma.QUOTE_CENTER.search("Refer your partners") and not ma.QUOTE_CENTER.search("We prefer cash transfers")


def test_default_company_ads_cap_is_eight():
    assert ma.DEFAULTS["company_ads_max"] == 8


def test_person_page_gets_no_company_ads_pull():
    rows = [ad("1", "1", "Sam Example", "https://sam.example/affiliate", copy="Join our affiliate program")]
    sc = FakeSC(search_rows=rows)
    cands, lines, _ = run(sc, company_max=3)
    assert cands == [] and all(p != ma.COMPANY_ADS_PATH for p, _ in sc.calls)


class _Page:
    def __init__(self, html):
        self.html = html


def test_restricted_vertical_landing_page_drops_the_brand_and_its_other_pages():
    rows = [ad("1", "1", "Aminolike Labs", "https://aminolike.example/affiliates",
               copy="Join our affiliate program and earn 15% commission."),
            ad("2", "2", "Aminolike Shop", "https://aminolike.example/collab", day="2026-10-05",
               copy="Join our affiliate program.")]
    pages = {"https://aminolike.example/affiliates": _Page(
        "<html><body><p>For research use only. Not for human consumption.</p></body></html>")}

    async def get(url, role="page"):
        return pages.get(url)

    cands, lines, _ = run(FakeSC(search_rows=rows), company_max=0, get=get)
    assert cands == [] and "restricted vertical landing page 1" in lines[0]
    assert "same brand as another page 1" in lines[0]


def test_vertical_words_inside_script_or_style_are_ignored():
    rows = [ad("1", "1", "Aminolike Labs", "https://aminolike.example/affiliates",
               copy="Join our affiliate program and earn 15% commission.")]
    page = _Page("<html><head><script>var n = 'research use only';</script><style>.x{}</style></head>"
                 "<body><p>Join our affiliate program.</p></body></html>")

    async def get(url, role="page"):
        return page

    cands, _, _ = run(FakeSC(search_rows=rows), company_max=0, get=get)
    assert [c.domain for c in cands] == ["aminolike.example"]


def test_failed_landing_fetch_keeps_the_brand_on_ad_copy():
    rows = [ad("1", "1", "Acme Gear Co", "https://acmegear.example/affiliate", copy="Join our affiliate program.")]

    async def get(url, role="page"):
        return None

    cands, _, _ = run(FakeSC(search_rows=rows), company_max=0, get=get)
    assert [c.domain for c in cands] == ["acmegear.example"]


def test_portal_ad_with_no_copy_is_dropped_not_guessed():
    rows = [ad("1", "1", "Acme Gear Co", "https://af.uppromote.com/acmegear/register", copy="",
               extra=["https://acmegear.example/"])]
    cands, lines, _ = run(FakeSC(search_rows=rows), company_max=0)
    assert cands == [] and "no ad copy 1" in lines[0]


def test_payday_disclaimer_on_a_funder_landing_page_does_not_drop_it():
    rows = [ad("1", "1", "Kestrel Funding Co", "https://kestrelfunding.example/partner/",
               copy="Refer them for $10K to $500K+ in working capital. Business financing only.")]
    page = _Page("<html><body><p>This is not a payday loan. Refer a business owner today.</p></body></html>")

    async def get(url, role="page"):
        return page

    cands, _, _ = run(FakeSC(search_rows=rows), company_max=0, get=get)
    assert [(c.list, c.domain) for c in cands] == [("finance", "kestrelfunding.example")]
