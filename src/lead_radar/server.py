"""Hosted dashboard (Railway): stats plus campaigns.

The only writes are campaigns (radars and their briefs) and hunt jobs. Jobs run one at a time in a subprocess
(`radar hunt`), so a slow hunt never blocks the page and a crash never takes the server down. Nothing here
contacts anyone: hunts only search, and every lead is for a human to read.

Run: RADAR_DASHBOARD_PASSWORD=... uvicorn lead_radar.server:app --workers 1
Env: RADAR_STATE_DIR (volume), RADAR_DAILY_CREDIT_CAP (default 300), RADAR_DAILY_AT (default 11:00 UTC), the API keys.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from . import config, contract, expander
from .config import REPO_ROOT
from .dashboard import build_data, render
from .db import Store
from .hunt import resolve_platforms, slug
from .models import API_PLATFORMS, LISTING_PLATFORMS, Radar

COOKIE = "lr_session"
DAILY_DAYS = 2          # daily passes look back 2 days; dedupe keeps only posts not seen before
WINDOWS = (1, 2, 3, 7, 14)
LOG_MAX = 20_000
JOB_TIMEOUT = 45 * 60
ESTIMATE = "about 90–140 ScrapeCreators credits per run"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _settings():
    return config.load()


def _store(settings=None) -> Store:
    return Store.open((settings or _settings()).db_path)


# auth -------------------------------------------------------------------------------------------------------
def _password() -> str:
    return os.environ.get("RADAR_DASHBOARD_PASSWORD", "")


def _session_value(pw: str) -> str:
    return hmac.new(pw.encode(), b"lead-radar-session-v1", hashlib.sha256).hexdigest()


def authed(request: Request) -> bool:
    pw = _password()
    if not pw:
        return False
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip().encode(), pw.encode()):
        return True
    cookie = request.cookies.get(COOKIE, "")
    return bool(cookie) and hmac.compare_digest(cookie.encode(), _session_value(pw).encode())


def guarded(fn):
    async def wrapper(request: Request):
        if authed(request):
            return await fn(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "sign in first"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return wrapper


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


# queries ----------------------------------------------------------------------------------------------------
def credits_today(st: Store) -> int:
    """ScrapeCreators credits used today: hunts (runs) plus resolution during enrich jobs."""
    day = datetime.now(timezone.utc).date().isoformat()
    row = st.conn.execute("SELECT COALESCE(SUM(credits_used), 0) n FROM runs WHERE substr(started_at, 1, 10) = ?",
                          (day,)).fetchone()
    prog = st.conn.execute("SELECT COALESCE(SUM(credits_used), 0) n FROM program_runs WHERE substr(created_at, 1, 10) = ?",
                           (day,)).fetchone()
    return int(row["n"]) + st.enrich_spend_today()[1] + int(prog["n"])


def daily_cap() -> int:
    try:
        return int(os.environ.get("RADAR_DAILY_CREDIT_CAP", "300"))
    except ValueError:
        return 1000


def campaigns(st: Store) -> list[dict]:
    q = st.conn.execute
    out = []
    for r in st.list_radars():
        tiers = {row["tier"]: row["n"] for row in q(
            "SELECT tier, COUNT(*) n FROM classifications WHERE radar_id=? AND role='buyer' "
            "AND tier IN ('high','medium','low') GROUP BY tier", (r.id,))}
        runs = q("SELECT COUNT(*) n, COALESCE(SUM(credits_used),0) c, MAX(started_at) last FROM runs WHERE radar_id=?",
                 (r.id,)).fetchone()
        brief = st.get_brief(r.intent_brief_id) if r.intent_brief_id else None
        out.append({"name": r.name, "query": r.query, "offer_context": r.offer_context or "",
                    "daily": r.schedule == "daily", "active": r.active, "days": r.lookback_days,
                    "brief_version": brief.version if brief else None, "runs": runs["n"], "credits": runs["c"],
                    "last_run": runs["last"], "high": tiers.get("high", 0), "medium": tiers.get("medium", 0),
                    "low": tiers.get("low", 0), "job_boards": has_boards(r),
                    "platforms": r.platforms, "job_leads": q("SELECT COUNT(*) FROM listing_verdicts WHERE radar_id=? AND employer_type='direct' "
                                   "AND fit IN ('high','medium')", (r.id,)).fetchone()[0]})
    return out


def has_boards(radar: Radar) -> bool:
    return any(p in LISTING_PLATFORMS for p in radar.platforms)


def platforms_for(job_boards: bool) -> list[str]:
    return resolve_platforms("api,listings" if job_boards else "api")


def jobs(st: Store, limit: int = 25) -> list[dict]:
    out = []
    for j in st.conn.execute("SELECT j.*, r.name radar FROM jobs j JOIN radars r ON r.id = j.radar_id "
                             "ORDER BY j.created_at DESC LIMIT ?", (limit,)):
        run = st.conn.execute("SELECT credits_used, counts FROM runs WHERE id=?", (j["run_id"],)).fetchone() \
            if j["run_id"] else None
        counts = json.loads(run["counts"]) if run else {}
        out.append({"id": j["id"], "campaign": j["radar"], "trigger": j["trigger"], "days": j["days"], "kind": j["kind"],
                    "status": j["status"], "note": j["note"], "run_id": j["run_id"], "exit_code": j["exit_code"],
                    "created_at": j["created_at"], "started_at": j["started_at"], "finished_at": j["finished_at"],
                    "credits": run["credits_used"] if run else None, "leads": counts.get("buyers"),
                    "job_leads": counts.get("listing_leads", 0) if run else None,
                    "log": j["log"][-4000:]})
    return out


def enqueue(st: Store, radar: Radar, trigger: str, days: int, kind: str = "hunt",
            target_run_id: str | None = None) -> dict:
    busy = st.conn.execute("SELECT id FROM jobs WHERE radar_id=? AND kind=? AND status IN ('queued','running')",
                           (radar.id, kind)).fetchone()
    if busy:
        return {"id": busy["id"], "already_queued": True}
    job_id = uuid.uuid4().hex[:12]
    st.conn.execute("INSERT INTO jobs (id, radar_id, trigger, days, created_at, kind, target_run_id) "
                    "VALUES (?,?,?,?,?,?,?)", (job_id, radar.id, trigger, days, _now(), kind, target_run_id))
    WORKER.wake()
    return {"id": job_id, "already_queued": False}


_balance_cache: dict = {"at": 0.0, "value": None}


async def credit_balance(settings) -> int | None:
    if time.time() - _balance_cache["at"] < 600:
        return _balance_cache["value"]
    value = None
    with contextlib.suppress(Exception):
        from .scrapecreators import ScrapeCreators
        async with ScrapeCreators(settings.require("SCRAPECREATORS_API_KEY"),
                                  base_url=settings.get("scrapecreators.base_url", "https://api.scrapecreators.com"),
                                  retries_on_5xx=0) as sc:
            value = await sc.credit_balance()
    _balance_cache.update(at=time.time(), value=value)
    return value


# worker -----------------------------------------------------------------------------------------------------
class Worker:
    """Runs queued hunt jobs one at a time, each as `python -m lead_radar.cli hunt ...`."""

    def __init__(self):
        self.event: asyncio.Event | None = None
        self.task: asyncio.Task | None = None

    def wake(self) -> None:
        if self.event:
            self.event.set()

    def start(self) -> None:
        self.event = asyncio.Event()
        self.task = asyncio.create_task(self.loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def loop(self) -> None:
        st0 = _store()
        for table in ("jobs", "program_runs"):
            st0.conn.execute(f"UPDATE {table} SET status='interrupted', finished_at=?, "
                             "note='the server restarted during the run' WHERE status='running'", (_now(),))
        while True:
            self.event.clear()
            st = _store()  # fresh each pass: an import can replace the database file
            with contextlib.suppress(Exception):
                st.purge_contacts()  # contact cards past RADAR_CONTACT_RETENTION_DAYS
            job = st.conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not job:
                prun = st.conn.execute("SELECT * FROM program_runs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if prun:
                    try:
                        await self.execute_programs(st, prun)
                    except Exception as exc:  # keep the worker alive whatever one run does
                        st.conn.execute("UPDATE program_runs SET status='failed', finished_at=?, note=? WHERE id=?",
                                        (_now(), f"worker error: {exc}"[:300], prun["id"]))
                    continue
                await self.event.wait()
                continue
            try:
                await self.execute(st, job)
            except Exception as exc:  # keep the worker alive whatever one job does
                st.conn.execute("UPDATE jobs SET status='failed', finished_at=?, note=? WHERE id=?",
                                (_now(), f"worker error: {exc}"[:300], job["id"]))

    async def execute(self, st: Store, job) -> None:
        if job["kind"] == "enrich":
            return await self.execute_enrich(st, job)
        radar = st.get_radar(job["radar_id"])
        if not radar or not radar.intent_brief_id:
            st.conn.execute("UPDATE jobs SET status='skipped', finished_at=?, note='campaign has no search plan' "
                            "WHERE id=?", (_now(), job["id"]))
            return
        used, cap = credits_today(st), daily_cap()
        if used >= cap:
            st.conn.execute("UPDATE jobs SET status='skipped', finished_at=?, note=? WHERE id=?",
                            (_now(), f"daily credit cap reached ({used}/{cap}); runs again tomorrow", job["id"]))
            return
        started = _now()
        st.conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (started, job["id"]))
        cmd = [sys.executable, "-m", "lead_radar.cli", "hunt", radar.query, "--name", radar.name,
               "--days", str(job["days"]), "--platforms", ",".join(radar.platforms) or "api"]
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(REPO_ROOT), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        log, last_flush = "", time.time()
        try:
            async with asyncio.timeout(JOB_TIMEOUT):
                while line := await proc.stdout.readline():
                    log = (log + line.decode(errors="replace"))[-LOG_MAX:]
                    if time.time() - last_flush > 2:
                        st.conn.execute("UPDATE jobs SET log=? WHERE id=?", (log, job["id"]))
                        last_flush = time.time()
                rc = await proc.wait()
        except TimeoutError:
            proc.kill()
            rc = await proc.wait()
            log += "\n[stopped: the run took longer than 45 minutes]"
        run = st.conn.execute("SELECT id FROM runs WHERE radar_id=? AND started_at >= ? ORDER BY started_at DESC LIMIT 1",
                              (radar.id, started)).fetchone()
        status = "done" if run and rc == 0 else ("done" if run else "failed")
        note = "" if rc == 0 else f"exit {rc}; see the log"
        st.conn.execute("UPDATE jobs SET status=?, exit_code=?, run_id=?, log=?, note=?, finished_at=? WHERE id=?",
                        (status, rc, run["id"] if run else None, log, note, _now(), job["id"]))
        if run and enrich_enabled():
            enqueue(st, radar, job["trigger"], job["days"], kind="enrich", target_run_id=run["id"])

    async def execute_enrich(self, st: Store, job) -> None:
        """Contact cards for the campaign's High/Medium leads and direct job leads (Data Box `enrich`).
        The log holds counts only; contact data never leaves the contacts table."""
        radar = st.get_radar(job["radar_id"])
        room = daily_cap() - credits_today(st)
        if not radar or room <= 0:
            st.conn.execute("UPDATE jobs SET status='skipped', finished_at=?, note=? WHERE id=?",
                            (_now(), "daily credit cap reached; cards wait for tomorrow" if radar else "no campaign",
                             job["id"]))
            return
        st.conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (_now(), job["id"]))
        cmd = [sys.executable, "-m", "lead_radar.cli", "enrich", radar.name, "--credit-room", str(room)]
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(REPO_ROOT), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        try:
            async with asyncio.timeout(JOB_TIMEOUT):
                out, _ = await proc.communicate()
        except TimeoutError:
            proc.kill()
            out = (await proc.communicate())[0] or b""
        rc = proc.returncode
        text = out.decode(errors="replace")
        summary = {}
        for line in text.splitlines():
            if line.startswith("ENRICH_SUMMARY "):
                with contextlib.suppress(ValueError):
                    summary = json.loads(line.split(" ", 1)[1])
        note = enrich_note(summary) if summary else f"exit {rc}; see the log"
        st.conn.execute("UPDATE jobs SET status=?, exit_code=?, log=?, note=?, credits_used=?, cost_usd=?, "
                        "finished_at=? WHERE id=?",
                        ("done" if rc == 0 and summary else "failed", rc, text[-LOG_MAX:], note,
                         int(summary.get("resolve_credits", 0)), float(summary.get("cost_usd", 0)), _now(), job["id"]))


    async def execute_programs(self, st: Store, run) -> None:
        """Program lane: seed new candidate companies, then verify every pending one (`radar programs ...`)."""
        room = daily_cap() - credits_today(st)
        searches = min(int(run["max_searches"]), max(room, 0))
        if searches <= 0:
            st.conn.execute("UPDATE program_runs SET status='skipped', finished_at=?, note=? WHERE id=?",
                            (_now(), "daily credit cap reached; try again tomorrow", run["id"]))
            return
        st.conn.execute("UPDATE program_runs SET status='running', started_at=? WHERE id=?", (_now(), run["id"]))
        log, rc, credits = "", 0, 0
        if run["kind"] == "growth":  # daily: fresh job posts and LinkedIn posts for both lists, then rank
            days = PROGRAM_WEEKLY_DAYS if datetime.now(timezone.utc).weekday() == 0 else PROGRAM_DAILY_DAYS
            steps = [["growth", "--days", str(days), "--budget", str(searches),
                      "--steps", PROGRAM_DAILY_STEPS, "--profile-limit", "0"]]
        else:
            steps = [["seed", "--max-searches", str(searches)], ["verify", "--concurrency", "16"]]
        for args in steps:
            cmd = [sys.executable, "-m", "lead_radar.cli", "programs", *args]
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(REPO_ROOT), stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
            last_flush = time.time()
            try:
                async with asyncio.timeout(JOB_TIMEOUT):
                    while line := await proc.stdout.readline():
                        text = line.decode(errors="replace")
                        log = (log + text)[-LOG_MAX:]
                        if (text.startswith("new companies") or text.startswith("GROWTH_SUMMARY")) and "credits" in text:
                            with contextlib.suppress(ValueError):
                                credits = int(text.rsplit("credits", 1)[1].strip())
                        if time.time() - last_flush > 2:
                            st.conn.execute("UPDATE program_runs SET log=?, credits_used=? WHERE id=?",
                                            (log, credits, run["id"]))
                            last_flush = time.time()
                    rc = await proc.wait()
            except TimeoutError:
                proc.kill()
                rc = await proc.wait()
                log += "\n[stopped: this step took longer than 45 minutes]"
            if rc != 0:
                break
        last = [line for line in log.splitlines() if line.startswith(("checked ", "new signals"))]
        note = last[-1] if rc == 0 and last else ("" if rc == 0 else f"exit {rc}; see the log")
        st.conn.execute("UPDATE program_runs SET status=?, exit_code=?, log=?, note=?, credits_used=?, finished_at=? "
                        "WHERE id=?", ("done" if rc == 0 else "failed", rc, log, note, credits, _now(), run["id"]))


WORKER = Worker()


def enrich_enabled() -> bool:
    return os.environ.get("RADAR_ENRICH", "on").lower() not in ("0", "off", "false", "no")


def enrich_note(s: dict) -> str:
    bits = [f"{s.get('enriched', 0)} cards", f"{s.get('deliverable', 0)} verified",
            f"{s.get('accept_all', 0)} accept-all", f"{s.get('unresolved', 0)} unresolvable",
            f"{s.get('deferred', 0)} waiting", f"${float(s.get('cost_usd', 0)):.3f}"]
    if s.get("stopped"):
        bits.append("stopped: " + s["stopped"].replace("_", " "))
    return " · ".join(bits)


def short_name(text: str, limit: int = 32) -> str:
    """A campaign name from the request, cut at a word boundary: 'a bookkeeper for my small business in NZ'
    -> 'a-bookkeeper-for-my-small'."""
    name = slug(text)
    if len(name) <= limit:
        return name
    cut = name[:limit + 1].rsplit("-", 1)[0]
    return cut or name[:limit]


# pages ------------------------------------------------------------------------------------------------------
async def health(request: Request):
    return PlainTextResponse("ok")


async def login_page(request: Request):
    if authed(request):
        return RedirectResponse("/app", status_code=303)
    return HTMLResponse(LOGIN_HTML)


async def api_login(request: Request):
    pw = _password()
    try:
        given = str((await request.json()).get("password", ""))
    except Exception:
        given = ""
    if not pw or not hmac.compare_digest(given.encode(), pw.encode()):
        await asyncio.sleep(1)
        return _err("That password is not right.", 401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, _session_value(pw), max_age=60 * 60 * 24 * 30, httponly=True,
                    secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
                    samesite="lax")
    return resp


async def api_logout(request: Request):
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@guarded
async def index(request: Request):
    return HTMLResponse(render(None, standalone=True, live=True))


@guarded
async def api_data(request: Request):
    settings = _settings()
    st = _store(settings)
    data = build_data(st, settings, include_contacts=True)
    data["campaigns"] = campaigns(st)
    data["jobs"] = jobs(st)
    data["credits"] = {"today": credits_today(st), "cap": daily_cap(), "balance": await credit_balance(settings),
                       "estimate": ESTIMATE, "daily_days": DAILY_DAYS}
    from .enrich import Limits
    lim = Limits()
    data["enrich"] = {"on": enrich_enabled(), "configured": bool(settings.secrets.get("DATABOX_URL")
                                                                  and settings.secrets.get("DATABOX_TOKEN")),
                      "today_usd": round(st.enrich_spend_today()[0], 4), "daily_usd": lim.daily_usd,
                      "max_per_run": lim.max_subjects, "retention_days": lim.retention_days}
    return JSONResponse(data)


EXPORT_COLUMNS = ["kind", "campaign", "tier", "platform", "posted_at", "url", "need", "quote", "role", "company_listed",
                  "contact_status", "name", "company", "email", "email_verdict", "phone", "linkedin_url", "website"]


@guarded
async def api_export(request: Request):
    """Leads and job leads with their contact cards as CSV, streamed (never written to the volume)."""
    import csv
    import io
    settings = _settings()
    st = _store(settings)
    radar = request.query_params.get("campaign") or None
    try:
        data = build_data(st, settings, radar, include_contacts=True)
    except ValueError as exc:
        return _err(str(exc), 404)
    contacts = data.get("contacts", {})

    def contact_cols(key):
        c = contacts.get(key) or {}
        f = c.get("fields", {})
        return [c.get("badge", ""), *[(f.get(k) or {}).get("value", "") for k in ("name", "company", "email")],
                (f.get("email") or {}).get("verdict", ""),
                *[(f.get(k) or {}).get("value", "") for k in ("phone", "linkedin_url", "website")]]

    def safe(row):  # untrusted text that starts like a formula would run in Sheets/Excel
        return [("'" + v) if isinstance(v, str) and v and v[0] in "=+-@\t\r" else v for v in row]

    def rows():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(EXPORT_COLUMNS)
        for l in data["leads"]:
            w.writerow(safe(["lead", l["radar"], l["tier"], l["platform"], l["posted_at"], l["url"], l["need"], l["quote"],
                        "", "", *contact_cols(l.get("contact"))]))
        for l in data["listings"]:
            w.writerow(safe(["job", l["radar"], l["fit"], l["board"], l["posted_at"], l["url"], l["need"], l["quote"],
                             l["role"], l["company"], *contact_cols(l.get("contact"))]))
        yield buf.getvalue()
    from starlette.responses import StreamingResponse
    name = f"lead-radar-{radar or 'all'}-{datetime.now(timezone.utc):%Y%m%d}.csv"
    return StreamingResponse(rows(), media_type="text/csv",
                             headers={"content-disposition": f'attachment; filename="{name}"',
                                      "cache-control": "no-store"})


@guarded
async def api_jobs(request: Request):
    st = _store()
    return JSONResponse({"jobs": jobs(st), "credits": {"today": credits_today(st), "cap": daily_cap()}})


@guarded
async def api_draft(request: Request):
    body = await request.json()
    offer = " ".join(str(body.get("offer", "")).split())
    context = " ".join(str(body.get("offer_context", "")).split()) or None
    if len(offer) < 3:
        return _err("Describe what people are asking for (a few words at least).")
    try:
        brief, _ = await expander.expand(_settings(), "draft", offer, context)
    except config.MissingSecret as exc:
        return _err(f"Missing key on the server: {exc}", 500)
    except Exception as exc:
        return _err(f"Couldn't draft a search plan: {exc}", 502)
    return JSONResponse({"brief": brief.model_dump(exclude={"id", "radar_id", "version"}),
                         "suggested_name": short_name(offer)})


@guarded
async def api_save_campaign(request: Request):
    body = await request.json()
    settings = _settings()
    st = _store(settings)
    is_new = bool(body.get("new"))
    offer = " ".join(str(body.get("offer", "")).split())
    name = slug(str(body.get("name") or offer))
    existing = st.get_radar(name)
    if is_new and existing:
        return _err(f"A campaign called '{name}' already exists. Pick another name.", 409)
    if not is_new and not existing:
        return _err(f"No campaign called '{name}'.", 404)
    try:
        days = int(body.get("days", 3))
    except (TypeError, ValueError):
        days = 0
    if days not in WINDOWS:
        return _err(f"Window must be one of {WINDOWS} days.")
    brief_body = body.get("brief")
    if not isinstance(brief_body, dict):
        return _err("The search plan is missing.")
    try:
        contract.validate_intent_brief(brief_body)
    except contract.ContractError as exc:
        return _err(f"The search plan isn't valid: {exc}")
    if is_new and len(offer) < 3:
        return _err("Describe what people are asking for.")
    boards = bool(body["job_boards"]) if "job_boards" in body else bool(existing and has_boards(existing))
    if boards and not [t for t in (brief_body.get("terms") or {}).get("onlinejobs", []) if str(t).strip()]:
        return _err("Job boards are on, but the plan has no job titles. Add a few under onlinejobs "
                    "(e.g. virtual assistant), or turn job boards off.")
    selected = body.get("platforms", existing.platforms if existing else list(API_PLATFORMS))
    if not isinstance(selected, list) or not selected or any(
        p not in (*API_PLATFORMS, *LISTING_PLATFORMS) for p in selected
    ):
        return _err("Platforms must be a nonempty list of supported search sources.")
    selected = list(dict.fromkeys(p for p in selected if p not in LISTING_PLATFORMS))
    if boards:
        selected += list(LISTING_PLATFORMS)
    if not selected:
        return _err("Select at least one search source.")
    radar = existing or Radar(id=uuid.uuid4().hex[:12], name=name, query=offer,
                              offer_context=" ".join(str(body.get("offer_context", "")).split()) or None,
                              platforms=platforms_for(boards))
    radar.platforms = selected
    radar.lookback_days = days
    radar.schedule = "daily" if body.get("daily") else None
    prev = st.get_brief(radar.intent_brief_id) if radar.intent_brief_id else None
    version = prev.version + 1 if prev else 1
    path = settings.state_dir / "briefs" / f"{name}-v{version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(brief_body, indent=2, ensure_ascii=False))
    try:
        brief = expander.load_brief_file(path, radar.id, version)
    except Exception as exc:
        return _err(f"The search plan isn't valid: {exc}")
    st.save_radar(radar)
    st.save_brief(brief)
    radar.intent_brief_id = brief.id
    st.save_radar(radar)
    job = enqueue(st, radar, "manual", days) if body.get("run_now") else None
    return JSONResponse({"name": name, "brief_version": version, "job": job})


EXAMPLES_DIR = REPO_ROOT / "config" / "examples"


def seed_examples(st: Store, settings=None) -> list[str]:
    """First start: add the example campaigns from config/examples, not daily, so a new install isn't empty.
    Runs once per state folder and only into an empty one: deleting or editing an example never brings it back,
    and an existing install never gets them. RADAR_EXAMPLES=off skips it."""
    settings = settings or _settings()
    marker = settings.state_dir / "examples-seeded.txt"
    if os.environ.get("RADAR_EXAMPLES", "on").strip().lower() == "off" or marker.exists():
        return []
    added = []
    if not st.list_radars():
        examples = [json.loads(p.read_text()) for p in EXAMPLES_DIR.glob("*.json")]
        for ex in sorted(examples, key=lambda e: e.get("order", 99)):
            contract.validate_intent_brief(ex["brief"])
            name, boards = slug(ex["name"]), bool(ex.get("job_boards"))
            radar = Radar(id=uuid.uuid4().hex[:12], name=name, query=ex["offer"],
                          offer_context=ex.get("offer_context") or None, platforms=platforms_for(boards))
            radar.platforms = [p for p in ex.get("platforms") or API_PLATFORMS if p in API_PLATFORMS]
            radar.platforms += list(LISTING_PLATFORMS) if boards else []
            radar.lookback_days = int(ex.get("days", 3))
            radar.schedule = None
            path = settings.state_dir / "briefs" / f"{name}-v1.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(ex["brief"], indent=2, ensure_ascii=False))
            brief = expander.load_brief_file(path, radar.id, 1)
            st.save_radar(radar)
            st.save_brief(brief)
            radar.intent_brief_id = brief.id
            st.save_radar(radar)
            added.append(name)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(", ".join(added) or "none (the install already had campaigns)")
    return added


@guarded
async def api_campaign(request: Request):
    name = request.path_params["name"]
    st = _store()
    radar = st.get_radar(name)
    if not radar:
        return _err(f"No campaign called '{name}'.", 404)
    if request.method == "GET":
        brief = st.get_brief(radar.intent_brief_id) if radar.intent_brief_id else None
        return JSONResponse({"name": radar.name, "offer": radar.query, "offer_context": radar.offer_context or "",
                             "days": radar.lookback_days, "daily": radar.schedule == "daily", "active": radar.active,
                             "job_boards": has_boards(radar), "platforms": radar.platforms,
                             "brief": brief.model_dump(exclude={"id", "radar_id", "version"}) if brief else None})
    body = await request.json()
    if "daily" in body:
        radar.schedule = "daily" if body["daily"] else None
    if "active" in body:
        radar.active = bool(body["active"])
    st.save_radar(radar)
    return JSONResponse({"name": radar.name, "daily": radar.schedule == "daily", "active": radar.active})


@guarded
async def api_run(request: Request):
    st = _store()
    radar = st.get_radar(request.path_params["name"])
    if not radar or not radar.intent_brief_id:
        return _err("No such campaign, or it has no search plan yet.", 404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    days = int(body.get("days") or radar.lookback_days)
    if days not in WINDOWS:
        return _err(f"Window must be one of {WINDOWS} days.")
    return JSONResponse({"job": enqueue(st, radar, "manual", days)})


def daily_at() -> tuple[int, int] | None:
    """When the server queues the daily campaigns by itself: RADAR_DAILY_AT as HH:MM in UTC (default 11:00).
    `off` (or an unreadable value) turns the built-in clock off; POST /api/run-daily still works."""
    raw = os.environ.get("RADAR_DAILY_AT", "11:00").strip().lower()
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _daily_marker() -> Path:
    return _settings().state_dir / "daily-last-run.txt"


def queue_daily(st: Store, day: str | None = None) -> list[dict]:
    """Queue every active daily campaign (and the program lane's growth run when it's on), and note the day (UTC)."""
    queued = []
    for radar in st.list_radars():
        if radar.schedule == "daily" and radar.active and radar.intent_brief_id:
            queued.append({"campaign": radar.name, **enqueue(st, radar, "daily", DAILY_DAYS)})
    if programs_daily_enabled():
        queued.append({"campaign": "affiliate-programs (growth signals)",
                       **enqueue_program_run(st, "daily", "growth", programs_daily_credits())})
    with contextlib.suppress(OSError):
        _daily_marker().write_text(day or datetime.now(timezone.utc).date().isoformat())
    return queued


class DailyClock:
    """Queues the daily campaigns once a day at RADAR_DAILY_AT (UTC) while the server is up, so no outside cron is
    needed. A server that was down (or a laptop that was asleep) at that time catches up when it next runs that
    day. A day already started, here or through POST /api/run-daily, is not started twice."""

    def __init__(self):
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def loop(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                self.tick(datetime.now(timezone.utc))
            await asyncio.sleep(60)

    def tick(self, now: datetime) -> list[dict] | None:
        at = daily_at()
        if at is None or (now.hour, now.minute) < at:
            return None
        try:
            last = _daily_marker().read_text().strip()
        except OSError:
            last = ""
        if last == now.date().isoformat():
            return None
        return queue_daily(_store(), now.date().isoformat())


DAILY = DailyClock()


@guarded
async def api_run_daily(request: Request):
    return JSONResponse({"queued": queue_daily(_store())})


@guarded
async def api_import(request: Request):
    """One-time move of an existing radar.sqlite into an empty server store (raw file as the request body)."""
    settings = _settings()
    st = _store(settings)
    if st.conn.execute("SELECT COUNT(*) n FROM runs").fetchone()["n"] and request.query_params.get("force") != "1":
        return _err("The server already has runs; import only works into an empty store.", 409)
    data = await request.body()
    if not data.startswith(b"SQLite format 3\x00"):
        return _err("That isn't a SQLite database file.")
    db_path = Path(settings.db_path)
    tmp = db_path.with_suffix(".import")
    tmp.write_bytes(data)
    try:
        check = sqlite3.connect(tmp)
        ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        has = check.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('radars','runs','posts')").fetchone()[0]
        check.close()
    except sqlite3.DatabaseError as exc:
        tmp.unlink(missing_ok=True)
        return _err(f"Unreadable database: {exc}")
    if not ok or has != 3:
        tmp.unlink(missing_ok=True)
        return _err("That database failed its integrity check or isn't a radar store.")
    st.conn.close()
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    os.replace(tmp, db_path)
    st = _store(settings)  # applies any newer migrations (e.g. jobs)
    n = {t: st.conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"] for t in ("radars", "runs", "posts")}
    return JSONResponse({"imported": n})


# program lane (companies that already run an affiliate program) --------------------------------------------
PROGRAM_DAILY_DAYS = 3
PROGRAM_WEEKLY_DAYS = 14  # Mondays look back two weeks, which also runs weekly-only sources (min_days in config)
# The daily growth run: LinkedIn jobs/posts/funders, then every platform source (programs.sources.REGISTRY), then
# the free program check on new companies.
PROGRAM_DAILY_STEPS = ("jobs,posts,finance,meta_ads,hiring_boards,finance_iso,linkedin_people,reddit,social_meta,"
                       "social_video,community,verify")   # daily growth runs look back a few days; signals are stored once, so overlap is free


def programs_daily_enabled() -> bool:
    return os.environ.get("RADAR_PROGRAMS_DAILY", "off").lower() not in ("0", "off", "false", "no")


def programs_daily_credits() -> int:
    try:
        return int(os.environ.get("RADAR_PROGRAMS_DAILY_CREDITS", "300"))
    except ValueError:
        return 300


def enqueue_program_run(st: Store, trigger: str, kind: str, max_searches: int) -> dict:
    busy = st.conn.execute("SELECT id FROM program_runs WHERE status IN ('queued','running')").fetchone()
    if busy:
        return {"id": busy["id"], "already_queued": True}
    run_id = uuid.uuid4().hex[:12]
    st.conn.execute("INSERT INTO program_runs (id, trigger, kind, max_searches, created_at) VALUES (?,?,?,?,?)",
                    (run_id, trigger, kind, max_searches, _now()))
    WORKER.wake()
    return {"id": run_id, "already_queued": False}


def lead_payload(r: dict) -> dict:
    """One list entry for the dashboard/API: company facts, score, why now, dated signals with links."""
    from .programs import signals as sg
    return {"domain": r["domain"], "name": sg.clean_name(r["name"], r["domain"]), "list": r["list"], "score": r["lead_score"],
            "why": sg.why(r["signals"]), "linkedin_url": r.get("linkedin_url"), "hq": r.get("hq"),
            "size": r.get("size"), "industry": r.get("industry"), "platforms": r["platforms"],
            "program_url": r["program_url"], "commission_text": r["commission_text"],
            "partner_count": r["partner_count"], "tags": r["tags"],
            "first_seen": r["first_seen"],
            "last_signal": max((s["first_seen"] for s in r["signals"]), default=None),
            "signals": [{k: s[k] for k in ("kind", "url", "quote", "signal_date", "first_seen", "person_name",
                                           "person_url")} | {"platform": s.get("platform") or "linkedin"}
                        for s in r["signals"][:6]]}


PROGRAM_FIELDS = ("domain", "name", "platforms", "program_url", "commission_text", "segment", "tags", "partner_count",
                  "offer", "score", "hiring_signal", "checked_at", "first_seen")


def program_runs(st: Store, limit: int = 10) -> list[dict]:
    return [dict(r) | {"log": r["log"][-4000:]} for r in
            st.conn.execute("SELECT * FROM program_runs ORDER BY created_at DESC LIMIT ?", (limit,))]


@guarded
async def api_programs(request: Request):
    from .programs import run as prun
    st = _store()
    companies = [{k: r[k] for k in PROGRAM_FIELDS} | {"found_via": sorted({s["source"] for s in r["sources"]}),
                                                     "evidence": r["evidence"][:3]}
                 for r in st.program_companies(["verified"])]
    from .programs import growth
    growth.rescore(st)
    today = datetime.now(timezone.utc).date().isoformat()
    lists = {}
    for lst in ("affiliate", "finance"):
        rows = growth.top_list(st, lst)
        lists[lst] = {"total": len(rows), "new_today": sum(1 for r in rows if growth.is_new(r, r["signals"], today)),
                      "leads": [lead_payload(r) for r in rows[:150]]}
    return JSONResponse({"companies": companies, "summary": prun.summary(st), "runs": program_runs(st),
                         "lists": lists, "daily": {"on": programs_daily_enabled(), "credits": programs_daily_credits()},
                         "credits": {"today": credits_today(st), "cap": daily_cap()}})


@guarded
async def api_programs_export(request: Request):
    """Verified companies as CSV (lead-warehouse headers); built in a temp dir, never kept on the volume."""
    import tempfile
    from .programs import run as prun
    try:
        min_score = int(request.query_params.get("min_score") or 0)
    except ValueError:
        min_score = 0
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "programs.csv"
        prun.export_csv(_store(), path, min_score=min_score)
        body = path.read_bytes()
    name = f"affiliate-programs-{datetime.now(timezone.utc):%Y%m%d}.csv"
    return Response(body, media_type="text/csv",
                    headers={"content-disposition": f'attachment; filename="{name}"', "cache-control": "no-store"})


@guarded
async def api_programs_run(request: Request):
    """Queue a program-lane run: 'discover' (seed + verify) or 'growth' (signals for both lists), within the cap."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = body.get("kind") or "discover"
    if kind not in ("discover", "growth"):
        return _err("kind must be discover or growth.")
    try:
        searches = max(1, min(int(body.get("max_searches") or 200), 1000))
    except (TypeError, ValueError):
        return _err("max_searches must be a number.")
    return JSONResponse({"run": enqueue_program_run(_store(), "manual", kind, searches)})


@guarded
async def api_programs_leads(request: Request):
    """The two ranked lists for agents: ?list=affiliate|finance&since=YYYY-MM-DD&min_score=N&limit=N.
    affiliate = verified program + at least one dated signal; finance = every eligible business-funding company.
    `since` keeps companies with a signal first seen on/after that date (new leads)."""
    from .programs import growth
    q = request.query_params
    lst = q.get("list") or "affiliate"
    if lst not in ("affiliate", "finance"):
        return _err("list must be affiliate or finance.")
    try:
        min_score = int(q.get("min_score") or 1)
        limit = int(q.get("limit") or 200)
    except ValueError:
        return _err("min_score and limit must be numbers.")
    since = q.get("since") or None
    if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        return _err("since must be YYYY-MM-DD.")
    st = _store()
    growth.rescore(st)
    rows = growth.top_list(st, lst, min_score=min_score, since=since)
    return JSONResponse({"list": lst, "total": len(rows), "leads": [lead_payload(r) for r in rows[:max(1, limit)]]})


@guarded
async def api_programs_leads_csv(request: Request):
    """One ranked list as CSV (?list=affiliate|finance), built in a temp dir, never kept on the volume."""
    import tempfile
    from .programs import growth
    lst = request.query_params.get("list") or "affiliate"
    if lst not in ("affiliate", "finance"):
        return _err("list must be affiliate or finance.")
    st = _store()
    growth.rescore(st)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "list.csv"
        growth.export_list(st, lst, path)
        body = path.read_bytes()
    name = f"{'affiliate-growth' if lst == 'affiliate' else 'finance'}-top-{datetime.now(timezone.utc):%Y%m%d}.csv"
    return Response(body, media_type="text/csv",
                    headers={"content-disposition": f'attachment; filename="{name}"', "cache-control": "no-store"})


@guarded
async def api_programs_import(request: Request):
    """Load program rows verified elsewhere: JSON {"companies": [...], "signals": [...]} as `radar programs dump`
    writes it. Companies first, so each signal's company exists."""
    try:
        body = await request.json()
    except Exception:
        body = None
    rows = body.get("companies") if isinstance(body, dict) else None
    sigs = (body.get("signals") or []) if isinstance(body, dict) else []
    if not isinstance(rows, list) or not isinstance(sigs, list):
        return _err('Send JSON: {"companies": [...], "signals": [...]}')
    st = _store()
    n = st.import_program_rows(r for r in rows if isinstance(r, dict))
    return JSONResponse({"imported": n, "signals": st.import_program_signals(s for s in sigs if isinstance(s, dict))})


@contextlib.asynccontextmanager
async def lifespan(app):
    try:
        seed_examples(_store())
    except Exception as exc:  # noqa: BLE001 - a bad example file must not stop the dashboard
        print(f"example campaigns not added: {exc}", file=sys.stderr)
    WORKER.start()
    DAILY.start()
    yield
    await DAILY.stop()
    await WORKER.stop()


routes = [
    Route("/health", health),
    Route("/login", login_page),
    Route("/api/login", api_login, methods=["POST"]),
    Route("/api/logout", api_logout, methods=["POST"]),
    Route("/", index),
    Route("/app", index),
    Route("/api/data", api_data),
    Route("/api/jobs", api_jobs),
    Route("/api/export.csv", api_export),
    Route("/api/campaigns/draft", api_draft, methods=["POST"]),
    Route("/api/campaigns", api_save_campaign, methods=["POST"]),
    Route("/api/campaigns/{name}", api_campaign, methods=["GET", "PATCH"]),
    Route("/api/campaigns/{name}/run", api_run, methods=["POST"]),
    Route("/api/run-daily", api_run_daily, methods=["POST"]),
    Route("/api/admin/import", api_import, methods=["POST"]),
    Route("/api/programs", api_programs),
    Route("/api/programs.csv", api_programs_export),
    Route("/api/programs/run", api_programs_run, methods=["POST"]),
    Route("/api/programs/import", api_programs_import, methods=["POST"]),
    Route("/api/programs/leads", api_programs_leads),
    Route("/api/programs/leads.csv", api_programs_leads_csv),
]
app = Starlette(routes=routes, lifespan=lifespan)


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Lead Radar · Sign in</title>
<style>
:root{--bg:#eef1f4;--surface:#fff;--ink:#15202b;--muted:#586674;--line:#d6dde4;--accent:#1d5aa6;--bad:#a8321e}
@media (prefers-color-scheme:dark){:root{--bg:#0e151d;--surface:#151f2a;--ink:#e4eaf0;--muted:#93a2b1;--line:#253241;
--accent:#7eaaf0;--bad:#f08a76;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
display:grid;place-items:center;min-height:100vh;padding-inline:16px}
form{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:24px;display:grid;gap:12px;
width:min(100%,340px)}
h1{margin:0;font-size:1.4rem}p{margin:0;color:var(--muted);font-size:.9rem}
input{font:inherit;padding:9px 11px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
button{font:inherit;font-weight:600;padding:9px;border:0;border-radius:6px;background:var(--accent);color:var(--surface);
cursor:pointer}#err{color:var(--bad);min-height:1.2em}
</style></head><body>
<form id="f"><h1>Lead Radar</h1><p>Enter the dashboard password.</p>
<label for="pw" style="position:absolute;left:-9999px">Password</label>
<input id="pw" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Sign in</button><p id="err" role="alert"></p></form>
<script>
document.getElementById("f").addEventListener("submit", async e => {
  e.preventDefault();
  const r = await fetch("/api/login", {method: "POST", headers: {"content-type": "application/json"},
    body: JSON.stringify({password: document.getElementById("pw").value})});
  if (r.ok) location.href = "/app"; else document.getElementById("err").textContent = (await r.json()).error;
});
</script></body></html>"""
