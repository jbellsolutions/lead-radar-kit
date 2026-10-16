"""M1 gate: schema, migrations and the lead-intent contract."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lead_radar import contract, db
from lead_radar.config import load as load_settings
from lead_radar.models import Classification, IntentBrief, Lead, Post, Radar, Run, post_id

FIXTURES = Path(__file__).parent / "fixtures" / "posts_va.json"
CASES = json.loads(FIXTURES.read_text())["cases"]


@pytest.fixture
def store(tmp_path):
    return db.Store.open(tmp_path / "radar.sqlite")


def _post(case, run_id="run-1"):
    p = case["post"]
    return Post(id=post_id(p["platform"], p["url"]), run_id=run_id, **p)


# --- migrations ----------------------------------------------------------

def test_migrations_apply_clean_and_idempotent(tmp_path):
    conn = db.connect(tmp_path / "m.sqlite")
    assert db.migrate(conn) == sorted(f.name for f in db.MIGRATIONS_DIR.glob("*.sql"))
    assert db.migrate(conn) == []
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"radars", "intent_briefs", "runs", "posts", "run_posts", "classifications", "drops", "leads",
            "classification_queue"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_db_file_is_owner_only(tmp_path):
    path = tmp_path / "radar.sqlite"
    db.Store.open(path)
    assert (path.stat().st_mode & 0o777) == 0o600


def _seed(store):
    store.save_radar(Radar(id="r1", name="va", query="virtual assistant"))
    store.save_run(Run(id="run-1", radar_id="r1"))


def test_run_posts_require_an_existing_run(store):
    store.save_radar(Radar(id="r1", name="va", query="virtual assistant"))
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_post(_post(CASES[0], run_id="no-such-run"))
    assert store.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0  # rolled back, no orphan


def test_check_constraints_reject_bad_tier(store):
    _seed(store)
    p = _post(CASES[0])
    store.upsert_post(p)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO classifications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.id, "r1", "buyer", "urgent", "x", "", "now", 0.5, "jev", 0, "lead-intent.v1", "now"))


# --- round trips ---------------------------------------------------------

def test_round_trip_all_models(store):
    radar = Radar(id="r1", name="va", query="virtual assistant", platforms=["reddit", "linkedin"])
    store.save_radar(radar)
    assert store.get_radar("va") == radar

    brief = IntentBrief(
        id="b1", radar_id="r1", offer="virtual assistant", buyer_profile="solo founders",
        need_statements=["need someone to answer my emails"], high_examples=["a"], medium_examples=["b"],
        low_examples=["c"], seller_signals=["I'm a VA", "DM me", "open spots", "for hire", "book a call"],
        terms={"reddit": ["need a VA"]}, version=1)
    store.save_brief(brief)
    assert store.get_brief("b1") == brief

    run = Run(id="run-1", radar_id="r1", per_source={"reddit": {"requests": 2, "posts": 10, "errors": 0}},
              credits_used=2, counts={"fetched": 10})
    store.save_run(run)
    assert store.get_run("run-1") == run
    assert store.get_run("latest").id == "run-1"

    p = _post(CASES[0])
    assert store.upsert_post(p) is True
    assert store.upsert_post(p) is False
    assert store.get_post(p.id) == p

    c = Classification(post_id=p.id, radar_id="r1", decided_by="jev",
                       contract_version=contract.contract_version(), **CASES[0]["expected"])
    store.save_classification(c)
    assert store.classifications("r1", ["high"]) == [c]
    assert store.seen_for_radar("r1", [p.id, "other"]) == {p.id}

    now = datetime.now(timezone.utc)
    lead = Lead(id="L1", radar_id="r1", handles={"reddit": "fixture_00"}, post_ids=[p.id], best_tier="high",
                score=0.93, need_summary="x", evidence_quote="y", first_seen=now, last_seen=now)
    store.save_lead(lead)
    assert store.leads("r1") == [lead]
    assert store.set_lead_status("L1", "r1", "approved")
    assert store.leads("r1")[0].status == "approved"


def test_drops_count_as_seen(store):
    _seed(store)
    p = _post(CASES[9])
    store.upsert_post(p)
    store.save_drop(p.id, "r1", "run-1", "seller_signal")
    assert store.seen_for_radar("r1", [p.id]) == {p.id}


def test_post_id_is_sha1_of_platform_and_url():
    assert post_id("reddit", "https://x/y") == post_id("reddit", "https://x/y")
    assert post_id("reddit", "https://x/y") != post_id("facebook", "https://x/y")
    assert len(post_id("reddit", "u")) == 40


def test_brief_caps_terms_at_eight():
    with pytest.raises(ValueError):
        IntentBrief(id="b", radar_id="r", offer="o", buyer_profile="p", need_statements=["n"],
                    high_examples=[], medium_examples=[], low_examples=[],
                    seller_signals=["a", "b", "c", "d", "e"], terms={"reddit": [str(i) for i in range(9)]})


# --- contract ------------------------------------------------------------

def test_fixture_mix_is_4_3_2_3():
    from collections import Counter
    assert Counter(c["label"] for c in CASES) == {"high": 4, "medium": 3, "low": 2, "seller": 3}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_contract_validates_fixture(case):
    contract.validate_lead_intent(case["expected"], case["post"]["text"])


def test_evidence_must_be_verbatim():
    case = CASES[0]
    bad = dict(case["expected"], evidence_quote="I desperately need a VA today")
    with pytest.raises(contract.ContractError, match="verbatim"):
        contract.validate_lead_intent(bad, case["post"]["text"])


def test_evidence_match_ignores_case_and_whitespace():
    case = CASES[0]
    q = "  anyone KNOW a good\nVA?   need someone   for inbox "
    contract.validate_lead_intent(dict(case["expected"], evidence_quote=q), case["post"]["text"])


def test_evidence_max_30_words():
    text = " ".join(f"w{i}" for i in range(40))
    out = {"role": "buyer", "tier": "low", "need_summary": "n", "evidence_quote": text,
           "urgency": "unknown", "confidence": 0.5}
    with pytest.raises(contract.ContractError, match="30 words"):
        contract.validate_lead_intent(out, text)


def test_seller_cannot_carry_a_tier():
    case = CASES[9]
    with pytest.raises(contract.ContractError, match="tier none"):
        contract.validate_lead_intent(dict(case["expected"], tier="high"), case["post"]["text"])


def test_non_none_tier_requires_evidence():
    case = CASES[0]
    with pytest.raises(contract.ContractError, match="requires an evidence_quote"):
        contract.validate_lead_intent(dict(case["expected"], evidence_quote=""), case["post"]["text"])


def test_schema_rejects_unknown_fields_and_bad_values():
    case = CASES[0]
    with pytest.raises(contract.ContractError):
        contract.validate_lead_intent(dict(case["expected"], confidence=1.4), case["post"]["text"])
    with pytest.raises(contract.ContractError):
        contract.validate_lead_intent(dict(case["expected"], extra="x"), case["post"]["text"])


def test_intent_brief_contract_requires_five_seller_signals():
    brief = {"offer": "va", "buyer_profile": "p", "need_statements": ["a", "b", "c"], "high_examples": ["h"],
             "medium_examples": ["m"], "low_examples": ["l"], "seller_signals": ["1", "2", "3", "4"],
             "terms": {"reddit": ["x"]}, "communities": {}, "negative_terms": []}
    with pytest.raises(contract.ContractError):
        contract.validate_intent_brief(brief)
    contract.validate_intent_brief(dict(brief, seller_signals=["1", "2", "3", "4", "5"]))


# --- config --------------------------------------------------------------

def test_config_resolves_scrapecreators_alias(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text("MCP_SCRAPE_CREATORS_API_KEY=abc123\n")
    s = load_settings(env_file=env, environ={})
    assert s.require("SCRAPECREATORS_API_KEY") == "abc123"
    assert "abc123" not in repr(s)
    assert s.get("jev.accept_confidence") == 0.8


def test_missing_secret_names_key_only(tmp_path):
    s = load_settings(env_file=tmp_path / "none", environ={})
    with pytest.raises(Exception, match="TYPESAFE_API_KEY"):
        s.require("TYPESAFE_API_KEY")


def test_brief_community_keys_are_normalized_to_platforms(tmp_path):
    import json
    from lead_radar.expander import load_brief_file
    src = json.loads((Path(__file__).parents[1] / "config" / "briefs" / "virtual-assistant.json").read_text())
    src["communities"] = {"subreddits": ["smallbusiness"], "facebook_groups": ["https://www.facebook.com/groups/1"],
                          "skool_groups": ["x"], "reddit": ["sales"]}
    f = tmp_path / "b.json"
    f.write_text(json.dumps(src))
    b = load_brief_file(f, "r1")
    assert b.communities == {"reddit": ["smallbusiness", "sales"], "facebook": ["https://www.facebook.com/groups/1"],
                             "skool": ["x"]}


def test_expander_tidy_drops_non_list_community_notes():
    from lead_radar.expander import tidy
    body = {"communities": {"reddit": ["smallbusiness", 3], "note": "suggestions only"}, "terms": {"reddit": ["a"]}}
    assert tidy(body)["communities"] == {"reddit": ["smallbusiness"]} and body["terms"] == {"reddit": ["a"]}
