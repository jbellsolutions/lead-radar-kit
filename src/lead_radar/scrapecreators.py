"""ScrapeCreators REST client (spec §5).

Policy:
- 402 (out of credits) raises OutOfCredits; the run stops and reports it.
- 403 and 404 raise SkipItem; the caller skips the item and logs it.
- 5xx and transport errors are retried up to `retries_on_5xx` times with backoff, then raise SourceError.
- At most `concurrency` requests are in flight per client, i.e. per run.
- Credits are metered from each response's `credits_charged`.

The API key travels only in the x-api-key header. It is never logged, and it is scrubbed from any error text.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

BASE_URL = "https://api.scrapecreators.com"


class ScrapeCreatorsError(RuntimeError):
    pass


class OutOfCredits(ScrapeCreatorsError):
    pass


class SkipItem(ScrapeCreatorsError):
    pass


class SourceError(ScrapeCreatorsError):
    pass


@dataclass
class Meter:
    requests: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    credits: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    usd: dict[str, float] = field(default_factory=lambda: defaultdict(float))  # non-credit spend (Data Box job_board)

    def total_credits(self) -> int:
        return sum(self.credits.values())


class ScrapeCreators:
    def __init__(self, api_key: str, *, base_url: str = BASE_URL, concurrency: int = 10,
                 timeout: float = 60.0, retries_on_5xx: int = 2, backoff: float = 1.5,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._key = api_key
        self._sem = asyncio.Semaphore(concurrency)
        self._retries = retries_on_5xx
        self._backoff = backoff
        self.meter = Meter()
        self.exhausted = False  # sticky: after one 402, every later call fails fast without touching the network
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport,
                                       headers={"x-api-key": api_key, "accept": "application/json"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self):
        await self._http.aclose()

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    async def get(self, path: str, params: dict | None = None, *, source: str = "unknown") -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            if self.exhausted:
                raise OutOfCredits(f"{path}: run stopped, ScrapeCreators credits exhausted")
            async with self._sem:
                try:
                    resp = await self._http.get(path, params=params)
                except httpx.HTTPError as exc:
                    resp, err = None, f"{type(exc).__name__}"
                else:
                    err = None
            self.meter.requests[source] += 1
            if resp is not None:
                status = resp.status_code
                if status == 402:
                    self.exhausted = True
                    raise OutOfCredits(f"{path}: 402 out of ScrapeCreators credits")
                if status in (403, 404):
                    raise SkipItem(f"{path}: {status}")
                if status < 500:
                    if status >= 400:
                        raise SourceError(self._scrub(f"{path}: {status} {resp.text[:200]}"))
                    data = resp.json()
                    self.meter.credits[source] += int(data.get("credits_charged") or 0) if isinstance(data, dict) else 0
                    if isinstance(data, dict) and data.get("success") is False:
                        raise SourceError(self._scrub(f"{path}: success=false {str(data.get('message', ''))[:200]}"))
                    return data
                err = f"{status}"
            if attempt >= self._retries:
                raise SourceError(self._scrub(f"{path}: {err} after {attempt + 1} attempts"))
            attempt += 1
            await asyncio.sleep(self._backoff * attempt)

    async def credit_balance(self) -> int:
        data = await self.get("/v1/account/credit-balance", source="account")
        return int(data.get("creditCount", 0))
