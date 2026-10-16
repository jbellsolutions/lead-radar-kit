"""OnlineJobs.ph: employers hiring Filipino remote staff (VA, admin, sales support...).

Read directly (server-rendered HTML; robots.txt allows job pages with Crawl-delay 5). Search pages list 30 cards
newest first; paging is a path offset (/jobseekers/jobsearch/30?jobkeyword=...). The employer is not named on the
site: a job page only carries `const employerId = N;`, so the poster's handle is `employer:N` and any company name
has to come from the description text.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote_plus

from selectolax.parser import HTMLParser

from ..models import IntentBrief
from ..models import post_id as _pid
from ..normalize import canonical_url, make_post
from .listing_base import ListingAdapter

BASE = "https://www.onlinejobs.ph"
PAGE_SIZE = 30
def post_id(platform: str, url: str) -> str:
    return _pid(platform, canonical_url(url))


EMPLOYER_RE = re.compile(r"const\s+employerId\s*=\s*(\d+)\s*;")


def _utc(stamp: str | None) -> datetime | None:
    try:
        return datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if stamp else None
    except ValueError:
        return None


def parse_search(html: str) -> list[dict]:
    """Cards on a search page: url, title, job type, pay, posted_at (UTC, from data-temp-2) and preview text."""
    out = []
    for box in HTMLParser(html).css("div.jobpost-cat-box"):
        link = box.css_first('a[href^="/jobseekers/job/"]')
        title_node = box.css_first("h4")
        if not link or not title_node:
            continue
        badge = title_node.css_first("span.badge")
        job_type = badge.text(strip=True) if badge else None
        title = title_node.text(deep=False, strip=True) or title_node.text(strip=True).replace(job_type or "", "").strip()
        posted = box.css_first("p[data-temp-2]")
        pay = box.css_first("dd")
        desc = box.css_first("div.desc")
        preview = desc.text(separator=" ", strip=True) if desc else ""
        out.append({"url": BASE + link.attributes["href"].split("?")[0], "title": title, "job_type": job_type,
                    "pay": pay.text(strip=True) if pay else None,
                    "posted_at": _utc(posted.attributes.get("data-temp-2") if posted else None),
                    "preview": re.sub(r"\s*See More\s*$", "", preview).rstrip("… ").strip()})
    return out


def parse_job(html: str) -> dict:
    """A job page: full overview text, the anonymous employer id, and the overview facts."""
    tree = HTMLParser(html)
    desc = tree.css_first("#job-description") or tree.css_first(".job-description")
    body = tree.body.text(separator="\n") if tree.body else ""
    facts = {}
    for label, key in (("TYPE OF WORK", "job_type"), ("WAGE / SALARY", "pay"), ("HOURS PER WEEK", "hours"),
                       ("DATE UPDATED", "updated")):
        m = re.search(re.escape(label) + r"\s*\n\s*(?:\n\s*)*([^\n]+)", body)
        if m:
            facts[key] = m.group(1).strip()
    emp = EMPLOYER_RE.search(html)
    return {"description": desc.text(separator="\n", strip=True) if desc else "",
            "employer_id": emp.group(1) if emp else None, **facts}


class OnlineJobsAdapter(ListingAdapter):
    platform = "onlinejobs"
    min_interval = 5.0

    async def _collect(self, brief: IntentBrief) -> None:
        keywords = self.terms(brief)
        if not keywords:
            self._notes.append("not searched: the brief has no onlinejobs terms (job titles a business would post)")
            return
        max_pages = int(self.cfg.get("max_pages", 3))
        max_detail = int(self.cfg.get("max_detail_pages", 40))
        cutoff = self.cutoff()
        cards: dict[str, dict] = {}
        fetched = old = 0
        for kw in keywords:  # one host with a crawl delay: sequential on purpose
            for page in range(max_pages):
                path = f"/jobseekers/jobsearch/{page * PAGE_SIZE}" if page else "/jobseekers/jobsearch"
                html = await self.fetch(f"{BASE}{path}?jobkeyword={quote_plus(kw)}")
                found = parse_search(html) if html else []
                reached_old = False
                fetched += len(found)
                for card in found:
                    if card["posted_at"] and card["posted_at"] < cutoff:
                        reached_old = True
                        old += 1
                        continue
                    cards.setdefault(card["url"], {**card, "term": kw})
                if reached_old or len(found) < PAGE_SIZE:
                    break
        seen = set(self.ctx.seen([post_id(self.platform, c["url"]) for c in cards.values()])) if self.ctx.seen else set()
        fresh = [c for c in cards.values() if post_id(self.platform, c["url"]) not in seen]
        # Detail pages carry the employer id (the only key for resolution): spend them on unseen listings whose
        # title matches a search term first, newest first.
        fresh.sort(key=lambda c: (not self.matches(c["title"], keywords), -(c["posted_at"] or cutoff).timestamp()))
        if len(fresh) > max_detail:
            self._notes.append(f"{len(fresh) - max_detail} more listings kept with their preview text only "
                               f"(detail pages capped at {max_detail} per run)")
        for i, card in enumerate(fresh):
            detail = {}
            if i < max_detail:
                html = await self.fetch(card["url"])
                detail = parse_job(html) if html else {}
            text = "\n\n".join(x for x in (card["title"], detail.get("description") or card["preview"]) if x)
            employer = detail.get("employer_id")
            self.add(make_post(
                platform=self.platform, url=card["url"], kind="job", text=text, title=card["title"],
                posted_at=card["posted_at"], author_handle=f"employer:{employer}" if employer else None,
                community="onlinejobs.ph", source_term=card["term"], run_id=self.ctx.run_id,
                raw={"pay": detail.get("pay") or card["pay"], "job_type": detail.get("job_type") or card["job_type"],
                     "hours": detail.get("hours"), "employer_id": employer}))
        self.tally(fetched, old, 0)
        if seen:
            self._notes.append(f"{len(cards) - len(fresh)} already seen by this campaign (not re-read)")
