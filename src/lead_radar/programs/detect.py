"""Decide whether a company runs an affiliate program, from its own pages.

`decide` is pure (pages in, verdict out) so it is tested against saved HTML; `verify` does the fetching.

Rules (from the M0 spike, see config/program_fingerprints.yaml):
- Only structural evidence names a platform: a portal URL with the company's slug, or a tracking script.
  Prose that names a network ("networks like ShareASale") is ignored.
- A company can run more than one platform (instantly.ai: Rewardful and PartnerStack).
- The homepage is always checked: tracking scripts load site-wide.
- A program page that redirects to the homepage is no program page (kajabi.com/affiliates).
- No platform, but a program page with commission language, is an in-house program (ClickFunnels).
"""
from __future__ import annotations

import asyncio
import html as htmllib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import yaml
from selectolax.parser import HTMLParser

from ..config import REPO_ROOT

FINGERPRINTS = REPO_ROOT / "config" / "program_fingerprints.yaml"
SNIPPET = 200  # max chars of a verbatim evidence snippet


@dataclass(frozen=True)
class Platform:
    name: str
    segment: str
    portal: tuple[re.Pattern, ...]
    script: tuple[re.Pattern, ...]


@dataclass(frozen=True)
class Fingerprints:
    platforms: tuple[Platform, ...]
    platform_subdomains: frozenset[str]
    excluded_hosts: frozenset[str]
    commission: tuple[re.Pattern, ...]
    program_link: re.Pattern
    guess_paths: tuple[str, ...]

    def is_excluded(self, host: str) -> bool:
        host = bare_host(host)
        return any(host == h or host.endswith("." + h) for h in self.excluded_hosts)


@lru_cache(maxsize=4)
def load(path: Path | str = FINGERPRINTS) -> Fingerprints:
    data = yaml.safe_load(Path(path).read_text())
    platforms = tuple(
        Platform(name, p.get("segment", "mixed"),
                 tuple(re.compile(r, re.I) for r in p.get("portal") or []),
                 tuple(re.compile(r, re.I) for r in p.get("script") or []))
        for name, p in data["platforms"].items())
    return Fingerprints(platforms, frozenset(data["platform_subdomains"]), frozenset(data["excluded_hosts"]),
                        tuple(re.compile(r, re.I) for r in data["commission_patterns"]),
                        re.compile(data["program_link"], re.I), tuple(data["guess_paths"]))


def bare_host(host_or_url: str) -> str:
    host = urlsplit(host_or_url).hostname if "//" in host_or_url else host_or_url
    host = (host or "").lower().strip().rstrip(".")
    return host[4:] if host.startswith("www.") else host


@dataclass
class Page:
    url: str          # what we asked for
    final_url: str    # after redirects
    status: int
    html: str
    role: str = "home"  # home | linked | guessed


@dataclass
class Evidence:
    platform: str
    kind: str      # portal | script
    match: str     # verbatim, <= SNIPPET chars
    slug: str | None
    page: str

    def as_dict(self) -> dict:
        return {"platform": self.platform, "kind": self.kind, "match": self.match, "slug": self.slug, "page": self.page}


@dataclass
class Detection:
    domain: str
    has_program: bool = False
    platforms: list[str] = field(default_factory=list)
    program_url: str | None = None
    commission_text: str | None = None
    evidence: list[dict] = field(default_factory=list)
    segment: str = "unknown"
    name: str | None = None
    error: str | None = None
    pages_checked: int = 0
    reachable: bool = True


def find_evidence(html: str, page_url: str, fp: Fingerprints) -> list[Evidence]:
    found: list[Evidence] = []
    seen: set[tuple[str, str, str | None]] = set()
    for p in fp.platforms:
        for rx in p.portal:
            for m in rx.finditer(html):
                slug = m.group(1).lower() if m.groups() else None
                if slug and slug in fp.platform_subdomains:
                    continue
                key = (p.name, "portal", slug)
                if key not in seen:
                    seen.add(key)
                    found.append(Evidence(p.name, "portal", _around(html, m), slug, page_url))
        for rx in p.script:
            m = rx.search(html)
            if m and (p.name, "script", None) not in seen:
                seen.add((p.name, "script", None))
                found.append(Evidence(p.name, "script", _around(html, m), None, page_url))
    return found


def _around(html: str, m: re.Match) -> str:
    """The matched URL/attribute, widened to its quotes so the snippet is the whole verbatim value."""
    start = max(html.rfind('"', 0, m.start()), html.rfind("'", 0, m.start())) + 1
    ends = [i for i in (html.find('"', m.end()), html.find("'", m.end())) if i != -1]
    end = min(ends) if ends else m.end()
    if m.start() - start > SNIPPET or end - m.end() > SNIPPET:
        start, end = m.start(), m.end()
    return htmllib.unescape(html[start:end])[:SNIPPET]


def page_text(html: str) -> str:
    tree = HTMLParser(html)
    for node in tree.css("script, style, noscript, svg"):
        node.decompose()
    body = tree.body or tree.root
    return re.sub(r"\s+", " ", body.text(separator=" ") if body else "").strip()


def title(html: str) -> str | None:
    node = HTMLParser(html).css_first("title")
    text = re.sub(r"\s+", " ", node.text()).strip() if node else ""
    return text[:120] or None


def commission_text(text: str, fp: Fingerprints) -> str | None:
    for rx in fp.commission:
        m = rx.search(text)
        if m:
            start, end = max(0, m.start() - 60), min(len(text), m.end() + 60)
            if start > 0:  # snap to whole words so the quote never starts or ends mid-word
                start = text.find(" ", start, m.start()) + 1 or start
            if end < len(text):
                end = text.rfind(" ", m.end(), end) if text.rfind(" ", m.end(), end) > 0 else end
            return text[start:end].strip()[:SNIPPET]
    return None


def program_links(html: str, base_url: str, fp: Fingerprints) -> list[str]:
    """Same-site links whose href or text points at an affiliate/referral program page, best first."""
    base_host = bare_host(base_url)
    out: list[str] = []
    for a in HTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if not (fp.program_link.search(href) or fp.program_link.search(a.text() or "")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if bare_host(url) != base_host or url in out or is_article(url):
            continue
        out.append(url)
    out.sort(key=lambda u: 0 if re.search(r"affiliat", u, re.I) else 1)
    return out


# Paths that are articles about affiliate programs, not a program page (a listicle's "commission" is someone else's).
ARTICLE = re.compile(r"/(?:blog|blogs|insights|article|articles|news|learn|resources|guides?|category|tag|posts?|"
                     r"academy|glossary|podcast|webinars?)(?:/|$)|/20\d\d/|(?:^|[/-])(?:best|top)-|-vs-|alternatives",
                     re.I)


def is_article(url: str) -> bool:
    return bool(ARTICLE.search(urlsplit(url).path))


def clean_name(page_title: str | None) -> str | None:
    """'Modge | The AI workspace' -> 'Modge'."""
    if not page_title:
        return None
    first = re.split(r"\s+[|\u2013\u2014-]\s+|:\s+", page_title, maxsplit=1)[0].strip()
    return (first or page_title)[:80]


def _is_home(url: str) -> bool:
    return urlsplit(url).path.strip("/") == ""


def decide(domain: str, pages: list[Page], fp: Fingerprints | None = None) -> Detection:
    fp = fp or load()
    det = Detection(domain=domain, pages_checked=len(pages))
    if not pages:
        det.reachable = False
        return det
    evidence: list[Evidence] = []
    for page in pages:
        evidence.extend(find_evidence(page.html, page.final_url, fp))
    # one record per (platform, kind, slug) across pages
    uniq: dict[tuple, Evidence] = {}
    for e in evidence:
        uniq.setdefault((e.platform, e.kind, e.slug), e)
    det.evidence = [e.as_dict() for e in uniq.values()]
    det.platforms = sorted({e.platform for e in uniq.values()})

    home = next((p for p in pages if p.role == "home"), None)
    if home is not None:
        det.name = clean_name(title(home.html))
    program_pages = [p for p in pages if p.role != "home" and p.status < 400 and not _is_home(p.final_url)
                     and not is_article(p.final_url)]
    for page in program_pages:
        text = commission_text(page_text(page.html), fp)
        if text:
            det.commission_text, det.program_url = text, page.final_url
            break
    if det.program_url is None:
        portal = next((e for e in uniq.values() if e.kind == "portal"), None)
        if program_pages:
            det.program_url = program_pages[0].final_url
        elif portal is not None:
            det.program_url = portal.match if portal.match.startswith("http") else None

    det.has_program = bool(det.platforms) or (det.commission_text is not None and bool(program_pages))
    if det.has_program and not det.platforms:
        det.platforms = ["in-house"]
    det.segment = _segment(det, home, fp)
    return det


_SEG_WORDS = {
    "info": re.compile(r"\b(?:coaching|coach|course|courses|masterclass|mentorship|academy|webinar|funding|credit repair)\b", re.I),
    "saas": re.compile(r"\b(?:free trial|start for free|api|integrations|dashboard|saas|software|platform)\b", re.I),
    "ecommerce": re.compile(r"\b(?:add to cart|shop now|free shipping|checkout|cart)\b", re.I),
}


def _segment(det: Detection, home: Page | None, fp: Fingerprints) -> str:
    by_platform = {p.name: p.segment for p in fp.platforms}
    segs = {by_platform.get(p) for p in det.platforms} - {None, "mixed"}
    if len(segs) == 1:
        return segs.pop()
    if home is None:
        return "unknown"
    if "cdn.shopify.com" in home.html:
        return "ecommerce"
    text = page_text(home.html)[:20000]
    counts = {seg: len(rx.findall(text)) for seg, rx in _SEG_WORDS.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] else "unknown"


# fetching ------------------------------------------------------------------------------------------------

async def _get(http, url: str, role: str) -> Page | None:
    """One polite GET. 404s and refusals return None; bodies of other 4xx are kept (they still carry scripts)."""
    from ..listings.http import Disallowed
    try:
        resp = await http.get(url)
    except Disallowed:
        return None
    except httpx.HTTPStatusError as exc:
        resp = exc.response
        if resp is None or resp.status_code >= 500:
            return None
    except (httpx.HTTPError, UnicodeDecodeError):
        return None
    if resp is None:
        return None
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype and ctype:
        return None
    return Page(url, str(resp.url), resp.status_code, resp.text[:3_000_000], role)


async def verify(domain: str, http, fp: Fingerprints | None = None, *, max_linked: int = 2) -> Detection:
    """Fetch the homepage, the program pages it links to (or guessed paths if none), and decide."""
    fp = fp or load()
    domain = bare_host(domain)
    home = await _get(http, f"https://{domain}/", "home")
    if home is None or home.status >= 400:
        alt = await _get(http, f"https://www.{domain}/", "home")
        home = alt if alt is not None and (home is None or alt.status < home.status) else home
    if home is None:
        det = decide(domain, [], fp)
        det.error = "homepage unreachable"
        return det
    pages = [home]
    linked = program_links(home.html, home.final_url, fp)[:max_linked]
    for url in linked:
        page = await _get(http, url, "linked")
        if page is not None:
            pages.append(page)
    det = decide(domain, pages, fp)
    if not det.commission_text:  # nothing linked, or the linked pages said nothing: try the usual paths
        origin = f"{urlsplit(home.final_url).scheme}://{urlsplit(home.final_url).netloc}"
        tried = {p.final_url.rstrip("/") for p in pages}
        for path in fp.guess_paths:
            if origin + path in tried:
                continue
            page = await _get(http, origin + path, "guessed")
            if page is not None and page.status < 400 and not _is_home(page.final_url):
                pages.append(page)
                det = decide(domain, pages, fp)
                if det.commission_text or not det.has_program:
                    break
    if home.status >= 400 and not det.has_program:
        det.reachable, det.error = False, f"homepage {home.status}"
    return det


async def verify_many(domains: list[str], http, fp: Fingerprints | None = None, *, concurrency: int = 16,
                      on_result=None) -> list[Detection]:
    fp = fp or load()
    sem = asyncio.Semaphore(concurrency)
    out: list[Detection] = []

    async def one(d: str):
        async with sem:
            try:
                det = await verify(d, http, fp)
            except Exception as exc:  # one bad site never stops the batch
                det = Detection(domain=d, reachable=False, error=f"{type(exc).__name__}: {str(exc)[:120]}")
        out.append(det)
        if on_result:
            on_result(det)

    await asyncio.gather(*(one(d) for d in domains))
    return out
