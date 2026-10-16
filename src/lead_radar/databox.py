"""Data Box client: Lead Radar calls Data Box as a tool (REST, bearer DATABOX_TOKEN).

  POST {DATABOX_URL}/api/v1/<tool>  -> 200 the result (finished) or a running handle {job_id, status, hint}
                                     -> 422 {"error", "code"?}  refused (budget used, provider down, bad input)
                                     -> 404 the box doesn't offer that tool;  401 wrong token
  Running jobs are polled with POST /api/v1/job {"action": "get", "job_id", "wait_seconds": <=60}.

Used for `job_board` (Indeed listings) and `enrich` (contact cards). Budgets live in Data Box's own environment;
nothing here can raise them. The token never appears in errors, and contact data is never logged.
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from .config import Settings

TERMINAL = {"succeeded", "failed", "cancelled", "refused", "interrupted"}


class DataBoxError(RuntimeError):
    """Data Box could not do the call. `code` is budget_exhausted | not_offered | unauthorized | refused | http."""

    def __init__(self, message: str, code: str = "refused", body: dict | None = None):
        super().__init__(message)
        self.code = code
        self.body = body or {}

    @property
    def budget(self) -> bool:
        return self.code == "budget_exhausted"


def _code(body: dict) -> str:
    if isinstance(body.get("code"), str):
        return body["code"]
    msg = str(body.get("error") or "").lower()
    if "budget" in msg or "daily cap" in msg or "spent today" in msg:
        return "budget_exhausted"
    if "not offered" in msg:
        return "not_offered"
    return "refused"


class DataBox:
    def __init__(self, url: str, token: str, *, timeout: float = 90.0, max_wait: float = 600.0,
                 transport: httpx.AsyncBaseTransport | None = None, clock=time.monotonic, sleep=asyncio.sleep,
                 poll_delay: float = 2.0):
        self.url = url.rstrip("/")
        self._sleep = sleep
        self.poll_delay = poll_delay
        self.max_wait = max_wait
        self._clock = clock
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport,
                                       headers={"authorization": f"Bearer {token}", "user-agent": "lead-radar/2"})

    @classmethod
    def from_settings(cls, settings: Settings, **kw) -> "DataBox | None":
        url = settings.secrets.get("DATABOX_URL") or os.environ.get("DATABOX_URL")
        token = settings.secrets.get("DATABOX_TOKEN")
        return cls(url, token, **kw) if url and token else None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, tool: str, args: dict) -> dict:
        try:
            resp = await self._http.post(f"{self.url}/api/v1/{tool}", json=args)
        except httpx.HTTPError as exc:
            raise DataBoxError(f"data-box {tool}: {type(exc).__name__}", "http") from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 401:
            raise DataBoxError("data-box rejected the token (401): check DATABOX_TOKEN", "unauthorized")
        if resp.status_code == 404:
            raise DataBoxError(f"data-box does not offer {tool}: {str(body.get('error') or '')[:160]}", "not_offered", body)
        if resp.status_code == 422:
            raise DataBoxError(f"data-box refused {tool}: {str(body.get('error') or '')[:200]}", _code(body), body)
        if resp.status_code >= 400:
            raise DataBoxError(f"data-box {tool}: HTTP {resp.status_code}", "http", body)
        return body

    async def call(self, tool: str, args: dict) -> dict:
        """Run a tool to completion (polling a running job) and return its result body."""
        body = await self._post(tool, args)
        started = self._clock()
        while body.get("status") not in (None, *TERMINAL) and body.get("job_id"):
            if self._clock() - started > self.max_wait:
                raise DataBoxError(f"data-box {tool} job {body['job_id']} still running after {int(self.max_wait)}s",
                                   "http", {"job_id": body["job_id"]})
            body = await self._post("job", {"action": "get", "job_id": body["job_id"], "wait_seconds": 60})
            await self._sleep(self.poll_delay)  # the box long-polls; this only guards against a busy loop
        if body.get("error"):
            raise DataBoxError(f"data-box {tool}: {str(body['error'])[:200]}", _code(body), body)
        return body

    async def job_board(self, **args) -> dict:
        return await self.call("job_board", args)

    async def enrich(self, **args) -> dict:
        return await self.call("enrich", args)
