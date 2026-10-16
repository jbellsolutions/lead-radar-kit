"""Read-only dashboard: one self-contained HTML page built from the local store.

`build_data` collects radars, runs and leads (buyers with a tier; sellers are counted, not listed).
`render` injects that data into `dashboard.html`. Nothing here writes to the store or contacts anyone.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE = Path(__file__).with_name("dashboard.html")
TIERS = ("high", "medium", "low")
CAVEATS = [
    "The classifier has not passed its accuracy gate (M3). On the held-out VA set, High precision is 0.69 overall "
    "and 0.96 outside job ads. Other offers are unmeasured, so read each lead before acting.",
    "Job posts (a business hiring for the role) can appear as leads; that boundary is still under review.",
    "Job leads (OnlineJobs.ph, We Work Remotely, Freelancer, Indeed) use their own classifier, whose precision is "
    "not measured yet. OnlineJobs.ph and Freelancer hide the employer, so many of them can't be traced to a person.",
    "X is covered only through Google site:x.com. Skool and private Facebook groups are not covered yet.",
    "Public posts only. Nobody was contacted.",
]


def contact_view(card: dict) -> dict:
    """What the live dashboard shows for one contact card: values plus each field's source and confidence."""
    from .enrich import badge
    full = card.get("card") or {}

    def meta(key):
        f = full.get(key)
        return {"source": f.get("source"), "confidence": f.get("confidence")} if isinstance(f, dict) else {}
    fields = {k: {"value": card.get(k), **meta(k)} for k in ("name", "company", "email", "phone", "linkedin_url",
                                                              "website") if card.get(k)}
    if "email" in fields:
        fields["email"]["verdict"] = card.get("email_verdict")
    return {"badge": badge(card), "status": card["status"], "reason": card.get("reason") or "", "fields": fields,
            "alt_emails": card.get("alt_emails") or [], "socials": card.get("socials") or {},
            "updated_at": card.get("updated_at"), "cost_usd": round(card.get("cost_usd") or 0, 4)}


def build_data(store, settings, radar: str | None = None, include_contacts: bool = False) -> dict:
    """include_contacts=True only for the password-protected live dashboard; static/artifact pages never carry
    contact data."""
    q = store.conn.execute
    credit_usd = float(settings.get("scrapecreators.credit_usd", 0) or 0)
    radars = [dict(r) for r in q("SELECT id, name, query, created_at FROM radars ORDER BY created_at")]
    if radar:
        radars = [r for r in radars if radar in (r["id"], r["name"])]
        if not radars:
            raise ValueError(f"no such radar: {radar}")
    ids = [r["id"] for r in radars]
    marks = ",".join("?" * len(ids)) or "''"
    names = {r["id"]: r["name"] for r in radars}

    # A lead belongs to the run that queued it for this radar (falls back to the post's first run).
    tally = {}
    for row in q(f"SELECT COALESCE(cq.run_id, p.run_id) run_id, c.role, c.tier, COUNT(*) n FROM classifications c "
                 f"JOIN posts p ON p.id = c.post_id LEFT JOIN classification_queue cq "
                 f"ON cq.post_id = c.post_id AND cq.radar_id = c.radar_id WHERE c.radar_id IN ({marks}) "
                 f"GROUP BY 1, 2, 3", ids):
        t = tally.setdefault(row["run_id"], {"high": 0, "medium": 0, "low": 0, "sellers": 0})
        if row["role"] == "buyer" and row["tier"] in TIERS:
            t[row["tier"]] += row["n"]
        elif row["role"] == "seller":
            t["sellers"] += row["n"]

    runs = []
    for r in q(f"SELECT * FROM runs WHERE radar_id IN ({marks}) ORDER BY started_at", ids):
        counts = json.loads(r["counts"] or "{}")
        per_source = json.loads(r["per_source"] or "{}")
        credits_cost = r["credits_used"] * credit_usd
        t = tally.get(r["id"], {"high": 0, "medium": 0, "low": 0, "sellers": 0})
        runs.append({
            "id": r["id"], "radar": names[r["radar_id"]], "started_at": r["started_at"],
            "fetched": counts.get("fetched", sum(s.get("posts", 0) for s in per_source.values())),
            "classified": counts.get("classified", 0), "credits": r["credits_used"],
            "credits_usd": round(credits_cost, 4), "models_usd": round(max(r["cost_usd"] - credits_cost, 0), 4),
            "cost_usd": round(r["cost_usd"], 4), "errors": sum(s.get("errors", 0) for s in per_source.values()),
            "high": t["high"], "medium": t["medium"], "low": t["low"], "sellers": t["sellers"],
            "listings": counts.get("listings_queued", 0), "job_leads": counts.get("listing_leads", 0),
        })

    leads = []
    from .enrich import subject_of
    contacts = store.contacts_for(ids) if include_contacts else {}
    for r in q(f"SELECT c.*, p.platform, p.url, p.author_handle, p.author_name, p.author_url, p.kind, p.posted_at, "
               f"p.text, p.title, "
               f"COALESCE(cq.run_id, p.run_id) run_id FROM classifications c JOIN posts p ON p.id = c.post_id "
               f"LEFT JOIN classification_queue cq ON cq.post_id = c.post_id AND cq.radar_id = c.radar_id "
               f"WHERE c.radar_id IN ({marks}) AND c.role = 'buyer' AND c.tier IN ('high','medium','low')", ids):
        leads.append({
            "radar": names[r["radar_id"]], "run": r["run_id"], "platform": r["platform"], "tier": r["tier"],
            "urgency": r["urgency"], "posted_at": r["posted_at"], "url": r["url"],
            "author": r["author_handle"] or r["author_name"] or "", "need": r["need_summary"],
            "quote": r["evidence_quote"], "decided_by": r["decided_by"], "confidence": round(r["confidence"], 2),
            "excerpt": " ".join((r["text"] or "").split())[:400],
        })
        if include_contacts:
            subject = subject_of(r["platform"], r["kind"], r["author_handle"], r["author_url"])
            leads[-1]["contact"] = subject if subject in contacts else None
    leads.sort(key=lambda d: (TIERS.index(d["tier"]), -_ts(d["posted_at"])))

    listings = []
    for r in q(f"SELECT v.*, p.platform, p.url, p.title, p.posted_at, p.text, p.raw, p.author_handle "
               f"FROM listing_verdicts v JOIN posts p ON p.id = v.post_id "
               f"WHERE v.radar_id IN ({marks}) AND v.fit IN ('high','medium','low')", ids):
        raw = json.loads(r["raw"] or "{}")
        listings.append({
            "id": r["post_id"], "radar": names[r["radar_id"]], "run": r["run_id"], "board": r["platform"],
            "fit": r["fit"], "employer": r["employer_type"], "lead": r["employer_type"] == "direct" and r["fit"] != "low",
            "role": r["role_title"] or r["title"] or "", "company": r["company_name"] or "",
            "need": r["need"], "quote": r["evidence_quote"], "posted_at": r["posted_at"], "url": r["url"],
            "pay": raw.get("pay") or raw.get("salary") or raw.get("price"), "job_type": raw.get("job_type"),
            "location": raw.get("location") or raw.get("region"), "subject": r["author_handle"],
            "excerpt": " ".join((r["text"] or "").split())[:400],
        })
        if include_contacts:
            subject = subject_of(r["platform"], "job", r["author_handle"], None)
            listings[-1]["contact"] = subject if subject in contacts else None
    listings.sort(key=lambda d: (not d["lead"], TIERS.index(d["fit"]), -_ts(d["posted_at"])))

    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "credit_usd": credit_usd,
            "radars": [{"name": r["name"], "query": r["query"]} for r in radars],
            "runs": runs, "leads": leads, "listings": listings, "caveats": CAVEATS,
            **({"contacts": {k: contact_view(v) for k, v in contacts.items()}} if include_contacts else {})}


def render(data: dict | None, standalone: bool = True, live: bool = False) -> str:
    """standalone=True adds a doctype/charset for opening the file locally; False is the bare body an
    Artifact publish wraps itself. live=True (the hosted server) embeds no data: the page fetches /api/data
    and shows the Campaigns tab."""
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = TEMPLATE.read_text().replace("/*__DATA__*/null", payload, 1)
    page = page.replace("/*__LIVE__*/false", "true" if live else "false", 1)
    if standalone:
        page = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>\n' + page
                + "\n</body></html>")
    return page


def _ts(v: str | None) -> float:
    try:
        return datetime.fromisoformat(v).timestamp() if v else 0.0
    except ValueError:
        return 0.0
