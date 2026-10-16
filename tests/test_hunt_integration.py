"""M2: full hunt over recorded fixtures, twice. The second identical run must queue zero already-seen posts."""
import asyncio
from pathlib import Path

from lead_radar import config
from lead_radar.db import Store
from lead_radar.hunt import hunt
from lead_radar.scrapecreators import ScrapeCreators

from sc_mock import FixtureTransport

ROOT = Path(__file__).parents[1]
BRIEF = ROOT / "config" / "briefs" / "virtual-assistant.json"


def _settings(tmp_path):
    return config.load(config_path=ROOT / "config" / "radar.yaml", env_file=tmp_path / "none",
                       environ={"SCRAPECREATORS_API_KEY": "k", "RADAR_STATE_DIR": str(tmp_path)})


def _hunt(settings, store, **kw):
    async def go():
        async with ScrapeCreators("k", transport=FixtureTransport(), backoff=0) as sc:
            return await hunt(settings, store, "virtual assistant", platforms="api", days=14,
                              brief_path=BRIEF, sc=sc, classify=False, **kw)
    return asyncio.run(go())


def test_second_identical_run_sends_nothing_to_classification(tmp_path):
    s = _settings(tmp_path)
    store = Store.open(s.db_path)
    first = _hunt(s, store)
    assert set(first.run.per_source) == {"reddit", "youtube", "tiktok", "instagram", "threads", "linkedin",
                                         "facebook", "google"}
    assert all(v["posts"] > 0 for v in first.run.per_source.values()), first.run.per_source
    assert first.run.counts["queued"] == first.run.counts["to_classify"] > 0
    assert first.run.credits_used == sum(v["credits"] for v in first.run.per_source.values())

    second = _hunt(s, store)
    assert second.radar.id == first.radar.id and second.brief.id == first.brief.id  # brief cached, not re-versioned
    assert second.run.counts["unique"] > 0
    assert second.run.counts["to_classify"] == 0 and second.run.counts["queued"] == 0
    assert store.get_run(second.run.id).counts["already_seen"] == second.run.counts["unique"]


def test_dry_run_writes_nothing(tmp_path):
    s = _settings(tmp_path)
    store = Store.open(s.db_path)
    res = _hunt(s, store, dry_run=True)
    assert res.run.counts["to_classify"] > 0 and "queued" not in res.run.counts
    for table in ("radars", "runs", "posts", "classification_queue"):
        assert store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_browser_platforms_reported_not_silent(tmp_path):
    s = _settings(tmp_path)
    store = Store.open(s.db_path)

    async def go():
        async with ScrapeCreators("k", transport=FixtureTransport(), backoff=0) as sc:
            return await hunt(s, store, "virtual assistant", platforms="threads,skool,x", brief_path=BRIEF, sc=sc,
                              classify=False)
    res = asyncio.run(go())
    assert res.run.per_source["skool"]["errors"] == 1 and res.run.per_source["x"]["errors"] == 1
    assert any("skool" in e for e in res.run.errors)


def test_402_stops_the_whole_run_and_is_reported(tmp_path):
    import httpx
    s = _settings(tmp_path)
    store = Store.open(s.db_path)
    t = FixtureTransport(overrides={"/v1/threads/search": httpx.Response(402, json={"success": False})})

    async def go():
        async with ScrapeCreators("k", transport=t, backoff=0) as sc:
            res = await hunt(s, store, "virtual assistant", platforms="api", brief_path=BRIEF, sc=sc, classify=False)
            return res, sc
    res, sc = asyncio.run(go())
    assert sc.exhausted
    first_402 = next(i for i, r in enumerate(t.calls) if r.url.path == "/v1/threads/search")
    # Adapters already in flight may finish the request they had open (concurrency cap 10), but nothing new starts.
    assert len(t.calls) - first_402 - 1 <= 10
    assert any("threads" in e and "run stopped" in e for e in res.run.errors)
    assert store.get_run(res.run.id) is not None  # the failed run is still recorded, never silent


def test_report_shows_cost_split_and_leads_by_post_platform(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from lead_radar import cli
    from lead_radar.db import Store
    from lead_radar.models import Classification, Post, Radar, Run, post_id
    monkeypatch.setenv("RADAR_STATE_DIR", str(tmp_path))
    store = Store.open(tmp_path / "radar.sqlite")
    store.save_radar(Radar(id="r1", name="va", query="q"))
    run = Run(id="run1", radar_id="r1", credits_used=10, cost_usd=0.0288,
              per_source={"google": {"requests": 1, "credits": 1, "posts": 2, "errors": 0, "skipped": 0,
                                     "notes": []},
                          "facebook": {"requests": 0, "credits": 0, "posts": 0, "errors": 0, "skipped": 0,
                                       "notes": ["not searched: no facebook groups configured"]}})
    store.save_run(run)
    for i, (plat, role, tier) in enumerate([("reddit", "buyer", "high"), ("facebook", "seller", "none")]):
        url = f"https://x/{i}"
        store.upsert_post(Post(id=post_id(plat, url), platform=plat, url=url, kind="post", text="need a VA now please",
                               source_term="t", run_id="run1"))
        store.save_classification(Classification(post_id=post_id(plat, url), radar_id="r1", role=role, tier=tier,
                                                 need_summary="", evidence_quote="need a VA" if tier != "none" else "",
                                                 urgency="unknown", confidence=0.9, decided_by="jev",
                                                 contract_version="t"))
    out = CliRunner().invoke(cli.app, ["report", "run1"]).output
    assert "note: facebook: not searched" in out
    assert "ScrapeCreators 10 credits $0.0188 + models $0.0100" in out
    assert "reddit" in out and "leads by the post's own platform" in out


def test_doctor_offline_reports_missing_required_key_without_values(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from lead_radar import cli, config as cfg
    real = cfg.load

    def fake_load(*a, **k):
        return real(env_file=tmp_path / "none",
                    environ={"RADAR_STATE_DIR": str(tmp_path), "OPENROUTER_API_KEY": "secret-value-xyz"})
    monkeypatch.setattr(cli.config, "load", fake_load)
    monkeypatch.setattr("shutil.which", lambda _: None)
    res = CliRunner().invoke(cli.app, ["doctor", "--offline"])
    assert res.exit_code == 1
    assert "FAIL  SCRAPECREATORS_API_KEY" in res.output and "ok    OPENROUTER_API_KEY" in res.output
    assert "warn  TYPESAFE_API_KEY" in res.output and "secret-value-xyz" not in res.output


def test_leads_command_lists_buyers_by_tier(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from lead_radar import cli
    from lead_radar.db import Store
    from lead_radar.models import Classification, Post, Radar, Run, post_id
    monkeypatch.setenv("RADAR_STATE_DIR", str(tmp_path))
    store = Store.open(tmp_path / "radar.sqlite")
    store.save_radar(Radar(id="r1", name="va", query="q"))
    store.save_run(Run(id="run1", radar_id="r1"))
    for i, (role, tier) in enumerate([("buyer", "medium"), ("buyer", "high"), ("seller", "none")]):
        url = f"https://x/{i}"
        store.upsert_post(Post(id=post_id("reddit", url), platform="reddit", url=url, kind="post",
                               text="need a VA now please", source_term="t", run_id="run1", author_handle=f"a{i}"))
        store.save_classification(Classification(post_id=post_id("reddit", url), radar_id="r1", role=role, tier=tier,
                                                 need_summary="wants a VA" if tier != "none" else "",
                                                 evidence_quote="need a VA" if tier != "none" else "",
                                                 urgency="unknown", confidence=0.9, decided_by="jev",
                                                 contract_version="t"))
    out = CliRunner().invoke(cli.app, ["leads", "run1"]).output
    assert out.startswith("2 leads") and out.index("[HIGH]") < out.index("[MEDIUM]") and "@a2" not in out
