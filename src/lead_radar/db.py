"""SQLite store (WAL). Numbered SQL files in migrations/ are applied once, in order."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .models import Classification, IntentBrief, Lead, Post, Radar, Run, utcnow

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        os.chmod(path, 0o600)  # lead data is personal; owner-only
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations. Returns the names applied this call."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        conn.execute("BEGIN")
        try:
            for statement in _statements(path.read_text()):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (path.name, utcnow().isoformat()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(path.name)
    return applied


def _statements(sql: str) -> Iterable[str]:
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    for chunk in "\n".join(lines).split(";"):
        if chunk.strip():
            yield chunk


def _j(value) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @contextmanager
    def tx(self):
        if self.conn.in_transaction:  # nested: the outer transaction owns commit/rollback
            yield
            return
        self.conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    @classmethod
    def open(cls, path: Path | str) -> "Store":
        conn = connect(path)
        migrate(conn)
        return cls(conn)

    # radars -------------------------------------------------------------
    def save_radar(self, r: Radar) -> None:
        self.conn.execute(
            """INSERT INTO radars VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, query=excluded.query,
                 offer_context=excluded.offer_context, platforms=excluded.platforms,
                 lookback_days=excluded.lookback_days, intent_brief_id=excluded.intent_brief_id,
                 schedule=excluded.schedule, active=excluded.active""",
            (r.id, r.name, r.query, r.offer_context, _j(r.platforms), r.lookback_days,
             r.intent_brief_id, r.schedule, int(r.active), _ts(r.created_at)),
        )

    def get_radar(self, radar_id: str) -> Radar | None:
        row = self.conn.execute("SELECT * FROM radars WHERE id=? OR name=?", (radar_id, radar_id)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["platforms"] = json.loads(d["platforms"])
        d["active"] = bool(d["active"])
        return Radar(**d)

    def list_radars(self) -> list[Radar]:
        ids = [r["id"] for r in self.conn.execute("SELECT id FROM radars ORDER BY created_at")]
        return [self.get_radar(i) for i in ids]

    # briefs -------------------------------------------------------------
    def save_brief(self, b: IntentBrief) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO intent_briefs VALUES (?,?,?,?,?)",
            (b.id, b.radar_id, b.version, b.model_dump_json(), utcnow().isoformat()),
        )

    def get_brief(self, brief_id: str) -> IntentBrief | None:
        row = self.conn.execute("SELECT body FROM intent_briefs WHERE id=?", (brief_id,)).fetchone()
        return IntentBrief.model_validate_json(row["body"]) if row else None

    # runs ---------------------------------------------------------------
    def save_run(self, run: Run) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run.id, run.radar_id, _ts(run.started_at), _ts(run.finished_at), _j(run.per_source),
             run.credits_used, run.browser_minutes, run.jev_calls, run.reasoning_tokens_in,
             run.reasoning_tokens_out, run.cost_usd, _j(run.counts), run.qc_disagreement, _j(run.errors)),
        )

    def get_run(self, run_id: str) -> Run | None:
        if run_id == "latest":
            row = self.conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        else:
            row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("per_source", "counts", "errors"):
            d[k] = json.loads(d[k])
        return Run(**d)

    # posts --------------------------------------------------------------
    def upsert_post(self, p: Post) -> bool:
        """Store a post and link it to its run, atomically; returns True if it was new to the store."""
        with self.tx():
            return self._upsert_post(p)

    def _upsert_post(self, p: Post) -> bool:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p.id, p.platform, p.url, p.parent_url, p.kind, p.author_handle, p.author_url, p.author_name,
             p.text, p.title, _ts(p.posted_at), _j(p.engagement), p.community, p.source_term, p.run_id,
             p.simhash, _j(p.raw), p.parent_text, utcnow().isoformat()),
        )
        self.conn.execute("INSERT OR IGNORE INTO run_posts VALUES (?,?)", (p.run_id, p.id))
        return cur.rowcount == 1

    def update_post_content(self, p: Post) -> None:
        """Replace text/author after a full-post fetch (e.g. verifying a Google snippet)."""
        self.conn.execute(
            "UPDATE posts SET text=?, author_handle=?, author_name=?, posted_at=?, simhash=?, raw=? WHERE id=?",
            (p.text, p.author_handle, p.author_name, _ts(p.posted_at), p.simhash, _j(p.raw), p.id))

    def get_post(self, pid: str) -> Post | None:
        row = self.conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d.pop("first_seen")
        d["engagement"] = json.loads(d["engagement"])
        d["raw"] = json.loads(d["raw"])
        return Post(**d)

    def seen_for_radar(self, radar_id: str, post_ids: Iterable[str]) -> set[str]:
        """Post IDs already queued, classified or dropped for this radar (never re-sent to classification)."""
        ids = list(post_ids)
        seen: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for table in ("classifications", "drops", "classification_queue", "listing_queue", "listing_verdicts"):
                seen.update(r[0] for r in self.conn.execute(
                    f"SELECT post_id FROM {table} WHERE radar_id=? AND post_id IN ({marks})", (radar_id, *chunk)))
        return seen

    def radar_posts(self, radar_id: str) -> list[Post]:
        """Every post this radar has fetched (for campaign detection across runs)."""
        rows = self.conn.execute(
            "SELECT DISTINCT rp.post_id FROM run_posts rp JOIN runs r ON r.id = rp.run_id WHERE r.radar_id=?", (radar_id,))
        return [p for p in (self.get_post(r[0]) for r in rows) if p]

    def author_simhashes(self, platform: str, author_handle: str) -> list[tuple[str, int]]:
        return [(r["id"], r["simhash"]) for r in self.conn.execute(
            "SELECT id, simhash FROM posts WHERE platform=? AND author_handle=?", (platform, author_handle))]

    # classification queue ----------------------------------------------
    def enqueue(self, post_ids: Iterable[str], radar_id: str, run_id: str) -> int:
        now = utcnow().isoformat()
        n = 0
        with self.tx():
            for pid in post_ids:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO classification_queue VALUES (?,?,?,'pending',?)", (pid, radar_id, run_id, now))
                n += cur.rowcount
        return n

    def pending(self, radar_id: str, limit: int | None = None) -> list[str]:
        sql = "SELECT post_id FROM classification_queue WHERE radar_id=? AND state='pending' ORDER BY enqueued_at"
        return [r[0] for r in self.conn.execute(sql + (f" LIMIT {int(limit)}" if limit else ""), (radar_id,))]

    def mark_queue(self, post_id: str, radar_id: str, state: str) -> None:
        self.conn.execute("UPDATE classification_queue SET state=? WHERE post_id=? AND radar_id=?", (state, post_id, radar_id))

    # job-board listings (own queue + verdicts; never the M3 cascade) ----------
    def enqueue_listings(self, post_ids: Iterable[str], radar_id: str, run_id: str) -> int:
        now = utcnow().isoformat()
        n = 0
        with self.tx():
            for pid in post_ids:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO listing_queue VALUES (?,?,?,'pending',?)", (pid, radar_id, run_id, now))
                n += cur.rowcount
        return n

    def pending_listings(self, radar_id: str, limit: int | None = None) -> list[str]:
        sql = "SELECT post_id FROM listing_queue WHERE radar_id=? AND state='pending' ORDER BY enqueued_at"
        return [r[0] for r in self.conn.execute(sql + (f" LIMIT {int(limit)}" if limit else ""), (radar_id,))]

    def mark_listing(self, post_id: str, radar_id: str, state: str) -> None:
        self.conn.execute("UPDATE listing_queue SET state=? WHERE post_id=? AND radar_id=?", (state, post_id, radar_id))

    def save_listing_verdict(self, v: dict) -> None:
        cols = ("post_id", "radar_id", "run_id", "employer_type", "fit", "role_title", "company_name", "need",
                "evidence_quote", "confidence", "decided_by", "reason", "contract_version", "created_at")
        row = {**v, "created_at": v.get("created_at") or utcnow().isoformat()}
        self.conn.execute(f"INSERT OR REPLACE INTO listing_verdicts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          tuple(row.get(c) for c in cols))

    def listing_verdicts(self, radar_id: str, fits: Iterable[str] | None = None) -> list[dict]:
        sql, args = "SELECT * FROM listing_verdicts WHERE radar_id=?", [radar_id]
        if fits:
            fits = list(fits)
            sql += f" AND fit IN ({','.join('?' * len(fits))})"
            args += fits
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY created_at DESC", args)]

    # program lane (companies running an affiliate program; one row per domain) --------
    def add_program_seed(self, domain: str, *, source: str, ref: str, name: str | None = None,
                         partner_count: int | None = None, offer: str | None = None,
                         tags: Iterable[str] = ()) -> bool:
        """Record a candidate company; merges sources/tags into an existing row. Returns True if it was new."""
        now = utcnow().isoformat()
        with self.tx():
            row = self.conn.execute("SELECT sources, tags FROM program_companies WHERE domain=?", (domain,)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO program_companies (domain, name, partner_count, offer, tags, sources, first_seen, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (domain, name, partner_count, offer, _j(sorted(set(tags))), _j([{"source": source, "ref": ref}]), now, now))
                return True
            sources = json.loads(row["sources"])
            if {"source": source, "ref": ref} not in sources:
                sources.append({"source": source, "ref": ref})
            merged = sorted(set(json.loads(row["tags"])) | set(tags))
            self.conn.execute(
                "UPDATE program_companies SET sources=?, tags=?, name=COALESCE(name, ?), partner_count=COALESCE(?, partner_count),"
                " offer=COALESCE(offer, ?), updated_at=? WHERE domain=?",
                (_j(sources), _j(merged), name, partner_count, offer, now, domain))
            return False

    def programs_to_check(self, limit: int | None = None, recheck_days: int | None = None) -> list[str]:
        sql = "SELECT domain FROM program_companies WHERE list='affiliate' AND (status='pending'"
        args: list = []
        if recheck_days is not None:
            sql += " OR checked_at < ?"
            args.append((utcnow() - timedelta(days=recheck_days)).isoformat())
        sql += ")"
        sql += " ORDER BY first_seen"
        return [r[0] for r in self.conn.execute(sql + (f" LIMIT {int(limit)}" if limit else ""), args)]

    # program lane: company profiles, growth signals, lead scores --------------------------------------------
    def program_company(self, domain: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM program_companies WHERE domain=?", (domain,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("platforms", "evidence", "tags", "sources"):
            d[k] = json.loads(d[k])
        return d

    def program_domain_for_linkedin(self, linkedin_url: str) -> str | None:
        row = self.conn.execute("SELECT domain FROM program_companies WHERE linkedin_url=?", (linkedin_url,)).fetchone()
        return row[0] if row else None

    def save_company_profile(self, domain: str, *, lst: str, source: str, ref: str, name: str | None,
                             linkedin_url: str | None, country: str | None, hq: str | None, size: str | None,
                             industry: str | None, excluded: str | None, tags: Iterable[str] = ()) -> bool:
        """Create or update a company from its LinkedIn page. Returns True if the company is new."""
        new = self.add_program_seed(domain, source=source, ref=ref, name=name, tags=tags)
        self.conn.execute(
            "UPDATE program_companies SET list=CASE WHEN ?='finance' THEN 'finance' ELSE list END, linkedin_url=?,"
            " country=?, hq=?, size=?, industry=?, excluded=?, profiled_at=?, name=COALESCE(?, name) WHERE domain=?",
            (lst, linkedin_url, country, hq, size, industry, excluded, utcnow().isoformat(), name, domain))
        return new

    def add_program_signal(self, domain: str, sig) -> bool:
        """Store a dated growth signal once (by kind + url). Returns True if it was new."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO program_signals (id, domain, kind, platform, url, quote, signal_date, person_name,"
            " person_url, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sig.id, domain, sig.kind, getattr(sig, "platform", None) or "linkedin", sig.url, (sig.quote or "")[:500],
             sig.signal_date, sig.person_name, sig.person_url, utcnow().isoformat()))
        return cur.rowcount == 1

    def program_signals(self, domains: Iterable[str] | None = None) -> dict[str, list[dict]]:
        sql, args = "SELECT * FROM program_signals", []
        if domains is not None:
            domains = list(domains)
            if not domains:
                return {}
            sql += f" WHERE domain IN ({','.join('?' * len(domains))})"
            args = domains
        out: dict[str, list[dict]] = {}
        for r in self.conn.execute(sql + " ORDER BY COALESCE(signal_date, first_seen) DESC", args):
            out.setdefault(r["domain"], []).append(dict(r))
        return out

    def set_lead_score(self, domain: str, value: int) -> None:
        self.conn.execute("UPDATE program_companies SET lead_score=? WHERE domain=?", (int(value), domain))

    IMPORT_FIELDS = ("name", "status", "platforms", "program_url", "commission_text", "evidence", "segment", "tags",
                     "partner_count", "offer", "sources", "hiring_signal", "score", "check_error", "checked_at", "first_seen",
                     "list", "linkedin_url", "country", "hq", "size", "industry", "excluded", "profiled_at", "lead_score")
    SIGNAL_FIELDS = ("id", "domain", "kind", "platform", "url", "quote", "signal_date", "person_name", "person_url",
                     "first_seen")

    def import_program_rows(self, rows: Iterable[dict]) -> int:
        """Upsert full program rows (e.g. a batch verified on another machine). Returns rows written."""
        now = utcnow().isoformat()
        n = 0
        with self.tx():
            for r in rows:
                domain = str(r.get("domain") or "").strip().lower()
                if not domain or r.get("status") not in ("pending", "verified", "no_program", "unreachable"):
                    continue
                vals = {k: r.get(k) for k in self.IMPORT_FIELDS}
                for k in ("platforms", "evidence", "tags", "sources"):
                    vals[k] = _j(vals[k] or [])
                vals["segment"] = vals["segment"] or "unknown"
                vals["score"] = int(vals["score"] or 0)
                vals["first_seen"] = vals["first_seen"] or now
                vals["list"] = vals["list"] if vals["list"] in ("affiliate", "finance") else "affiliate"
                vals["lead_score"] = int(vals["lead_score"] or 0)
                cols = ("domain", *self.IMPORT_FIELDS, "updated_at")
                self.conn.execute(
                    f"INSERT OR REPLACE INTO program_companies ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    (domain, *[vals[k] for k in self.IMPORT_FIELDS], now))
                n += 1
        return n

    def import_program_signals(self, rows: Iterable[dict]) -> int:
        """Add signals dumped elsewhere, keeping their ids and first_seen; skips ones already here or orphaned."""
        n = 0
        with self.tx():
            for r in rows:
                vals = {k: r.get(k) for k in self.SIGNAL_FIELDS}
                if not (vals["id"] and vals["domain"] and vals["kind"] and vals["url"] and vals["first_seen"]):
                    continue
                if not self.conn.execute("SELECT 1 FROM program_companies WHERE domain=?", (vals["domain"],)).fetchone():
                    continue
                vals["quote"] = (vals["quote"] or "")[:500]
                vals["platform"] = vals["platform"] or "linkedin"
                try:
                    cur = self.conn.execute(
                        f"INSERT OR IGNORE INTO program_signals ({','.join(self.SIGNAL_FIELDS)}) "
                        f"VALUES ({','.join('?' * len(self.SIGNAL_FIELDS))})", tuple(vals.values()))
                except sqlite3.IntegrityError:  # unknown kind
                    continue
                n += cur.rowcount
        return n

    def requeue_in_house(self) -> int:
        """Send in-house verdicts back to pending (after a detection-rule change)."""
        cur = self.conn.execute("UPDATE program_companies SET status='pending' WHERE status='verified'"
                                " AND platforms='[\"in-house\"]'")
        return cur.rowcount

    def requeue_rate_limited(self) -> int:
        """Send rows that failed on a 429 back to pending (re-run verify with low concurrency)."""
        cur = self.conn.execute("UPDATE program_companies SET status='pending' WHERE status='unreachable'"
                                " AND check_error LIKE '%429%'")
        return cur.rowcount

    def save_program_check(self, domain: str, *, status: str, platforms: list[str], program_url: str | None,
                           commission_text: str | None, evidence: list[dict], segment: str, name: str | None,
                           score: int, error: str | None) -> None:
        now = utcnow().isoformat()
        self.conn.execute(
            "UPDATE program_companies SET status=?, platforms=?, program_url=?, commission_text=?, evidence=?, segment=?,"
            " name=COALESCE(?, name), score=?, check_error=?, checked_at=?, updated_at=? WHERE domain=?",
            (status, _j(platforms), program_url, commission_text, _j(evidence), segment, name, score, error, now, now, domain))

    def program_companies(self, statuses: Iterable[str] | None = None) -> list[dict]:
        sql, args = "SELECT * FROM program_companies", []
        if statuses:
            statuses = list(statuses)
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args += statuses
        out = []
        for r in self.conn.execute(sql + " ORDER BY score DESC, domain", args):
            d = dict(r)
            for k in ("platforms", "evidence", "tags", "sources"):
                d[k] = json.loads(d[k])
            out.append(d)
        return out

    def source_spend_today(self, platform: str) -> float:
        """USD a metered source (e.g. indeed) has cost across all runs started today (UTC)."""
        today = utcnow().date().isoformat()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(json_extract(per_source, ?)), 0) FROM runs WHERE substr(started_at, 1, 10) = ?",
            (f'$."{platform}".cost_usd', today)).fetchone()
        return float(row[0] or 0)

    # contacts (Data Box enrich cards, keyed by subject) --------------------------
    CONTACT_FIELDS = ("name", "company", "domain", "website", "email", "email_verdict", "phone", "linkedin_url")

    def get_contact(self, subject: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM contacts WHERE subject=?", (subject,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("alt_emails", "socials", "card", "sources"):
            d[k] = json.loads(d[k] or ("[]" if k == "alt_emails" else "{}"))
        return d

    def save_contact(self, subject: str, *, status: str, reason: str = "", fields: dict | None = None,
                     card: dict | None = None, sources: dict | None = None, attempt: bool = False,
                     databox_job_id: str | None = None, credits: int = 0, cost_usd: float = 0.0,
                     retention_days: int = 90) -> None:
        """Create or update a card. Fields not given keep their value; credits/cost/attempts accumulate."""
        now = utcnow()
        f = {k: v for k, v in (fields or {}).items() if k in self.CONTACT_FIELDS}
        prev = self.get_contact(subject)
        if prev is None:
            self.conn.execute(
                "INSERT INTO contacts (subject, status, reason, created_at, updated_at, expires_at) VALUES (?,?,?,?,?,?)",
                (subject, status, reason, now.isoformat(), now.isoformat(),
                 (now + timedelta(days=retention_days)).isoformat()))
        sets = {"status": status, "reason": reason, "updated_at": now.isoformat(), **f}
        if fields and "alt_emails" in fields:
            sets["alt_emails"] = _j(fields["alt_emails"] or [])
        if fields and "socials" in fields:
            sets["socials"] = _j(fields["socials"] or {})
        if card is not None:
            sets["card"] = _j(card)
        if sources is not None:
            sets["sources"] = _j(sources)
        if databox_job_id:
            sets["databox_job_id"] = databox_job_id
        cols = ", ".join(f"{k}=?" for k in sets)
        self.conn.execute(f"UPDATE contacts SET {cols}, attempts=attempts+?, credits_used=credits_used+?, "
                          f"cost_usd=cost_usd+? WHERE subject=?",
                          (*sets.values(), int(attempt), int(credits), float(cost_usd), subject))

    def link_contact(self, subject: str, post_id: str, radar_id: str, kind: str, tier: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO contact_links VALUES (?,?,?,?,?)",
                          (subject, post_id, radar_id, kind, tier))

    def contacts_for(self, radar_ids: Iterable[str]) -> dict[str, dict]:
        """subject -> card, for every contact linked to these radars."""
        ids = list(radar_ids)
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        subjects = [r[0] for r in self.conn.execute(
            f"SELECT DISTINCT subject FROM contact_links WHERE radar_id IN ({marks})", ids)]
        return {s: c for s in subjects if (c := self.get_contact(s))}

    def enrich_spend_today(self) -> tuple[float, int]:
        """(USD, ScrapeCreators credits) spent on enrichment today, from enrich jobs."""
        today = utcnow().date().isoformat()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(credits_used),0) FROM jobs "
            "WHERE kind='enrich' AND substr(COALESCE(started_at, created_at),1,10)=?", (today,)).fetchone()
        return float(row[0]), int(row[1])

    def purge_contacts(self, now: datetime | None = None) -> int:
        """Delete cards past retention (links go with them). Returns how many were deleted."""
        now = now or utcnow()
        with self.tx():
            cur = self.conn.execute("DELETE FROM contacts WHERE expires_at < ?", (now.isoformat(),))
        return cur.rowcount

    def calibration_count(self, radar_id: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM calibration WHERE radar_id=?", (radar_id,)).fetchone()[0]

    def save_calibration(self, post_id: str, radar_id: str, run_id: str, jev_tier: str, reasoning_tier: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO calibration VALUES (?,?,?,?,?,?,?)",
                          (post_id, radar_id, run_id, jev_tier, reasoning_tier, int(jev_tier == reasoning_tier),
                           utcnow().isoformat()))

    # classifications / drops -------------------------------------------
    def save_classification(self, c: Classification) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO classifications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.post_id, c.radar_id, c.role, c.tier, c.need_summary, c.evidence_quote, c.urgency,
             c.confidence, c.decided_by, int(c.freshness_downgrade), c.contract_version, _ts(c.created_at)),
        )

    def save_drop(self, post_id: str, radar_id: str, run_id: str, reason: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO drops VALUES (?,?,?,?,?)",
                          (post_id, radar_id, run_id, reason, utcnow().isoformat()))

    def classifications(self, radar_id: str, tiers: Iterable[str] | None = None) -> list[Classification]:
        sql, args = "SELECT * FROM classifications WHERE radar_id=?", [radar_id]
        if tiers:
            tiers = list(tiers)
            sql += f" AND tier IN ({','.join('?' * len(tiers))})"
            args += tiers
        out = []
        for r in self.conn.execute(sql, args):
            d = dict(r)
            d["freshness_downgrade"] = bool(d["freshness_downgrade"])
            out.append(Classification(**d))
        return out

    # leads --------------------------------------------------------------
    def save_lead(self, lead: Lead) -> None:
        self.conn.execute(
            """INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id, radar_id) DO UPDATE SET display_name=excluded.display_name,
                 handles=excluded.handles, profile_urls=excluded.profile_urls, post_ids=excluded.post_ids,
                 best_tier=excluded.best_tier, score=excluded.score, need_summary=excluded.need_summary,
                 evidence_quote=excluded.evidence_quote, suggested_opener=excluded.suggested_opener,
                 bio=excluded.bio, website=excluded.website, last_seen=excluded.last_seen""",
            (lead.id, lead.radar_id, lead.display_name, _j(lead.handles), _j(lead.profile_urls),
             _j(lead.post_ids), lead.best_tier, lead.score, lead.need_summary, lead.evidence_quote,
             lead.suggested_opener, lead.bio, lead.website, lead.status, _ts(lead.first_seen), _ts(lead.last_seen)),
        )

    def leads(self, radar_id: str, tiers: Iterable[str] | None = None) -> list[Lead]:
        sql, args = "SELECT * FROM leads WHERE radar_id=?", [radar_id]
        if tiers:
            tiers = list(tiers)
            sql += f" AND best_tier IN ({','.join('?' * len(tiers))})"
            args += tiers
        out = []
        for r in self.conn.execute(sql + " ORDER BY score DESC", args):
            d = dict(r)
            for k in ("handles", "profile_urls", "post_ids"):
                d[k] = json.loads(d[k])
            out.append(Lead(**d))
        return out

    def set_lead_status(self, lead_id: str, radar_id: str, status: str) -> bool:
        cur = self.conn.execute("UPDATE leads SET status=? WHERE id=? AND radar_id=?", (status, lead_id, radar_id))
        return cur.rowcount == 1
