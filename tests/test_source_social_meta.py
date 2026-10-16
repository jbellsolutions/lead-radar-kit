"""social_meta source: brand-owned Instagram/Threads recruiting posts. Offline only: fake ScrapeCreators client and
fake homepage fetches, synthetic payloads shaped like the recorded fixtures. Person names are synthetic
("Pat Example") and must never reach ctx.say."""
import asyncio
import inspect
from datetime import datetime, timezone

import pytest

from lead_radar.programs import detect, signals as sg, sources as srcs
from lead_radar.programs.sources import social_meta as sm

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
BRAND_HOME = "https://acme.example/"


class FakeMeter:
    def __init__(self):
        self.n = 0

    def total_credits(self):
        return self.n


class FakeSC:
    """Routes path -> callable(params) returning a payload or raising sg.SkipItem. Each answered call is 1 credit."""

    def __init__(self, routes):
        self.routes, self.meter, self.exhausted, self.calls = routes, FakeMeter(), False, []

    async def get(self, path, params, source=""):
        self.calls.append((path, dict(params)))
        if path not in self.routes:
            raise sg.SkipItem(f"{path}: 404")
        out = self.routes[path](params)
        self.meter.n += 1
        return out


class FakeCtx(srcs.SourceContext):
    """SourceContext whose page fetches are served from a dict (no network)."""

    def __init__(self, pages=None, **kw):
        super().__init__(**kw)
        self.pages = pages or {}
        self.page_calls = []

    async def get(self, url, role="page"):
        self.page_calls.append(url)
        return self.pages.get(url)


def page(url, html, final=None, status=200):
    return detect.Page(url, final or url, status, html, "home")


HOME = page(BRAND_HOME, "<html><head><title>Acme Brands | Shop</title></head><body>$ 20</body></html>")


def reel(code, owner, caption, taken="2026-10-05T12:00:00.000Z", full_name="Acme Brands", **extra):
    item = {"url": f"https://www.instagram.com/reel/{code}/", "shortcode": code, "caption": caption, "taken_at": taken,
            "owner": {"username": owner, "full_name": full_name, "is_verified": False},
            "is_paid_partnership": False, "is_ad": False}
    item.update(extra)
    return item


def brand_profile(username="acmebrands", full_name="Acme Brands", external="https://acme.example/affiliates",
                  category="E-commerce website", business=False, professional=True, bio_links=None):
    """Shaped like the live payload: a verified affiliate-program brand reports is_business_account false."""
    return {"success": True, "credits_charged": 1, "data": {"user": {
        "username": username, "full_name": full_name, "is_business_account": business,
        "is_professional_account": professional, "category_name": category, "business_category_name": None,
        "external_url": external, "bio_links": bio_links or [], "is_verified": True}}}


GOOD = "Join our Affiliate Program and earn 30% commission on every sale."


def routes(reels=None, threads=None, profiles=None, linktree=None):
    reels, threads = reels or {}, threads or {}
    profiles, linktree = profiles or {}, linktree or {}

    def reel_search(p):
        return {"success": True, "reels": reels.get(p["query"], [])}

    def thread_search(p):
        return {"success": True, "posts": threads.get(p["query"], [])}

    def profile(p):
        if p["handle"] not in profiles:
            raise sg.SkipItem("404")
        return profiles[p["handle"]]

    def tree(p):
        if p["url"] not in linktree:
            raise sg.SkipItem("404")
        return linktree[p["url"]]

    return {"/v2/instagram/reels/search": reel_search, "/v1/threads/search": thread_search,
            "/v1/instagram/profile": profile, "/v1/linktree": tree}


def run(sc, pages=None, limit=15, cfg=None, days=30):
    conf = dict(sm.DEFAULTS)
    conf.update(cfg or {})
    msgs = []
    ctx = FakeCtx(pages=pages or {"https://acme.example/": HOME}, sc=sc, http=None, budget=sg.Budget(sc, limit),
                  cfg=conf, signals_cfg=sg.load(), days=days, now=NOW, say=msgs.append)
    return asyncio.run(sm.collect(ctx)), ctx, msgs


def only_phrase(query, items):
    """Reels for one phrase; every other default phrase returns nothing."""
    return {query: items}


def _drops(caption, owner="acmebrands", full_name="Acme Brands"):
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("X1", owner, caption, full_name=full_name)]),
                       profiles={owner: brand_profile(username=owner, full_name=full_name)}))
    cands, ctx, _ = run(sc)
    return cands, sc


# contract --------------------------------------------------------------------------------------------------------

def test_registry_contract():
    assert sm.NAME == "social_meta" and isinstance(sm.DEFAULTS, dict) and inspect.iscoroutinefunction(sm.collect)
    assert sm.DEFAULTS["max_profiles"] == 10 and sm.DEFAULTS["enabled"] is True
    assert sm.DEFAULTS["reel_phrases"] == ["join our affiliate program", "our affiliate program"]
    assert sm.DEFAULTS["threads_queries"] == []


def test_threads_is_off_by_default():
    sc = FakeSC(routes(threads={"affiliate program": [{"code": "T0", "caption": {"text": GOOD}, "taken_at": 1786968236,
                                                      "user": {"username": "acmebrands"}}]},
                       profiles={"acmebrands": brand_profile()}))
    run(sc, days=3650)
    assert not [c for c in sc.calls if c[0] == "/v1/threads/search"]


# buyer kept -------------------------------------------------------------------------------------------------------

def test_brand_post_is_kept_with_canonical_url_and_dated_signal():
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("ABC123", "acmebrands", GOOD + " ?igsh=x")]),
                       profiles={"acmebrands": brand_profile()}))
    cands, ctx, msgs = run(sc)
    assert len(cands) == 1
    c = cands[0]
    assert c.list == "affiliate" and c.require_us is True and c.linkedin_url is None
    assert c.domain == "acme.example" and c.company_name == "Acme Brands"
    s = c.signal
    assert s.kind == "social_post" and s.platform == "instagram"
    assert s.url == "https://www.instagram.com/reel/ABC123/" and s.signal_date == "2026-10-05"
    assert s.quote == "Join our Affiliate Program and earn 30% commission on every sale."
    assert s.kind in sg.KINDS and s.platform in sg.PLATFORM_LABEL


def test_budget_zero_makes_no_call():
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("ABC123", "acmebrands", GOOD)]),
                       profiles={"acmebrands": brand_profile()}))
    cands, ctx, msgs = run(sc, limit=0)
    assert cands == [] and sc.calls == [] and sc.meter.total_credits() == 0


def test_profile_lookups_are_cached_and_capped():
    reels = [reel(f"R{i}", "acmebrands", GOOD) for i in range(3)]
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", reels), profiles={"acmebrands": brand_profile()}))
    cands, ctx, _ = run(sc)
    assert len([c for c in sc.calls if c[0] == "/v1/instagram/profile"]) == 1
    assert len(cands) == 1


def test_profile_cap_limits_distinct_authors():
    owners = [f"brand{i}" for i in range(4)]
    items = [reel(f"S{i}", o, GOOD) for i, o in enumerate(owners)]
    profiles = {o: brand_profile(username=o, full_name=f"Brand {o}") for o in owners}
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", items), profiles=profiles))
    run(sc, cfg={"max_profiles": 2})
    assert len([c for c in sc.calls if c[0] == "/v1/instagram/profile"]) == 2


# filters ---------------------------------------------------------------------------------------------------------

def test_creator_promoting_a_code_is_dropped_before_any_profile_call():
    cands, sc = _drops("Use my code PAT10 and join the affiliate program", owner="pat.example", full_name="Pat Example")
    assert cands == [] and not [c for c in sc.calls if c[0] == "/v1/instagram/profile"]


def test_first_person_caption_is_dropped():
    cands, sc = _drops("I love this! Join our affiliate program and earn commission")
    assert cands == []


def test_capital_i_is_first_person_but_i_e_is_not():
    """Review minor: 'i.e.' was read as creator voice. Only the capital I counts."""
    assert len(_drops("Join our affiliate program (i.e. earn 20% per sale)")[0]) == 1
    assert _drops("Join our affiliate program. Thanks, I love it")[0] == []


def test_tagging_another_account_is_dropped():
    cands, sc = _drops("Join our affiliate program with @otherbrand and earn 20% commission")
    assert cands == []


def test_dotted_handle_of_the_brand_itself_is_not_another_account():
    """Review minor: '@runway.ai' is the runwayapp account's own handle, not another account."""
    pages = {"https://runway.example/": page("https://runway.example/", "<title>Runway | AI Video</title>")}
    caption = "Join our affiliate program and earn 20% commission. Follow @runway.ai for news."
    sc = FakeSC(routes(
        reels=only_phrase("join our affiliate program", [reel("RW2", "runwayapp", caption, full_name="Runway")]),
        profiles={"runwayapp": brand_profile(username="runwayapp", full_name="Runway",
                                             external="https://runway.example/affiliate-program")}))
    cands, ctx, _ = run(sc, pages=pages)
    assert [c.domain for c in cands] == ["runway.example"]


def test_program_named_for_another_brand_is_dropped():
    cands, sc = _drops("Become a Zenith Affiliate today, our 25% recurring commission is live")
    assert cands == []


def test_possessive_program_for_another_brand_is_dropped():
    cands, sc = _drops("Join Zenith's affiliate program and earn 20% commission")
    assert cands == []


def test_bare_program_name_for_another_brand_is_dropped():
    cands, sc = _drops("Join our Zenith Affiliate Program and earn 20% commission")
    assert cands == []


def test_lead_in_word_before_program_is_not_a_program_owner():
    """Review: 'Join Affiliate Program' must not read 'Join' as a brand name."""
    cands, sc = _drops("Join Affiliate Program and earn 20% commission. We love our creators.")
    assert len(cands) == 1


def test_third_party_partnering_copy_is_dropped():
    """Review blocker P1: 'We're partnering with Zenith! Join Zenith's affiliate program' from a brand-looking account."""
    cands, sc = _drops("We're partnering with Zenith! Join Zenith's affiliate program and earn 20% commission")
    assert cands == []
    rx = sg.load()["_rx"]
    assert sm.post_gate("We are partnering with Zenith to grow our affiliate program and earn 20%", "acmebrands",
                        "Acme Brands", rx, []) == "partners with another brand"


def test_partner_with_us_is_kept():
    cands, sc = _drops("Partner with us! Join our affiliate program and earn 20% commission.")
    assert len(cands) == 1


def test_named_brand_program_without_we_is_kept():
    """Live finding: 'Become a Runway Affiliate' from the Runway account has no we/our and is a real program."""
    pages = {"https://runway.example/": page("https://runway.example/", "<title>Runway | AI Video</title>")}
    caption = "Become a Runway Affiliate. Earn on every subscriber you bring in with a unique code."
    sc = FakeSC(routes(
        reels=only_phrase("join our affiliate program", [reel("RW1", "runwayapp", caption, full_name="Runway")]),
        profiles={"runwayapp": brand_profile(username="runwayapp", full_name="Runway",
                                             external="https://runway.example/affiliate-program")}))
    cands, ctx, _ = run(sc, pages=pages)
    assert [c.domain for c in cands] == ["runway.example"]
    assert cands[0].company_name == "Runway"


def test_be_part_of_our_affiliate_program_is_buyer_language():
    """Live finding: 'Be part of the nutribullet Affiliate Program!' is recruiting copy the shared gate misses."""
    assert sm.post_gate("Be part of the nutribullet Affiliate Program! Join us this October.", "acmebrands",
                        "Acme Brands", sg.load()["_rx"], []) is None


def test_ambassador_without_commission_is_dropped():
    cands, sc = _drops("Apply to be an ambassador and wear our gear")
    assert cands == []


def test_ambassador_with_commission_is_kept():
    cands, sc = _drops("Apply to be an ambassador. Our partners earn 20% commission on every order.")
    assert len(cands) == 1


def test_seller_copy_is_dropped():
    cands, sc = _drops("Join our affiliate program. We help brands launch affiliate programs in a week")
    assert cands == []


def test_third_party_name_in_the_program_sentence_is_dropped():
    cands, sc = _drops("Join our affiliate program with Pat Example as our top earner")
    assert cands == []


def test_shout_out_outside_the_program_sentence_is_not_quoted():
    """Review minor: the quote is the program sentence only, so a shout-out does not reach Signal.quote."""
    cands, sc = _drops("Congrats to Pat Example, our top earner! Join our affiliate program and earn 20% commission.")
    assert len(cands) == 1
    assert cands[0].signal.quote == "Join our affiliate program and earn 20% commission."


def test_line_break_ends_the_program_sentence():
    """Review minor: captions with line breaks and no full stops are split on the break before any collapsing."""
    cands, sc = _drops("Join our affiliate program\nPat Example is our top earner")
    assert len(cands) == 1 and cands[0].signal.quote == "Join our affiliate program"
    assert sm.program_sentence("Earn big\nJoin our affiliate program today") == "Join our affiliate program today"


def test_paid_partnership_is_dropped_without_a_profile_call():
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("P1", "acmebrands", GOOD, is_paid_partnership=True)]),
                       profiles={"acmebrands": brand_profile()}))
    cands, ctx, _ = run(sc)
    assert cands == [] and not [c for c in sc.calls if c[0] == "/v1/instagram/profile"]


def test_undated_or_out_of_window_post_is_dropped():
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("O1", "acmebrands", GOOD, taken="2026-06-01T00:00:00.000Z")]),
                       profiles={"acmebrands": brand_profile()}))
    cands, ctx, _ = run(sc, days=30)
    assert cands == []


def test_three_day_window_requests_last_week_and_drops_older_posts():
    """Review: the daily run is a 3-day window. The reel search must ask for last-week, and older posts drop."""
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [
        reel("W1", "acmebrands", GOOD, taken="2026-10-08T12:00:00.000Z"),
        reel("W2", "oldbrand", GOOD, taken="2026-10-05T12:00:00.000Z")]),
        profiles={"acmebrands": brand_profile(), "oldbrand": brand_profile(username="oldbrand", full_name="Old Brand",
                                                                           external="https://old.example/")}))
    cands, ctx, _ = run(sc, days=3)
    assert [c.domain for c in cands] == ["acme.example"]
    reel_calls = [c for c in sc.calls if c[0] == "/v2/instagram/reels/search"]
    assert reel_calls and all(c[1]["date_posted"] == "last-week" for c in reel_calls)
    assert not [c for c in sc.calls if c[0] == "/v1/instagram/profile" and c[1]["handle"] == "oldbrand"]


# profile gates ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("profile", [
    brand_profile(professional=False, business=False),
    brand_profile(category="Digital creator"),
    brand_profile(category="Marketing Agency"),
])
def test_creator_and_seller_accounts_are_dropped(profile):
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("A1", "acmebrands", GOOD)]),
                       profiles={"acmebrands": profile}))
    cands, ctx, _ = run(sc)
    assert cands == []


def test_verified_brand_without_business_flag_is_kept():
    """Live finding: a verified affiliate-program brand has is_business_account false and no category."""
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("V1", "acmebrands", GOOD)]),
                       profiles={"acmebrands": brand_profile(business=False, category=None)}))
    cands, ctx, _ = run(sc)
    assert [c.domain for c in cands] == ["acme.example"]


def test_payhip_storefront_is_not_a_brand_domain():
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("PH1", "acmebrands", GOOD)]),
                       profiles={"acmebrands": brand_profile(external="https://payhip.com/acme")}))
    cands, ctx, _ = run(sc, cfg={"max_linktree": 0})
    assert cands == [] and ctx.page_calls == []


def test_primary_external_link_wins_over_many_other_links():
    """Live finding: the Runway bio lists five outside links; the external link still names the brand."""
    bio = [{"url": "https://runway.example/news/a"}, {"url": "https://apple.example/x"},
           {"url": "https://youtu.be/abc"}, {"url": "https://runway.example/news/b"}]
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("PR1", "acmebrands", GOOD)]),
                       profiles={"acmebrands": brand_profile(external="https://acme.example/", bio_links=bio)}))
    cands, ctx, _ = run(sc)
    assert [c.domain for c in cands] == ["acme.example"]


def test_named_program_needs_a_lead_in_verb():
    assert sm.named_program("Become a Runway Affiliate today") == "Runway"
    assert sm.named_program("How to join Higfy Affiliate Program") == "Higfy"
    assert sm.named_program("Apply for our Social Media Ambassador program") is None
    assert sm.named_program("Apply to become a Young European Ambassador") is None
    assert sm.named_program("Join our College Ambassador Program") is None


def test_display_name_drops_the_tagline():
    assert sm.display_name("LALAL.AI - AI music tool") == "LALAL.AI"
    assert sm.display_name("Acme Brands | Shop") == "Acme Brands"
    assert sm.display_name("Acme Brands") == "Acme Brands"


def test_no_brand_link_is_dropped():
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("A1", "acmebrands", GOOD)]),
                       profiles={"acmebrands": brand_profile(external="https://linktr.ee/acme")}))
    cands, ctx, msgs = run(sc, cfg={"max_linktree": 0})
    assert cands == [] and ctx.page_calls == []


def test_lynx_wrapped_external_link_resolves_to_brand():
    lynx = "https://l.instagram.com/?u=https%3A%2F%2Facme.example%2Faffiliates&e=AT0abc"
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("L1", "acmebrands", GOOD)]),
                       profiles={"acmebrands": brand_profile(external=lynx)}))
    cands, ctx, _ = run(sc)
    assert [c.domain for c in cands] == ["acme.example"]


def test_linktree_hub_is_followed_once_within_its_cap():
    tree = {"https://linktr.ee/acme": {"success": True, "links": [{"url": "https://acme.example/affiliates"}]}}
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("T1", "acmebrands", GOOD)]),
                       profiles={"acmebrands": brand_profile(external="https://linktr.ee/acme")}, linktree=tree))
    cands, ctx, _ = run(sc, cfg={"max_linktree": 2})
    assert [c.domain for c in cands] == ["acme.example"]
    assert len([c for c in sc.calls if c[0] == "/v1/linktree"]) == 1


# robustness ------------------------------------------------------------------------------------------------------

def test_malformed_bio_link_and_item_do_not_stop_the_run():
    """Review major: a malformed bio URL and a malformed item must cost one row each, not the source."""
    bad_url_item = dict(reel("BAD2", "acmebrands", GOOD), url=5)
    items = ["junk", bad_url_item, reel("OK1", "acmebrands", GOOD), reel("OK2", "badlink", GOOD)]
    profiles = {"acmebrands": brand_profile(),
                "badlink": brand_profile(username="badlink", full_name="Bad Link", external="https://[abc")}
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", items), profiles=profiles))
    cands, ctx, _ = run(sc)
    assert [c.domain for c in cands] == ["acme.example"]
    assert sm.LAST_STATS["not an object"] == 1 and sm.LAST_STATS["malformed post"] == 1
    assert sm.LAST_STATS["no brand link"] == 1


def test_millisecond_threads_timestamp_is_undated_and_the_instagram_brand_is_kept():
    """Review major: a millisecond epoch raised out of datetime and aborted the source."""
    ms_post = {"code": "MS1", "caption": {"text": GOOD}, "taken_at": 1786968236000, "user": {"username": "msbrand"},
               "url": "https://www.threads.com/@msbrand/post/MS1"}
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("MS2", "acmebrands", GOOD)]),
                       threads={"affiliate program": [ms_post]},
                       profiles={"acmebrands": brand_profile(), "msbrand": brand_profile(username="msbrand")}))
    cands, ctx, _ = run(sc, cfg={"threads_queries": ["affiliate program"]}, days=3650)
    assert [c.signal.platform for c in cands] == ["instagram"]
    assert sm.post_date(1786968236000) is None


def test_out_of_credits_mid_run_keeps_the_brands_already_found():
    """Review major: a 402 on a later profile must not discard the brand already resolved."""
    items = [reel("OC1", "acmebrands", GOOD), reel("OC2", "brand2x", GOOD), reel("OC3", "brand3x", GOOD)]
    rts = routes(reels=only_phrase("join our affiliate program", items), profiles={"acmebrands": brand_profile()})

    def profile(p):
        if p["handle"] == "acmebrands":
            return brand_profile()
        raise sg.OutOfCredits("402")

    rts["/v1/instagram/profile"] = profile
    sc = FakeSC(rts)
    cands, ctx, _ = run(sc)
    assert [c.domain for c in cands] == ["acme.example"]
    assert [c[1]["handle"] for c in sc.calls if c[0] == "/v1/instagram/profile"] == ["acmebrands", "brand2x"]
    assert sm.LAST_STATS["out of credits"] == 1


def test_out_of_credits_during_search_makes_no_profile_calls():
    def boom(params):
        raise sg.OutOfCredits("402")

    rts = routes(reels=only_phrase("join our affiliate program", [reel("S0", "acmebrands", GOOD)]),
                 profiles={"acmebrands": brand_profile()})
    rts["/v2/instagram/reels/search"] = boom
    sc = FakeSC(rts)
    cands, ctx, _ = run(sc)
    assert cands == [] and not [c for c in sc.calls if c[0] == "/v1/instagram/profile"]
    assert sm.LAST_STATS["out of credits"] == 1


# homepage gates --------------------------------------------------------------------------------------------------

def test_homepage_that_does_not_name_the_brand_is_dropped():
    pages = {"https://acme.example/": page("https://acme.example/", "<title>Something Else Entirely</title>")}
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("H1", "acmebrands", GOOD)]),
                       profiles={"acmebrands": brand_profile()}))
    cands, ctx, _ = run(sc, pages=pages)
    assert cands == []


def test_parked_homepage_is_dropped():
    pages = {"https://acme.example/": page("https://acme.example/", "<title>acme.example is for sale | HugeDomains</title>")}
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("H2", "acmebrands", GOOD)]),
                       profiles={"acmebrands": brand_profile()}))
    cands, ctx, _ = run(sc, pages=pages)
    assert cands == []


def test_currency_list_in_page_source_is_not_a_us_signal():
    """Live finding: pages embed every currency symbol in JSON config and encode $ as an entity."""
    pages = {"https://acme.example/": page("https://acme.example/",
             '<title>Acme Brands</title><script>{"currency_signs": {"USD": "\\u0026#36;", "EUR": "\\u0026euro;"}}</script>')}
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("H4", "acmebrands", GOOD)]),
                       profiles={"acmebrands": brand_profile()}))
    cands, ctx, _ = run(sc, pages=pages)
    assert [c.domain for c in cands] == ["acme.example"]


def test_non_us_domain_is_dropped_without_a_fetch():
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("N1", "acmebrands", GOOD)]),
                       profiles={"acmebrands": brand_profile(external="https://acme.co.uk/affiliates")}))
    cands, ctx, msgs = run(sc, pages={"https://acme.co.uk/": page("https://acme.co.uk/", "<title>Acme Brands</title>")})
    assert cands == [] and ctx.page_calls == []


def test_redirect_to_the_brands_real_domain_is_used():
    pages = {"https://acme-store.example/": page("https://acme-store.example/", "<title>Acme Brands</title>",
                                                 final="https://acme.example/")}
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("R9", "acmebrands", GOOD)]),
                       profiles={"acmebrands": brand_profile(external="https://acme-store.example/affiliates")}))
    cands, ctx, _ = run(sc, pages=pages)
    assert [c.domain for c in cands] == ["acme.example"]


# threads ---------------------------------------------------------------------------------------------------------

EPOCH = 1786968236
EPOCH_ISO = datetime.fromtimestamp(EPOCH, tz=timezone.utc).date().isoformat()
THREADS_ON = {"threads_queries": ["affiliate program"]}


def test_threads_brand_post_is_kept_with_threads_url_and_epoch_date():
    post = {"id": "1_2", "code": "XYZ789", "caption": {"text": GOOD, "pk": "9"}, "taken_at": EPOCH,
            "user": {"username": "acmebrands", "full_name": "Acme Brands", "is_verified": False},
            "url": "https://www.threads.com/@acmebrands/post/XYZ789"}
    sc = FakeSC(routes(threads={"affiliate program": [post]}, profiles={"acmebrands": brand_profile()}))
    cands, ctx, _ = run(sc, days=3650, cfg=THREADS_ON)
    assert len(cands) == 1
    s = cands[0].signal
    assert s.platform == "threads" and s.url == "https://www.threads.com/@acmebrands/post/XYZ789"
    assert s.signal_date == EPOCH_ISO
    thread_calls = [c for c in sc.calls if c[0] == "/v1/threads/search"]
    assert thread_calls and "start_date" not in thread_calls[0][1]


def test_threads_null_caption_is_ignored():
    """The post is searched (query matches the configured one) and dropped by the caption gate."""
    post = {"code": "N0", "caption": None, "taken_at": EPOCH, "user": {"username": "acmebrands"}}
    sc = FakeSC(routes(threads={"affiliate program": [post]}, profiles={"acmebrands": brand_profile()}))
    cands, ctx, _ = run(sc, days=3650, cfg=THREADS_ON)
    assert cands == [] and sm.LAST_STATS.get("no caption") == 1
    assert [c for c in sc.calls if c[0] == "/v1/threads/search"]


# helpers ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,iso", [
    ("2026-09-26T21:52:38.000Z", "2026-09-26"), (EPOCH, EPOCH_ISO), (str(EPOCH), EPOCH_ISO),
    ("One Month Ago", None), (None, None), (True, None), (1786968236000, None),
])
def test_post_date(value, iso):
    assert sm.post_date(value) == iso


def test_reel_window_mapping_is_only_valid_api_values():
    assert sm._date_posted(3) == "last-week" and sm._date_posted(7) == "last-week"
    assert sm._date_posted(30) == "last-month" and sm._date_posted(90) == "last-year"


@pytest.mark.parametrize("url,canon", [
    ("https://www.instagram.com/p/ABC_1-x/?igsh=zz", "https://www.instagram.com/p/ABC_1-x/"),
    ("https://www.instagram.com/reel/DdrBPeCOapU/", "https://www.instagram.com/reel/DdrBPeCOapU/"),
    ("https://www.instagram.com/reels/DOq6eV6iIgD/", "https://www.instagram.com/reel/DOq6eV6iIgD/"),
])
def test_instagram_url_is_canonical(url, canon):
    assert sm.instagram_url({"url": url}) == canon


def test_profile_user_reads_nested_and_flat_shapes():
    assert sm.profile_user(brand_profile())["username"] == "acmebrands"
    assert sm.profile_user({"user": {"username": "x"}})["username"] == "x"
    assert sm.profile_user({"success": True}) is None


def test_say_output_has_no_person_names_or_handles():
    sc = FakeSC(routes(reels=only_phrase("join our affiliate program", [reel("Q1", "pat.example", "Use my code PAT10",
                                                                              full_name="Pat Example")])))
    cands, ctx, msgs = run(sc)
    text = " ".join(msgs)
    assert "Pat" not in text and "pat.example" not in text and "Example" not in text
    assert msgs and msgs[0].startswith("social_meta:")
