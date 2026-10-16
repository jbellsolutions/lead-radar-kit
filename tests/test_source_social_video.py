"""social_video source: brand-owned TikTok and YouTube program posts. Offline only: a fake ScrapeCreators client and
fake homepage GETs. Fixtures and names are synthetic (brands are invented; the individual is "Pat Example")."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lead_radar.programs import detect, signals as sg, sources as srcs
from lead_radar.programs.sources import social_video as sv

FIX = Path(__file__).parent / "fixtures" / "sources" / "social_video"
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def fixture(name):
    return json.loads((FIX / name).read_text())


def _missing(path):
    raise sg.SkipItem(f"{path}: 404")


class FakeMeter:
    def __init__(self):
        self.n = 0

    def total_credits(self):
        return self.n


class FakeSC:
    """Stands in for ScrapeCreators: one credit per call, 404 (SkipItem) for unknown handles or channels."""

    def __init__(self, tiktok_profiles, yt_channels):
        self.meter, self.exhausted, self.calls = FakeMeter(), False, []
        self.profiles, self.channels = tiktok_profiles, yt_channels
        self.routes = {
            "/v1/tiktok/search/keyword": lambda p: fixture("tiktok_search.json"),
            "/v1/tiktok/profile": lambda p: self.profiles.get(p["handle"]) or _missing("profile"),
            "/v1/youtube/search": lambda p: fixture("youtube_search.json"),
            "/v1/youtube/channel": lambda p: self.channels.get(p["channelId"]) or _missing("channel"),
        }

    async def get(self, path, params=None, *, source="unknown"):
        params = dict(params or {})
        self.calls.append((path, params))
        self.meter.n += 1
        fn = self.routes.get(path)
        if fn is None:
            raise sg.SkipItem(f"{path}: 404")
        return json.loads(json.dumps(fn(params)))

    def paths(self):
        return [p for p, _ in self.calls]


def fake_sc():
    return FakeSC(fixture("tiktok_profiles.json"), fixture("youtube_channels.json"))


def page(url, final, html, role="home"):
    return detect.Page(url, final, 200, html, role)


PAGES = {
    "https://northwind-lamps.example/": page(
        "https://northwind-lamps.example/", "https://northwind-lamps.example/",
        '<html><head><title>Northwind Lamps | Warm light for every room</title></head><body>'
        '<a href="/affiliate">Affiliate Program</a></body></html>'),
    "https://northwind-lamps.example/affiliate": page(
        "https://northwind-lamps.example/affiliate", "https://northwind-lamps.example/affiliate",
        "<html><head><title>Affiliate</title></head><body><p>Northwind Lamps affiliate program pays 25% recurring "
        "commission on every sale.</p></body></html>", "linked"),
    "https://harbortools.example/": page(
        "https://harbortools.example/", "https://harbortools.example/",
        '<html><head><title>Harbor Tools | Contractor tools</title></head><body>'
        '<a href="https://www.youtube.com/@HarborTools">YouTube</a></body></html>'),
    "https://emberkitchen.example/": page(
        "https://emberkitchen.example/", "https://emberkitchen.example/",
        '<html><head><title>Ember Kitchen | Cookware</title></head><body>'
        '<a href="https://www.youtube.com/channel/UCember0000000000000001">Watch</a></body></html>'),
    "https://rankinghub.example/": page(
        "https://rankinghub.example/", "https://rankinghub.example/",
        '<html><head><title>Ranking Hub | Reviews</title></head><body>'
        '<a href="https://www.youtube.com/channel/UCrankhub0000000000001">Watch</a></body></html>'),
    "https://plain.example/": page(
        "https://plain.example/", "https://plain.example/",
        "<html><head><title>Plain Co | Home</title></head><body>Nothing here</body></html>"),
    "https://plain.example/affiliate": page(
        "https://plain.example/affiliate", "https://plain.example/affiliate",
        "<html><body><p>Plain Co may earn an affiliate commission when you buy through our links.</p></body></html>",
        "linked"),
    "https://brandco.example/": page(
        "https://brandco.example/", "https://brandco.example/",
        "<html><head><title>Brandco Agency Pro Services | Home</title></head><body></body></html>"),
    "https://otherchannel.example/": page(
        "https://otherchannel.example/", "https://otherchannel.example/",
        '<html><head><title>Harbor Tools | Contractor tools</title></head><body>'
        '<a href="https://www.youtube.com/@OtherChannel">Our other channel</a>'
        '<a href="/affiliate">Affiliate Program</a></body></html>'),
    "https://otherchannel.example/affiliate": page(
        "https://otherchannel.example/affiliate", "https://otherchannel.example/affiliate",
        "<html><body><p>Harbor Tools affiliate program pays commission.</p></body></html>", "linked"),
}


def make_ctx(sc, *, cap=15, days=30, overrides=None, pages=None, say=None):
    cfg = dict(sv.DEFAULTS)
    cfg.update(overrides or {})
    ctx = srcs.SourceContext(sc=sc, http=None, budget=sg.Budget(sc, cap), cfg=cfg, signals_cfg={}, days=days,
                             now=NOW, say=say or (lambda msg: None), http_cap=cfg["http_cap"])
    fetched, table = [], (PAGES if pages is None else pages)

    async def get(url, role="page"):
        fetched.append(url)
        return table.get(url)

    ctx.get = get
    ctx.fetched = fetched
    return ctx


def run(ctx):
    return asyncio.run(sv.collect(ctx))


def gather(ctx):
    return asyncio.run(sv.gather(ctx))


def verify(ctx, domain, name, platform, ours):
    return asyncio.run(sv.verify_site(ctx, domain, name, platform, ours))


# registry, dates, URLs, windows -------------------------------------------------------------------------------------

def test_registered_with_defaults():
    mod = srcs.load("social_video")
    assert mod.NAME == "social_video" and srcs.REGISTRY["social_video"] == "social_video"
    assert sv.DEFAULTS["max_profiles"] == 8 and sv.DEFAULTS["max_channels"] == 5
    assert sv.DEFAULTS["enabled"] is False and sv.DEFAULTS["tiktok_credits"] == 6
    merged = srcs.settings("social_video", mod, {"sources": {"social_video": {"enabled": True, "credits": 15}}})
    assert merged["credits"] == 15 and merged["max_profiles"] == 8 and merged["tiktok_queries"]


def test_tiktok_dates_and_urls():
    assert sv.tiktok_date({"create_time": 1790461336}) == "2026-09-26"
    assert sv.tiktok_date({"create_time": "1790461336"}) == "2026-09-26"
    assert sv.tiktok_date({"create_time_utc": "2026-09-26T22:22:16.000Z"}) == "2026-09-26"
    assert sv.tiktok_date({}) is None
    assert sv.tiktok_handle("https://www.tiktok.com/@northwindlamps/video/7310000000000000001") \
        == ("northwindlamps", "7310000000000000001")
    assert sv.tiktok_handle("https://www.tiktok.com/discover/anything") is None
    assert sv.tiktok_video_url("northwindlamps", "7310000000000000001") \
        == "https://www.tiktok.com/@northwindlamps/video/7310000000000000001"


def test_youtube_dates_and_urls():
    assert sv.youtube_date("2026-10-06T03:00:00-07:00") == "2026-10-06"
    assert sv.youtube_date(None) is None
    assert sv.youtube_video_url("AbCdEfGhIjK") == "https://www.youtube.com/watch?v=AbCdEfGhIjK"


def test_date_windows():
    assert (sv.tiktok_window(30), sv.tiktok_window(3), sv.tiktok_window(1)) == ("this-month", "this-week", "yesterday")
    assert (sv.youtube_window(30), sv.youtube_window(3), sv.youtube_window(1)) == ("this_month", "this_week", "today")


# text gates ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,reason", [
    ("Join our affiliate program and earn 20% commission on every sale. We're looking for creators!", None),
    ("Our affiliate program is open. Join us!", None),
    ("Our ambassador program pays 30% commission per order. Apply today!", None),
    ("How to Join the Enerlocks Affiliate Program | Platinum Plus Guide. Learn how to easily navigate the Enerlocks "
     "website to access and join our affiliate program.", None),
    ("Use my code PAT10 and join my team, make money from home", "mlm"),
    ("We're looking for ambassadors to join our ambassador family. Apply to join us and get a clothing credit.",
     "ambassador perks only"),
    ("TikTok Shop affiliates wanted, join our TikTok shop affiliate program", "platform program (TikTok Shop)"),
    ("We help brands launch their affiliate program. Book a call today.", "seller"),
    ("Join our affiliate program. I love it!", "individual voice"),
    ("Join our affiliate program in our casino partner network", "vertical"),
    ("Apply to our student ambassador program, fully funded by the university!", "non-business ambassador"),
    ("Affiliate marketing tips: join the best affiliate programs today", "listicle or guide"),
    ("Top 10 affiliate programs you should join in 2026. We ranked 50 networks.", "listicle or guide"),
    ("How to join affiliate programs for beginners. In this video we show you the way.", "listicle or guide"),
    ("Join our affiliate marketing course and learn to earn commission. We teach you the playbook.",
     "course or tutorial"),
    ("Join the conversation", "no buyer language"),
])
def test_text_gate(text, reason):
    assert sv.text_drop(text) == reason


def test_tiktok_owner_and_us_country():
    assert sv.tiktok_owner({"isOrganization": 1}) == "organisation"
    assert sv.tiktok_owner({"commerceUserInfo": {"commerceUser": True}}) == "seller"
    assert sv.tiktok_owner({"ttSeller": True}) == "seller"
    assert sv.tiktok_owner({"isOrganization": 0, "ttSeller": False, "verified": True,
                            "commerceUserInfo": {"commerceUser": False}}) == ""
    assert sv.is_us("United States") and sv.is_us("US") and sv.is_us(" united states ")
    assert not sv.is_us("United Kingdom") and not sv.is_us(None) and not sv.is_us("")


def test_site_domains_skip_aggregators_and_platforms():
    got = sv.site_domains(["https://linktr.ee/x", "https://www.northwind-lamps.example/", "https://campsite.to/abc",
                           "https://instagram.com/brand"], "shop at acme.com")
    assert got == ["northwind-lamps.example", "acme.com"]
    assert sv.site_domains([], "") == []


def test_third_party_tag():
    assert sv.third_party_tag("see https://mailroom.example/?ref=agency123 now", "harbortools.example")
    assert not sv.third_party_tag("see https://harbortools.example/?ref=x now", "harbortools.example")
    assert not sv.third_party_tag("no links here", "harbortools.example")


def test_quote_is_short_and_verbatim():
    text = "Intro. " * 80 + "Join our affiliate program and earn commission. " + "Outro words. " * 40
    q = sv.quote_for(text)
    assert len(q) <= 400
    assert q in " ".join(text.split())
    assert "affiliate program" in q


# the brand must be named -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,text,want", [
    ("Northwind Lamps", "Join our affiliate program. We're looking for creators to share Northwind!", True),
    ("Harbor Tools", "Join our affiliate program at harbortools.example today.", True),
    ("Trainer's Box", "Join the trainer's box affiliate program", True),
    ("Ember Kitchen", "Join our affiliate program: 15% commission", False),
    ("Ghost Brand", "Join our affiliate program and earn commission", False),
    ("Kaza", "Kaza affiliates wanted", False),         # under MIN_NAME normalised characters
])
def test_named_in_text(name, text, want):
    assert sv.named_in(name, text) is want


@pytest.mark.parametrize("name,html,want", [
    ("Northwind Lamps", "<title>Northwind Lamps | Warm light</title>", True),
    ("Harbor Tools", "<title>Home - Harbor Tools</title>", True),
    ("Trainer's Box", "<title>TRAINER'S BOX® -No.1 Channel | Coaches</title>", True),
    ("Enerlocks", '<meta property="og:site_name" content="Enerlocks"><title>Shop</title>', True),
    ("Kaza", "<title>Kazaam SEO Agency</title>", False),
    ("Brandco", "<title>Brandco Agency Pro Services | Home</title>", False),
])
def test_homepage_names_whole_account_name(name, html, want):
    assert sv.names_homepage(html, name) is want


def test_label_and_ids_helpers():
    assert sv.label_names("Northwind Lamps", "northwind-lamps.example")
    assert not sv.label_names("Northwind Lamps", "elsewhere.example")
    assert sv.linked_ids('<a href="https://www.youtube.com/@HarborTools">x</a>', "youtube") == {"@harbortools"}
    assert sv.linked_ids('<a href="https://www.tiktok.com/@NorthwindLamps">x</a>', "tiktok") == {"@northwindlamps"}
    assert sv.youtube_ids("UCharbor000000000000001", "@HarborTools") \
        == {"channel/ucharbor000000000000001", "@harbortools"}


# owner and site verification ------------------------------------------------------------------------------------

def test_verify_accepts_backlink_and_program_page():
    ctx = make_ctx(fake_sc())
    harbor = sv.youtube_ids("UCharbor000000000000001", "@HarborTools")
    assert verify(ctx, "harbortools.example", "Harbor Tools", "youtube", harbor) == ("harbortools.example", "backlink")
    nw = sv.tiktok_ids("northwindlamps")  # the homepage has no TikTok link: the program page must carry the brand
    assert verify(ctx, "northwind-lamps.example", "Northwind Lamps", "tiktok", nw) \
        == ("northwind-lamps.example", "program page")
    assert "https://northwind-lamps.example/affiliate" in ctx.fetched


def test_verify_rejects_wrong_name_parked_missing_and_short_names():
    ctx = make_ctx(fake_sc())
    harbor = sv.youtube_ids("UCharbor000000000000001", "@HarborTools")
    assert verify(ctx, "harbortools.example", "Other Brand", "youtube", harbor) \
        == (None, "homepage does not name the account")
    parked = {"https://parked.example/": page("https://parked.example/", "https://parked.example/",
                                              "<title>parked.example is for sale | HugeDomains</title>")}
    assert verify(make_ctx(fake_sc(), pages=parked), "parked.example", "Parked Co", "tiktok", {"@parked"}) \
        == (None, "parked domain")
    assert verify(make_ctx(fake_sc(), pages={}), "gone.example", "Goneville", "tiktok", set()) \
        == (None, "homepage unreachable")
    short = make_ctx(fake_sc())
    assert verify(short, "harbortools.example", "AI", "youtube", harbor) == (None, "name too short")
    assert short.fetched == []


def test_verify_rejects_whole_word_mismatch():
    ctx = make_ctx(fake_sc())
    assert verify(ctx, "brandco.example", "Brandco", "tiktok", {"@brandco"}) == (None, "homepage does not name the account")


def test_verify_rejects_redirect_to_platform_and_caps_probes():
    redirected = {"https://brandx.example/": page("https://brandx.example/", "https://www.tiktok.com/@brandx",
                                                  "<title>Brandx | Shop</title>")}
    ctx = make_ctx(fake_sc(), pages=redirected)
    assert verify(ctx, "brandx.example", "Brandx", "tiktok", {"@brandx"}) == (None, "redirected to a platform")
    ctx = make_ctx(fake_sc(), pages={"https://plain.example/": PAGES["https://plain.example/"]})
    assert verify(ctx, "plain.example", "Plain Co", "tiktok", {"@plainco"}) == (None, "no backlink or program page")
    assert 1 <= len(ctx.fetched) <= 1 + sv.MAX_PROGRAM_PROBES


def test_disclosure_page_without_program_terms_is_not_evidence():
    """Probe 2a: an 'affiliate commission' disclosure page is not a program page, so no program is proven."""
    ctx = make_ctx(fake_sc())
    assert verify(ctx, "plain.example", "Plain Co", "tiktok", {"@plainco"}) == (None, "no backlink or program page")
    assert "https://plain.example/affiliate" in ctx.fetched


def test_homepage_linking_another_account_is_rejected():
    """Probe 1: the homepage links a different channel of the same platform, so the candidate channel is not proven."""
    ctx = make_ctx(fake_sc(), pages={"https://otherchannel.example/": PAGES["https://otherchannel.example/"],
                                     "https://otherchannel.example/affiliate": PAGES["https://otherchannel.example/affiliate"]})
    ours = sv.youtube_ids("UCharbor000000000000001", "@HarborTools")
    assert verify(ctx, "otherchannel.example", "Harbor Tools", "youtube", ours) \
        == (None, "homepage links another account")
    assert ctx.fetched == ["https://otherchannel.example/"]


# the run ------------------------------------------------------------------------------------------------------------

def test_collect_keeps_only_brand_videos_with_the_right_shape():
    cands = run(make_ctx(fake_sc()))
    by_domain = {c.domain: c for c in cands}
    assert set(by_domain) == {"northwind-lamps.example", "harbortools.example"}
    nw = by_domain["northwind-lamps.example"]
    assert nw.list == "affiliate" and nw.require_us is True and nw.linkedin_url is None
    assert nw.company_name == "Northwind Lamps"
    assert nw.signal.kind == "social_post" and nw.signal.platform == "tiktok"
    assert nw.signal.url == "https://www.tiktok.com/@northwindlamps/video/7310000000000000001"
    assert nw.signal.signal_date == "2026-10-05"
    assert nw.signal.person_name is None and nw.signal.person_url is None
    assert len(nw.signal.quote) <= 400 and "Join our affiliate program" in nw.signal.quote
    hb = by_domain["harbortools.example"]
    assert hb.signal.platform == "youtube" and hb.company_name == "Harbor Tools"
    assert hb.signal.url == "https://www.youtube.com/watch?v=AbCdEfGhIjK"
    assert hb.signal.signal_date == "2026-10-06"
    assert all(c.signal.platform in sg.PLATFORM_LABEL and c.signal.kind in sg.KINDS for c in cands)


def test_one_candidate_per_company_and_newest_video_wins():
    cands = run(make_ctx(fake_sc()))
    assert len(cands) == len({c.domain for c in cands})
    hb = [c for c in cands if c.domain == "harbortools.example"][0]
    assert hb.signal.signal_date == "2026-10-06"


def test_credit_spend_and_call_pattern():
    sc = fake_sc()
    run(make_ctx(sc))
    assert sc.meter.n == 12
    assert sc.paths().count("/v1/tiktok/search/keyword") == 3 and sc.paths().count("/v1/youtube/search") == 3
    assert sc.paths().count("/v1/tiktok/profile") == 3   # northwind, ghost, unlinked: never the text-dropped accounts
    assert sc.paths().count("/v1/youtube/channel") == 3  # harbor, crown, ember: never the side-hustle channel
    requested = {p["channelId"] for path, p in sc.calls if path == "/v1/youtube/channel"}
    assert requested == {"UCharbor000000000000001", "UCcrownuk00000000000001", "UCember0000000000000001"}
    handles = {p["handle"] for path, p in sc.calls if path == "/v1/tiktok/profile"}
    assert handles == {"northwindlamps", "ghostbrandco", "unlinkedco"}


def test_zero_budget_makes_no_calls():
    sc = fake_sc()
    assert run(make_ctx(sc, cap=0)) == []
    assert sc.calls == []


def test_budget_stops_the_run_midway_and_youtube_goes_first():
    sc = fake_sc()
    found, stats = gather(make_ctx(sc, cap=4))
    assert sc.meter.n == 4
    assert sc.paths().count("/v1/tiktok/search/keyword") == 0    # the run ended before the TikTok leg
    assert [f.domain for f in found] == ["harbortools.example"]  # the newest YouTube lookup spent the last credit


def test_tiktok_sub_budget_is_respected():
    sc = fake_sc()
    run(make_ctx(sc, overrides={"tiktok_credits": 2}))
    assert sc.paths().count("/v1/tiktok/search/keyword") == 2
    assert sc.paths().count("/v1/tiktok/profile") == 0


def test_max_profiles_cap_is_respected():
    sc = fake_sc()
    run(make_ctx(sc, overrides={"max_profiles": 1}))
    assert sc.paths().count("/v1/tiktok/profile") == 1


def test_unresolvable_brand_is_dropped_not_guessed():
    pages = {k: v for k, v in PAGES.items() if "harbor" not in k}
    cands = run(make_ctx(fake_sc(), pages=pages))
    assert [c.domain for c in cands] == ["northwind-lamps.example"]


def test_third_party_promo_is_dropped_with_its_own_reason():
    ctx = make_ctx(fake_sc())
    found, stats = gather(ctx)
    assert "https://emberkitchen.example/" in ctx.fetched    # the owner gate was reached ...
    assert stats["third-party tag"] == 1                     # ... and the tracked link dropped the video
    assert all("ember" not in f.domain for f in found)


def test_listicle_channel_is_dropped_before_any_channel_lookup():
    """Probe 3: a ranking channel whose homepage names it and links back never costs a channel lookup."""
    sc = fake_sc()
    sc.routes["/v1/youtube/search"] = lambda p: {"videos": [{
        "id": "RankVidAAA1", "title": "Top 10 affiliate programs for 2026",
        "description": "Join the best affiliate programs of 2026. We ranked them all. Ranking Hub reviews.",
        "publishDate": "2026-10-06T03:00:00-07:00", "channel": {"id": "UCrankhub0000000000001", "title": "Ranking Hub"},
    }], "shorts": []}
    found, stats = gather(make_ctx(sc))
    assert "UCrankhub0000000000001" not in {p["channelId"] for path, p in sc.calls if path == "/v1/youtube/channel"}
    assert stats["youtube text: listicle or guide"] >= 1
    assert all(f.platform != "youtube" for f in found)


def test_tiktok_flood_does_not_starve_youtube():
    """Probe 4: six passing TikTok accounts cannot use YouTube's lookups; each leg keeps its own share."""
    sc = fake_sc()
    items = [{"aweme_id": f"73100000000000000{i}", "url": f"https://www.tiktok.com/@acct{i}ltd/video/73100000000000000{i}",
              "desc": "Join our affiliate program and earn commission.", "region": "US", "create_time": 1791212400,
              "author": {"nickname": ""}} for i in range(6)]
    sc.routes["/v1/tiktok/search/keyword"] = lambda p: {"search_item_list": items}
    ctx = make_ctx(sc)
    run(ctx)
    assert sc.paths().count("/v1/youtube/channel") == 3
    assert sc.paths().count("/v1/tiktok/profile") == 3
    assert sc.meter.n <= 15


def test_youtube_out_of_credits_keeps_what_was_found():
    sc = fake_sc()
    calls = {"n": 0}

    def channel(p):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise sg.OutOfCredits("exhausted")
        return sc.channels.get(p["channelId"]) or _missing("channel")

    sc.routes["/v1/youtube/channel"] = channel
    found, stats = gather(make_ctx(sc))
    assert [f.domain for f in found] == ["harbortools.example"]
    assert sc.paths().count("/v1/tiktok/search/keyword") == 0    # the TikTok leg did not run after the exhaustion


def test_non_us_channel_is_dropped_and_unknown_country_is_not_us():
    channels = fixture("youtube_channels.json")
    channels["UCharbor000000000000001"]["country"] = None
    found, stats = gather(make_ctx(FakeSC(fixture("tiktok_profiles.json"), channels)))
    assert all(f.domain != "harbortools.example" for f in found)
    assert stats["youtube: channel not US"] >= 1


def test_tiktok_seller_flag_needs_a_bio_domain_with_the_account_name():
    profiles = fixture("tiktok_profiles.json")
    profiles["northwindlamps"] = {"user": {"nickname": "Northwind Lamps", "isOrganization": 0,
                                           "commerceUserInfo": {"commerceUser": True},
                                           "bioLink": {"link": "https://elsewhere.example/"}, "signature": ""}}
    found, stats = gather(make_ctx(FakeSC(profiles, fixture("youtube_channels.json"))))
    assert all(f.domain != "northwind-lamps.example" for f in found)
    assert stats["tiktok: no brand site"] >= 1


def test_shorts_only_feed_makes_no_channel_calls():
    sc = fake_sc()
    yt_only = fixture("youtube_search.json")
    sc.routes["/v1/youtube/search"] = lambda p: {"success": True, "videos": [], "shorts": yt_only["shorts"]}
    cands = run(make_ctx(sc))
    assert all(c.signal.platform == "tiktok" for c in cands)
    assert "/v1/youtube/channel" not in sc.paths()


def test_progress_lines_carry_no_personal_data():
    said = []
    run(make_ctx(fake_sc(), say=said.append))
    blob = "\n".join(said)
    assert said
    assert "Pat Example" not in blob and "pat.example" not in blob and "@" not in blob


def test_site_builders_never_anchor_a_company():
    assert sv.hosted("trainersbox.webnode.page") and sv.hosted("webnode.page") and sv.hosted("foo.netlify.app")
    assert not sv.hosted("enerlocks.com") and not sv.hosted("notwebnode.page.example")
    assert sv.site_domains(["https://trainersbox.webnode.page/", "https://enerlocks.com/"], "") == ["enerlocks.com"]
    assert sv.site_domains([], "our shop at trainersbox.net") == ["trainersbox.net"]


def test_display_name_keeps_the_account_name_only():
    assert sv.display_name("TRAINER'S BOX® -No.1 Channel | Coaches & Trainers") == "TRAINER'S BOX"
    assert sv.display_name("Northwind Lamps") == "Northwind Lamps"
    assert sv.display_name("Acme-Co") == "Acme-Co"
