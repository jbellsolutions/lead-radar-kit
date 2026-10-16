"""Program lane: detection rules (from the M0 spike), seed parsing, portal resolution, store, export."""
import asyncio
import csv
from pathlib import Path

import pytest

from lead_radar.db import Store
from lead_radar.programs import detect, run as prun, seeds

FIX = Path(__file__).parent / "fixtures" / "programs"


def html(name: str) -> str:
    return (FIX / name).read_text()


def page(name: str, url: str, role: str = "home", final: str | None = None, status: int = 200) -> detect.Page:
    return detect.Page(url, final or url, status, html(name), role)


# detection -------------------------------------------------------------------------------------------------

def test_partnerstack_portal_and_commission_on_program_page():
    det = detect.decide("kit.com", [page("plain_home.html", "https://kit.com/"),
                                    page("kit_affiliate.html", "https://kit.com/affiliate", "linked")])
    assert det.has_program and det.platforms == ["partnerstack"]
    assert det.program_url == "https://kit.com/affiliate"
    assert "50% recurring commission" in det.commission_text
    assert det.evidence[0]["slug"] == "kit" and "company=kit&group" in det.evidence[0]["match"]


def test_site_wide_scripts_find_two_platforms_on_the_homepage():
    det = detect.decide("instantly.ai", [page("instantly_home.html", "https://instantly.ai/")])
    assert det.platforms == ["partnerstack", "rewardful"]
    assert {e["kind"] for e in det.evidence} == {"script"}
    assert det.segment == "saas"


def test_prose_mentions_never_name_a_platform():
    det = detect.decide("lemlist.com", [page("lemlist_affiliate.html", "https://lemlist.com/affiliates", "linked")])
    assert det.platforms == ["partnerstack"]
    assert [e["slug"] for e in det.evidence] == ["lemlist"]  # support.partnerstack.com is the platform's own


def test_impact_brand_portal():
    det = detect.decide("shopify.com", [page("shopify_affiliates.html", "https://shopify.com/affiliates", "linked")])
    assert det.platforms == ["impact"] and det.evidence[0]["slug"] == "shopify"


def test_in_house_program_needs_a_program_page_with_commission_language():
    det = detect.decide("clickfunnels.com", [page("plain_home.html", "https://clickfunnels.com/"),
                                            page("clickfunnels_affiliates.html", "https://clickfunnels.com/affiliates", "guessed")])
    assert det.has_program and det.platforms == ["in-house"]
    assert "30% Monthly Recurring Commission" in det.commission_text


def test_program_page_that_redirects_home_is_not_a_program():
    det = detect.decide("kajabi.com", [page("kajabi_home.html", "https://kajabi.com/"),
                                      page("kajabi_home.html", "https://kajabi.com/affiliates", "linked",
                                           final="https://www.kajabi.com/")])
    assert not det.has_program and det.platforms == [] and det.reachable


def test_no_pages_is_unreachable():
    det = detect.decide("gone.example", [])
    assert not det.has_program and not det.reachable


def test_program_links_prefers_affiliate_pages_on_the_same_site():
    links = detect.program_links(
        '<a href="/partners/referral">Refer</a><a href="/affiliates">Affiliates</a>'
        '<a href="https://other.com/affiliate">x</a><a href="#affiliate">y</a>', "https://acme.com/", detect.load())
    assert links == ["https://acme.com/affiliates", "https://acme.com/partners/referral"]


# fetching (fake polite client) -----------------------------------------------------------------------------

class FakeResp:
    def __init__(self, url, text, status=200, final=None):
        self.url, self.text, self.status_code = final or url, text, status
        self.headers = {"content-type": "text/html"}


class FakeHTTP:
    def __init__(self, pages: dict):
        self.pages, self.calls = pages, []

    async def get(self, url):
        self.calls.append(url)
        hit = self.pages.get(url)
        return None if hit is None else FakeResp(url, *hit) if isinstance(hit, tuple) else FakeResp(url, hit)

    async def aclose(self):
        pass


def test_verify_follows_the_homepage_link_to_the_program_page():
    http = FakeHTTP({"https://acme.com/": '<title>Acme</title><a href="/affiliate">Affiliate program</a>',
                     "https://acme.com/affiliate": html("kit_affiliate.html")})
    det = asyncio.run(detect.verify("acme.com", http))
    assert det.has_program and det.platforms == ["partnerstack"] and det.name == "Acme"
    assert http.calls == ["https://acme.com/", "https://acme.com/affiliate"]


def test_verify_guesses_paths_when_the_homepage_links_nowhere():
    http = FakeHTTP({"https://acme.com/": html("plain_home.html"),
                     "https://acme.com/affiliate": html("clickfunnels_affiliates.html")})
    det = asyncio.run(detect.verify("acme.com", http))
    assert det.platforms == ["in-house"]
    assert http.calls[:3] == ["https://acme.com/", "https://acme.com/affiliates", "https://acme.com/affiliate"]


def test_verify_unreachable_homepage():
    det = asyncio.run(detect.verify("gone.example", FakeHTTP({})))
    assert not det.reachable and det.error == "homepage unreachable"


# seeds -----------------------------------------------------------------------------------------------------

def test_partnerstack_marketplace_state():
    got = {s.domain: s for s in seeds.parse_partnerstack_market(html("partnerstack_market.html"))}
    assert set(got) == {"elevenlabs.io", "breezy.hr"}
    assert got["elevenlabs.io"].partner_count == 1200 and got["elevenlabs.io"].tags == ["Artificial Intelligence"]
    assert got["breezy.hr"].platform_hint == "partnerstack"


def test_marketplace_layout_change_is_loud():
    with pytest.raises(ValueError):
        seeds.parse_partnerstack_market("<html>no state</html>")


@pytest.mark.parametrize("url,expected", [
    ("https://rella.getrewardful.com/", ("rewardful", "rella")),
    ("https://help.getrewardful.com/en/", None),
    ("https://af.uppromote.com/eyevos/register", ("uppromote", "eyevos")),
    ("https://notion.partnerstack.com/", ("partnerstack", "notion")),
    ("https://market.partnerstack.com/", None),
    ("https://app.impact.com/campaign-promo-signup/SSLS.brand?exec", ("impact", "ssls")),
])
def test_portal_of(url, expected):
    assert seeds.portal_of(url, detect.load()) == expected


def test_search_results_keep_portals_and_company_sites_drop_platforms_and_social():
    results = [{"url": "https://rella.getrewardful.com/"}, {"url": "https://www.getrewardful.com/pricing"},
               {"url": "https://www.linkedin.com/jobs/view/1"}, {"url": "https://wpcoachify.com/affiliate-program/", "title": "WP Coachify"}]
    got = seeds.seeds_from_results(results, "dork", detect.load())
    assert [(s.domain, s.portal_url, s.name) for s in got] == [(None, "https://rella.getrewardful.com/", None), ("wpcoachify.com", None, None)]


def test_portal_resolves_to_the_company_site():
    fp = detect.load()
    assert seeds.site_from_portal(html("rewardful_portal.html"), "https://rella.getrewardful.com/", fp) == "rella.ai"
    assert seeds.site_from_portal(html("uppromote_portal.html"), "https://af.uppromote.com/eyevos/register", fp) \
        == "customer-2350.myshopify.com"


def test_myshopify_portal_follows_the_store_redirect():
    http = FakeHTTP({"https://af.uppromote.com/eyevos/register": html("uppromote_portal.html"),
                     "https://customer-2350.myshopify.com/": ("<html>store</html>", 200, "https://eyevos.com/")})
    s = seeds.Seed("serp", "dork", portal_url="https://af.uppromote.com/eyevos/register")
    assert asyncio.run(seeds.resolve_portal(s, http)) == "eyevos.com"


def test_dork_list_expands_niches():
    cfg = {"portal_dorks": ["site:tolt.io"], "niche_dorks": ['"affiliate program" {niche}'], "niches": ["coaching", "pets"]}
    assert seeds.dork_list(cfg) == ["site:tolt.io", '"affiliate program" coaching', '"affiliate program" pets']


# store, scoring, export ------------------------------------------------------------------------------------

@pytest.fixture
def store():
    return Store.open(":memory:")


def test_seed_rows_merge_sources(store):
    assert store.add_program_seed("breezy.hr", source="partnerstack-marketplace", ref="breezy", partner_count=12, tags=["HR"])
    assert not store.add_program_seed("breezy.hr", source="serp", ref="https://breezy.getrewardful.com/", name="Breezy")
    row = store.program_companies()[0]
    assert [s["source"] for s in row["sources"]] == ["partnerstack-marketplace", "serp"]
    assert row["partner_count"] == 12 and row["name"] == "Breezy" and row["tags"] == ["HR"]
    assert store.programs_to_check() == ["breezy.hr"]


def test_score_rewards_paid_platforms_small_programs_and_hiring():
    base = {"status": "verified", "platforms": ["in-house"]}
    assert prun.score({"status": "no_program"}) == 0
    assert prun.score(base) == 40
    assert prun.score({**base, "platforms": ["partnerstack"], "partner_count": 12, "commission_text": "20%"}) == 80
    assert prun.score({**base, "hiring_signal": "affiliate manager"}) == 60


def test_verify_run_stores_verdicts_and_exports_csv(store, monkeypatch, tmp_path):
    store.add_program_seed("kit.com", source="partnerstack-marketplace", ref="kit", partner_count=30)
    store.add_program_seed("acme.com", source="serp", ref="dork")
    store.add_program_seed("gone.example", source="serp", ref="dork")
    fake = FakeHTTP({"https://kit.com/": '<title>Kit</title><a href="/affiliate">Affiliates</a>',
                     "https://kit.com/affiliate": html("kit_affiliate.html"),
                     "https://acme.com/": html("plain_home.html")})
    monkeypatch.setattr(prun, "_http", lambda *a, **k: fake)
    rep = asyncio.run(prun.verify(None, store, concurrency=2))
    assert (rep.checked, rep.verified, rep.no_program, rep.unreachable) == (3, 1, 1, 1)
    kit = store.program_companies(["verified"])[0]
    assert kit["domain"] == "kit.com" and kit["score"] == 80 and kit["platforms"] == ["partnerstack"]
    out = tmp_path / "programs.csv"
    assert prun.export_csv(store, out) == 1
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["domain"] == "kit.com" and rows[0]["affiliate platform"] == "partnerstack"
    assert rows[0]["company"] == "Kit" and rows[0]["website"] == "https://kit.com"
    assert store.programs_to_check() == []


# browser escalation ----------------------------------------------------------------------------------------

class FakeBB:
    def __init__(self, reads: dict):
        self.reads, self.calls = reads, []

    async def read(self, url, role):
        from lead_radar.programs.browserbox import as_html
        self.calls.append(url)
        data = self.reads.get(url)
        return None if data is None else detect.Page(url, data.get("final_url", url), 200, as_html(data), role)

    async def aclose(self):
        pass


def test_escalation_rechecks_unreachable_rows_through_the_browser(store):
    from lead_radar.programs import browserbox
    store.add_program_seed("fiverr.com", source="serp", ref="dork")
    store.save_program_check("fiverr.com", status="unreachable", platforms=[], program_url=None, commission_text=None,
                             evidence=[], segment="unknown", name=None, score=0, error="homepage 403")
    bb = FakeBB({"https://fiverr.com/": {"title": "Fiverr", "text": "Find talent",
                                         "links": ["https://affiliates.fiverr.com/", "https://fiverr.com/affiliates"]},
                 "https://fiverr.com/affiliates": {"text": "Earn up to $1,000 per referral. 10% revenue share commission."}})
    rep = asyncio.run(prun.escalate(None, store, bb=bb))
    assert (rep.checked, rep.verified) == (1, 1)
    row = store.program_companies(["verified"])[0]
    assert row["platforms"] == ["in-house"] and "commission" in row["commission_text"]
    assert bb.calls == ["https://fiverr.com/", "https://fiverr.com/affiliates"]
    assert 'href="https://fiverr.com/affiliates"' in browserbox.as_html({"links": ["https://fiverr.com/affiliates"]})


def test_marketplace_listing_and_portal_count_as_evidence(store):
    store.add_program_seed("elevenlabs.io", source="partnerstack-marketplace", ref="elevenlabs", partner_count=1200)
    store.add_program_seed("rella.ai", source="serp", ref="https://rella.getrewardful.com/")
    store.add_program_seed("acme.com", source="serp", ref='"join our affiliate program" pets')
    for d, status, plats in (("elevenlabs.io", "verified", ["in-house"]), ("rella.ai", "no_program", []),
                             ("acme.com", "no_program", [])):
        store.save_program_check(d, status=status, platforms=plats, program_url=None, commission_text=None,
                                 evidence=[], segment="unknown", name=None, score=0, error=None)
    assert prun.apply_source_evidence(store) == 2
    rows = {r["domain"]: r for r in store.program_companies()}
    assert rows["elevenlabs.io"]["platforms"] == ["partnerstack"] and rows["elevenlabs.io"]["segment"] == "saas"
    assert rows["rella.ai"]["status"] == "verified" and rows["rella.ai"]["platforms"] == ["rewardful"]
    assert rows["rella.ai"]["evidence"][0]["kind"] == "listing"
    assert rows["acme.com"]["status"] == "no_program"
    assert prun.apply_source_evidence(store) == 0  # idempotent


def test_rate_limited_rows_can_be_requeued(store):
    for d, err in (("a.com", "homepage 429"), ("b.com", "homepage 403")):
        store.add_program_seed(d, source="serp", ref="dork")
        store.save_program_check(d, status="unreachable", platforms=[], program_url=None, commission_text=None,
                                 evidence=[], segment="unknown", name=None, score=0, error=err)
    assert store.requeue_rate_limited() == 1
    assert store.programs_to_check() == ["a.com"]


def test_articles_about_affiliate_programs_are_not_program_pages():
    fp = detect.load()
    links = detect.program_links('<a href="/blog/best-affiliate-programs">x</a><a href="/top-10-affiliate-programs">y</a>'
                                 '<a href="/affiliate-program">z</a>', "https://acme.com/", fp)
    assert links == ["https://acme.com/affiliate-program"]
    det = detect.decide("acme.com", [page("plain_home.html", "https://acme.com/"),
                                    page("clickfunnels_affiliates.html", "https://acme.com/blog/2025/affiliates", "linked")])
    assert not det.has_program


def test_company_name_from_homepage_title():
    assert detect.clean_name("Modge | The AI workspace") == "Modge"
    assert detect.clean_name("Kit - Email marketing for creators") == "Kit"
    assert detect.clean_name("Acme") == "Acme"


def test_commission_quote_is_whole_words():
    text = "Read all rules are in the Affiliate Program Terms and Conditions here. Commission 30% of first payment, " \
           "paid monthly to every partner who refers a customer through their link forever"
    quote = detect.commission_text(text, detect.load())
    assert "30% of first payment" in quote
    assert quote.split()[0] in text.split() and quote.split()[-1] in text.split()
