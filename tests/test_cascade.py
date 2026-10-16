"""M3: Stage 0 rules, evidence spans, Jev interpretation, Stage 2 validation, and the cascade. Offline (mocked)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from lead_radar import config, contract
from lead_radar.classify import spans, stage0_rules, stage2_reasoning
from lead_radar.classify.cascade import classify_pending
from lead_radar.classify.stage1_jev import JevClient, build_request, interpret
from lead_radar.db import Store
from lead_radar.expander import load_brief_file
from lead_radar.models import Post, Radar, Run, post_id

ROOT = Path(__file__).parents[1]
BRIEF = load_brief_file(ROOT / "config" / "briefs" / "virtual-assistant.json", "r1")
NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


def P(text, *, platform="reddit", kind="post", days_old=1, raw=None, url=None, author="a1"):
    url = url or f"https://example.com/{abs(hash((text, platform, kind)))}"
    return Post(id=post_id(platform, url), platform=platform, url=url, kind=kind, text=text, source_term="t",
                run_id="run-1", posted_at=NOW - timedelta(days=days_old), raw=raw or {}, author_handle=author)


def s0(post, **kw):
    return stage0_rules.check(post, BRIEF, lookback_days=kw.get("lookback", 14), languages=["en"], now=NOW)


# --- Stage 0 ----------------------------------------------------------------

@pytest.mark.parametrize("post,reason", [
    (P("Anyone know a good VA for my inbox? Need help ASAP please.", days_old=20), "older_than_lookback"),
    (P("need a VA?"), "too_short"),
    (P("Necesito una asistente virtual para mi negocio, alguien sabe?", platform="tiktok", kind="comment",
       raw={"comment_language": "es"}), "language:es"),
    (P("我需要一个虚拟助理来处理我的电子邮件和日程安排，请问有人推荐吗？谢谢大家的帮助和建议"), "language:non_latin"),
    (P("How to hire a virtual assistant in 2026 - full guide for beginners", platform="youtube", kind="video"),
     "creator_own_post"),
    (P("Thanks everyone for watching, glad the video helped you all!", platform="youtube", kind="comment",
       raw={"is_creator": True}), "creator_reply"),
    (P("I'm a VA with 5 open spots for inbox and calendar management. Reasonable rates."), "seller_signal:I'm a VA"),
    (P("New VA course launching next week, become a virtual assistant from home."),
     "negative_term:VA course"),
])
def test_stage0_drops_with_reason(post, reason):
    assert s0(post) == reason


@pytest.mark.parametrize("text", [
    "I'm a VA and honestly I need a VA myself for overflow work. Anyone recommend one?",   # seller phrase + ask
    "Where can I find a virtual assistant? My last job posting got zero good applicants.",  # 'job' + question
    "URGENTLY LOOKING FOR WORK - looking for a Virtual Assistant role, any leads appreciated",  # job seeker: Stage 2's call
    "Drowning in admin and chasing invoices every single weekend, something has to change.",
])
def test_stage0_keeps_posts_with_an_ask_or_no_signal(text):
    assert s0(P(text)) is None


def test_stage0_unknown_date_is_kept():
    p = P("Anyone know a good virtual assistant for inbox and scheduling?")
    p.posted_at = None
    assert s0(p) is None


# --- spans ---------------------------------------------------------------------

def test_spans_are_exact_non_overlapping_and_capped():
    text = ("Hey all. " + " ".join(f"word{i}" for i in range(45)) + ". Need a VA for inbox ASAP! Who do you use?")
    cands = spans.candidates(text)
    assert 1 <= len(cands) <= spans.MAX_CANDIDATES
    for c in cands:
        assert c["text"] in text and len(c["text"].split()) <= 30
    for a in cands:
        for b in cands:
            assert a is b or a["text"] not in b["text"]


# --- Jev interpretation ---------------------------------------------------------

def jev_answer(choice, options, p=0.95):
    options = list(options)
    rest = (1 - p) / max(1, len(options) - 1)
    probs = {o: (p if o == choice else rest) for o in options}
    return {"choice": choice, "confidence": p, "probabilities": probs}


def jev_result(body, picks, probs=None):
    probs = probs or {}
    return {"model": "jev-test", "usage": {"input_tokens": 10},
            "answers": {q: jev_answer(picks[q], spec["criteria"], probs.get(q, 0.95))
                        for q, spec in body["questions"].items()}}


HIGH = P("Anyone know a good VA? Need someone for inbox and scheduling ASAP.")


def test_jev_accepts_confident_high_with_verbatim_evidence():
    body, lookup = build_request(HIGH, BRIEF, "m")
    span = next(k for k, v in lookup["spans"].items() if "Need someone" in v)
    v = interpret(jev_result(body, {"role": "buyer", "tier": "high", "urgency": "now", "evidence_span": span,
                                    "need": "need_2"}), body, lookup, HIGH, 0.8)
    assert v.reason == "accepted" and v.output["tier"] == "high" and v.output["urgency"] == "now"
    assert contract.is_verbatim(v.output["evidence_quote"], HIGH.text)
    assert v.output["need_summary"] == BRIEF.need_statements[2]


def test_jev_low_tier_confidence_goes_to_stage2():
    body, lookup = build_request(HIGH, BRIEF, "m")
    v = interpret(jev_result(body, {"role": "buyer", "tier": "high", "urgency": "now", "evidence_span": "span_0",
                                    "need": "need_0"}, {"tier": 0.6}), body, lookup, HIGH, 0.8)
    assert v.output is None and v.reason == "low_confidence:tier"


def test_jev_confident_seller_forces_tier_none_even_if_tier_head_disagrees():
    body, lookup = build_request(HIGH, BRIEF, "m")
    v = interpret(jev_result(body, {"role": "seller", "tier": "high", "urgency": "now", "evidence_span": "span_0",
                                    "need": "need_0"}), body, lookup, HIGH, 0.8)
    assert v.reason == "accepted" and v.output["role"] == "seller" and v.output["tier"] == "none"
    assert v.output["evidence_quote"] == "" and v.output["need_summary"] == ""


def test_jev_unsure_between_buyer_and_not_goes_to_stage2():
    body, lookup = build_request(HIGH, BRIEF, "m")
    res = jev_result(body, {"role": "other", "tier": "none", "urgency": "unknown", "evidence_span": "none",
                            "need": "other"})
    res["answers"]["role"]["probabilities"] = {"buyer": 0.35, "seller": 0.05, "job_seeker": 0.0, "other": 0.6}
    v = interpret(res, body, lookup, HIGH, 0.8)
    assert v.output is None and v.reason == "low_confidence:role"


def test_jev_tier_none_uses_p_not_buyer_and_maps_job_seeker():
    body, lookup = build_request(HIGH, BRIEF, "m")
    res = jev_result(body, {"role": "job_seeker", "tier": "none", "urgency": "unknown", "evidence_span": "none",
                            "need": "other"})
    res["answers"]["role"]["probabilities"] = {"buyer": 0.05, "seller": 0.45, "job_seeker": 0.5, "other": 0.0}
    res["answers"]["role"]["confidence"] = 0.5
    v = interpret(res, body, lookup, HIGH, 0.8)
    assert v.reason == "accepted" and v.output["role"] == "other" and v.output["tier"] == "none"
    assert v.output["evidence_quote"] == "" and v.output["urgency"] == "unknown"


def test_jev_need_falls_back_to_evidence_span_when_unsure():
    body, lookup = build_request(HIGH, BRIEF, "m")
    v = interpret(jev_result(body, {"role": "buyer", "tier": "high", "urgency": "now", "evidence_span": "span_0",
                                    "need": "need_1"}, {"need": 0.4}), body, lookup, HIGH, 0.8)
    assert v.reason == "accepted" and v.output["need_summary"] == lookup["spans"]["span_0"]


def test_jev_invalid_response():
    body, lookup = build_request(HIGH, BRIEF, "m")
    v = interpret({"answers": {"role": {"choice": "nope"}}}, body, lookup, HIGH, 0.8)
    assert v.output is None and v.reason == "invalid_response"


# --- Stage 2 --------------------------------------------------------------------

def _settings(tmp_path, typesafe=False):
    env = {"SCRAPECREATORS_API_KEY": "k", "OPENROUTER_API_KEY": "or", "RADAR_STATE_DIR": str(tmp_path)}
    if typesafe:
        env["TYPESAFE_API_KEY"] = "ts"
    return config.load(config_path=ROOT / "config" / "radar.yaml", env_file=tmp_path / "none", environ=env)


class OpenRouterMock(httpx.AsyncBaseTransport):
    """Answers by keyword; `script` overrides with a list of raw contents (per call) for failure tests."""

    def __init__(self, script=None, status=None):
        self.script, self.status, self.calls = list(script or []), list(status or []), []

    async def handle_async_request(self, request):
        body = json.loads(request.content)
        self.calls.append(body["model"])
        if self.status:
            code = self.status.pop(0)
            if code != 200:
                return httpx.Response(code, text="upstream error")
        text = json.loads(body["messages"][1]["content"])["post_text"]
        if self.script:
            content = self.script.pop(0)
        elif "open spots" in text or "[FOR HIRE]" in text:
            content = json.dumps({"role": "seller", "tier": "none", "need_summary": "", "evidence_quote": "",
                                  "urgency": "unknown", "confidence": 0.95})
        elif "?" in text:
            first = text.split("?")[0] + "?"
            content = json.dumps({"role": "buyer", "tier": "high", "need_summary": "wants a VA",
                                  "evidence_quote": first, "urgency": "now", "confidence": 0.9})
        else:
            content = json.dumps({"role": "buyer", "tier": "medium", "need_summary": "admin overload",
                                  "evidence_quote": text.split(".")[0], "urgency": "unknown", "confidence": 0.7})
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})


def test_stage2_valid(tmp_path):
    v = asyncio.run(stage2_reasoning.classify(_settings(tmp_path), HIGH, BRIEF, transport=OpenRouterMock()))
    assert v.ok and v.output["tier"] == "high" and v.tokens_in == 1000


def test_stage2_fabricated_evidence_retries_then_gives_up(tmp_path):
    bad = json.dumps({"role": "buyer", "tier": "high", "need_summary": "x", "evidence_quote": "I urgently need a VA",
                      "urgency": "now", "confidence": 0.9})
    t = OpenRouterMock(script=[bad, "not json at all"])
    v = asyncio.run(stage2_reasoning.classify(_settings(tmp_path), HIGH, BRIEF, transport=t))
    assert not v.ok and v.output["tier"] == "none" and v.output["confidence"] == 0 and len(t.calls) == 2


def test_stage2_falls_back_to_second_model_on_provider_error(tmp_path):
    t = OpenRouterMock(status=[500, 200])
    s = _settings(tmp_path)
    v = asyncio.run(stage2_reasoning.classify(s, HIGH, BRIEF, transport=t))
    assert v.ok and t.calls == [s.get("models.reasoning"), s.get("models.reasoning_fallback")]


def test_stage2_seller_role_forces_tier_none(tmp_path):
    lying = json.dumps({"role": "seller", "tier": "high", "need_summary": "x", "evidence_quote": "open spots",
                        "urgency": "now", "confidence": 0.9})
    p = P("I'm a VA with open spots for clients, message me today.")
    v = asyncio.run(stage2_reasoning.classify(_settings(tmp_path), p, BRIEF, transport=OpenRouterMock(script=[lying])))
    assert v.ok and v.output["tier"] == "none" and v.output["role"] == "seller"


# --- cascade ----------------------------------------------------------------------

class TypeSafeMock(httpx.AsyncBaseTransport):
    """Confident on sellers and on explicit asks; unsure on everything else."""

    def __init__(self):
        self.calls = 0

    async def handle_async_request(self, request):
        self.calls += 1
        body = json.loads(request.content)
        text = body["state"]["post_text"]
        span = next((k for k, v in body["questions"]["evidence_span"]["criteria"].items() if k != "none"), "none")
        if "open spots" in text:
            picks, probs = {"role": "seller", "tier": "none", "urgency": "unknown", "evidence_span": "none",
                            "need": "other"}, {}
        elif "?" in text:
            picks, probs = {"role": "buyer", "tier": "high", "urgency": "now", "evidence_span": span,
                            "need": "need_0"}, {}
        else:
            picks, probs = {"role": "buyer", "tier": "medium", "urgency": "unknown", "evidence_span": span,
                            "need": "need_0"}, {"tier": 0.5}
        return httpx.Response(200, json=jev_result(body, picks, probs))


def _seed(tmp_path, posts, typesafe=False):
    s = _settings(tmp_path, typesafe)
    store = Store.open(s.db_path)
    radar = Radar(id="r1", name="va", query="virtual assistant", intent_brief_id=BRIEF.id)
    store.save_radar(radar.model_copy(update={"intent_brief_id": None}))
    store.save_brief(BRIEF)
    store.save_radar(radar)
    run = Run(id="run-1", radar_id="r1")
    store.save_run(run)
    for p in posts:
        store.upsert_post(p)
    store.enqueue([p.id for p in posts], "r1", "run-1")
    return s, store, radar, run


POSTS = [
    P("Anyone know a good VA? Need someone for inbox and scheduling ASAP.", url="https://e.com/1"),
    P("Where do I find a VA who can manage my Shopify customer emails?", url="https://e.com/2"),
    P("Drowning in admin work lately. Thinking about getting help at some point.", url="https://e.com/3"),
    P("Need a hand with your inbox? I have open spots this month, great rates.", url="https://e.com/4"),
    P("Old post: can anyone recommend a virtual assistant agency for my firm?", url="https://e.com/5", days_old=30),
    P("How to hire a VA - my full process explained step by step", platform="youtube", kind="video",
      url="https://e.com/6"),
]


def test_cascade_heuristic_mode_sends_everything_to_stage2(tmp_path):
    s, store, radar, run = _seed(tmp_path, POSTS)
    counts = asyncio.run(classify_pending(s, store, radar, BRIEF, run, s2_transport=OpenRouterMock(), now=NOW))
    assert counts["jev_mode"] == "heuristic" and run.errors[0].startswith("JEV IN HEURISTIC MODE")
    assert counts["stage0_reasons"] == {"older_than_lookback": 1, "creator_own_post": 1}
    assert counts["classified"] == 4 and counts["decided_by"] == {"reasoning": 4} and counts["jev_share"] == 0
    assert store.pending("r1") == []
    again = asyncio.run(classify_pending(s, store, radar, BRIEF, run, s2_transport=OpenRouterMock(), now=NOW))
    assert again["pending_in"] == 0


def test_cascade_with_jev_calibration_and_share(tmp_path):
    s, store, radar, run = _seed(tmp_path, POSTS, typesafe=True)
    ts = TypeSafeMock()
    jev = JevClient("ts", "jev-test", transport=ts)
    counts = asyncio.run(classify_pending(s, store, radar, BRIEF, run, jev=jev, s2_transport=OpenRouterMock(), now=NOW))
    # 4 survive Stage 0: two confident asks (Jev high), one seller (Jev none), one unsure (Stage 2)
    assert counts["jev_mode"] == "typesafe" and ts.calls == 4
    assert counts["decided_by"] == {"jev": 3, "reasoning": 1} and counts["jev_share"] == 0.75
    assert counts["calibration_checked"] == 2 and counts["calibration_agreed"] == 2
    assert store.calibration_count("r1") == 2
    tiers = {c.post_id: c.tier for c in store.classifications("r1")}
    assert tiers[POSTS[3].id] == "none" and tiers[POSTS[0].id] == "high"


def test_calibration_disagreement_uses_stage2(tmp_path):
    s, store, radar, run = _seed(tmp_path, POSTS[:1], typesafe=True)
    medium = json.dumps({"role": "buyer", "tier": "medium", "need_summary": "x", "urgency": "soon",
                         "evidence_quote": "Anyone know a good VA?", "confidence": 0.8})
    jev = JevClient("ts", "jev-test", transport=TypeSafeMock())
    asyncio.run(classify_pending(s, store, radar, BRIEF, run, jev=jev, s2_transport=OpenRouterMock(script=[medium]),
                                 now=NOW))
    c = store.classifications("r1")[0]
    assert c.tier == "medium" and c.decided_by == "reasoning"
    assert store.conn.execute("SELECT agreed FROM calibration").fetchone()[0] == 0


def test_freshness_downgrades_old_high(tmp_path):
    old = P("Can anyone recommend a virtual assistant agency for my firm?", url="https://e.com/9", days_old=20)
    s, store, radar, run = _seed(tmp_path, [old])
    radar.lookback_days = 30
    asyncio.run(classify_pending(s, store, radar, BRIEF, run, s2_transport=OpenRouterMock(), now=NOW))
    c = store.classifications("r1")[0]
    assert c.tier == "medium" and c.freshness_downgrade


class FullPostMock(httpx.AsyncBaseTransport):
    def __init__(self, reddit_body):
        self.body, self.calls = reddit_body, []

    async def handle_async_request(self, request):
        self.calls.append(request.url.path)
        if request.url.path == "/v1/reddit/post" and self.body:
            return httpx.Response(200, json={"success": True, "credits_charged": 1, **self.body})
        return httpx.Response(404, json={"success": False})


def _google_post(platform="reddit"):
    return P("Need a VA? Looking for help\n\n6 days ago ... I need a virtual assistant for my inbox, anyone?",
             platform=platform, url=f"https://www.{platform}.com/r/x/comments/abc/need", raw={"via": "google"}, author=None)


def test_google_high_is_verified_against_the_full_post(tmp_path):
    from lead_radar.scrapecreators import ScrapeCreators
    gp = _google_post()
    s, store, radar, run = _seed(tmp_path, [gp])
    full = {"title": "Looking for work as a VA", "selftext": "Hi! I'm a VA looking for clients, open spots, DM me.",
            "author": "realauthor", "created_utc": int(NOW.timestamp())}
    t = FullPostMock(full)
    sc = ScrapeCreators("k", transport=t)
    asyncio.run(classify_pending(s, store, radar, BRIEF, run, s2_transport=OpenRouterMock(), now=NOW, sc=sc))
    c = store.classifications("r1")[0]
    p = store.get_post(gp.id)
    assert t.calls == ["/v1/reddit/post"] and p.author_handle == "realauthor" and p.raw["verified_full_post"]
    assert c.tier == "none" and c.role == "seller" and run.counts["google_verified"] == 1


def test_unverifiable_google_high_is_capped_at_medium(tmp_path):
    from lead_radar.scrapecreators import ScrapeCreators
    gp = _google_post("quora")
    s, store, radar, run = _seed(tmp_path, [gp])
    sc = ScrapeCreators("k", transport=FullPostMock(None))
    asyncio.run(classify_pending(s, store, radar, BRIEF, run, s2_transport=OpenRouterMock(), now=NOW, sc=sc))
    assert store.classifications("r1")[0].tier == "medium" and run.counts["google_unverified_capped"] == 1
