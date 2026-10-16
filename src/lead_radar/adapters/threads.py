from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import normalize as n
from .base import SourceAdapter


class ThreadsAdapter(SourceAdapter):
    """/v1/threads/search. No pagination; at most 10 results per request. Posts are the ask."""
    platform = "threads"

    async def _collect(self, brief):
        start = (datetime.now(timezone.utc) - timedelta(days=self.ctx.lookback_days)).date().isoformat()

        async def search(term):
            data = await self.call("/v1/threads/search", {"query": term, "start_date": start, "trim": "true"})
            for item in (data or {}).get("posts") or []:
                self.add(n.threads_post(item, run_id=self.ctx.run_id, term=term))

        await self.run_all((search(t) for t in self.terms(brief)))
