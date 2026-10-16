"""Verify a run before delivering it: every lead's evidence is verbatim in its post (the contract rule), the
per-source table adds up to the run's credits, and (optionally) an exported CSV lists exactly the run's leads.

    PYTHONPATH=src .venv/bin/python scripts/radar_verify_run.py <radar> [--run <id>[,<id>...]] [--csv leads.csv]

Exit 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import csv
import sys

from lead_radar import config, contract
from lead_radar.db import Store


def lead_rows(store: Store, radar_id: str, run_ids: list[str]) -> list[dict]:
    marks = ",".join("?" * len(run_ids))
    return [dict(r) for r in store.conn.execute(
        "SELECT DISTINCT c.post_id, c.tier, c.evidence_quote, p.platform, p.url, p.text FROM classifications c "
        f"JOIN run_posts rp ON rp.post_id = c.post_id AND rp.run_id IN ({marks}) JOIN posts p ON p.id = c.post_id "
        "WHERE c.radar_id = ? AND c.role = 'buyer' AND c.tier IN ('high','medium','low')", (*run_ids, radar_id))]


def check(store: Store, radar_id: str, run_ids: list[str], csv_path: str | None = None) -> list[tuple[str, bool, str]]:
    results = []
    leads = lead_rows(store, radar_id, run_ids)
    bad = [r["url"] for r in leads if not contract.is_verbatim(r["evidence_quote"], r["text"])]
    results.append(("evidence verbatim", not bad, f"{len(leads) - len(bad)}/{len(leads)} leads" +
                    (f"; not verbatim: {bad[:5]}" if bad else "")))
    emails = [dict(r) for r in store.conn.execute(
        "SELECT DISTINCT ct.email_verdict FROM contacts ct JOIN contact_links cl ON cl.subject = ct.subject "
        "JOIN run_posts rp ON rp.post_id = cl.post_id WHERE cl.radar_id = ? AND ct.email IS NOT NULL "
        f"AND rp.run_id IN ({','.join('?' * len(run_ids))})", (radar_id, *run_ids))]
    weak = [e["email_verdict"] for e in emails if e["email_verdict"] not in ("deliverable", "accept_all")]
    results.append(("contact emails verified", not weak,
                    f"{len(emails)} verdict kinds; unverified: {sorted(set(weak))}" if weak else "every email is "
                    "deliverable or accept-all"))
    for rid in run_ids:
        run = store.get_run(rid)
        src_credits = sum(int(s.get("credits", 0)) for s in run.per_source.values())
        results.append((f"credits add up ({rid})", src_credits == run.credits_used,
                        f"per-source {src_credits} vs run {run.credits_used}"))
    if csv_path:
        rows = list(csv.reader(open(csv_path, newline="")))
        start = next(i for i, r in enumerate(rows) if r and r[0].startswith("LEADS")) + 2
        urls = []
        for r in rows[start:]:
            if not r:
                break
            urls.append(r[5])
        want = sorted(r["url"] for r in leads)
        results.append(("csv leads match the run", sorted(urls) == want, f"csv {len(urls)} vs run {len(want)}"))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("radar")
    ap.add_argument("--run", default="latest", help="run id(s), comma-separated; default the radar's latest run")
    ap.add_argument("--csv")
    a = ap.parse_args()
    store = Store.open(config.load().db_path)
    radar = store.get_radar(a.radar)
    if not radar:
        raise SystemExit("no such radar")
    if a.run == "latest":
        row = store.conn.execute("SELECT id FROM runs WHERE radar_id=? ORDER BY started_at DESC LIMIT 1",
                                 (radar.id,)).fetchone()
        run_ids = [row["id"]] if row else []
    else:
        run_ids = [x.strip() for x in a.run.split(",") if x.strip()]
    results = check(store, radar.id, run_ids, a.csv)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
