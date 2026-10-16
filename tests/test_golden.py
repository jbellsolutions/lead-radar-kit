"""M3: golden-set sampling design, blind export, label merge and weighted evaluation."""
import json

import pytest

from lead_radar import golden


def G(pid, tier, role="buyer", stratum="pred_none", weight=1.0):
    return {"post_id": pid, "tier": tier, "role": role, "stratum": stratum, "weight": weight}


def test_weighted_precision_recall_and_leakage():
    gold = [G("a", "high"), G("b", "high"), G("c", "none", "seller"), G("d", "high", weight=10.0), G("e", "none")]
    preds = {"a": {"tier": "high", "decided_by": "jev"}, "b": {"tier": "medium", "decided_by": "reasoning"},
             "c": {"tier": "high", "decided_by": "jev"}, "d": {"tier": "none", "decided_by": "jev"},
             "e": {"tier": "none", "decided_by": "jev"}}
    rep = golden.evaluate(gold, {k: {**v, "role": "buyer"} for k, v in preds.items()})
    assert rep.per_tier["high"]["precision"] == 0.5          # a right, c wrong
    assert rep.per_tier["high"]["recall"] == round(1 / 12, 4)  # a (w1) of a+b+d (w1+1+10)
    assert rep.seller_leakage == round(1 / 3, 4)               # c is a seller among a, b, c predicted high/medium
    assert rep.jev_share == round(13 / 14, 4)                  # weighted: only b (w1) went to reasoning
    assert rep.confusion["high"] == {"high": 1, "medium": 1, "none": 1}


def test_gate_thresholds():
    rep = golden.evaluate([G("a", "high")], {"a": {"tier": "high", "role": "buyer", "decided_by": "jev"}})
    g = rep.gate()
    assert g["high_precision"]["pass"] and g["jev_share"]["pass"]
    assert g["seller_leakage"]["value"] == 0.0 and g["seller_leakage"]["pass"]


def test_job_seekers_count_as_leakage():
    rep = golden.evaluate([G("a", "none", "job_seeker")], {"a": {"tier": "medium", "role": "buyer", "decided_by": "jev"}})
    assert rep.seller_leakage == 1.0


def test_merge_labels_enforces_contract_and_completeness(tmp_path):
    items = [{"post_id": "a", "stratum": "pred_high", "weight": 1.0}, {"post_id": "b", "stratum": "pred_none", "weight": 3.0}]
    f = tmp_path / "labels.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in [
        {"post_id": "a", "role": "seller", "tier": "high"},   # contradicts the contract -> none
        {"post_id": "b", "role": "buyer", "tier": "medium"}]))
    rows = golden.merge_labels(items, f, "claude-opus-subagent")
    assert rows[0]["tier"] == "none" and rows[1]["weight"] == 3.0 and rows[1]["labeled_by"] == "claude-opus-subagent"
    f.write_text(json.dumps({"post_id": "a", "role": "buyer", "tier": "high"}))
    with pytest.raises(ValueError, match="no label"):
        golden.merge_labels(items, f, "x")
    f.write_text(json.dumps({"post_id": "a", "role": "buyer", "tier": "urgent"}))
    with pytest.raises(ValueError, match="bad label"):
        golden.merge_labels(items, f, "x")


def test_wilson_interval():
    lo, hi = golden.wilson(19, 26)
    assert 0.53 < lo < 0.56 and 0.86 < hi < 0.88
    assert golden.wilson(0, 0) is None


def test_sample_pools_several_held_out_runs_and_excludes_others(tmp_path):
    from datetime import datetime, timezone
    from lead_radar.db import Store
    from lead_radar.models import Classification, Post, Radar, Run, post_id
    store = Store.open(tmp_path / "g.sqlite")
    store.save_radar(Radar(id="r1", name="va", query="virtual assistant"))
    for run in ("diag", "pool-a", "pool-b"):
        store.save_run(Run(id=run, radar_id="r1"))
        for i in range(3):
            url = f"https://example.com/{run}/{i}"
            p = Post(id=post_id("reddit", url), platform="reddit", url=url, kind="post", text=f"need a VA {run} {i}",
                     source_term="t", run_id=run, posted_at=datetime(2026, 9, 27, tzinfo=timezone.utc))
            store.upsert_post(p)
            store.save_classification(Classification(post_id=p.id, radar_id="r1", role="buyer",
                                                     tier="high" if i == 0 else "none", need_summary="",
                                                     evidence_quote="", urgency="unknown", confidence=0.9,
                                                     decided_by="jev", contract_version="t"))
    items = golden.sample(store, "r1", n=50, from_run=["pool-a", "pool-b"])
    runs = {store.get_post(it["post_id"]).run_id for it in items}
    assert runs == {"pool-a", "pool-b"} and len(items) == 6
    assert len(golden.sample(store, "r1", n=50, from_run="diag")) == 3


def test_label_guide_and_classifier_share_the_policies():
    from lead_radar.classify import prompts
    for policy in (prompts.JOB_LISTING_POLICY, prompts.VENTING_POLICY, prompts.HOMONYM_POLICY):
        assert policy in golden.LABEL_GUIDE and policy in prompts.EXCLUSIONS


def test_sample_censuses_high_and_randomly_samples_a_large_seller_stratum(tmp_path):
    from datetime import datetime, timezone
    from lead_radar.db import Store
    from lead_radar.models import Classification, Post, Radar, Run, post_id
    store = Store.open(tmp_path / "g.sqlite")
    store.save_radar(Radar(id="r1", name="va", query="virtual assistant"))
    store.save_run(Run(id="run", radar_id="r1"))
    spec = [("buyer", "high")] * 5 + [("seller", "none")] * 60 + [("other", "none")] * 100
    for i, (role, tier) in enumerate(spec):
        url = f"https://example.com/{i}"
        p = Post(id=post_id("reddit", url), platform="reddit", url=url, kind="post", text=f"post {i}",
                 source_term="t", run_id="run", posted_at=datetime(2026, 9, 27, tzinfo=timezone.utc))
        store.upsert_post(p)
        store.save_classification(Classification(post_id=p.id, radar_id="r1", role=role, tier=tier, need_summary="",
                                                 evidence_quote="", urgency="unknown", confidence=0.9,
                                                 decided_by="jev", contract_version="t"))
    items = golden.sample(store, "r1", n=35, seed=1)
    by = {}
    for it in items:
        by.setdefault(it["stratum"], []).append(it)
    assert len(by["pred_high"]) == 5 and all(it["weight"] == 1.0 for it in by["pred_high"])
    assert len(by["pred_seller"]) == 10 and by["pred_seller"][0]["weight"] == 6.0
    assert len(by["pred_none"]) == 20 and by["pred_none"][0]["weight"] == 5.0
    with pytest.raises(ValueError, match="raise n"):
        golden.sample(store, "r1", n=3)
