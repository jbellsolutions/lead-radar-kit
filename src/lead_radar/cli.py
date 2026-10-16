"""radar CLI (spec §11). M2 ships hunt, list and report; save/export/label/eval land with their milestones."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer

from . import config
from .db import Store
from .hunt import hunt as run_hunt

app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_show_locals=False,
                  help="Lead Radar v2. Finds people asking; never contacts anyone.")


def _store(settings) -> Store:
    return Store.open(settings.db_path)


def _print_sources(run) -> None:
    typer.echo(f"{'source':<11}{'requests':>9}{'credits':>9}{'posts':>7}{'errors':>8}{'skipped':>9}")
    for name, s in sorted(run.per_source.items()):
        typer.echo(f"{name:<11}{s.get('requests', 0):>9}{s.get('credits', 0):>9}{s.get('posts', 0):>7}"
                   f"{s.get('errors', 0):>8}{s.get('skipped', 0):>9}")
    typer.echo(f"{'TOTAL':<11}{sum(s.get('requests', 0) for s in run.per_source.values()):>9}{run.credits_used:>9}"
               f"{sum(s.get('posts', 0) for s in run.per_source.values()):>7}")
    for name, s in sorted(run.per_source.items()):
        for note in s.get("notes") or []:
            typer.echo(f"note: {name}: {note}")


def _cost_line(settings, run) -> str:
    credits_usd = run.credits_used * float(settings.get("scrapecreators.credit_usd", 0) or 0)
    models = run.cost_usd - credits_usd
    return (f"cost ${run.cost_usd:.4f} total = ScrapeCreators {run.credits_used} credits ${credits_usd:.4f} "
            f"+ models ${models:.4f}")


def _print_platform_leads(store, run) -> None:
    """Classification results for the posts this run fetched, by each post's own platform (Google results count
    under the site they came from, so these rows need not match the per-source table above)."""
    rows = store.conn.execute(
        "SELECT p.platform, c.role, c.tier, COUNT(*) n FROM classifications c "
        "JOIN run_posts rp ON rp.post_id = c.post_id AND rp.run_id = ? JOIN posts p ON p.id = c.post_id "
        "WHERE c.radar_id = ? GROUP BY 1, 2, 3", (run.id, run.radar_id)).fetchall()
    if not rows:
        return
    by: dict = {}
    for r in rows:
        d = by.setdefault(r["platform"], {"classified": 0, "high": 0, "medium": 0, "low": 0, "sellers": 0})
        d["classified"] += r["n"]
        if r["role"] == "seller":
            d["sellers"] += r["n"]
        elif r["role"] == "buyer" and r["tier"] in ("high", "medium", "low"):
            d[r["tier"]] += r["n"]
    typer.echo("leads by the post's own platform:")
    typer.echo(f"{'platform':<11}{'classified':>11}{'high':>6}{'medium':>8}{'low':>5}{'sellers':>9}")
    for name, d in sorted(by.items()):
        typer.echo(f"{name:<11}{d['classified']:>11}{d['high']:>6}{d['medium']:>8}{d['low']:>5}{d['sellers']:>9}")


@app.command()
def hunt(query: str,
         platforms: str = typer.Option("all", help="all | api | listings | comma list, e.g. reddit,onlinejobs. all and listings include Indeed (paid, capped by RADAR_LISTINGS_DAILY_USD)"),
         days: int = typer.Option(14, help="lookback window in days"),
         dry_run: bool = typer.Option(False, "--dry-run", help="fetch and count only; no DB writes, no classification"),
         brief: Optional[Path] = typer.Option(None, help="use this intent-brief JSON instead of the expander"),
         name: Optional[str] = typer.Option(None, help="radar name (default: slug of the query)"),
         offer_context: Optional[str] = typer.Option(None, help="who we are / what we sell"),
         push_ghl: bool = typer.Option(False, "--push-ghl", help="not available in v2"),
         no_classify: bool = typer.Option(False, "--no-classify", help="fetch and queue only; classify later")):
    """Search for people publicly asking for QUERY."""
    if push_ghl:
        typer.echo("GHL push is not part of v2 (decision 2026-09-28).", err=True)
        raise typer.Exit(2)
    settings = config.load()
    store = _store(settings)
    try:
        res = asyncio.run(run_hunt(settings, store, query, platforms=platforms, days=days, dry_run=dry_run,
                                   brief_path=brief, radar_name=name, offer_context=offer_context,
                                   classify=not no_classify))
    except config.MissingSecret as exc:
        typer.echo(f"stopped: {exc}", err=True)
        raise typer.Exit(3)
    run = res.run
    typer.echo(f"radar={res.radar.name} run={run.id} brief={res.brief.id} dry_run={res.dry_run} days={res.radar.lookback_days}")
    _print_sources(run)
    typer.echo("counts: " + json.dumps(run.counts))
    if run.errors:
        typer.echo(f"errors ({len(run.errors)}):")
        for e in run.errors:
            typer.echo(f"  - {e}")
    if not dry_run:
        _print_classification(run)
        typer.echo(_cost_line(settings, run))
        _print_platform_leads(store, run)


def _print_classification(run) -> None:
    c = run.counts
    if "classified" not in c:
        typer.echo(f"queued for classification: {c.get('queued', 0)} (run `radar classify` to process)")
        return
    if c.get("jev_mode") == "heuristic":
        typer.secho("!! JEV IN HEURISTIC MODE: every post went to the reasoning model", fg="red", bold=True)
    typer.echo(f"stage0 dropped {c['stage0_dropped']} {c['stage0_reasons']}")
    typer.echo(f"classified {c['classified']}: high {c['high']}, medium {c['medium']}, low {c['low']}, "
               f"none {c['none']} (sellers {c['sellers']}); decided_by {c['decided_by']}; jev_share {c['jev_share']}")
    typer.echo(f"calibration {c['calibration_agreed']}/{c['calibration_checked']} agreed; stage2 calls {c['stage2_calls']} "
               f"(failed {c['stage2_failed']}); tokens {run.reasoning_tokens_in}/{run.reasoning_tokens_out}")


@app.command()
def classify(radar: str = typer.Argument(..., help="radar name or id"),
             limit: Optional[int] = typer.Option(None, help="classify at most N pending posts")):
    """Run the classifier cascade over a radar's pending queue."""
    import uuid
    from .classify.cascade import classify_pending
    from .hunt import run_cost
    from .models import Run, utcnow
    settings = config.load()
    store = _store(settings)
    r = store.get_radar(radar)
    if not r or not r.intent_brief_id:
        typer.echo("no such radar (or it has no brief)", err=True)
        raise typer.Exit(1)
    brief = store.get_brief(r.intent_brief_id)
    run = Run(id=uuid.uuid4().hex[:12], radar_id=r.id)
    store.save_run(run)
    asyncio.run(classify_pending(settings, store, r, brief, run, limit=limit))
    run.cost_usd = run_cost(settings, run)
    run.finished_at = utcnow()
    store.save_run(run)
    typer.echo(f"radar={r.name} run={run.id} (classification only)")
    _print_classification(run)
    typer.echo(_cost_line(settings, run))


@app.command()
def label(radar: str = typer.Argument(..., help="radar name or id"),
          n: int = typer.Option(200, help="sample size"),
          out: Path = typer.Option(Path("tests/golden/va_v1.jsonl"), help="golden JSONL to write"),
          export: Optional[Path] = typer.Option(None, help="write a BLIND batch for an external labeler and stop"),
          labels: Optional[Path] = typer.Option(None, help="import labels ({post_id,tier,role} JSONL) for --design"),
          design: Path = typer.Option(Path("tests/golden/va_v1.design.json"), help="sampling design file"),
          labeled_by: str = typer.Option("human", help="who labeled (stored on every row)"),
          seed: int = typer.Option(7),
          from_run: Optional[str] = typer.Option(None, help="sample only posts first fetched by these runs (held-out; comma-separated to pool)")):
    """Build a golden set. Default: an interactive single-keypress TUI for a human (the model's verdict is never shown).
    --export: sample, then write a blind batch for another labeler. --labels: merge that labeler's output."""
    from . import golden
    settings = config.load()
    store = _store(settings)
    r = store.get_radar(radar)
    if not r:
        typer.echo("no such radar", err=True)
        raise typer.Exit(1)
    if labels:
        items = json.loads(design.read_text())["items"]
        rows = golden.merge_labels(items, labels, labeled_by)
        golden.write_golden(rows, out)
        typer.echo(f"wrote {len(rows)} labels to {out}")
        return
    runs = [x.strip() for x in from_run.split(",") if x.strip()] if from_run else None
    items = golden.sample(store, r.id, n, seed, from_run=runs)
    design.parent.mkdir(parents=True, exist_ok=True)
    design.write_text(json.dumps({"radar": r.name, "seed": seed, "from_run": runs, "items": items}, indent=1))
    if export:
        golden.export_blind(store, r.id, items, export)
        typer.echo(f"sampled {len(items)} (design: {design}); blind batch: {export}")
        return
    rows = golden.label_tui(store, items, labeled_by)
    golden.write_golden(rows, out)
    typer.echo(f"wrote {len(rows)} labels to {out}")


@app.command("eval")
def eval_cmd(golden_path: Path = typer.Option(Path("tests/golden/va_v1.jsonl"), "--golden"),
             radar: str = typer.Option("virtual-assistant", help="radar whose brief the posts are judged against"),
             stored: bool = typer.Option(False, "--stored", help="score stored verdicts instead of re-running the cascade"),
             out: Optional[Path] = typer.Option(None, help="write the report JSON here")):
    """Score the cascade against a golden set: precision/recall per tier, seller leakage, share decided by Jev."""
    import tempfile
    import uuid
    from . import golden
    from .classify.cascade import classify_pending
    from .models import Run
    settings = config.load()
    store = _store(settings)
    r = store.get_radar(radar)
    brief = store.get_brief(r.intent_brief_id)
    gold = golden.read_golden(golden_path)
    ids = [g["post_id"] for g in gold]
    if stored:
        preds = {c.post_id: {"tier": c.tier, "role": c.role, "decided_by": c.decided_by}
                 for c in store.classifications(r.id) if c.post_id in set(ids)}
        run = None
    else:
        # Re-run the current cascade in a scratch store: no calibration, so it measures the steady-state cascade.
        # Per-post Stage 0 checks are skipped (these posts already passed them, and some would now age out of the
        # lookback), but campaign detection runs over the radar's full history, and campaign posts count as
        # predicted none, decided by rules.
        from .dedupe import templated_campaigns
        campaign = templated_campaigns(store.radar_posts(r.id)) & set(ids)
        tmp = Store.open(Path(tempfile.mkdtemp()) / "eval.sqlite")
        tmp.save_radar(r.model_copy(update={"intent_brief_id": None}))
        tmp.save_brief(brief)
        tmp.save_radar(r)
        run = Run(id="eval-" + uuid.uuid4().hex[:8], radar_id=r.id)
        tmp.save_run(run)
        for pid in ids:
            p = store.get_post(pid)
            tmp.upsert_post(p.model_copy(update={"run_id": run.id}))
        tmp.enqueue([i for i in ids if i not in campaign], r.id, run.id)
        asyncio.run(classify_pending(settings, tmp, r, brief, run, skip_stage0=True, calibrate=False))
        verified = {p.id for p in (tmp.get_post(i) for i in ids) if p and p.raw.get("verified_full_post")}
        preds = {c.post_id: {"tier": c.tier, "role": c.role, "decided_by": c.decided_by,
                             "google_verified": c.post_id in verified} for c in tmp.classifications(r.id)}
        preds.update({pid: {"tier": "none", "role": "seller", "decided_by": "rules"} for pid in campaign})
        typer.echo(f"templated_campaign drops among golden posts: {len(campaign)}")
    rep = golden.evaluate(gold, preds)
    gate = rep.gate()
    if not stored:
        typer.echo("note: live eval (Jev + Stage 2 + Google full-post verification calls; results vary run to run)")
    if rep.intervals.get("weighted_items_in_high_medium"):
        typer.secho("warning: some predicted high/medium posts carry sampling weight != 1; the unweighted CIs "
                    "describe the sample, not the weighted estimate", fg="yellow")
    typer.echo(f"golden={golden_path} n={rep.n} labeled_by={sorted({g.get('labeled_by') for g in gold})}")
    typer.echo(f"{'tier':<8}{'precision':>10}{'recall':>8}{'n_pred':>8}{'n_gold':>8}")
    for t, m in rep.per_tier.items():
        typer.echo(f"{t:<8}{str(m['precision']):>10}{str(m['recall']):>8}{m['n_pred']:>8}{m['n_gold']:>8}")
    typer.echo(f"seller leakage into high/medium: {rep.seller_leakage} (weighted denominator {rep.leakage_n:.1f})")
    typer.echo(f"jev share (weighted): {rep.jev_share}  decided_by: {rep.decided_by}")
    if run is not None:
        c = run.counts
        typer.echo(f"eval run: jev_mode {c.get('jev_mode')}, stage2 calls {c.get('stage2_calls')}, "
                   f"cost ${c.get('stage2_cost_usd')}")
    typer.echo("confusion (gold -> predicted): " + json.dumps(rep.confusion))
    for k, v in gate.items():
        cnt = rep.intervals["counts"][k]
        typer.echo(f"GATE {k}: {v['value']} (unweighted {cnt[0]}/{cnt[1]}, 95% CI {v['ci95']}) target {v['target']} "
                   f"-> {'PASS' if v['pass'] else 'FAIL'}")
    if out:
        per_post = [{"post_id": g["post_id"], "gold_tier": g["tier"], "gold_role": g["role"], "stratum": g.get("stratum"),
                     "weight": g.get("weight"), **{f"pred_{k}": v for k, v in (preds.get(g["post_id"]) or {}).items()}}
                    for g in gold]
        out.write_text(json.dumps({"per_tier": rep.per_tier, "seller_leakage": rep.seller_leakage,
                                   "jev_share": rep.jev_share, "decided_by": rep.decided_by, "intervals": rep.intervals,
                                   "confusion": rep.confusion, "gate": gate, "n": rep.n,
                                   "run_counts": run.counts if run is not None else None,
                                   "predictions": per_post}, indent=1))


@app.command("list")
def list_radars():
    """List saved radars."""
    store = _store(config.load())
    for r in store.list_radars():
        typer.echo(f"{r.name:<30} query={r.query!r} platforms={','.join(r.platforms)} days={r.lookback_days} "
                   f"schedule={r.schedule or '-'} active={r.active}")


@app.command()
def doctor(offline: bool = typer.Option(False, "--offline", help="only check that keys are present; no network")):
    """Check setup before a hunt: keys present (names only, never values), ScrapeCreators balance, OpenRouter key,
    state dir writable, gws auth for Sheet export. Exit 1 if anything required fails."""
    import shutil
    import subprocess
    import httpx
    from .scrapecreators import ScrapeCreators
    settings = config.load()
    rows, failed = [], False

    def add(name, ok, detail, required=True):
        nonlocal failed
        failed |= required and not ok
        rows.append((name, "ok" if ok else ("FAIL" if required else "warn"), detail))

    for key, req, why in (("SCRAPECREATORS_API_KEY", True, "all search"),
                          ("OPENROUTER_API_KEY", True, "expander + Stage 2"),
                          ("TYPESAFE_API_KEY", False, "Jev; without it every post goes to Stage 2 (heuristic mode)")):
        add(key, settings.has(key), "present" if settings.has(key) else f"missing ({why})", req)
    import os
    has_pw = bool(os.environ.get("RADAR_DASHBOARD_PASSWORD")
                  or config._parse_env_file(config.DEFAULT_ENV_FILE).get("RADAR_DASHBOARD_PASSWORD"))
    add("RADAR_DASHBOARD_PASSWORD", has_pw, "present" if has_pw else
        "missing (the dashboard login; `.venv/bin/python scripts/set_key.py RADAR_DASHBOARD_PASSWORD --generate`)",
        required=False)
    try:
        d = settings.db_path.parent.resolve()
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".doctor"
        probe.write_text("ok")
        probe.unlink()
        add("state dir", True, str(d))
    except OSError as exc:
        add("state dir", False, f"{settings.db_path.parent}: {exc}")
    if not offline:
        if settings.has("SCRAPECREATORS_API_KEY"):
            async def bal():
                async with ScrapeCreators(settings.require("SCRAPECREATORS_API_KEY"),
                                          base_url=settings.get("scrapecreators.base_url",
                                                                "https://api.scrapecreators.com")) as sc:
                    return await sc.credit_balance()
            try:
                n = asyncio.run(bal())
                add("ScrapeCreators", n > 0, f"{n} credits" + ("" if n > 0 else " (out of credits)"))
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                add("ScrapeCreators", False, f"{type(exc).__name__}: {str(exc)[:120]}")
        if settings.has("OPENROUTER_API_KEY"):
            try:
                r = httpx.get("https://openrouter.ai/api/v1/key", timeout=20,
                              headers={"Authorization": f"Bearer {settings.require('OPENROUTER_API_KEY')}"})
                add("OpenRouter", r.status_code == 200, f"HTTP {r.status_code}")
            except httpx.HTTPError as exc:
                add("OpenRouter", False, type(exc).__name__)
    if shutil.which("gws"):
        try:
            st = json.loads(subprocess.run(["gws", "auth", "status"], capture_output=True, text=True,
                                           timeout=30).stdout or "{}")
            ok = bool(st.get("encryption_valid", True)) and (st.get("encrypted_credentials_exists")
                                                            or st.get("plain_credentials_exists"))
            add("gws (Sheet export)", ok, "authenticated" if ok else "not usable: run `gws auth login`, or export "
                "with --xlsx/--csv", required=False)
        except (subprocess.SubprocessError, ValueError) as exc:
            add("gws (Sheet export)", False, type(exc).__name__, required=False)
    else:
        add("gws (Sheet export)", False, "gws not installed; export with --xlsx/--csv", required=False)
    for name, status, detail in rows:
        typer.echo(f"{status:<5} {name:<24} {detail}")
    raise typer.Exit(1 if failed else 0)


@app.command()
def leads(run_id: str = typer.Argument("latest", help="run id (comma-separate to combine passes) or 'latest'"),
          radar: Optional[str] = typer.Option(None, help="radar name/id (default: the run's radar)")):
    """List a run's leads (buyers High/Medium/Low) readably: tier, platform, date, author, need, quote, URL."""
    store = _store(config.load())
    ids = [x.strip() for x in run_id.split(",") if x.strip()]
    runs = [store.get_run(i) for i in ids]
    if not runs or any(r is None for r in runs):
        typer.echo("no such run", err=True)
        raise typer.Exit(1)
    r = store.get_radar(radar) if radar else store.get_radar(runs[0].radar_id)
    marks = ",".join("?" * len(runs))
    rows = store.conn.execute(
        "SELECT DISTINCT c.tier, c.urgency, c.need_summary, c.evidence_quote, c.decided_by, c.confidence, p.platform, "
        "p.posted_at, p.author_handle, p.url FROM classifications c "
        f"JOIN run_posts rp ON rp.post_id = c.post_id AND rp.run_id IN ({marks}) JOIN posts p ON p.id = c.post_id "
        "WHERE c.radar_id = ? AND c.role = 'buyer' AND c.tier IN ('high','medium','low') "
        "ORDER BY CASE c.tier WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, p.posted_at DESC",
        (*[x.id for x in runs], r.id)).fetchall()
    typer.echo(f"{len(rows)} leads (radar {r.name}, runs {','.join(x.id for x in runs)})")
    for i, x in enumerate(rows, 1):
        typer.echo(f"{i:>3}. [{x['tier'].upper()}] {x['platform']} {(x['posted_at'] or '')[:16]} "
                   f"@{x['author_handle'] or '?'} ({x['decided_by']} {x['confidence']:.2f}, urgency {x['urgency']})")
        typer.echo(f"     need:  {x['need_summary']}")
        typer.echo(f"     quote: \"{x['evidence_quote']}\"")
        typer.echo(f"     {x['url']}")


@app.command()
def dashboard(out: Optional[Path] = typer.Option(None, help="output file (default: <state dir>/dashboard.html)"),
              radar: Optional[str] = typer.Option(None, help="only this radar (default: all radars)"),
              artifact: bool = typer.Option(False, "--artifact",
                                            help="write the bare page body, for publishing as a claude.ai Artifact")):
    """Write a read-only HTML dashboard (leads, per-platform and per-run charts, run log) from the local store."""
    from .dashboard import build_data, render
    settings = config.load()
    try:
        data = build_data(_store(settings), settings, radar)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    path = out or Path(settings.db_path).parent / "dashboard.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data, standalone=not artifact))
    typer.echo(f"dashboard {path} radars={len(data['radars'])} runs={len(data['runs'])} leads={len(data['leads'])}")


@app.command()
def enrich(radar: str = typer.Argument(..., help="radar name or id"),
           run_id: Optional[str] = typer.Option(None, "--run", help="only leads this run found (default: all)"),
           max_subjects: Optional[int] = typer.Option(None, "--max", help="at most N subjects (RADAR_ENRICH_MAX_PER_RUN)"),
           credit_room: Optional[int] = typer.Option(None, "--credit-room",
                                                     help="ScrapeCreators credits resolution may still use today")):
    """Contact cards for High/Medium leads and direct job leads: resolve (free + ScrapeCreators), then Data Box
    `enrich` (email verified by Reacher, LinkedIn, phone for High). Prints counts only, never contact data."""
    from .databox import DataBox
    from .enrich import Limits, run_enrich
    from .scrapecreators import ScrapeCreators
    settings = config.load()
    store = _store(settings)
    r = store.get_radar(radar)
    if not r:
        typer.echo("no such radar", err=True)
        raise typer.Exit(1)
    limits = Limits()
    if max_subjects is not None:
        limits.max_subjects = max_subjects

    async def go():
        box = DataBox.from_settings(settings)
        sc = ScrapeCreators(settings.require("SCRAPECREATORS_API_KEY"),
                            base_url=settings.get("scrapecreators.base_url", "https://api.scrapecreators.com")) \
            if settings.has("SCRAPECREATORS_API_KEY") else None
        try:
            return await run_enrich(store, r.id, run_id=run_id, sc=sc, box=box, limits=limits, credit_room=credit_room)
        finally:
            if box:
                await box.aclose()
            if sc:
                await sc.aclose()
    try:
        summary = asyncio.run(go())
    except Exception as exc:  # never let a traceback (and its locals: cards, names) reach the hosted Activity log
        typer.echo(f"enrich failed: {type(exc).__name__} {getattr(exc, 'code', '')}".rstrip(), err=True)
        raise typer.Exit(1)
    if not (settings.secrets.get("DATABOX_URL") and settings.secrets.get("DATABOX_TOKEN")):
        typer.echo("note: DATABOX_URL / DATABOX_TOKEN not set; resolvable subjects were marked pending")
    typer.echo("ENRICH_SUMMARY " + json.dumps(summary, sort_keys=True))


@app.command("trigger-daily")
def trigger_daily(url: Optional[str] = typer.Option(None, help="dashboard URL (default: RADAR_APP_URL)")):
    """Ask the hosted dashboard to queue today's daily campaigns. The Railway cron service runs this once a day."""
    import os

    import httpx
    base = (url or os.environ.get("RADAR_APP_URL", "")).rstrip("/")
    token = os.environ.get("RADAR_DASHBOARD_PASSWORD", "")
    if not base or not token:
        typer.echo("set RADAR_APP_URL and RADAR_DASHBOARD_PASSWORD", err=True)
        raise typer.Exit(2)
    resp = httpx.post(f"{base}/api/run-daily", headers={"Authorization": f"Bearer {token}"}, timeout=60)
    typer.echo(f"{resp.status_code} {resp.text[:500]}")
    raise typer.Exit(0 if resp.status_code == 200 else 1)


@app.command()
def serve(port: int = typer.Option(8080, help="port to listen on"),
          host: str = typer.Option("127.0.0.1", help="127.0.0.1 = this computer only; 0.0.0.0 = your network too")):
    """Run the dashboard on this computer. Reads .env.local; open http://localhost:PORT and log in with
    RADAR_DASHBOARD_PASSWORD. While it runs, daily campaigns start at RADAR_DAILY_AT (UTC)."""
    import os

    for key, value in config._parse_env_file(config.DEFAULT_ENV_FILE).items():
        os.environ.setdefault(key, value)
    if not os.environ.get("RADAR_DASHBOARD_PASSWORD"):
        typer.echo("RADAR_DASHBOARD_PASSWORD isn't set. Run: "
                   ".venv/bin/python scripts/set_key.py RADAR_DASHBOARD_PASSWORD --generate", err=True)
        raise typer.Exit(2)
    import uvicorn

    typer.echo(f"Lead Radar dashboard: http://localhost:{port}  (Ctrl+C to stop)")
    uvicorn.run("lead_radar.server:app", host=host, port=port, workers=1)


@app.command()
def report(run_id: str = typer.Argument("latest")):
    """Show a run record: per-source fetch table, counts, cost split, leads by platform, errors."""
    settings = config.load()
    store = _store(settings)
    run = store.get_run(run_id)
    if not run:
        typer.echo("no such run", err=True)
        raise typer.Exit(1)
    typer.echo(f"run={run.id} radar={run.radar_id} started={run.started_at} finished={run.finished_at}")
    _print_sources(run)
    typer.echo("counts: " + json.dumps(run.counts))
    typer.echo(_cost_line(settings, run))
    _print_platform_leads(store, run)
    for e in run.errors:
        typer.echo(f"  - {e}")




programs_app = typer.Typer(no_args_is_help=True, help="Program lane: companies that already run an affiliate program. "
                                                       "Read-only.")
app.add_typer(programs_app, name="programs")


@programs_app.command("seed")
def programs_seed(sources: str = typer.Option("partnerstack,serp", help="comma list: partnerstack, serp"),
                  max_searches: int = typer.Option(200, help="ScrapeCreators Google searches (1 credit each)"),
                  max_pages: int = typer.Option(5, help="result pages per dork")):
    """Collect candidate companies (marketplaces, search dorks) into the program table."""
    from .programs import run as prun
    settings = config.load()
    store = _store(settings)
    rep = asyncio.run(prun.seed(settings, store, sources=[s.strip() for s in sources.split(",") if s.strip()],
                                max_searches=max_searches, max_pages=max_pages))
    for note in rep.notes:
        typer.echo(f"note: {note}")
    typer.echo(f"new companies {rep.added}, already known {rep.seen}, portals not resolved {rep.unresolved}, "
               f"searches {rep.searches}, credits {rep.credits}")


@programs_app.command("verify")
def programs_verify(limit: Optional[int] = typer.Option(None, help="check at most N companies"),
                    concurrency: int = typer.Option(16, help="sites checked in parallel"),
                    recheck_days: Optional[int] = typer.Option(None, help="also re-check rows older than N days"),
                    retry_rate_limited: bool = typer.Option(False, "--retry-rate-limited",
                                                            help="first re-queue sites that answered 429 (use a low --concurrency)")):
    """Fetch each pending company's site and decide: program (platform + evidence), no program, or unreachable."""
    from .programs import run as prun
    settings = config.load()
    store = _store(settings)
    if retry_rate_limited:
        typer.echo(f"re-queued {store.requeue_rate_limited()} rate-limited sites")

    def progress(rep):
        if rep.checked % 25 == 0:
            typer.echo(f"  checked {rep.checked}: verified {rep.verified}, no program {rep.no_program}, "
                       f"unreachable {rep.unreachable}")

    rep = asyncio.run(prun.verify(settings, store, limit=limit, concurrency=concurrency,
                                  recheck_days=recheck_days, progress=progress))
    typer.echo(f"checked {rep.checked}: verified {rep.verified}, no program {rep.no_program}, unreachable {rep.unreachable}")
    typer.echo(f"marketplace/portal listings applied to {rep.listing_upgrades} rows (see `radar programs report`)")
    typer.echo("verified by platform: " + json.dumps(dict(sorted(rep.by_platform.items(), key=lambda kv: -kv[1]))))
    typer.echo("verified by segment: " + json.dumps(rep.by_segment))


@programs_app.command("escalate")
def programs_escalate(limit: int = typer.Option(50, help="re-check at most N unreachable companies (~10-25s each)")):
    """Re-check companies that refused plain HTTP through Browser Box `read` (real browser, read-only, one at a time)."""
    from .programs import run as prun
    settings = config.load()

    def progress(rep):
        typer.echo(f"  {rep.checked}: verified {rep.verified}, no program {rep.no_program}, still unreachable {rep.unreachable}")

    rep = asyncio.run(prun.escalate(settings, _store(settings), limit=limit, progress=progress))
    typer.echo(f"checked {rep.checked}: verified {rep.verified}, no program {rep.no_program}, unreachable {rep.unreachable}")


@programs_app.command("report")
def programs_report():
    """Counts by status, platform and segment."""
    from .programs import run as prun
    settings = config.load()
    typer.echo(json.dumps(prun.summary(_store(settings)), indent=2))


@programs_app.command("growth")
def programs_growth(days: int = typer.Option(30, help="look-back window for job posts and LinkedIn posts"),
                    budget: int = typer.Option(300, help="max ScrapeCreators credits for this run"),
                    lists: str = typer.Option("affiliate,finance", help="affiliate, finance or both"),
                    steps: str = typer.Option("jobs,posts,staff,finance,profile,meta_ads,hiring_boards,finance_iso,"
                                              "linkedin_people,reddit,social_meta,social_video,community,verify",
                                              help="jobs, posts, staff, finance, profile, verify, and any source in "
                                                   "programs.sources.REGISTRY"),
                    profile_limit: int = typer.Option(400, help="known companies to profile on LinkedIn this run")):
    """Growth signals (hiring, affiliate staff, program posts) and the finance list; ranks both lists."""
    from .programs import growth
    settings = config.load()
    rep = asyncio.run(growth.run(settings, _store(settings), days=days, budget=budget,
                                 lists=tuple(x.strip() for x in lists.split(",") if x.strip()),
                                 steps=tuple(x.strip() for x in steps.split(",") if x.strip()),
                                 profile_limit=profile_limit, progress=lambda m: typer.echo(f"  {m}")))
    typer.echo(f"candidates {json.dumps(rep.candidates)}; resolved {rep.resolved} ({rep.by_homepage} by homepage), "
               f"unresolved {rep.unresolved}, new companies {rep.new_companies}")
    typer.echo(f"new signals {json.dumps(rep.new_signals)}; excluded {json.dumps(rep.excluded)}")
    if rep.sources:
        typer.echo(f"sources {json.dumps(rep.sources, sort_keys=True)}")
    typer.echo(f"GROWTH_SUMMARY credits {rep.credits}")


@programs_app.command("lookalikes")
def programs_lookalikes(budget: int = typer.Option(200, help="max ScrapeCreators credits"),
                        per_list: Optional[int] = typer.Option(None, help="top companies per list to expand")):
    """LinkedIn 'similar pages' of the top companies in each list, checked like any other company."""
    from .programs import growth
    settings = config.load()
    rep = asyncio.run(growth.lookalikes(settings, _store(settings), budget=budget, per_list=per_list))
    typer.echo(f"lookalikes {rep.candidates.get('lookalike', 0)}; resolved {rep.resolved}, new companies "
               f"{rep.new_companies}; excluded {json.dumps(rep.excluded)}; credits {rep.credits}")


@programs_app.command("lists")
def programs_lists(out_dir: Path = typer.Option(Path("~/.lead-radar/exports"), help="folder for the two CSVs"),
                   limit: Optional[int] = typer.Option(None, help="top N per list (default: all eligible)")):
    """Write the two ranked lists: affiliate-growth-top.csv and finance-top.csv."""
    from .programs import growth
    store = _store(config.load())
    growth.rescore(store)
    d = out_dir.expanduser()
    for lst, name in (("affiliate", "affiliate-growth-top.csv"), ("finance", "finance-top.csv")):
        n = growth.export_list(store, lst, d / name, limit=limit)
        typer.echo(f"{name}: {n} companies -> {d / name}")


@programs_app.command("dump")
def programs_dump(out: Path = typer.Option(..., help="JSON file for POST /api/programs/import on the hosted dashboard")):
    """Write every program row (all statuses) as JSON, to load a locally verified batch into the hosted store."""
    settings = config.load()
    store = _store(settings)
    rows = store.program_companies()
    sigs = [s for group in store.program_signals().values() for s in group]
    out.expanduser().write_text(json.dumps({"companies": rows, "signals": sigs}))
    typer.echo(f"wrote {len(rows)} rows and {len(sigs)} signals to {out}")


@programs_app.command("export")
def programs_export(out: Path = typer.Option(..., help="CSV path (lead-warehouse friendly headers)"),
                    min_score: int = typer.Option(0, help="only rows scoring at least this")):
    """Write verified companies to CSV, best first. Load into the warehouse with:
    lw ingest <csv> --source lead-radar-programs --list "Affiliate Program Cos"."""
    from .programs import run as prun
    settings = config.load()
    n = prun.export_csv(_store(settings), out.expanduser(), min_score=min_score)
    typer.echo(f"wrote {n} companies to {out}")


def main():  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
