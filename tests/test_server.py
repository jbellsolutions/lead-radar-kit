"""Hosted dashboard: password gate, campaign create/edit, job queue, daily trigger, credit cap, import guard.

The worker is never started here (no lifespan), so no hunt subprocess runs and nothing calls a live API.
"""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from lead_radar import expander, server
from lead_radar.db import Store
from lead_radar.models import Run

ROOT = Path(__file__).parents[1]
BRIEF = json.loads((ROOT / "config" / "briefs" / "virtual-assistant.json").read_text())


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DASHBOARD_PASSWORD", "pw-123")
    for key in ("SCRAPECREATORS_API_KEY", "MCP_SCRAPE_CREATORS_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(server.config, "DEFAULT_ENV_FILE", tmp_path / "none")
    server._balance_cache.update(at=1e12, value=1234)  # never call ScrapeCreators from tests
    return TestClient(server.app)  # no `with`: lifespan (the job worker) does not start


def _auth():
    return {"Authorization": "Bearer pw-123"}


def _new(client, name="va-test", run_now=False, daily=True):
    return client.post("/api/campaigns", headers=_auth(), json={
        "new": True, "name": name, "offer": "virtual assistant", "offer_context": "", "days": 3,
        "daily": daily, "run_now": run_now, "brief": BRIEF})


def test_health_is_open_and_everything_else_needs_the_password(client):
    assert client.get("/health").text == "ok"
    assert client.get("/api/data").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.post("/api/run-daily").status_code == 401
    assert client.get("/api/data", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_login_sets_cookie_and_serves_live_page(client):
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": "pw-123"}).status_code == 200
    page = client.get("/").text
    assert "const LIVE = true;" in page and 'id="view-campaigns"' in page
    assert client.get("/api/data").status_code == 200


def test_no_password_configured_locks_everything(client, monkeypatch):
    monkeypatch.setenv("RADAR_DASHBOARD_PASSWORD", "")
    assert client.get("/api/data", headers={"Authorization": "Bearer "}).status_code == 401


def test_create_campaign_saves_brief_and_queues_one_job(client, tmp_path):
    r = _new(client, run_now=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["brief_version"] == 1 and body["job"]["already_queued"] is False
    assert _new(client).status_code == 409  # name taken
    again = client.post("/api/campaigns/va-test/run", headers=_auth(), json={})
    assert again.json()["job"]["already_queued"] is True  # one job per campaign at a time
    data = client.get("/api/data", headers=_auth()).json()
    camp = data["campaigns"][0]
    assert camp["name"] == "va-test" and camp["daily"] and camp["brief_version"] == 1
    assert data["jobs"][0]["status"] == "queued" and data["credits"]["balance"] == 1234
    assert (tmp_path / "briefs" / "va-test-v1.json").exists()


def test_edit_campaign_makes_a_new_brief_version(client):
    _new(client)
    got = client.get("/api/campaigns/va-test", headers=_auth()).json()
    brief = got["brief"]
    brief["terms"]["reddit"] = ["need a virtual assistant"]
    r = client.post("/api/campaigns", headers=_auth(), json={"name": "va-test", "days": 7, "daily": False,
                                                              "brief": brief})
    assert r.json()["brief_version"] == 2
    again = client.get("/api/campaigns/va-test", headers=_auth()).json()
    assert again["brief"]["terms"]["reddit"] == ["need a virtual assistant"] and again["days"] == 7
    assert again["daily"] is False


def test_invalid_brief_and_window_are_rejected(client):
    bad = dict(BRIEF, seller_signals=["one"])
    r = client.post("/api/campaigns", headers=_auth(), json={"new": True, "name": "x", "offer": "virtual assistant",
                                                             "days": 3, "brief": bad})
    assert r.status_code == 400 and "isn't valid" in r.json()["error"]
    r = client.post("/api/campaigns", headers=_auth(), json={"new": True, "name": "y", "offer": "virtual assistant",
                                                             "days": 5, "brief": BRIEF})
    assert r.status_code == 400


def test_run_daily_queues_only_daily_active_campaigns(client):
    _new(client, "daily-one", daily=True)
    _new(client, "manual-one", daily=False)
    client.patch("/api/campaigns/daily-one", headers=_auth(), json={"active": True})
    queued = client.post("/api/run-daily", headers=_auth()).json()["queued"]
    assert [q["campaign"] for q in queued] == ["daily-one"]  # the program lane's daily run is off by default
    jobs = client.get("/api/jobs", headers=_auth()).json()["jobs"]
    assert jobs[0]["trigger"] == "daily" and jobs[0]["days"] == server.DAILY_DAYS


def test_worker_skips_when_daily_credit_cap_reached(client, tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DAILY_CREDIT_CAP", "100")
    _new(client, run_now=True)
    st = Store.open(tmp_path / "radar.sqlite")
    radar = st.get_radar("va-test")
    st.save_run(Run(id="r-today", radar_id=radar.id, credits_used=150, started_at=datetime.now(timezone.utc)))
    job = st.conn.execute("SELECT * FROM jobs").fetchone()
    asyncio.run(server.Worker().execute(st, job))
    row = st.conn.execute("SELECT status, note FROM jobs").fetchone()
    assert row["status"] == "skipped" and "cap" in row["note"]


def test_import_refuses_non_sqlite_and_non_empty_store(client, tmp_path):
    assert client.post("/api/admin/import", headers=_auth(), content=b"hello").status_code == 400
    _new(client)
    st = Store.open(tmp_path / "radar.sqlite")
    st.save_run(Run(id="r1", radar_id=st.get_radar("va-test").id))
    src = Store.open(tmp_path / "other.sqlite")
    src.conn.close()
    assert client.post("/api/admin/import", headers=_auth(),
                       content=(tmp_path / "other.sqlite").read_bytes()).status_code == 409


def test_import_into_empty_store(client, tmp_path):
    src_dir = tmp_path / "src"
    src = Store.open(src_dir / "radar.sqlite")
    from lead_radar.models import Radar
    src.save_radar(Radar(id="r9", name="imported", query="q"))
    src.save_run(Run(id="run9", radar_id="r9"))
    src.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    src.conn.close()
    r = client.post("/api/admin/import", headers=_auth(), content=(src_dir / "radar.sqlite").read_bytes())
    assert r.status_code == 200, r.text
    assert r.json()["imported"]["runs"] == 1
    assert client.get("/api/data", headers=_auth()).json()["campaigns"][0]["name"] == "imported"


def test_draft_needs_an_offer(client):
    assert client.post("/api/campaigns/draft", headers=_auth(), json={"offer": "x"}).status_code == 400


def test_brief_round_trip_matches_expander_loader(tmp_path):
    path = tmp_path / "b.json"
    path.write_text(json.dumps(BRIEF))
    assert expander.load_brief_file(path, "r1", 1).terms


def test_short_name_cuts_at_word_boundary():
    assert server.short_name("a bookkeeper for my small business in New Zealand") == "a-bookkeeper-for-my-small"
    assert server.short_name("VA") == "va"


def test_job_boards_toggle_sets_platforms_and_needs_job_titles(client, tmp_path):
    no_titles = {**BRIEF, "terms": {k: v for k, v in BRIEF["terms"].items() if k not in ("onlinejobs", "indeed")}}
    r = client.post("/api/campaigns", headers=_auth(), json={
        "new": True, "name": "boards", "offer": "virtual assistant", "days": 3, "job_boards": True, "brief": no_titles})
    assert r.status_code == 400 and "job titles" in r.json()["error"]
    assert client.post("/api/campaigns", headers=_auth(), json={
        "new": True, "name": "boards", "offer": "virtual assistant", "days": 3, "job_boards": True,
        "brief": BRIEF}).status_code == 200
    st = Store.open(tmp_path / "radar.sqlite")
    assert {"onlinejobs", "weworkremotely", "freelancer", "indeed", "reddit"} <= set(st.get_radar("boards").platforms)
    got = client.get("/api/campaigns/boards", headers=_auth()).json()
    assert got["job_boards"] is True
    camp = client.get("/api/data", headers=_auth()).json()["campaigns"][0]
    assert camp["job_boards"] is True and camp["job_leads"] == 0
    # editing without the flag keeps it; turning it off drops the boards
    client.post("/api/campaigns", headers=_auth(), json={"name": "boards", "days": 3, "brief": BRIEF})
    assert "onlinejobs" in Store.open(tmp_path / "radar.sqlite").get_radar("boards").platforms
    client.post("/api/campaigns", headers=_auth(), json={"name": "boards", "days": 3, "brief": BRIEF, "job_boards": False})
    assert "onlinejobs" not in Store.open(tmp_path / "radar.sqlite").get_radar("boards").platforms


def test_worker_runs_the_campaign_platforms(client, tmp_path, monkeypatch):
    client.post("/api/campaigns", headers=_auth(), json={
        "new": True, "name": "boards", "offer": "virtual assistant", "days": 3, "job_boards": True, "brief": BRIEF,
        "run_now": True})
    seen = {}

    async def fake_exec(*cmd, **kw):
        seen["cmd"] = cmd
        raise RuntimeError("stop here")
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    st = server._store()
    job = st.conn.execute("SELECT * FROM jobs").fetchone()
    with pytest.raises(RuntimeError):
        asyncio.run(server.WORKER.execute(st, job))
    platforms = seen["cmd"][seen["cmd"].index("--platforms") + 1].split(",")
    assert "onlinejobs" in platforms and "reddit" in platforms


class _Proc:
    def __init__(self, out, rc=0):
        self.out, self.returncode = out, rc

    async def communicate(self):
        return self.out, b""


def test_enrich_job_records_counts_and_spend_only(client, tmp_path, monkeypatch):
    _new(client)
    st = server._store()
    radar = st.get_radar("va-test")
    server.enqueue(st, radar, "manual", 2, kind="enrich")
    summary = {"enriched": 2, "deliverable": 1, "accept_all": 1, "unresolved": 3, "deferred": 0, "cost_usd": 0.05,
               "resolve_credits": 6, "stopped": ""}

    async def fake_exec(*cmd, **kw):
        assert cmd[3] == "enrich" and "--credit-room" in cmd
        return _Proc(("note: x\nENRICH_SUMMARY " + json.dumps(summary)).encode())
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    job = st.conn.execute("SELECT * FROM jobs WHERE kind='enrich'").fetchone()
    asyncio.run(server.WORKER.execute(st, job))
    row = st.conn.execute("SELECT status, note, credits_used, cost_usd FROM jobs WHERE id=?", (job["id"],)).fetchone()
    assert row["status"] == "done" and row["credits_used"] == 6 and row["cost_usd"] == 0.05
    assert row["note"].startswith("2 cards · 1 verified · 1 accept-all · 3 unresolvable")
    assert server.credits_today(st) == 6  # resolution credits count toward the daily cap
    data = client.get("/api/data", headers=_auth()).json()
    assert data["enrich"]["today_usd"] == 0.05 and data["jobs"][0]["kind"] == "enrich"


def test_hunt_and_enrich_jobs_queue_independently(client):
    _new(client, run_now=True)
    st = server._store()
    radar = st.get_radar("va-test")
    first = server.enqueue(st, radar, "manual", 2, kind="enrich")
    assert first["already_queued"] is False
    assert server.enqueue(st, radar, "manual", 2, kind="enrich")["already_queued"] is True
    assert server.enqueue(st, radar, "manual", 2)["already_queued"] is True  # the hunt from run_now


def test_export_csv_streams_leads_with_contact_columns(client):
    _new(client)
    r = client.get("/api/export.csv", headers=_auth())
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["cache-control"] == "no-store"
    assert r.text.splitlines()[0] == ",".join(server.EXPORT_COLUMNS)
    assert client.get("/api/export.csv").status_code == 401
    assert client.get("/api/export.csv?campaign=nope", headers=_auth()).status_code == 404


def test_export_csv_escapes_formulas(client, tmp_path):
    from lead_radar.models import Classification, Post, post_id
    _new(client)
    st = Store.open(tmp_path / "radar.sqlite")
    radar = st.get_radar("va-test")
    url = "https://reddit.com/r/x/evil"
    st.save_run(Run(id="r9", radar_id=radar.id))
    st.upsert_post(Post(id=post_id("reddit", url), platform="reddit", url=url, kind="post",
                        text="=HYPERLINK(evil) need a VA", source_term="t", run_id="r9"))
    st.save_classification(Classification(post_id=post_id("reddit", url), radar_id=radar.id, role="buyer", tier="high",
                                          need_summary="+cmd", evidence_quote="=HYPERLINK(evil) need a VA",
                                          urgency="now", confidence=0.9, decided_by="reasoning", contract_version="t"))
    body = client.get("/api/export.csv", headers=_auth()).text
    assert "'=HYPERLINK(evil) need a VA" in body and "'+cmd" in body
    assert ",=HYPERLINK" not in body


# program lane (affiliate-program companies) ----------------------------------------------------------------

PROGRAM_ROW = {"domain": "kit.com", "name": "Kit", "status": "verified", "platforms": ["partnerstack"],
               "program_url": "https://kit.com/affiliate", "commission_text": "Earn 50% recurring commission",
               "evidence": [{"platform": "partnerstack", "kind": "portal", "match": "https://kit.partnerstack.com/",
                             "slug": "kit", "page": "https://kit.com/affiliate"}],
               "segment": "saas", "tags": ["Marketing"], "partner_count": 30, "offer": None,
               "sources": [{"source": "partnerstack-marketplace", "ref": "kit"}], "score": 80}


def test_programs_endpoints_need_the_password(client):
    for path in ("/api/programs", "/api/programs.csv"):
        assert client.get(path).status_code == 401
    assert client.post("/api/programs/run", json={}).status_code == 401
    assert client.post("/api/programs/import", json={"companies": [PROGRAM_ROW]}).status_code == 401


def test_programs_import_list_and_csv(client):
    bad = {"domain": "x.com", "status": "nonsense"}
    r = client.post("/api/programs/import", headers=_auth(), json={"companies": [PROGRAM_ROW, bad, "junk"]})
    assert r.json() == {"imported": 1, "signals": 0}
    data = client.get("/api/programs", headers=_auth()).json()
    assert [c["domain"] for c in data["companies"]] == ["kit.com"]
    assert data["companies"][0]["found_via"] == ["partnerstack-marketplace"]
    assert data["summary"]["by_platform"] == {"partnerstack": 1}
    csv_text = client.get("/api/programs.csv", headers=_auth()).text
    assert csv_text.splitlines()[0].startswith("company,domain,website,category")
    assert "kit.com" in csv_text
    assert client.post("/api/programs/import", headers=_auth(), json={"nope": 1}).status_code == 400


def test_programs_run_queues_once(client):
    first = client.post("/api/programs/run", headers=_auth(), json={"max_searches": 50}).json()["run"]
    again = client.post("/api/programs/run", headers=_auth(), json={}).json()["run"]
    assert not first["already_queued"] and again == {"id": first["id"], "already_queued": True}
    assert client.get("/api/programs", headers=_auth()).json()["runs"][0]["max_searches"] == 50


def test_program_run_skips_at_the_credit_cap(client, monkeypatch):
    monkeypatch.setenv("RADAR_DAILY_CREDIT_CAP", "0")
    client.post("/api/programs/run", headers=_auth(), json={})
    st = server._store()
    asyncio.run(server.WORKER.execute_programs(st, st.conn.execute("SELECT * FROM program_runs").fetchone()))
    row = st.conn.execute("SELECT status, note FROM program_runs").fetchone()
    assert row["status"] == "skipped" and "cap" in row["note"]


def test_program_run_seeds_then_verifies_and_meters_credits(client, monkeypatch):
    calls = []

    class Proc:
        def __init__(self, lines):
            self.lines = [l.encode() for l in lines]
            self.stdout = self

        async def readline(self):
            return self.lines.pop(0) if self.lines else b""

        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kw):
        calls.append(cmd[cmd.index("programs") + 1])
        if "seed" in cmd:
            return Proc(["note: partnerstack marketplace: 3 programs\n",
                         "new companies 3, already known 0, portals not resolved 0, searches 12, credits 12\n"])
        return Proc(["checked 3: verified 2, no program 1, unreachable 0\n"])
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    client.post("/api/programs/run", headers=_auth(), json={"max_searches": 20})
    st = server._store()
    asyncio.run(server.WORKER.execute_programs(st, st.conn.execute("SELECT * FROM program_runs").fetchone()))
    row = st.conn.execute("SELECT * FROM program_runs").fetchone()
    assert calls == ["seed", "verify"]
    assert row["status"] == "done" and row["credits_used"] == 12 and row["note"].startswith("checked 3")
    assert server.credits_today(st) == 12


def test_daily_trigger_queues_one_growth_run_with_the_daily_credit_budget(client, monkeypatch):
    monkeypatch.setenv("RADAR_PROGRAMS_DAILY", "on")
    monkeypatch.setenv("RADAR_PROGRAMS_DAILY_CREDITS", "90")
    client.post("/api/run-daily", headers=_auth())
    client.post("/api/run-daily", headers=_auth())  # second trigger the same day: still one run
    runs = client.get("/api/programs", headers=_auth()).json()["runs"]
    assert [(r["kind"], r["trigger"], r["max_searches"]) for r in runs] == [("growth", "daily", 90)]
    monkeypatch.setenv("RADAR_PROGRAMS_DAILY", "off")
    server._store().conn.execute("UPDATE program_runs SET status='done'")
    assert client.post("/api/run-daily", headers=_auth()).json()["queued"] == []


def test_programs_run_rejects_unknown_kind(client):
    assert client.post("/api/programs/run", headers=_auth(), json={"kind": "nope"}).status_code == 400


def test_growth_run_calls_growth_once_and_meters_credits(client, monkeypatch):
    calls = []

    class Proc:
        def __init__(self, lines):
            self.lines = [l.encode() for l in lines]
            self.stdout = self

        async def readline(self):
            return self.lines.pop(0) if self.lines else b""

        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kw):
        calls.append(list(cmd[cmd.index("programs") + 1:]))
        return Proc(["  jobs: 4 postings\n", 'new signals {"job": 3}; excluded {}\n', "GROWTH_SUMMARY credits 37\n"])
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    client.post("/api/programs/run", headers=_auth(), json={"kind": "growth", "max_searches": 60})
    st = server._store()
    asyncio.run(server.WORKER.execute_programs(st, st.conn.execute("SELECT * FROM program_runs").fetchone()))
    row = st.conn.execute("SELECT * FROM program_runs").fetchone()
    assert len(calls) == 1 and calls[0][0] == "growth"
    assert calls[0][calls[0].index("--days") + 1] in (str(server.PROGRAM_DAILY_DAYS), str(server.PROGRAM_WEEKLY_DAYS))
    assert calls[0][calls[0].index("--budget") + 1] == "60"
    assert row["status"] == "done" and row["credits_used"] == 37 and row["note"].startswith("new signals")
    assert server.credits_today(st) == 37


def _seed_lists(st):
    from lead_radar.programs import signals as sg
    today = datetime.now(timezone.utc).date().isoformat()
    st.add_program_seed("acmeco.com", source="serp", ref="x", name="AcmeCo")
    st.save_program_check("acmeco.com", status="verified", platforms=["rewardful"], program_url="https://acmeco.com/aff",
                          commission_text="Earn 30%", evidence=[], segment="saas", name="AcmeCo", score=60, error=None)
    st.save_company_profile("acmeco.com", lst="affiliate", source="signal:job", ref="x", name="AcmeCo",
                            linkedin_url="https://www.linkedin.com/company/acmeco", country="US", hq="Austin, Texas",
                            size="51-200 employees", industry="Software Development", excluded=None)
    st.add_program_signal("acmeco.com", sg.Signal("job", "https://jobs.lever.co/acmeco/1", "Affiliate Manager", today))
    st.save_company_profile("fundco.example", lst="finance", source="finance", ref="x", name="FundCo",
                            linkedin_url="https://www.linkedin.com/company/fundco", country="US", hq="Miami, Florida",
                            size="11-50 employees", industry="Financial Services", excluded=None)
    st.save_company_profile("offshore.example", lst="finance", source="finance", ref="x", name="Offshore",
                            linkedin_url=None, country="CY", hq=None, size=None, industry="Financial Services",
                            excluded="not US (CY)")


def test_leads_api_returns_each_ranked_list_for_agents(client):
    _seed_lists(server._store())
    assert client.get("/api/programs/leads").status_code == 401
    aff = client.get("/api/programs/leads?list=affiliate", headers=_auth()).json()
    assert aff["total"] == 1 and aff["leads"][0]["domain"] == "acmeco.com"
    lead = aff["leads"][0]
    assert lead["score"] > 0 and lead["why"].startswith("hiring") and lead["signals"][0]["kind"] == "job"
    fin = client.get("/api/programs/leads?list=finance", headers=_auth()).json()
    assert [r["domain"] for r in fin["leads"]] == ["fundco.example"]  # the excluded one never shows
    assert client.get("/api/programs/leads?list=affiliate&since=2999-01-01", headers=_auth()).json()["total"] == 0
    for bad in ("list=x", "since=yesterday", "limit=ten"):
        assert client.get("/api/programs/leads?" + bad, headers=_auth()).status_code == 400
    page = client.get("/api/programs", headers=_auth()).json()
    assert page["lists"]["affiliate"]["new_today"] == 1 and page["lists"]["finance"]["total"] == 1
    assert page["daily"]["on"] is False  # the program lane's daily run is off by default
    csv_text = client.get("/api/programs/leads.csv?list=finance", headers=_auth()).text
    assert csv_text.startswith("rank,") and "FundCo" in csv_text and "Offshore" not in csv_text


def test_import_carries_lists_and_signals(client):
    src = Store.open(":memory:")
    _seed_lists(src)
    body = {"companies": src.program_companies(),
            "signals": [s for g in src.program_signals().values() for s in g] + [{"id": "x", "domain": "ghost.example",
                        "kind": "job", "url": "https://x", "first_seen": "2026-01-01"}]}
    r = client.post("/api/programs/import", headers=_auth(), json=body).json()
    assert r == {"imported": 3, "signals": 1}  # the orphan signal is skipped
    st = server._store()
    assert st.program_company("fundco.example")["list"] == "finance"
    assert st.program_company("offshore.example")["excluded"] == "not US (CY)"
    again = client.post("/api/programs/import", headers=_auth(), json=body).json()
    assert again["signals"] == 0


def test_live_page_has_the_programs_tab(client):
    page = client.get("/", headers=_auth()).text
    assert 'id="view-programs"' in page and 'id="tab-programs"' in page
    assert 'id="listPick"' in page and 'id="progSignals"' in page


def test_campaign_platform_restrictions_survive_editor_save(client, tmp_path):
    platforms = ['instagram', 'tiktok', 'facebook', 'google']
    r = client.post('/api/campaigns', headers=_auth(), json={
        'new': True, 'name': 'homeschool', 'offer': 'homeschool help', 'days': 3,
        'daily': False, 'job_boards': False, 'platforms': platforms, 'brief': BRIEF})
    assert r.status_code == 200
    got = client.get('/api/campaigns/homeschool', headers=_auth()).json()
    assert got['platforms'] == platforms
    got.update(new=False, name='homeschool', run_now=False)
    assert client.post('/api/campaigns', headers=_auth(), json=got).status_code == 200
    assert Store.open(tmp_path / 'radar.sqlite').get_radar('homeschool').platforms == platforms
    got['platforms'] = ['invalid']
    assert client.post('/api/campaigns', headers=_auth(), json=got).status_code == 400


def test_activity_includes_job_leads_without_changing_social_count(client, tmp_path):
    _new(client, run_now=True)
    st = Store.open(tmp_path / 'radar.sqlite')
    radar = st.get_radar('va-test')
    st.save_run(Run(id='job-run', radar_id=radar.id, counts={'buyers': 0, 'listing_leads': 170}))
    st.conn.execute("UPDATE jobs SET run_id='job-run', status='done'")
    job = client.get('/api/jobs', headers=_auth()).json()['jobs'][0]
    assert job['leads'] == 0 and job['job_leads'] == 170


def test_program_summary_counts_the_affiliate_list_only(client):
    _seed_lists(server._store())
    st = client.get("/api/programs", headers=_auth()).json()["summary"]["by_status"]
    assert st == {"verified": 1}  # finance rows are never site-checked, so not "still to check"


# --- built-in daily clock ----------------------------------------------------------------------------------------

def test_daily_at_reads_hh_mm_in_utc_and_off(monkeypatch):
    monkeypatch.delenv("RADAR_DAILY_AT", raising=False)
    assert server.daily_at() == (11, 0)
    for raw, want in (("7:30", (7, 30)), ("23:59", (23, 59)), ("off", None), ("25:00", None), ("noon", None)):
        monkeypatch.setenv("RADAR_DAILY_AT", raw)
        assert server.daily_at() == want


def test_daily_clock_queues_once_a_day_after_its_time(client, monkeypatch):
    monkeypatch.setenv("RADAR_DAILY_AT", "11:00")
    _new(client, "daily-one", daily=True)
    _new(client, "manual-one", daily=False)
    morning = datetime(2026, 10, 15, 10, 59, tzinfo=timezone.utc)
    assert server.DAILY.tick(morning) is None                      # not time yet
    queued = server.DAILY.tick(morning.replace(hour=11, minute=0))
    assert [q["campaign"] for q in queued] == ["daily-one"]
    assert server.DAILY.tick(morning.replace(hour=18)) is None     # once a day
    st = server._store()
    st.conn.execute("UPDATE jobs SET status='done'")
    queued = server.DAILY.tick(datetime(2026, 10, 16, 11, 5, tzinfo=timezone.utc))
    assert [q["campaign"] for q in queued] == ["daily-one"]         # next day, again
    monkeypatch.setenv("RADAR_DAILY_AT", "off")
    assert server.DAILY.tick(datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)) is None


def test_a_manual_daily_trigger_counts_for_the_clock(client, monkeypatch):
    monkeypatch.setenv("RADAR_DAILY_AT", "00:00")
    _new(client, "daily-one", daily=True)
    client.post("/api/run-daily", headers=_auth())
    assert server.DAILY.tick(datetime.now(timezone.utc)) is None


def test_a_new_install_starts_with_the_example_campaigns_not_daily(client, monkeypatch):
    st = server._store()
    added = server.seed_examples(st)
    names = {e.stem for e in server.EXAMPLES_DIR.glob("*.json")}
    assert set(added) == names and len(added) >= 5
    page = client.get("/api/data", headers=_auth()).json()
    assert {c["name"] for c in page["campaigns"]} == names
    assert not any(c["daily"] for c in page["campaigns"])           # nothing runs or spends by itself
    va = client.get("/api/campaigns/virtual-assistant", headers=_auth()).json()
    assert va["job_boards"] and va["brief"]["need_statements"]
    assert server.seed_examples(st) == []                             # once per state folder


def test_examples_never_come_back_or_land_in_an_existing_install(client, monkeypatch, tmp_path):
    _new(client, "mine", daily=False)
    assert server.seed_examples(server._store()) == []                # had campaigns: left alone
    assert [c["name"] for c in client.get("/api/data", headers=_auth()).json()["campaigns"]] == ["mine"]
    other = tmp_path / "other"
    monkeypatch.setenv("RADAR_STATE_DIR", str(other))
    monkeypatch.setenv("RADAR_EXAMPLES", "off")
    assert server.seed_examples(server._store()) == []


def test_every_example_campaign_is_a_valid_search_plan():
    from lead_radar import contract
    for path in server.EXAMPLES_DIR.glob("*.json"):
        ex = json.loads(path.read_text())
        contract.validate_intent_brief(ex["brief"])
        assert ex["daily"] is False and ex["name"] == path.stem
        if ex["job_boards"]:
            assert ex["brief"]["terms"].get("onlinejobs")
