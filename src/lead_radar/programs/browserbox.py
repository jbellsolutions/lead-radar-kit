"""Browser Box client for the program lane: `read` only (BROWSERBOX_URL REST, bearer BROWSERBOX_TOKEN).

  POST {BROWSERBOX_URL}/api/v1/read {"url"} -> {title, final_url, status_code, links, text}

Used for sites that refuse plain HTTP (403) or render their program page with JavaScript. `read` opens the page
in the box's real browser and returns text and links; it never clicks or types. Script tags are not returned,
so detection here rests on portal links and commission language. The box runs one browser at a time, so this is
a slow, sequential escalation pass, never the bulk check.
"""
from __future__ import annotations

import html as htmllib

import httpx

from .detect import Page

TOOL = "read"  # the only Browser Box tool this lane may call (tests/test_no_contact.py)


class BrowserBoxError(RuntimeError):
    pass


class BrowserBox:
    def __init__(self, url: str, token: str, *, timeout: float = 120.0, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport,
                                       headers={"authorization": f"Bearer {token}", "user-agent": "lead-radar/2"})

    @classmethod
    def from_settings(cls, settings) -> "BrowserBox | None":
        url, token = settings.secret("BROWSERBOX_URL"), settings.secret("BROWSERBOX_TOKEN")
        return cls(url, token) if url and token else None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def read(self, url: str, role: str) -> Page | None:
        try:
            resp = await self._http.post(f"{self.url}/api/v1/{TOOL}", json={"url": url})
        except httpx.HTTPError as exc:
            raise BrowserBoxError(f"browser box {TOOL}: {type(exc).__name__}") from None
        if resp.status_code in (401, 403):
            raise BrowserBoxError(f"browser box {TOOL}: {resp.status_code} (token?)")
        if resp.status_code >= 400:
            return None
        data = resp.json()
        return Page(url, data.get("final_url") or url, int(data.get("status_code") or 200), as_html(data), role)


def as_html(data: dict) -> str:
    """Rebuild a minimal page from `read` output so the same detection rules apply."""
    links = "".join(f'<a href="{htmllib.escape(str(link), quote=True)}">{htmllib.escape(str(link))}</a>'
                    for link in data.get("links") or [])
    return (f"<html><head><title>{htmllib.escape(data.get('title') or '')}</title></head><body>"
            f"<p>{htmllib.escape(data.get('text') or '')}</p>{links}</body></html>")


async def verify(domain: str, bb: BrowserBox, fp=None, *, max_reads: int = 3):
    """The detect.verify flow through the real browser: homepage, then a linked (or the usual) program page."""
    from . import detect
    fp = fp or detect.load()
    home = await bb.read(f"https://{domain}/", "home")
    if home is None:
        det = detect.decide(domain, [], fp)
        det.error = "browser read failed"
        return det
    pages = [home]
    origin = home.final_url.split("/", 3)[:3]
    candidates = detect.program_links(home.html, home.final_url, fp) or ["/".join(origin) + p for p in fp.guess_paths[:2]]
    for url in candidates[: max_reads - 1]:
        page = await bb.read(url, "linked")
        if page is not None and page.status < 400:
            pages.append(page)
            det = detect.decide(domain, pages, fp)
            if det.commission_text:
                break
    det = detect.decide(domain, pages, fp)
    if home.status >= 400 and not det.has_program:
        det.reachable, det.error = False, f"homepage {home.status} (browser)"
    return det
