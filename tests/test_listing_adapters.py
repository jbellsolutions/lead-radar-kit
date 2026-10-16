"""Job boards (OnlineJobs.ph, We Work Remotely, Freelancer, Indeed via Data Box) and the listing classifier.

Offline: synthetic fixtures in tests/fixtures/listings shaped like the real pages; no real listings or people.
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx

from lead_radar import config
from lead_radar.adapters import LISTING_ADAPTERS, AdapterContext
from lead_radar.classify import listings as lc
from lead_radar.databox import DataBox, DataBoxError
from lead_radar.db import Store
from lead_radar.expander import load_brief_file
from lead_radar.hunt import hunt, resolve_platforms
from lead_radar.models import LISTING_PLATFORMS, Post
from lead_radar.scrapecreators import Meter, ScrapeCreators

ROOT = Path(__file__).parents[1]
FIX = ROOT / "tests" / "fixtures" / "listings"
BRIEF = ROOT / "config" / "briefs" / "virtual-assistant.json"


def run(coro):
    return asyncio.run(coro)


def brief():
    return load_brief_file(BRIEF, "r1")


def fixture(name: str) -> str:
    now = datetime.now(timezone.utc)

    def fill(m):
        kind, hours = m.group(1), int(m.group(2))
        t = now - timedelta(hours=hours)
        return t.strftime("%Y-%m-%d %H:%M:%S") if kind == "ISO" else format_datetime(t)
    return re.sub(r"\{\{(ISO|RFC):-(\d+)h\}\}", fill, (FIX / name).read_text())


class Boards(httpx.AsyncBaseTransport):
    """Serves the fixtures by URL and records every request (method + URL)."""

    def __init__(self, robots_ojs="robots_ojs.txt"):
        self.calls: list[tuple[str, str]] = []
        self.robots_ojs = robots_ojs

    async def handle_async_request(self, request):
        url = str(request.url)
        self.calls.append((request.method, url))
        path, host = request.url.path, request.url.host
        body, status = "", 404
        if path == "/robots.txt":
            name = {"www.onlinejobs.ph": self.robots_ojs, "www.freelancer.com": "robots_fl.txt"}.get(host)
            if name:
                body, status = (FIX / name).read_text() if (FIX / name).exists() else name, 200
        elif host == "www.onlinejobs.ph" and path == "/jobseekers/jobsearch":
            body, status = fixture("ojs_page0.html"), 200
        elif host == "www.onlinejobs.ph" and path == "/jobseekers/jobsearch/30":
            body, status = fixture("ojs_page1.html"), 200
        elif host == "www.onlinejobs.ph" and path.startswith("/jobseekers/job/"):
            body, status = fixture("ojs_job.html"), 200
        elif host == "weworkremotely.com" and path.startswith("/categories/"):
            body, status = fixture("wwr.rss"), 200
        elif host == "www.freelancer.com" and path == "/jobs/virtual-assistant/":
            body, status = fixture("fl_va.html"), 200
        elif host == "www.freelancer.com":
            body, status = "<html><body></body></html>", 200
        return httpx.Response(status, text=body)


class Sleeps:
    def __init__(self):
        self.waits: list[float] = []

    async def __call__(self, seconds):
        self.waits.append(seconds)


def collect(platform, transport=None, *, brief_obj=None, databox=None, **ctxkw):
    transport = transport or Boards()
    sleeps = Sleeps()
    sc = SimpleNamespace(meter=Meter())
    ctx = AdapterContext(run_id="run-1", listing_transport=transport, listing_sleep=sleeps, databox=databox, **ctxkw)
    res = run(LISTING_ADAPTERS[platform](sc, ctx).collect(brief_obj or brief()))
    return res, transport, sleeps, sc


# --- registry / platform aliases ---------------------------------------------

def test_listing_platforms_registered_and_aliased():
    assert set(LISTING_ADAPTERS) == set(LISTING_PLATFORMS)
    assert resolve_platforms("listings") == list(LISTING_PLATFORMS)
    assert set(LISTING_PLATFORMS) <= set(resolve_platforms("all"))
    assert not set(LISTING_PLATFORMS) & set(resolve_platforms("api"))


# --- OnlineJobs.ph ------------------------------------------------------------

def test_onlinejobs_pages_until_lookback_and_caps_detail_pages():
    res, t, sleeps, sc = collect("onlinejobs", max_terms=1, lookback_days=2,
                                 listings={"onlinejobs": {"max_detail_pages": 5}})
    paths = [u for _, u in t.calls]
    assert any("/jobseekers/jobsearch/30?" in u for u in paths)
    assert not any("/jobseekers/jobsearch/60" in u for u in paths), "kept paging past the lookback window"
    assert res.stats.errors == 0, res.stats.error_messages
    assert len(res.posts) == 31  # 30 fresh on page 0 + the one card on page 1 inside 48h
    assert all(p.kind == "job" and p.platform == "onlinejobs" for p in res.posts)
    detail = [u for u in paths if "/jobseekers/job/" in u]
    assert len(detail) == 5
    assert sum(1 for p in res.posts if p.author_handle == "employer:123456") == 5
    assert any("detail pages capped at 5" in n for n in res.stats.notes)
    first = res.posts[0]
    assert first.url.startswith("https://www.onlinejobs.ph/jobseekers/job/synthetic-role-") and "?" not in first.url
    assert first.raw["employer_id"] == "123456" and first.raw["hours"] == "20"
    assert "Part Time" not in (first.title or "")  # the badge is not part of the title
    assert res.stats.requests == sc.meter.requests["onlinejobs"] == len(t.calls) - 1  # robots.txt isn't metered


def test_onlinejobs_honours_crawl_delay_and_is_get_only():
    _, t, sleeps, _ = collect("onlinejobs", max_terms=1, lookback_days=2,
                              listings={"onlinejobs": {"max_detail_pages": 3}})
    assert {m for m, _ in t.calls} == {"GET"}
    assert sleeps.waits and min(sleeps.waits) >= 4.9  # robots Crawl-delay: 5 (fake clock: waits are requested)


def test_onlinejobs_respects_robots_disallow(tmp_path):
    (FIX / "robots_block.tmp").write_text("User-agent: *\nDisallow: /jobseekers/\n")
    try:
        res, t, _, _ = collect("onlinejobs", Boards(robots_ojs="robots_block.tmp"), max_terms=1)
    finally:
        (FIX / "robots_block.tmp").unlink()
    assert res.posts == []
    assert [u for _, u in t.calls] == ["https://www.onlinejobs.ph/robots.txt"]
    assert any("robots.txt disallows" in e for e in res.stats.error_messages)


def test_onlinejobs_without_terms_is_a_note_not_an_error():
    b = brief()
    b.terms.pop("onlinejobs")
    res, t, _, _ = collect("onlinejobs", brief_obj=b)
    assert t.calls == [] and res.stats.errors == 0 and res.stats.notes


# --- We Work Remotely / Freelancer --------------------------------------------

def test_weworkremotely_rss_filters_by_keyword_and_window():
    res, _, _, _ = collect("weworkremotely", lookback_days=7,
                           listings={"weworkremotely": {"categories": ["remote-customer-support-jobs"]}})
    assert res.stats.errors == 0, res.stats.error_messages
    assert [(p.author_name, p.title) for p in res.posts] == [("Example Labs", "Remote Executive Assistant")]
    p = res.posts[0]
    assert p.author_handle == "wwr:co:example-labs" and p.raw["company"] == "Example Labs"
    assert p.text.startswith("Example Labs: Remote Executive Assistant")


def test_boards_without_job_titles_search_nothing():
    """A stale brief without job titles must not turn into "accept every listing in the category"."""
    b = brief()
    b.terms.pop("onlinejobs")
    b.terms.pop("indeed")
    for platform in ("weworkremotely", "freelancer"):
        res, t, _, _ = collect(platform, brief_obj=b)
        assert res.posts == [] and t.calls == [] and "no " + platform in res.stats.notes[0]


def test_boards_report_what_they_dropped():
    res, _, _, _ = collect("weworkremotely", lookback_days=7,
                           listings={"weworkremotely": {"categories": ["remote-customer-support-jobs"]}})
    assert "fetched 3, dropped 1 outside the window, 1 off-topic, kept 1" in res.stats.notes


def test_onlinejobs_skips_detail_pages_for_seen_listings():
    from lead_radar.models import post_id
    from lead_radar.normalize import canonical_url
    seen_url = canonical_url("https://www.onlinejobs.ph/jobseekers/job/synthetic-role-1")
    res, t, _, _ = collect("onlinejobs", max_terms=1, lookback_days=2, listings={"onlinejobs": {"max_detail_pages": 40}},
                           seen=lambda ids: {i for i in ids if i == post_id("onlinejobs", seen_url)})
    detail = [u for _, u in t.calls if "/jobseekers/job/" in u]
    assert len(detail) == 30 and not any(u.endswith("synthetic-role-1") for u in detail)
    assert any("1 already seen" in n for n in res.stats.notes)


def test_freelancer_reads_category_paths_only():
    res, t, _, _ = collect("freelancer", listings={"freelancer": {"categories": ["virtual-assistant"], "max_pages": 2}})
    assert [p.title for p in res.posts] == ["Virtual assistant for inbox"]
    assert res.posts[0].raw["price"] == "$250 Average bid"
    assert all("?" not in u for _, u in t.calls if "robots" not in u)
    assert res.posts[0].author_handle is None  # Freelancer clients are anonymous


# --- Indeed via Data Box job_board --------------------------------------------

class FakeBox:
    def __init__(self, listings=None, cost=0.04, error=None):
        self.calls, self.listings, self.cost, self.error = [], listings or [], cost, error

    async def job_board(self, **args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return {"listings": self.listings, "count": len(self.listings), "cost_usd": self.cost, "status": "succeeded"}


def _indeed_item(**kw):
    item = {"source": "indeed", "title": "Executive Assistant", "company": "Example Widgets Co",
            "company_website": "https://example.com", "location": "Remote", "salary": None, "job_type": "Full-time",
            "posted_at": (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(),
            "url": "https://www.indeed.com/viewjob?jk=synthetic1", "description": "Support our founder with email."}
    return {**item, **kw}


def test_indeed_through_databox_meters_spend_and_names_company():
    box = FakeBox([_indeed_item()])
    res, _, _, sc = collect("indeed", databox=box, listings={"indeed": {"budget_left_usd": 2.0, "max_queries": 2}})
    assert len(box.calls) == 2 and box.calls[0]["source"] == "indeed"
    assert box.calls[0]["max_cost_usd"] == 0.25 and box.calls[0]["posted_within_days"] == 14
    assert res.stats.cost_usd == round(sc.meter.usd["indeed"], 6) == 0.08
    assert len(res.posts) == 1  # the same listing returned for both queries is kept once
    p = res.posts[0]
    assert p.author_handle == "indeed:co:example-widgets-co" and p.raw["company_website"] == "https://example.com"


def test_indeed_stops_on_budget_without_error():
    res, _, _, _ = collect("indeed", databox=FakeBox([_indeed_item()]), listings={"indeed": {"budget_left_usd": 0}})
    assert res.posts == [] and res.stats.errors == 0
    assert any("budget" in n for n in res.stats.notes)
    box = FakeBox(error=DataBoxError("refused", "budget_exhausted"))
    res, _, _, _ = collect("indeed", databox=box, listings={"indeed": {"budget_left_usd": 2}})
    assert res.stats.errors == 0 and any("Data Box job_board budget" in n for n in res.stats.notes)


def test_indeed_without_databox_is_a_note():
    res, _, _, _ = collect("indeed")
    assert res.stats.errors == 0 and "DATABOX_URL" in res.stats.notes[0]


def test_databox_client_polls_running_jobs_and_maps_refusals():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content), request.headers["authorization"]))
        if request.url.path.endswith("/job_board"):
            return httpx.Response(200, json={"job_id": "j1", "status": "running", "cost_usd": 0, "hint": "poll"})
        if request.url.path.endswith("/job"):
            return httpx.Response(200, json={"job_id": "j1", "status": "succeeded", "cost_usd": 0.03,
                                             "listings": [], "count": 0})
        return httpx.Response(422, json={"error": "standing enrich budget is spent for today"})

    async def go():
        box = DataBox("https://box.test/", "tok", transport=httpx.MockTransport(handler))
        try:
            done = await box.job_board(source="indeed", query="va")
            try:
                await box.enrich(name="x")
            except DataBoxError as exc:
                return done, exc
        finally:
            await box.aclose()
    done, exc = run(go())
    assert done["status"] == "succeeded" and done["cost_usd"] == 0.03
    assert seen[1][0] == "/api/v1/job" and seen[1][1] == {"action": "get", "job_id": "j1", "wait_seconds": 60}
    assert seen[0][2] == "Bearer tok"
    assert exc.budget and "tok" not in str(exc)


# --- listing classifier -------------------------------------------------------

def _post(text, platform="onlinejobs", **kw):
    from lead_radar.normalize import make_post
    return make_post(platform=platform, url=f"https://example.test/{abs(hash(text))}", kind="job", text=text,
                     run_id="run-1", source_term="va", posted_at=datetime.now(timezone.utc), **kw)


def test_rules_rule_out_scams_and_old_listings():
    b = brief()
    now = datetime.now(timezone.utc)
    assert lc.rules(_post("Pay the registration fee of $20 to start. Virtual assistant wanted for our team."), b,
                    lookback_days=14, now=now) == "pay_to_apply_or_mlm"
    old = _post("Virtual assistant needed for inbox and calendar management, part time.")
    old = old.model_copy(update={"posted_at": now - timedelta(days=30)})
    assert lc.rules(old, b, lookback_days=14, now=now) == "older_than_lookback"
    # the VA brief's negative term "job" (aimed at job seekers on social) must not drop job listings
    assert "job" in b.negative_terms
    assert lc.rules(_post("Full-time job: virtual assistant for our clinic's inbox and calendar."), b,
                    lookback_days=14, now=now) is None


def test_validate_drops_invented_company_and_caps_staffing():
    p = _post("Our client, a growing dental practice, needs a virtual assistant for scheduling and patient emails.")
    out = lc.validate({"employer_type": "staffing", "fit": "high", "role_title": "Virtual Assistant",
                       "company_name": "Bright Smiles Dental", "need": "scheduling help",
                       "evidence_quote": "needs a virtual assistant for scheduling", "confidence": 0.9}, p)
    assert out["fit"] == "medium" and out["company_name"] == ""
    p2 = _post("Example Widgets Co is hiring a virtual assistant to run our inbox.", raw={"company": ""})
    out2 = lc.validate({"employer_type": "direct", "fit": "high", "role_title": "VA", "company_name": "Example Widgets Co",
                        "need": "inbox", "evidence_quote": "hiring a virtual assistant to run our inbox",
                        "confidence": 0.8}, p2)
    assert out2["company_name"] == "Example Widgets Co" and lc.is_lead(out2)


def _openrouter(verdict):
    def handler(request):
        return httpx.Response(200, json={"model": "m", "usage": {"prompt_tokens": 100, "completion_tokens": 40},
                                         "choices": [{"message": {"content": json.dumps(verdict)}}]})
    return httpx.MockTransport(handler)


def _settings(tmp_path):
    return config.load(config_path=ROOT / "config" / "radar.yaml", env_file=tmp_path / "none",
                       environ={"SCRAPECREATORS_API_KEY": "k", "OPENROUTER_API_KEY": "or",
                                "RADAR_STATE_DIR": str(tmp_path)})


def test_hunt_with_job_boards_classifies_listings_outside_the_cascade(tmp_path):
    s = _settings(tmp_path)
    store = Store.open(s.db_path)
    verdict = {"employer_type": "direct", "fit": "high", "role_title": "Virtual Assistant",
               "company_name": "Example Widgets Co", "need": "inbox and calendar help",
               "evidence_quote": "Example Widgets Co is hiring a virtual assistant.", "confidence": 0.9}
    sleeps = Sleeps()

    async def go():
        async with ScrapeCreators("k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"credits": 10})),
                                  backoff=0) as sc:
            return await hunt(s, store, "virtual assistant", platforms="onlinejobs,weworkremotely", days=2,
                              brief_path=BRIEF, sc=sc, use_jev=False, s2_transport=_openrouter(verdict),
                              listing_transport=Boards(), listing_sleep=sleeps)
    res = run(go())
    c = res.run.counts
    assert c["listings_fetched"] >= 31 and c["listings_queued"] == c["listings_new"] > 0
    assert c["to_classify"] == 0 and store.pending(res.radar.id) == []  # nothing entered the M3 cascade
    verdicts = store.listing_verdicts(res.radar.id)
    assert len(verdicts) == c["listings_queued"]
    leads = [v for v in verdicts if lc.is_lead(v)]
    assert leads and c["listing_leads"] == len(leads)
    assert any(v["company_name"] == "Example Widgets Co" for v in leads)  # verbatim in the detail text
    assert res.run.cost_usd > 0 and c["listing_cost_usd"] > 0
    assert store.classifications(res.radar.id) == []

    again = run(go())  # second run: everything already seen
    assert again.run.counts["listings_new"] == 0
