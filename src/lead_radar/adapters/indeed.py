"""Indeed: Cloudflare blocks direct reads, so listings come from Data Box `job_board` (Apify, paid).

Spend is capped twice: Lead Radar's own daily budget (RADAR_LISTINGS_DAILY_USD, default $2, passed in as
`budget_left_usd`) and Data Box's standing job_board pool. When either is used up the board records a note, not an
error, and the run carries on with the free boards. Indeed names the company, so these listings can be enriched.
"""
from __future__ import annotations

import re
from datetime import datetime

from ..databox import DataBoxError
from ..models import IntentBrief
from ..normalize import make_post
from .listing_base import ListingAdapter


def _when(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class IndeedAdapter(ListingAdapter):
    platform = "indeed"

    async def _collect(self, brief: IntentBrief) -> None:
        box = self.ctx.databox
        if box is None:
            self._notes.append("not searched: DATABOX_URL / DATABOX_TOKEN not set (Indeed comes through Data Box job_board)")
            return
        keywords = self.require_keywords(brief)
        if not keywords:
            return
        left = float(self.cfg.get("budget_left_usd", 0) or 0)
        per_call = float(self.cfg.get("max_cost_per_query_usd", 0.25))
        max_items = int(self.cfg.get("max_items_per_query", 25))
        cutoff = self.cutoff()
        seen: set[str] = set()
        for kw in keywords[: int(self.cfg.get("max_queries", 3))]:
            cap = round(min(per_call, left), 4)
            if cap < 0.01:
                self._notes.append(f"stopped: Indeed daily budget used (RADAR_LISTINGS_DAILY_USD); "
                                   f"skipped {kw!r} and later terms")
                return
            try:
                body = await box.job_board(source="indeed", query=kw, location=self.cfg.get("location"),
                                           country=self.cfg.get("country"), posted_within_days=self.ctx.lookback_days,
                                           max_items=max_items, max_cost_usd=cap,
                                           request_id=f"{self.ctx.run_id}:{re.sub(r'[^a-z0-9]+', '-', kw.lower())}")
            except DataBoxError as exc:
                if exc.budget:
                    self._notes.append("stopped: Data Box job_board budget used for today")
                else:
                    self._errors.append(str(exc))
                return
            spent = float(body.get("cost_usd") or 0)
            left -= spent
            self.sc.meter.usd[self.platform] += spent
            self.sc.meter.requests[self.platform] += 1
            for item in body.get("listings") or []:
                url = str(item.get("url") or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                posted = _when(item.get("posted_at"))
                if posted and posted.tzinfo and posted < cutoff:
                    continue
                company = str(item.get("company") or "").strip()
                title = str(item.get("title") or "").strip()
                text = "\n\n".join(x for x in (f"{company}: {title}" if company else title,
                                               str(item.get("description") or "")) if x)
                slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
                self.add(make_post(
                    platform=self.platform, url=url, kind="job", title=title, text=text,
                    posted_at=posted if posted and posted.tzinfo else None,
                    author_handle=f"indeed:co:{slug}" if slug else None, author_name=company or None,
                    community="indeed", source_term=kw, run_id=self.ctx.run_id,
                    raw={"company": company, "company_website": item.get("company_website"),
                         "location": item.get("location"), "salary": item.get("salary"),
                         "job_type": item.get("job_type")}))
