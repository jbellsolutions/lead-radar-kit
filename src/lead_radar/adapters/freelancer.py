"""Freelancer.com: open projects on category pages (/jobs/<category>/, /jobs/<category>/<n>/).

robots.txt disallows search query strings (/jobs/?*), so only category paths are read. Clients are anonymous
and cards carry no posting date ("6 days left"), so these listings can't be enriched and age comes from
the days-left badge only as a hint.
"""
from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from ..models import IntentBrief
from ..normalize import make_post
from .listing_base import ListingAdapter

BASE = "https://www.freelancer.com"
DEFAULT_CATEGORIES = ["virtual-assistant", "lead-generation", "data-entry"]


def parse_category(html: str) -> list[dict]:
    out = []
    for card in HTMLParser(html).css("div.JobSearchCard-item"):
        link = card.css_first("a.JobSearchCard-primary-heading-link")
        if not link or not link.attributes.get("href"):
            continue
        desc = card.css_first("p.JobSearchCard-primary-description")
        days = card.css_first("span.JobSearchCard-primary-heading-days")
        price = card.css_first("div.JobSearchCard-primary-price")
        tags = [a.text(strip=True) for a in card.css("a.JobSearchCard-primary-tagsLink")]
        out.append({"url": BASE + link.attributes["href"].split("?")[0], "title": link.text(strip=True),
                    "text": desc.text(separator=" ", strip=True) if desc else "",
                    "days_left": days.text(strip=True) if days else None,
                    "price": re.sub(r"\s+", " ", price.text(separator=" ", strip=True)) if price else None, "tags": tags})
    return out


class FreelancerAdapter(ListingAdapter):
    platform = "freelancer"

    async def _collect(self, brief: IntentBrief) -> None:
        keywords = self.require_keywords(brief)
        if not keywords:
            return
        fetched = off = 0
        categories = self.communities(brief) or list(self.cfg.get("categories") or DEFAULT_CATEGORIES)
        pages = int(self.cfg.get("max_pages", 2))
        for slug in categories[: self.ctx.max_terms]:
            slug = slug.strip().strip("/").split("/")[-1]
            for page in range(1, pages + 1):
                html = await self.fetch(f"{BASE}/jobs/{slug}/" + (f"{page}/" if page > 1 else ""))
                cards = parse_category(html) if html else []
                for job in cards:
                    fetched += 1
                    if not self.matches(f"{job['title']} {job['text']}", keywords):  # tags echo the category
                        off += 1
                        continue
                    self.add(make_post(
                        platform=self.platform, url=job["url"], kind="job", title=job["title"],
                        text=f"{job['title']}\n\n{job['text']}", community=f"freelancer:{slug}",
                        source_term=",".join(keywords[:3]), run_id=self.ctx.run_id,
                        raw={"days_left": job["days_left"], "price": job["price"], "tags": job["tags"]}))
                if not cards:
                    break
        self.tally(fetched, 0, off)
