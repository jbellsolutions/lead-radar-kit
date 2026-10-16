"""Enrichment pass: High/Medium buyer leads and direct job leads -> resolution -> Data Box `enrich` -> contact card.

Order: High before Medium, newest first. Each subject (person or company) is enriched once across campaigns; a fresh
card, an unresolvable subject or 3 failed attempts are skipped. Spend is capped three ways: RADAR_ENRICH_MAX_PER_RUN
subjects, RADAR_ENRICH_DAILY_USD (Lead Radar's own daily budget) and Data Box's standing enrich pool. When a budget
runs out the remaining subjects are marked `deferred` and the next daily pass retries them.

Nothing here logs contact data: the summary and every error carry counts and codes only.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from . import resolve as rs
from .databox import DataBox, DataBoxError
from .db import Store
from .models import Post

FRESH_DAYS = 30
MAX_ATTEMPTS = 3
CARD_FIELDS = ("name", "company", "domain", "website", "phone", "linkedin_url")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


@dataclass
class Limits:
    max_subjects: int = field(default_factory=lambda: _env_int("RADAR_ENRICH_MAX_PER_RUN", 3))
    daily_usd: float = field(default_factory=lambda: _env_float("RADAR_ENRICH_DAILY_USD", 3.0))
    per_lead_usd: float = field(default_factory=lambda: _env_float("RADAR_ENRICH_PER_LEAD_USD", 0.25))
    retention_days: int = field(default_factory=lambda: _env_int("RADAR_CONTACT_RETENTION_DAYS", 90))
    resolve_credits: int = 4


@dataclass
class Target:
    subject: str
    tier: str              # high | medium (lead tier or listing fit)
    posts: list[Post]
    links: list[tuple[str, str, str, str]]  # (post_id, radar_id, kind, tier)
    verdict: dict | None = None             # listing verdict, for job posts


def targets(store: Store, radar_id: str, run_id: str | None = None) -> list[Target]:
    """Subjects worth enriching for this radar (optionally only what `run_id` found), High first then newest."""
    q = store.conn.execute
    rows = []
    run_clause = " AND COALESCE(cq.run_id, p.run_id) = ?" if run_id else ""
    for r in q("SELECT c.post_id, c.tier FROM classifications c JOIN posts p ON p.id = c.post_id "
               "LEFT JOIN classification_queue cq ON cq.post_id = c.post_id AND cq.radar_id = c.radar_id "
               f"WHERE c.radar_id=? AND c.role='buyer' AND c.tier IN ('high','medium'){run_clause}",
               (radar_id, run_id) if run_id else (radar_id,)):
        rows.append((r["post_id"], r["tier"], "lead", None))
    vrun = " AND run_id = ?" if run_id else ""
    for v in q(f"SELECT * FROM listing_verdicts WHERE radar_id=? AND employer_type='direct' "
               f"AND fit IN ('high','medium'){vrun}", (radar_id, run_id) if run_id else (radar_id,)):
        rows.append((v["post_id"], v["fit"], "listing", dict(v)))
    by_subject: dict[str, Target] = {}
    unkeyed = 0
    for pid, tier, kind, verdict in rows:
        post = store.get_post(pid)
        if not post:
            continue
        key = rs.subject_key(post)
        if not key:
            unkeyed += 1
            continue
        t = by_subject.setdefault(key, Target(key, tier, [], [], verdict))
        t.posts.append(post)
        t.links.append((pid, radar_id, kind, tier))
        if tier == "high":
            t.tier = "high"
    out = list(by_subject.values())
    for t in out:
        t.posts.sort(key=lambda p: p.posted_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    out.sort(key=lambda t: (t.tier != "high", -(t.posts[0].posted_at.timestamp() if t.posts[0].posted_at else 0)))
    return out


def skip_reason(card: dict | None, now: datetime) -> str | None:
    if not card:
        return None
    updated = datetime.fromisoformat(card["updated_at"])
    if card["status"] == "enriched" and updated > now - timedelta(days=FRESH_DAYS):
        return "fresh"
    if card["status"] == "unresolved" and updated > now - timedelta(days=FRESH_DAYS):
        return "unresolved"
    if card["attempts"] >= MAX_ATTEMPTS and card["status"] in ("failed", "deferred"):
        return "attempts"
    return None


def _value(f):
    return f.get("value") if isinstance(f, dict) else f


def card_fields(body: dict) -> dict:
    """Flatten a Data Box card into contact columns (values only; the full card is kept separately)."""
    card = body.get("card") or {}
    out = {k: _value(card.get(k)) for k in CARD_FIELDS}
    email = card.get("email") or {}
    out["email"] = _value(email)
    out["email_verdict"] = (email.get("verdict") if isinstance(email, dict) else None) or body.get("verdict")
    out["alt_emails"] = [a.get("value") for a in card.get("alt_emails") or [] if isinstance(a, dict) and a.get("value")]
    out["socials"] = {k: _value(v) for k, v in (card.get("socials") or {}).items() if _value(v)}
    return out


def request_id(subject: str, attempt: int) -> str:
    return "lr-" + hashlib.sha256(f"{subject}#{attempt}".encode()).hexdigest()[:20]


async def run_enrich(store: Store, radar_id: str, *, run_id: str | None = None, sc=None, box: DataBox | None = None,
                     limits: Limits | None = None, credit_room: int | None = None,
                     now: datetime | None = None) -> dict:
    """Enrich this radar's targets. Returns counts only (safe to log)."""
    limits = limits or Limits()
    now = now or datetime.now(timezone.utc)
    spent_usd, _ = store.enrich_spend_today()
    budget = max(0.0, limits.daily_usd - spent_usd)
    s = {"targets": 0, "skipped_fresh": 0, "skipped_unresolved": 0, "skipped_attempts": 0, "unresolved": 0,
         "enriched": 0, "with_email": 0, "deliverable": 0, "accept_all": 0, "deferred": 0, "failed": 0,
         "resolve_credits": 0, "cost_usd": 0.0, "stopped": ""}
    todo = targets(store, radar_id, run_id)
    s["targets"] = len(todo)
    done = 0
    stop = ""
    for t in todo:
        card = store.get_contact(t.subject)
        skip = skip_reason(card, now)
        for link in t.links:
            if card:
                store.link_contact(t.subject, *link)
        if skip:
            s["skipped_" + skip] += 1
            continue
        if not stop and done >= limits.max_subjects:
            stop = "max_per_run"
        if not stop and budget < min(0.01, limits.per_lead_usd):
            stop = "daily_budget"
        if not stop and credit_room is not None and s["resolve_credits"] + limits.resolve_credits > credit_room:
            stop = "credit_cap"
        if stop:
            store.save_contact(t.subject, status="deferred", reason=stop, retention_days=limits.retention_days)
            for link in t.links:
                store.link_contact(t.subject, *link)
            s["deferred"] += 1
            continue
        res = await rs.resolve(sc, t.posts, verdict=t.verdict, max_credits=limits.resolve_credits)
        s["resolve_credits"] += res.credits
        done += 1
        if res.status != "ready":
            store.save_contact(t.subject, status="unresolved", reason=res.reason, sources=res.sources,
                               credits=res.credits, retention_days=limits.retention_days)
            for link in t.links:
                store.link_contact(t.subject, *link)
            s["unresolved"] += 1
            continue
        if box is None:
            store.save_contact(t.subject, status="deferred", reason="data_box_not_configured", sources=res.sources,
                               credits=res.credits, retention_days=limits.retention_days)
            for link in t.links:
                store.link_contact(t.subject, *link)
            s["deferred"] += 1
            continue
        attempt = (card or {}).get("attempts", 0)
        want = ["email", "linkedin", "socials"] + (["phone"] if t.tier == "high" else [])
        args = {**res.args, "want": want, "max_cost_usd": round(min(limits.per_lead_usd, budget), 4),
                "request_id": request_id(t.subject, attempt), "wait_seconds": 60}
        if card and card.get("status") == "deferred" and card.get("reason") == "verify_quota" and card.get("databox_job_id"):
            args["resume_from"] = card["databox_job_id"]
        try:
            body = await box.enrich(**args)
        except DataBoxError as exc:
            if exc.code in ("unauthorized",):
                raise  # wrong token / box misconfigured: fail the job loudly
            status = "deferred" if exc.budget or exc.code in ("provider_down", "not_offered", "http") else "failed"
            store.save_contact(t.subject, status=status, reason=exc.code, sources=res.sources, attempt=True,
                               credits=res.credits, retention_days=limits.retention_days)
            for link in t.links:
                store.link_contact(t.subject, *link)
            s[status] += 1
            if exc.budget:
                stop = "databox_budget"
            continue
        cost = float(body.get("cost_usd") or 0)
        budget -= cost
        s["cost_usd"] += cost
        f = card_fields(body)
        quota = f.get("email_verdict") == "verify_quota"
        store.save_contact(t.subject, status="deferred" if quota else "enriched", reason="verify_quota" if quota else "",
                           fields=f, card=body.get("card") or {}, sources=res.sources, attempt=True,
                           databox_job_id=body.get("job_id"), credits=res.credits, cost_usd=cost,
                           retention_days=limits.retention_days)
        for link in t.links:
            store.link_contact(t.subject, *link)
        if quota:
            s["deferred"] += 1
            continue
        s["enriched"] += 1
        if f.get("email"):
            s["with_email"] += 1
        if f.get("email_verdict") in ("deliverable", "accept_all"):
            s[f["email_verdict"]] += 1
    s["cost_usd"] = round(s["cost_usd"], 6)
    s["stopped"] = stop
    return s


def badge(card: dict | None) -> str:
    """What the dashboard shows for a lead's contact."""
    if not card:
        return "none"
    st, verdict = card.get("status"), card.get("email_verdict")
    if st == "enriched":
        return {"deliverable": "verified", "accept_all": "accept_all"}.get(verdict, "not_found")
    return {"unresolved": "unresolvable", "deferred": "pending", "pending": "pending", "failed": "failed"}[st]


def subject_of(platform: str, kind: str, author_handle: str | None, author_url: str | None) -> str | None:
    return rs.subject_key(SimpleNamespace(platform=platform, kind=kind, author_handle=author_handle,
                                          author_url=author_url))
