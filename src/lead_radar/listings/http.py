"""Polite, GET-only HTTP for job boards that are read directly (no ScrapeCreators).

- Honours robots.txt (fetched once per host per client) and refuses disallowed paths.
- Waits at least the host's crawl delay between requests (robots Crawl-delay or the configured minimum).
- Backs off on 429/503, never retries anything else, and counts every request on the shared meter.
"""
from __future__ import annotations

import asyncio
import time
import urllib.robotparser
from urllib.parse import urlsplit

import httpx

USER_AGENT = "LeadRadar/2.0 (+https://github.com/jbellsolutions/lead-radar-kit; read-only job search)"


class Disallowed(Exception):
    """robots.txt disallows this URL for us."""


class PoliteClient:
    def __init__(self, meter, source: str, *, min_interval: float = 1.0, timeout: float = 30.0, retries: int = 2,
                 transport: httpx.AsyncBaseTransport | None = None, sleep=asyncio.sleep, clock=time.monotonic):
        self.meter = meter
        self.source = source
        self.min_interval = min_interval
        self.retries = retries
        self._sleep = sleep
        self._clock = clock
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=True,
                                       headers={"user-agent": USER_AGENT, "accept-language": "en"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _robots_for(self, origin: str) -> urllib.robotparser.RobotFileParser | None:
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                resp = await self._http.get(origin + "/robots.txt")
                parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except httpx.HTTPError:
                parser.parse([])
            self._robots[origin] = parser
        return self._robots[origin]

    def _delay(self, origin: str) -> float:
        robots = self._robots.get(origin)
        crawl = robots.crawl_delay(USER_AGENT) if robots else None
        return max(self.min_interval, float(crawl or 0))

    async def get(self, url: str) -> httpx.Response | None:
        """GET url politely. Returns the response (2xx) or None for a 404/410; raises on other failures."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        robots = await self._robots_for(origin)
        if robots and not robots.can_fetch(USER_AGENT, url):
            raise Disallowed(url)
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            for attempt in range(self.retries + 1):
                wait = self._last.get(origin, -1e9) + self._delay(origin) - self._clock()
                if wait > 0:
                    await self._sleep(wait)
                self._last[origin] = self._clock()
                self.meter.requests[self.source] += 1
                resp = await self._http.get(url)
                if resp.status_code in (429, 503) and attempt < self.retries:
                    await self._sleep(self._delay(origin) * (attempt + 2))
                    continue
                if resp.status_code in (404, 410):
                    return None
                resp.raise_for_status()
                return resp
        return None
