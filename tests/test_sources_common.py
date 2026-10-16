"""Program-lane source framework: shared helpers, migration 011, isolation and budgets in growth.run_sources,
the US gate and the known-company fast path. Synthetic data only."""
import asyncio
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from lead_radar import db as dbmod
from lead_radar.db import Store
from lead_radar.programs import growth, signals as sg, sources as srcs
from lead_radar.programs.sources import common as c

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def days_ago(n):
    return (NOW - timedelta(days=n)).date().isoformat()


# helpers ---------------------------------------------------------------------------------------------------------

def test_unwrap_and_hosts():
    assert c.unwrap("https://l.facebook.com/l.php?u=https%3A%2F%2Fshop.acme.com%2Faffiliates%3Futm_source%3Dfb&h=x") \
        == "https://shop.acme.com/affiliates"
    assert c.host_of("https://get.dripcapital.example/x") == "dripcapital.example"
    assert c.host_of("https://af.uppromote.com/acme/register") is None
    assert c.host_of("https://linktr.ee/acme") is None
    assert c.registrable("shop.acme.co.uk") == "acme.co.uk"


def test_brand_root_needs_a_majority_host():
    assert c.brand_root(["https://acme.com/a", "https://acme.com/b", "https://bit.ly/x", "https://other.com"]) == "acme.com"
    assert c.brand_root(["https://a.com", "https://b.com", "https://c.com", "https://d.com"]) is None
    assert c.brand_root(["https://instagram.com/acme"]) is None


def test_domains_in_text():
    assert c.domains_in_text("Try acme.io or https://www.Shop.Acme.io/x, not linktr.ee/me") == ["acme.io"]


@pytest.mark.parametrize("a,b,same", [
    ("Acme Co.", "ACME, Inc", True), ("Blue River Analytics", "Blue River Analytics LLC", True),
    ("Rev Catch", "RevCatch", True), ("Acme Labs", "Acme Capital", False), ("Bright", "Bright Horizons", False),
])
def test_names_match(a, b, same):
    assert c.names_match(a, b) is same


def test_page_names_brand_and_parked():
    assert c.page_names_brand("<title>Acme Co | Invoicing for plumbers</title>", "Acme")
    assert not c.page_names_brand("<title>Something else</title>", "Acme")
    assert c.parked("<title>acme.com is for sale | HugeDomains</title>")
    assert not c.parked("<title>Tarte</title><script>onmousedown sedo</script>")


@pytest.mark.parametrize("text,iso", [
    ("3 days ago", "2026-10-07"), ("Posted yesterday", "2026-10-09"), ("2 hours ago", "2026-10-10"),
    ("a week ago", "2026-10-03"), ("Sep 21, 2026", "2026-09-21"), ("2026-09-30T10:00", "2026-09-30"),
    (str(int(datetime(2026, 10, 9, 15, tzinfo=timezone.utc).timestamp())), "2026-10-09"), ("One Month Ago", None),
    ("", None),
])
def test_relative_date(text, iso):
    assert c.relative_date(text, NOW) == iso


@pytest.mark.parametrize("text,keep", [
    ("We just launched our affiliate program - 30% recurring", True),
    ("Join our affiliate program, link in bio", True),
    ("Now accepting new ISOs, paying up to 12 points", True),
    ("Looking for affiliates for our skincare brand", True),
    ("Hiring an affiliate manager to grow our partner program", True),
    ("Love this brand! affiliate", False),
    ("Join the conversation", False),
])
def test_buyer_language(text, keep):
    assert c.buyer_language(text) is keep


@pytest.mark.parametrize("text,reason", [
    ("Use my code SAVE10 at checkout", "individual_affiliate"),
    ("Join my team and make money online", "mlm"),
    ("We help brands grow their affiliate program", "seller"),
    ("Best casino affiliate program 50% revshare", "vertical"),
    ("Which affiliate programs to join for beginners?", "seeker"),
    ("Recruiting for a client: affiliate manager", "recruiter"),
    ("We just launched our affiliate program", None),
    ("Book now, limited slots for our referral program", None),
])
def test_text_gate(text, reason):
    assert c.text_gate(text) == reason


# migration 011 ---------------------------------------------------------------------------------------------------

def test_migration_011_keeps_signal_ids_and_adds_platform(tmp_path):
    path = tmp_path / "r.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    real = sorted(dbmod.MIGRATIONS_DIR.glob("*.sql"))
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for p in real:  # apply up to 010 by hand, then let migrate() apply 011
        if p.name.startswith("011"):
            break
        for stmt in dbmod._statements(p.read_text()):
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (p.name,))
    conn.execute("INSERT INTO program_companies (domain, first_seen, updated_at) VALUES ('acme.com', 'x', 'x')")
    for kind in ("job", "partner_program", "company_post"):
        conn.execute("INSERT INTO program_signals (id, domain, kind, url, first_seen) VALUES (?, 'acme.com', ?, ?, 'x')",
                     (f"id-{kind}", kind, f"https://x/{kind}"))
    conn.commit()
    assert dbmod.migrate(conn) == ["011_program_signal_sources.sql"]
    rows = {r["id"]: r["platform"] for r in conn.execute("SELECT id, platform FROM program_signals")}
    assert rows == {"id-job": "job_board", "id-partner_program": "web", "id-company_post": "linkedin"}
    conn.execute("INSERT INTO program_signals (id, domain, kind, platform, url, first_seen) "
                 "VALUES ('n', 'acme.com', 'paid_ad', 'meta_ads', 'https://ad', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO program_signals (id, domain, kind, url, first_seen) "
                     "VALUES ('m', 'acme.com', 'nonsense', 'https://y', 'x')")


# scoring ---------------------------------------------------------------------------------------------------------

def test_recruiting_ads_and_social_posts_score_and_prove_a_program():
    row = {"list": "affiliate", "status": "no_program", "size": "11-50 employees"}
    ad = [{"kind": "paid_ad", "platform": "meta_ads", "quote": "Join our affiliate program", "signal_date": days_ago(3)}]
    assert sg.lead_score(row, ad, NOW) == 60
    both = ad + [{"kind": "social_post", "platform": "instagram", "quote": "Our ambassador program is open",
                  "signal_date": days_ago(5)}]
    assert sg.lead_score(row, both, NOW) == 80  # +15 post, +5 seen on two platforms
    vague = [{"kind": "social_post", "platform": "tiktok", "quote": "new drop today", "signal_date": days_ago(1)}]
    assert sg.lead_score(row, vague, NOW) == 0
    assert "Meta" not in sg.why(ad) and sg.why(both).startswith("running ads to recruit affiliates")
    assert sg.why(both[1:]) == f"Instagram post ({days_ago(5)})"


def test_sub_budget_caps_a_source_inside_the_run():
    class M:
        n = 0

        def total_credits(self):
            return self.n

    sc = types.SimpleNamespace(meter=M(), exhausted=False)
    run = sg.Budget(sc, 10)
    sc.meter.n = 4
    sub = run.sub(3)
    assert sub.ok() and sub.left == 3
    sc.meter.n = 7
    assert not sub.ok() and run.ok()
    sub2 = run.sub(50)
    assert sub2.left == 3  # never more than the run has left


# run_sources: isolation, US gate, known fast path -------------------------------------------------------------------

class FakeMeter:
    def __init__(self):
        self.n = 0

    def total_credits(self):
        return self.n


class FakeSC:
    def __init__(self, companies):
        self.companies, self.meter, self.exhausted, self.calls = companies, FakeMeter(), False, []

    async def get(self, path, params, source=""):
        self.meter.n += 1
        self.calls.append(path)
        if path == "/v1/linkedin/company" and params["url"] in self.companies:
            return self.companies[params["url"]]
        raise sg.SkipItem("404")


def company(name, website, country="US"):
    return {"name": name, "website": website, "location": {"country": country}, "industry": "Software Development",
            "size": "11-50 employees", "description": f"{name} makes software", "posts": [], "similarPages": []}


@pytest.fixture
def fake_sources(monkeypatch):
    mods = {}

    def add(name, collect, **defaults):
        m = types.ModuleType(f"fake_{name}")
        m.NAME, m.DEFAULTS, m.collect = name, defaults, collect
        mods[name] = m
    monkeypatch.setattr(srcs, "REGISTRY", {})
    monkeypatch.setattr(srcs, "load", lambda name: mods[name])

    def register(name, collect, **defaults):
        add(name, collect, **defaults)
        srcs.REGISTRY[name] = name
    return register


def _run(store, sc, steps):
    rep = growth.GrowthReport()
    res = growth.Resolver(sc, None, store, sg.Budget(sc, 100), sg.load())
    touched = asyncio.run(growth.run_sources(sc, None, store, res, sg.Budget(sc, 100), sg.load(), rep, steps,
                                             ("affiliate", "finance"), 3, lambda m: None))
    return rep, touched


def test_a_failing_source_does_not_stop_the_others(fake_sources):
    store = Store.open(":memory:")
    sc = FakeSC({"https://www.linkedin.com/company/acme": company("Acme", "https://acme.com")})

    async def broken(ctx):
        raise RuntimeError("layout changed")

    async def good(ctx):
        assert ctx.budget.left <= 7 and ctx.cfg["credits"] == 7 and ctx.cfg["phrase"] == "x"
        return [sg.Candidate("affiliate", sg.Signal("paid_ad", "https://ads/1", "Join our affiliate program",
                                                    "2026-10-09", platform="meta_ads"),
                             linkedin_url="https://www.linkedin.com/company/acme")]
    fake_sources("broken", broken)
    fake_sources("good", good, credits=7, phrase="x")
    rep, touched = _run(store, sc, ("broken", "good"))
    assert "RuntimeError" in rep.sources["broken"]["error"] and rep.sources["good"]["candidates"] == 1
    assert touched == ["acme.com"]
    sig = store.program_signals()["acme.com"][0]
    assert sig["kind"] == "paid_ad" and sig["platform"] == "meta_ads"


def test_social_candidates_need_a_confirmed_us_company(fake_sources):
    store = Store.open(":memory:")
    sc = FakeSC({"https://www.linkedin.com/company/uk": company("Brit", "https://brit.co.uk", country="GB"),
                 "https://www.linkedin.com/company/us": company("Yank", "https://yank.com")})

    async def social(ctx):
        return [sg.Candidate("affiliate", sg.Signal("social_post", f"https://ig/{k}", "Join our affiliate program",
                                                    platform="instagram"),
                             linkedin_url=f"https://www.linkedin.com/company/{k}", require_us=True) for k in ("uk", "us")]
    fake_sources("social", social)
    rep, touched = _run(store, sc, ("social",))
    assert touched == ["yank.com"] and rep.excluded == {"US not confirmed": 1}
    assert store.program_company("brit.co.uk") is None


def test_known_companies_get_new_signals_without_a_linkedin_lookup(fake_sources):
    store = Store.open(":memory:")
    store.save_company_profile("acme.com", lst="affiliate", source="x", ref="x", name="Acme",
                               linkedin_url="https://www.linkedin.com/company/acme", country="US", hq=None, size=None,
                               industry=None, excluded=None)
    store.save_company_profile("bad.com", lst="affiliate", source="x", ref="x", name="Bad", linkedin_url=None,
                               country="US", hq=None, size=None, industry=None, excluded="recruiter")
    sc = FakeSC({})

    async def src(ctx):
        return [sg.Candidate("affiliate", sg.Signal("job", "https://builtin/1", "Affiliate Manager", platform="builtin"),
                             linkedin_url="https://www.linkedin.com/company/acme"),
                sg.Candidate("affiliate", sg.Signal("job", "https://builtin/2", "Affiliate Manager", platform="builtin"),
                             domain="bad.com")]
    fake_sources("src", src)
    rep, touched = _run(store, sc, ("src",))
    assert sc.calls == [] and touched == ["acme.com"] and rep.excluded == {"already excluded": 1}
    assert store.program_signals()["acme.com"][0]["platform"] == "builtin"


def test_weekly_sources_skip_short_windows(fake_sources):
    store = Store.open(":memory:")
    ran = []

    async def a(ctx):
        ran.append(ctx.days)
        return []
    fake_sources("weekly", a, min_days=14)
    _run(store, FakeSC({}), ("weekly",))  # 3-day run
    assert ran == []


def test_disabled_and_unrequested_sources_do_not_run(fake_sources):
    store = Store.open(":memory:")
    ran = []

    async def a(ctx):
        ran.append("a")
        return []
    fake_sources("a", a, enabled=False)
    fake_sources("b", a)
    _run(store, FakeSC({}), ("a",))
    assert ran == []


def test_registry_modules_follow_the_contract():
    """Every registered source module that exists exposes NAME, DEFAULTS and an async collect(ctx)."""
    import importlib
    import inspect
    for key, mod in srcs.REGISTRY.items():
        try:
            m = importlib.import_module(f"lead_radar.programs.sources.{mod}")
        except ImportError:
            continue  # not built yet
        assert m.NAME == key and isinstance(m.DEFAULTS, dict) and inspect.iscoroutinefunction(m.collect)


def test_a_homepage_linkedin_link_to_another_company_is_not_trusted():
    store = Store.open(":memory:")
    sc = FakeSC({"https://www.linkedin.com/company/bigparent": company("Big Parent Holdings", "https://bigparent.com"),
                 "https://www.linkedin.com/company/acme": company("Acme", "https://www.acme.com")})
    res = growth.Resolver(sc, None, store, sg.Budget(sc, 10), sg.load())

    async def footer(domain):
        return {"acme.com": "https://www.linkedin.com/company/acme",
                "brandx.com": "https://www.linkedin.com/company/bigparent"}[domain]
    res.linkedin_for_domain = footer
    ok = asyncio.run(res.resolve(sg.Candidate("affiliate", domain="acme.com")))
    wrong = asyncio.run(res.resolve(sg.Candidate("affiliate", domain="brandx.com", company_name="Brand X")))
    assert ok.domain == "acme.com" and wrong is None


@pytest.mark.parametrize("text,us", [
    ("Acme Goods, 12 Main St, Austin, TX 78701", True),
    ("Proudly made in the USA. Free shipping.", True),
    ("Ships to the USA and Canada. Acme Ltd, Registered in England", False),
    ("Our studio in Lisbon", False),
    ("Limited time offer! Free shipping in the United States and Canada", True),
    ("Acme Goods, 4 Pine Rd, Dallas, Texas 75201", True),
])
def test_homepage_us(text, us):
    assert growth.homepage_us(text) is us


def test_brands_without_a_linkedin_page_resolve_from_their_own_homepage(fake_sources, monkeypatch):
    store = Store.open(":memory:")
    pages = {"https://kineo.example/": "<title>Kineo | Red light therapy</title><p>Kineo, 5 Elm St, Boise, ID 83702</p>",
             "https://faraway.example/": "<title>Faraway</title><p>Faraway Pty, Sydney, Australia</p>",
             "https://parked.example/": "<title>parked.example is for sale | HugeDomains</title>",
             "https://footer.example/": '<title>Footer Co</title><a href="/pages/contact">Contact</a>',
             "https://footer.example/pages/contact": "<p>Footer Co, 9 Oak Ave, Miami, FL 33101</p>"}

    async def fake_get(http, url, role):
        from lead_radar.programs import detect
        html = pages.get(url)
        return detect.Page(url, url, 200, html, role) if html else None
    monkeypatch.setattr(growth.detect, "_get", fake_get)

    async def ads(ctx):
        return [sg.Candidate("affiliate", sg.Signal("paid_ad", f"https://ads/{d}", "Join our affiliate program",
                                                    "2026-10-09", platform="meta_ads"),
                             company_name=n, domain=d, require_us=True)
                for n, d in (("Kineo", "kineo.example"), ("Faraway", "faraway.example"), ("Parked", "parked.example"),
                             ("Footer Co", "footer.example"))]
    fake_sources("ads", ads)
    sc = FakeSC({})
    rep = growth.GrowthReport()
    res = growth.Resolver(sc, object(), store, sg.Budget(sc, 0), sg.load())  # no credits: LinkedIn never found
    touched = asyncio.run(growth.run_sources(sc, object(), store, res, sg.Budget(sc, 100), sg.load(), rep, ("ads",),
                                             ("affiliate",), 3, lambda m: None))
    assert touched == ["kineo.example", "footer.example"] and rep.by_homepage == 3 and rep.unresolved == 1
    assert rep.excluded == {"US not confirmed": 1}
    row = store.program_company("kineo.example")
    assert row["country"] == "US" and row["linkedin_url"] is None and row["name"] == "Kineo"
