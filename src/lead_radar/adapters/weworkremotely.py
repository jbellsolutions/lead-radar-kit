"""We Work Remotely: category RSS feeds. Titles are "Company: Role", so the employer is named.

Feeds come from `communities.weworkremotely` (category slugs, e.g. remote-customer-support-jobs); items are kept
when they match the brief's keywords and fall inside the lookback window.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from selectolax.parser import HTMLParser

from ..models import IntentBrief
from ..normalize import make_post
from .listing_base import ListingAdapter

BASE = "https://weworkremotely.com"
DEFAULT_CATEGORIES = ["remote-customer-support-jobs", "remote-sales-and-marketing-jobs",
                      "remote-management-and-finance-jobs", "all-other-remote-jobs"]


def parse_feed(xml: str) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        get = lambda tag: (item.findtext(tag) or "").strip()  # noqa: E731
        title = get("title")
        company, _, role = title.partition(": ")
        if not role:
            company, role = "", title
        try:
            posted = parsedate_to_datetime(get("pubDate")) if get("pubDate") else None
        except (TypeError, ValueError):
            posted = None
        html = get("description")
        text = HTMLParser(html).text(separator="\n", strip=True) if html else ""
        out.append({"url": get("link") or get("guid"), "company": company.strip(), "role": role.strip(),
                    "region": get("region"), "category": get("category"), "job_type": get("type"),
                    "posted_at": posted, "text": re.sub(r"\n{3,}", "\n\n", text)})
    return out


class WeWorkRemotelyAdapter(ListingAdapter):
    platform = "weworkremotely"

    async def _collect(self, brief: IntentBrief) -> None:
        keywords = self.require_keywords(brief)
        if not keywords:
            return
        fetched = old = off = 0
        categories = self.communities(brief) or list(self.cfg.get("categories") or DEFAULT_CATEGORIES)
        cutoff = self.cutoff()
        for slug in categories[: self.ctx.max_terms]:
            slug = slug.strip().strip("/").removesuffix(".rss").split("/")[-1]
            xml = await self.fetch(f"{BASE}/categories/{slug}.rss")
            for job in parse_feed(xml or ""):
                fetched += 1
                if job["posted_at"] and job["posted_at"] < cutoff:
                    old += 1
                    continue
                if not self.matches(f"{job['role']} {job['text'][:1500]}", keywords):
                    off += 1
                    continue
                handle = "wwr:co:" + re.sub(r"[^a-z0-9]+", "-", job["company"].lower()).strip("-") if job["company"] else None
                self.add(make_post(
                    platform=self.platform, url=job["url"], kind="job", title=job["role"],
                    text=f"{job['company']}: {job['role']}\n\n{job['text']}" if job["company"] else job["text"],
                    posted_at=job["posted_at"], author_handle=handle, author_name=job["company"] or None,
                    community=f"weworkremotely:{slug}", source_term=",".join(keywords[:3]), run_id=self.ctx.run_id,
                    raw={"company": job["company"], "region": job["region"], "job_type": job["job_type"],
                         "category": job["category"]}))
        self.tally(fetched, old, off)
