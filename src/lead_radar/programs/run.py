"""Program lane runs: seed -> verify -> score -> export. Each step is resumable from the DB."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..listings.http import PoliteClient
from ..scrapecreators import Meter, OutOfCredits, ScrapeCreators
from . import browserbox, detect, seeds as seedmod

# Platforms a company pays for (budget for partner growth); enterprise ones signal a bigger program.
PAID = {"partnerstack", "impact", "cj", "awin", "shareasale", "refersion", "everflow", "firstpromoter", "rewardful",
        "tolt", "promotekit", "tapfiliate", "reditus", "uppromote", "goaffpro", "leaddyno", "postaffiliatepro",
        "trackdesk", "affiliatewp"}
ENTERPRISE = {"impact", "cj", "awin", "partnerstack"}


def score(row: dict) -> int:
    """0-100 fit. A verified program is the floor; budget, room to grow and hiring push it up."""
    if row.get("status") != "verified":
        return 0
    platforms = set(row.get("platforms") or [])
    s = 40
    if platforms & PAID:
        s += 15
    if platforms & ENTERPRISE:
        s += 5
    pc = row.get("partner_count")
    if pc is not None:
        s += 15 if pc < 50 else 8 if pc < 250 else 0  # few partners = room for more partners
    if row.get("hiring_signal"):
        s += 20
    if row.get("commission_text"):
        s += 5
    return min(100, s)


@dataclass
class SeedReport:
    added: int = 0
    seen: int = 0
    unresolved: int = 0
    searches: int = 0
    credits: int = 0
    notes: list[str] = field(default_factory=list)


def _http(min_interval: float = 1.0) -> PoliteClient:
    return PoliteClient(Meter(), "programs", min_interval=min_interval, timeout=20.0, retries=1)


async def seed(settings, store, *, sources: list[str], max_searches: int = 200, max_pages: int = 5) -> SeedReport:
    cfg = settings.get("programs", {}) or {}
    rep = SeedReport()
    http = _http()
    found: list[seedmod.Seed] = []
    try:
        if "partnerstack" in sources:
            try:
                ps = await seedmod.partnerstack_market(http)
                found.extend(ps)
                rep.notes.append(f"partnerstack marketplace: {len(ps)} programs")
            except Exception as exc:
                rep.notes.append(f"partnerstack marketplace failed: {type(exc).__name__}: {str(exc)[:160]}")
        if "serp" in sources:
            dorks = seedmod.dork_list(cfg)
            async with ScrapeCreators(settings.require("SCRAPECREATORS_API_KEY"), concurrency=4) as sc:
                try:
                    hits, rep.searches, notes = await seedmod.serp(sc, dorks, max_pages=max_pages, budget=max_searches)
                except OutOfCredits as exc:
                    hits, notes = [], [f"stopped: {exc}"]
                rep.credits = sc.meter.total_credits()
            found.extend(hits)
            rep.notes.extend(notes)
        fp = detect.load()
        for s in found:
            domain = s.domain
            if domain is None and s.portal_url:
                domain = await seedmod.resolve_portal(s, http, fp)
                if domain is None:
                    rep.unresolved += 1
                    continue
            domain = detect.bare_host(domain)
            if not domain or fp.is_excluded(domain):
                continue
            ref = s.ref if s.source != "serp" else (s.portal_url or s.ref)
            new = store.add_program_seed(domain, source=s.source, ref=ref, name=s.name,
                                         partner_count=s.partner_count, offer=s.offer, tags=s.tags)
            rep.added += new
            rep.seen += not new
    finally:
        await http.aclose()
    return rep


@dataclass
class VerifyReport:
    checked: int = 0
    verified: int = 0
    no_program: int = 0
    unreachable: int = 0
    by_platform: dict[str, int] = field(default_factory=dict)
    by_segment: dict[str, int] = field(default_factory=dict)
    listing_upgrades: int = 0  # rows verified or re-tagged by marketplace/portal evidence after the site check


async def verify(settings, store, *, limit: int | None = None, concurrency: int = 16,
                 recheck_days: int | None = None, progress=None) -> VerifyReport:
    domains = store.programs_to_check(limit=limit, recheck_days=recheck_days)
    rep = VerifyReport()
    rows = {r["domain"]: r for r in store.program_companies()}
    http = _http()

    save = _saver(store, rows, rep, progress)
    try:
        await detect.verify_many(domains, http, concurrency=concurrency, on_result=save)
    finally:
        await http.aclose()
    rep.listing_upgrades = apply_source_evidence(store)
    return rep


def _saver(store, rows: dict, rep: VerifyReport, progress=None):
    def save(det: detect.Detection):
        status = "verified" if det.has_program else ("no_program" if det.reachable else "unreachable")
        row = {**rows.get(det.domain, {}), "status": status, "platforms": det.platforms,
               "commission_text": det.commission_text}
        listed = any(src["source"] == "partnerstack-marketplace" for src in row.get("sources") or [])
        name = row.get("name") if listed and row.get("name") else (det.name or row.get("name"))  # marketplace names are cleanest
        store.save_program_check(det.domain, status=status, platforms=det.platforms, program_url=det.program_url,
                                 commission_text=det.commission_text, evidence=det.evidence, segment=det.segment,
                                 name=name, score=score(row), error=det.error)
        rep.checked += 1
        setattr(rep, status, getattr(rep, status) + 1)
        if status == "verified":
            for p in det.platforms:
                rep.by_platform[p] = rep.by_platform.get(p, 0) + 1
            rep.by_segment[det.segment] = rep.by_segment.get(det.segment, 0) + 1
        if progress:
            progress(rep)
    return save


async def escalate(settings, store, *, limit: int = 50, progress=None, bb=None) -> VerifyReport:
    """Re-check unreachable companies through Browser Box `read`, one at a time (the box has one browser)."""
    bb = bb or browserbox.BrowserBox.from_settings(settings)
    if bb is None:
        raise browserbox.BrowserBoxError("BROWSERBOX_URL / BROWSERBOX_TOKEN not set (.env.local)")
    rows = {r["domain"]: r for r in store.program_companies(["unreachable"])}
    rep = VerifyReport()
    save = _saver(store, rows, rep, progress)
    fp = detect.load()
    try:
        for domain in list(rows)[:limit]:
            try:
                det = await browserbox.verify(domain, bb, fp)
            except browserbox.BrowserBoxError:
                raise
            except Exception as exc:
                det = detect.Detection(domain=domain, reachable=False, error=f"browser: {type(exc).__name__}")
            save(det)
    finally:
        await bb.aclose()
    return rep


def source_platforms(row: dict, fp=None) -> list[dict]:
    """Evidence from where the company was found: a marketplace listing or a platform portal is itself proof
    that the company runs a program on that platform, even when its own site never links it."""
    fp = fp or detect.load()
    out = []
    for src in row.get("sources") or []:
        if src["source"] == "partnerstack-marketplace":
            out.append({"platform": "partnerstack", "kind": "listing", "slug": src["ref"],
                        "match": f"{seedmod.PARTNERSTACK_MARKET}{src['ref']}", "page": seedmod.PARTNERSTACK_MARKET})
        elif src["source"] == "serp":
            portal = seedmod.portal_of(src["ref"], fp)
            if portal:
                out.append({"platform": portal[0], "kind": "listing", "slug": portal[1], "match": src["ref"][:200],
                            "page": src["ref"][:200]})
    return out


def apply_source_evidence(store) -> int:
    """Merge listing evidence into every checked row; returns how many rows it changed."""
    fp = detect.load()
    changed = 0
    for row in store.program_companies(["verified", "no_program", "unreachable"]):
        listed = source_platforms(row, fp)
        if not listed:
            continue
        have = {(e["platform"], e["kind"], e.get("slug")) for e in row["evidence"]}
        new = [e for e in listed if (e["platform"], e["kind"], e["slug"]) not in have]
        platforms = sorted((set(row["platforms"]) - {"in-house"}) | {e["platform"] for e in listed})
        if not new and row["status"] == "verified" and platforms == row["platforms"]:
            continue
        row = {**row, "status": "verified", "platforms": platforms, "evidence": row["evidence"] + new}
        segment = row["segment"]
        if segment == "unknown":
            segs = {p.segment for p in fp.platforms if p.name in platforms} - {"mixed"}
            segment = segs.pop() if len(segs) == 1 else segment
        store.save_program_check(row["domain"], status="verified", platforms=platforms, program_url=row["program_url"],
                                 commission_text=row["commission_text"], evidence=row["evidence"], segment=segment,
                                 name=row["name"], score=score(row), error=None)
        changed += 1
    return changed


EXPORT_COLUMNS = ["company", "domain", "website", "category", "affiliate platform", "program url", "commission",
                  "partner count", "offer", "tags", "score", "evidence", "found via", "hiring signal", "checked at"]


def export_csv(store, path: Path, *, min_score: int = 0) -> int:
    """Verified companies, best first. Headers map onto lead-warehouse (`lw ingest`): company/domain/website/category;
    the rest land in the company's attrs."""
    rows = [r for r in store.program_companies(["verified"]) if r["score"] >= min_score]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(EXPORT_COLUMNS)
        for r in rows:
            ev = "; ".join(f"{e['platform']}:{e['kind']}:{e['match']}" for e in r["evidence"][:3])
            w.writerow([_cell(v) for v in (
                r["name"] or r["domain"], r["domain"], f"https://{r['domain']}", r["segment"], ", ".join(r["platforms"]),
                r["program_url"] or "", r["commission_text"] or "", r["partner_count"] if r["partner_count"] is not None else "",
                r["offer"] or "", ", ".join(r["tags"]), r["score"], ev,
                ", ".join(sorted({s["source"] for s in r["sources"]})), r["hiring_signal"] or "", r["checked_at"] or "")])
    return len(rows)


def _cell(v) -> str:
    """CSV formula-injection guard (same rule as the leads export)."""
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def summary(store) -> dict:
    rows = [r for r in store.program_companies() if r.get("list", "affiliate") == "affiliate"]  # finance: not site-checked
    out = {"total": len(rows), "by_status": {}, "by_platform": {}, "by_segment": {}}
    for r in rows:
        out["by_status"][r["status"]] = out["by_status"].get(r["status"], 0) + 1
        if r["status"] == "verified":
            for p in r["platforms"]:
                out["by_platform"][p] = out["by_platform"].get(p, 0) + 1
            out["by_segment"][r["segment"]] = out["by_segment"].get(r["segment"], 0) + 1
    return json.loads(json.dumps(out))
