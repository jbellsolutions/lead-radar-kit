"""Resolution, enrichment (fake Data Box) and the contact-data rules. All people, emails and phones are synthetic."""
import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytest

from lead_radar import resolve as rs
from lead_radar.dashboard import build_data
from lead_radar.databox import DataBoxError
from lead_radar.db import Store
from lead_radar.enrich import Limits, badge, card_fields, run_enrich, targets
from lead_radar.models import Classification, Post, Radar, Run, post_id
from lead_radar.resolve import text as tx
from lead_radar.scrapecreators import Meter

NOW = datetime.now(timezone.utc)
EMAIL, PHONE = "pat.example@examplewidgets.test", "+1 555 010 0199"


def run(coro):
    return asyncio.run(coro)


class FakeSC:
    """ScrapeCreators stand-in: path -> synthetic body, 1 credit per call."""

    def __init__(self, bodies):
        self.bodies, self.meter, self.calls = bodies, Meter(), []

    async def get(self, path, params=None, *, source="x"):
        self.calls.append((path, params))
        self.meter.requests[source] += 1
        self.meter.credits[source] += 1
        return self.bodies.get(path) or {}


def _post(platform="instagram", handle="patbuilds", text="Need a VA for my agency asap", kind="post", **kw):
    url = kw.pop("url", f"https://example.test/{platform}/{handle}/{abs(hash(text)) % 10**8}")
    return Post(id=post_id(platform, url), platform=platform, url=url, kind=kind, text=text, author_handle=handle,
                source_term="t", run_id="run1", posted_at=kw.pop("posted_at", NOW), **kw)


# --- text clues ---------------------------------------------------------------

def test_text_clues_are_conservative():
    c = tx.clues(f"I run Example Widgets Co. Email {EMAIL} or call {PHONE}. https://examplewidgets.test/about "
                 "https://www.linkedin.com/in/pat-example https://linktr.ee/patbuilds https://instagram.com/patbuilds")
    assert c["emails"] == [EMAIL] and c["phones"] == [PHONE]
    assert c["websites"] == ["https://examplewidgets.test/about"]
    assert c["linkedin"] == ["https://www.linkedin.com/in/pat-example"] and c["link_in_bio"] == ["https://linktr.ee/patbuilds"]
    assert c["companies"] == ["Example Widgets Co"]
    assert tx.domain_of("someone@gmail.com") is None and tx.domain_of(EMAIL) == "examplewidgets.test"
    assert tx.phones("order #12345678 on 2026-10-04") == []


def test_subject_keys():
    assert rs.subject_key(_post(handle="@PatBuilds")) == "instagram:patbuilds"
    job = _post("onlinejobs", "employer:123", kind="job")
    assert rs.subject_key(job) == "onlinejobs:employer:123"
    assert rs.subject_key(_post("indeed", "indeed:co:example-widgets-co", kind="job")) == "indeed:co:example-widgets-co"
    assert rs.subject_key(_post("freelancer", None, kind="job")) is None


# --- resolution -------------------------------------------------------------------

IG = {"data": {"user": {"username": "patbuilds", "full_name": "Pat Example", "biography": "Founder of Example Widgets Co",
                        "bio_links": [{"url": "https://linktr.ee/patbuilds"}], "external_url": "https://linktr.ee/patbuilds"}}}
LINKTREE = {"links": [{"url": "https://examplewidgets.test"}, {"url": "https://www.linkedin.com/in/pat-example"}],
            "email_address": EMAIL}


def test_resolve_profile_and_link_in_bio_within_credit_cap():
    sc = FakeSC({"/v1/instagram/profile": IG, "/v1/linktree": LINKTREE})
    r = run(rs.resolve(sc, [_post()], max_credits=4))
    assert r.status == "ready" and r.credits == 2
    a = r.args
    assert (a["name"], a["first_name"], a["last_name"]) == ("Pat Example", "Pat", "Example")
    assert a["domain"] == "examplewidgets.test" and a["linkedin_url"] == "https://www.linkedin.com/in/pat-example"
    assert a["emails_seen"] == [EMAIL] and a["company"] == "Example Widgets Co"
    assert r.sources["name"] == "instagram profile" and r.sources["emails_seen"] == "link-in-bio page"
    assert EMAIL not in json.dumps(r.sources)  # provenance names places, never values


def test_reddit_author_without_clues_is_unresolved_for_free():
    sc = FakeSC({})
    r = run(rs.resolve(sc, [_post("reddit", "throwaway_1", "anyone know a good VA service?")]))
    assert r.status == "unresolved" and r.reason == "no name or company" and sc.calls == []


def test_listing_resolves_from_the_board_company():
    p = _post("indeed", "indeed:co:example-widgets-co", "Example Widgets Co: Executive Assistant", kind="job",
              raw={"company": "Example Widgets Co", "company_website": "https://examplewidgets.test"})
    r = run(rs.resolve(FakeSC({}), [p], verdict={"company_name": "Example Widgets Co", "role_title": "EA"}))
    assert r.status == "ready" and r.args["domain"] == "examplewidgets.test" and r.args["hints"]["role_title"] == "EA"
    anon = _post("onlinejobs", "employer:9", "Virtual assistant needed for inbox work", kind="job")
    assert run(rs.resolve(FakeSC({}), [anon], verdict={"company_name": None})).status == "unresolved"


# --- enrichment pass ------------------------------------------------------------------

def _store(tmp_path, n=4):
    st = Store.open(tmp_path / "radar.sqlite")
    st.save_radar(Radar(id="r1", name="va", query="va"))
    st.save_run(Run(id="run1", radar_id="r1"))
    for i in range(n):
        p = _post(handle=f"person{i}", text=f"Need a VA for my agency, post {i}", posted_at=NOW - timedelta(hours=i))
        st.upsert_post(p)
        st.save_classification(Classification(post_id=p.id, radar_id="r1", role="buyer",
                                              tier="high" if i == 2 else "medium", need_summary="VA",
                                              evidence_quote="Need a VA", urgency="now", confidence=0.9,
                                              decided_by="reasoning", contract_version="t"))
    return st


CARD = {"card": {"name": {"value": "Pat Example", "source": "input", "confidence": 0.9},
                 "email": {"value": EMAIL, "source": "site", "confidence": 0.9, "verdict": "deliverable"},
                 "phone": {"value": PHONE, "source": "moltsets", "confidence": 0.7},
                 "linkedin_url": {"value": "https://www.linkedin.com/in/pat-example", "source": "input", "confidence": 1},
                 "alt_emails": [], "socials": {}},
        "verdict": "deliverable", "job_id": "dbj1", "status": "succeeded", "cost_usd": 0.02}


class FakeBox:
    def __init__(self, body=CARD, error=None):
        self.calls, self.body, self.error = [], body, error

    async def enrich(self, **args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.body


def test_enrich_high_first_caps_per_run_and_stores_cards(tmp_path):
    st = _store(tmp_path)
    assert [t.subject for t in targets(st, "r1")][0] == "instagram:person2"  # High first
    sc = FakeSC({"/v1/instagram/profile": IG, "/v1/linktree": LINKTREE})
    box = FakeBox()
    s = run(run_enrich(st, "r1", sc=sc, box=box, limits=Limits(max_subjects=2, daily_usd=1, per_lead_usd=0.25)))
    assert (s["enriched"], s["deliverable"], s["deferred"], s["cost_usd"]) == (2, 2, 2, 0.04)
    assert box.calls[0]["want"] == ["email", "linkedin", "socials", "phone"]  # phone only for High
    assert box.calls[1]["want"] == ["email", "linkedin", "socials"]
    assert box.calls[0]["max_cost_usd"] == 0.25 and box.calls[0]["request_id"].startswith("lr-")
    c = st.get_contact("instagram:person2")
    assert c["status"] == "enriched" and c["email"] == EMAIL and c["email_verdict"] == "deliverable"
    assert c["phone"] == PHONE and c["attempts"] == 1 and c["cost_usd"] == 0.02 and badge(c) == "verified"
    assert st.get_contact("instagram:person3")["reason"] == "max_per_run"
    # second pass: fresh cards are skipped, deferred ones get their turn
    s2 = run(run_enrich(st, "r1", sc=sc, box=box, limits=Limits(max_subjects=5, daily_usd=1)))
    assert s2["skipped_fresh"] == 2 and s2["enriched"] == 2


def test_budget_refusal_defers_the_rest_and_bad_token_fails_loudly(tmp_path):
    st = _store(tmp_path)
    sc = FakeSC({"/v1/instagram/profile": IG})
    s = run(run_enrich(st, "r1", sc=sc, box=FakeBox(error=DataBoxError("refused", "budget_exhausted")),
                       limits=Limits(max_subjects=5, daily_usd=1)))
    assert s["deferred"] == 4 and s["stopped"] == "databox_budget" and s["enriched"] == 0
    with pytest.raises(DataBoxError):
        run(run_enrich(_store(tmp_path / "b"), "r1", sc=sc, box=FakeBox(error=DataBoxError("401", "unauthorized")),
                       limits=Limits(max_subjects=5, daily_usd=1)))


def test_without_data_box_resolvable_subjects_wait(tmp_path):
    st = _store(tmp_path, n=1)
    s = run(run_enrich(st, "r1", sc=FakeSC({"/v1/instagram/profile": IG}), box=None, limits=Limits(daily_usd=1)))
    assert s["deferred"] == 1 and st.get_contact("instagram:person0")["reason"] == "data_box_not_configured"


def test_verify_quota_keeps_the_job_to_resume(tmp_path):
    st = _store(tmp_path, n=1)
    body = {**CARD, "card": {**CARD["card"], "email": None}, "verdict": "verify_quota"}
    sc = FakeSC({"/v1/instagram/profile": IG})
    run(run_enrich(st, "r1", sc=sc, box=FakeBox(body), limits=Limits(daily_usd=1)))
    c = st.get_contact("instagram:person0")
    assert c["status"] == "deferred" and c["reason"] == "verify_quota" and c["databox_job_id"] == "dbj1"
    box = FakeBox()
    run(run_enrich(st, "r1", sc=sc, box=box, limits=Limits(daily_usd=1)))
    assert box.calls[0]["resume_from"] == "dbj1"


def test_card_fields_flatten():
    f = card_fields(CARD)
    assert f["email"] == EMAIL and f["email_verdict"] == "deliverable" and f["name"] == "Pat Example"


def test_contact_data_not_logged(tmp_path, caplog, capsys):
    """Definition of done: emails, phones and names from cards never reach logs, run errors or summaries."""
    caplog.set_level(logging.DEBUG)
    st = _store(tmp_path)
    s = run(run_enrich(st, "r1", sc=FakeSC({"/v1/instagram/profile": IG, "/v1/linktree": LINKTREE}), box=FakeBox(),
                       limits=Limits(max_subjects=4, daily_usd=1)))
    out = json.dumps(s) + caplog.text + capsys.readouterr().out
    for secret in (EMAIL, PHONE, "Pat Example"):
        assert secret not in out
    assert "pat" not in json.dumps({k: v for k, v in s.items()}).lower()


def test_static_dashboard_never_carries_contacts(tmp_path):
    st = _store(tmp_path, n=1)
    run(run_enrich(st, "r1", sc=FakeSC({"/v1/instagram/profile": IG}), box=FakeBox(), limits=Limits(daily_usd=1)))

    class S:
        def get(self, key, default=None):
            return default
    static = json.dumps(build_data(st, S()))
    assert EMAIL not in static and "contacts" not in json.loads(static)
    live = build_data(st, S(), include_contacts=True)
    assert live["leads"][0]["contact"] == "instagram:person0"
    assert live["contacts"]["instagram:person0"]["fields"]["email"]["verdict"] == "deliverable"


def test_purge_removes_expired_cards(tmp_path):
    st = _store(tmp_path, n=1)
    run(run_enrich(st, "r1", sc=FakeSC({"/v1/instagram/profile": IG}), box=FakeBox(),
                   limits=Limits(daily_usd=1, retention_days=1)))
    assert st.purge_contacts(NOW + timedelta(days=2)) == 1
    assert st.get_contact("instagram:person0") is None
    assert st.conn.execute("SELECT COUNT(*) FROM contact_links").fetchone()[0] == 0


def test_profile_beats_urls_and_names_in_post_text():
    """A post that names a tool's site must not become the author's domain (it would verify a stranger)."""
    sc = FakeSC({"/v1/threads/profile": {"username": "patbuilds", "full_name": "Pat Example",
                                         "biography": "Owner of Example Widgets Co", "bio_links": [
                                             {"url": "https://examplewidgets.test"}]}})
    post = _post("threads", "patbuilds", "We use toolvendor.test and I run Facebook ads, need a VA. "
                 "Email me pat@examplewidgets.test")
    r = run(rs.resolve(sc, [post]))
    assert r.args["domain"] == "examplewidgets.test" and r.args["website"] == "https://examplewidgets.test"
    assert r.args["company"] == "Example Widgets Co" and r.args["emails_seen"] == ["pat@examplewidgets.test"]
    bare = run(rs.resolve(FakeSC({}), [_post("reddit", "someone", "We use toolvendor.test, I run Acme Widgets")]))
    assert "website" not in bare.args and "company" not in bare.args and bare.status == "unresolved"
    assert tx.companies("I run Facebook ads for coaches") == []


def test_storefront_is_not_a_domain():
    sc = FakeSC({"/v1/instagram/profile": {"data": {"user": {"username": "p", "full_name": "Pat Example",
                                                             "external_url": "https://pat.myshopify.com"}}}})
    r = run(rs.resolve(sc, [_post()]))
    assert "domain" not in r.args and "website" not in r.args and r.status == "unresolved"


def test_enrich_cli_failure_prints_no_contact_data(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from lead_radar import cli, enrich as en
    monkeypatch.setenv("RADAR_STATE_DIR", str(tmp_path))
    _store(tmp_path, n=1)

    async def boom(*a, **kw):
        card = {"email": EMAIL, "name": "Pat Example", "phone": PHONE}  # locals a pretty traceback would print
        raise DataBoxError("data-box rejected the token (401)", "unauthorized", card)
    monkeypatch.setattr(en, "run_enrich", boom)
    res = CliRunner().invoke(cli.app, ["enrich", "va"])
    assert res.exit_code == 1 and "unauthorized" in res.output
    for secret in (EMAIL, PHONE, "Pat Example"):
        assert secret not in res.output
