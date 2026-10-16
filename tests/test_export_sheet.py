"""The Sheet export's row builders (the gws calls are not exercised offline)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("radar_export_sheet", Path(__file__).parents[1] / "scripts" / "radar_export_sheet.py")
ex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ex)


def R(platform, role, tier, posted="2026-09-29T10:00:00+00:00", **kw):
    return {"platform": platform, "role": role, "tier": tier, "urgency": "soon", "posted_at": posted,
            "author_handle": "a", "url": "https://x/" + platform + tier, "evidence_quote": "need leads",
            "need_summary": "wants leads", "decided_by": "jev", "confidence": 0.91, "text": "need leads  now", **kw}


def test_leads_exclude_sellers_and_none_and_sort_by_tier_then_recency():
    rows = [R("reddit", "buyer", "low"), R("reddit", "seller", "none"), R("linkedin", "buyer", "high", "2026-09-28T00:00:00+00:00"),
            R("threads", "buyer", "high", "2026-09-29T00:00:00+00:00"), R("tiktok", "other", "none"), R("reddit", "buyer", "none")]
    out = ex.lead_rows(rows)
    assert [(r[0], r[1]) for r in out] == [("threads", "High"), ("linkedin", "High"), ("reddit", "Low")]
    assert out[0][10] == "need leads now" and out[0][11] == ""  # post text; no contact yet


def test_platform_rows_count_tiers_sellers_drops_and_sources():
    rows = [R("reddit", "buyer", "high"), R("reddit", "seller", "none"), R("linkedin", "buyer", "medium")]
    drops = [{"platform": "reddit", "reason": "too_short"}, {"platform": "youtube", "reason": "creator_own_post"}]
    per_source = {"reddit": {"posts": 40, "credits": 9, "errors": 0}, "google": {"posts": 12, "credits": 4, "errors": 1}}
    got = {r[0]: r for r in ex.platform_rows(rows, drops, per_source)}
    assert got["reddit"] == ["reddit", 40, 2, 1, 0, 0, 1, 1, 1, 9, 0]
    assert got["linkedin"][4] == 1 and got["youtube"][8] == 1 and got["google"][1] == 12


def test_csv_has_sparklines_totals_and_neutralizes_formula_text(tmp_path):
    import csv
    leads = ex.lead_rows([R("reddit", "buyer", "high", text="=HYPERLINK(evil)"), R("threads", "buyer", "medium")])
    leads[0][6] = "-1 weird quote"
    plat = ex.platform_rows([R("reddit", "buyer", "high"), R("threads", "buyer", "medium")], [],
                            {"reddit": {"posts": 4, "credits": 2, "errors": 0}})
    out = tmp_path / "s.csv"
    ex.write_csv(str(out), "T", leads, plat, [["Caveat", "+not a formula"]])
    rows = list(csv.reader(out.open()))
    assert rows[4][0] == "reddit" and rows[4][11].startswith("=SPARKLINE({D5,E5,F5}")
    assert rows[6] == ["TOTAL"] + [f"=SUM({c}5:{c}6)" for c in "BCDEFGHIJK"] and rows[7] == [ex.BASES_NOTE]
    lead = next(r for r in rows if r and r[0] == "reddit" and r[1] == "High")
    assert lead[6] == "'-1 weird quote" and lead[10] == "'=HYPERLINK(evil)"
    assert rows[-1] == ["Caveat", "'+not a formula"]


def test_xlsx_has_three_tabs_and_two_charts(tmp_path):
    import pytest
    openpyxl = pytest.importorskip("openpyxl")
    plat = ex.platform_rows([R("reddit", "buyer", "high"), R("linkedin", "seller", "none")], [], {})
    out = tmp_path / "s.xlsx"
    ex.write_xlsx(str(out), ex.lead_rows([R("reddit", "buyer", "high")]), plat, [["Radar", "x"]], 72)
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["Leads", "By platform", "Run"]
    assert len(wb["By platform"]._charts) == 2 and wb["Leads"].max_row == 2


def test_safe_leaves_empty_and_plain_cells_alone():
    assert ex._safe(["", None, 3, "ok", "=x"]) == ["", None, 3, "ok", "'=x"]


def test_verify_run_flags_non_verbatim_evidence_and_csv_mismatch(tmp_path):
    import importlib.util as iu
    from lead_radar.db import Store
    from lead_radar.models import Classification, Post, Radar, Run, post_id
    spec2 = iu.spec_from_file_location("vr", Path(__file__).parents[1] / "scripts" / "radar_verify_run.py")
    vr = iu.module_from_spec(spec2)
    spec2.loader.exec_module(vr)
    store = Store.open(tmp_path / "s.sqlite")
    store.save_radar(Radar(id="r1", name="x", query="q"))
    store.save_run(Run(id="run1", radar_id="r1", credits_used=3, per_source={"reddit": {"credits": 3}}))
    for i, quote in enumerate(["need more clients", "totally invented"]):
        url = f"https://x/{i}"
        store.upsert_post(Post(id=post_id("reddit", url), platform="reddit", url=url, kind="post",
                               text="I really need more clients this month", source_term="t", run_id="run1"))
        store.conn.execute("INSERT INTO classifications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (post_id("reddit", url), "r1", "buyer", "medium", "", quote, "unknown", 0.9, "jev", 0, "t",
                            "2026-09-30"))
    f = tmp_path / "l.csv"
    f.write_text("LEADS\nPlatform,Tier,U,P,A,Post URL\nreddit,Medium,u,p,a,https://x/0\n\n")
    got = {name: ok for name, ok, _ in vr.check(store, "r1", ["run1"], str(f))}
    assert got == {"evidence verbatim": False, "credits add up (run1)": True, "csv leads match the run": False,
                   "contact emails verified": True}
