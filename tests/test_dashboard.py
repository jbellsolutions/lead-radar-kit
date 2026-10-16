"""Dashboard: data comes from the store, sellers are counted not listed, post text can't break out of the page."""
import json
import re

from typer.testing import CliRunner

from lead_radar import cli
from lead_radar.dashboard import build_data, render
from lead_radar.db import Store
from lead_radar.models import Classification, Post, Radar, Run, post_id


class _Settings:
    def get(self, key, default=None):
        return {"scrapecreators.credit_usd": 0.002}.get(key, default)


def _store(tmp_path):
    store = Store.open(tmp_path / "radar.sqlite")
    store.save_radar(Radar(id="r1", name="va", query="virtual assistant"))
    store.save_run(Run(id="run1", radar_id="r1", credits_used=100, cost_usd=0.25))
    rows = [("buyer", "medium", "2026-09-29T10:00:00+00:00"), ("buyer", "high", "2026-09-28T10:00:00+00:00"),
            ("seller", "none", None), ("buyer", "high", "2026-09-30T10:00:00+00:00")]
    for i, (role, tier, posted) in enumerate(rows):
        url = f"https://reddit.com/r/x/{i}"
        text = "need a VA now </script><script>alert(1)</script>" if i == 0 else "need a VA now"
        store.upsert_post(Post(id=post_id("reddit", url), platform="reddit", url=url, kind="post", text=text,
                               source_term="t", run_id="run1", author_handle=f"a{i}", posted_at=posted))
        store.save_classification(Classification(post_id=post_id("reddit", url), radar_id="r1", role=role, tier=tier,
                                                 need_summary="wants a VA" if tier != "none" else "",
                                                 evidence_quote="need a VA" if tier != "none" else "",
                                                 urgency="unknown", confidence=0.9, decided_by="jev",
                                                 contract_version="t"))
    return store


def test_build_data_lists_buyers_by_tier_then_newest_and_counts_sellers(tmp_path):
    data = build_data(_store(tmp_path), _Settings())
    assert [(l["tier"], l["author"]) for l in data["leads"]] == [("high", "a3"), ("high", "a1"), ("medium", "a0")]
    run = data["runs"][0]
    assert (run["high"], run["medium"], run["low"], run["sellers"]) == (2, 1, 0, 1)
    assert run["credits_usd"] == 0.2 and run["models_usd"] == 0.05
    assert data["caveats"]


def test_build_data_unknown_radar_raises(tmp_path):
    try:
        build_data(_store(tmp_path), _Settings(), radar="nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_render_embeds_data_safely(tmp_path):
    page = render(build_data(_store(tmp_path), _Settings()), standalone=False)
    assert not page.lstrip().lower().startswith("<!doctype") and page.startswith("<title>Lead Radar</title>")
    blob = re.search(r'<script id="radar-data" type="application/json">(.*?)</script>', page, re.S).group(1)
    assert "</script>" not in blob and len(json.loads(blob)["leads"]) == 3
    assert render({"radars": [], "runs": [], "leads": [], "caveats": []}).lower().startswith("<!doctype html>")


def test_dashboard_command_writes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_STATE_DIR", str(tmp_path))
    _store(tmp_path)
    out = CliRunner().invoke(cli.app, ["dashboard"]).output
    assert "leads=3" in out and (tmp_path / "dashboard.html").exists()


def test_build_data_lists_job_listings_leads_first(tmp_path):
    store = _store(tmp_path)
    rows = [("direct", "medium", "Example Widgets Co"), ("staffing", "medium", ""), ("direct", "high", ""),
            ("unknown", "none", "")]
    for i, (employer, fit, company) in enumerate(rows):
        url = f"https://www.onlinejobs.ph/jobseekers/job/x-{i}"
        pid = post_id("onlinejobs", url)
        store.upsert_post(Post(id=pid, platform="onlinejobs", url=url, kind="job", text="Hiring a virtual assistant",
                               title="Virtual Assistant", source_term="va", run_id="run1",
                               raw={"pay": "$5/hour"}, posted_at="2026-10-01T10:00:00+00:00"))
        store.save_listing_verdict({"post_id": pid, "radar_id": "r1", "run_id": "run1", "employer_type": employer,
                                    "fit": fit, "role_title": "VA", "company_name": company or None, "need": "inbox help",
                                    "evidence_quote": "Hiring a virtual assistant" if fit != "none" else "",
                                    "confidence": 0.8, "decided_by": "reasoning", "contract_version": "t"})
    got = [(l["employer"], l["fit"], l["lead"]) for l in build_data(store, _Settings())["listings"]]
    assert got == [("direct", "high", True), ("direct", "medium", True), ("staffing", "medium", False)]
    page = render(build_data(store, _Settings()), standalone=False)
    assert 'id="view-jobs"' in page and "Example Widgets Co" in page
