"""Seed sources for the program lane: where candidate companies come from.

- PartnerStack marketplace (market.partnerstack.com): public, server-rendered; the page state carries every
  listed program with its website, offer, tags and partner count. Every entry already runs a program.
- Search dorks (ScrapeCreators /v1/google/search, 1 credit per page): platform portal pages such as
  <slug>.getrewardful.com or af.uppromote.com/<shop>/register, plus in-house program pages by niche.
  A portal hit is a slug, not a domain: `resolve_portal` fetches the portal and takes the company's own site
  from its links (a *.myshopify.com link is followed to the store's real domain).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ..scrapecreators import SkipItem
from .detect import Fingerprints, bare_host, load

PARTNERSTACK_MARKET = "https://market.partnerstack.com/"
STATE_MARKER = "window.__INITIAL_STATE__ = "


@dataclass
class Seed:
    source: str                    # partnerstack-marketplace | serp
    ref: str                       # where it came from (marketplace slug, dork, result URL)
    domain: str | None = None
    portal_url: str | None = None  # set when the domain still has to be resolved
    name: str | None = None
    platform_hint: str | None = None
    partner_count: int | None = None
    offer: str | None = None
    tags: list[str] = field(default_factory=list)


# PartnerStack marketplace ----------------------------------------------------------------------------------

def parse_partnerstack_market(html: str) -> list[Seed]:
    i = html.find(STATE_MARKER)
    if i < 0:
        raise ValueError("PartnerStack marketplace: page state not found (layout changed?)")
    state, _ = json.JSONDecoder().raw_decode(html[i + len(STATE_MARKER):])
    companies = ((state.get("company") or {}).get("companies") or {})
    seeds = []
    for slug, c in companies.items():
        website = (c.get("website") or "").strip()
        if not website or c.get("archived") or c.get("test"):
            continue
        domain = bare_host(website if "//" in website else "https://" + website)
        if not domain or "." not in domain:
            continue
        tags = sorted({t.get("name") for t in c.get("tags") or []
                       if t.get("collection") in ("marketplace", "product") and t.get("name")})
        count = c.get("partnership_count")
        seeds.append(Seed("partnerstack-marketplace", slug, domain=domain, name=c.get("name") or c.get("splitName"),
                          platform_hint="partnerstack", partner_count=int(count) if isinstance(count, (int, float)) else None,
                          offer=(c.get("offer") or "")[:200] or None, tags=tags))
    return seeds


async def partnerstack_market(http) -> list[Seed]:
    resp = await http.get(PARTNERSTACK_MARKET)
    if resp is None:
        raise ValueError("PartnerStack marketplace: 404")
    return parse_partnerstack_market(resp.text)


# Search dorks ----------------------------------------------------------------------------------------------

def portal_of(url: str, fp: Fingerprints) -> tuple[str, str] | None:
    """(platform, slug) when url is a company's portal on an affiliate platform."""
    probe = url if "//" in url else "https://" + url
    for p in fp.platforms:
        for rx in p.portal:
            m = rx.search(probe)
            if m and m.groups():
                slug = m.group(1).lower()
                if slug not in fp.platform_subdomains:
                    return p.name, slug
    return None


def seeds_from_results(results: list[dict], dork: str, fp: Fingerprints) -> list[Seed]:
    seeds = []
    for r in results:
        url = (r.get("url") or r.get("link") or "").strip()
        if not url.startswith("http"):
            continue
        portal = portal_of(url, fp)
        if portal:
            seeds.append(Seed("serp", dork, portal_url=url, platform_hint=portal[0], name=None))
            continue
        host = bare_host(url)
        if not host or fp.is_excluded(host):
            continue
        seeds.append(Seed("serp", dork, domain=host))  # result titles are article titles, not company names
    return seeds


async def serp(sc, dorks: list[str], *, max_pages: int, budget: int) -> tuple[list[Seed], int, list[str]]:
    """Run each dork for up to max_pages pages, stopping at `budget` searches. Returns (seeds, searches, notes)."""
    fp = load()
    seeds: list[Seed] = []
    notes: list[str] = []
    used = 0
    for dork in dorks:
        got = 0
        for page in range(1, max_pages + 1):
            if used >= budget:
                notes.append(f"search budget {budget} reached; stopped before {dork!r} page {page}")
                return seeds, used, notes
            used += 1
            try:
                data = await sc.get("/v1/google/search", {"query": dork, "page": page}, source="programs")
            except SkipItem:  # ScrapeCreators answers 404 for "no results"
                break
            results = (data or {}).get("results") or []
            new = seeds_from_results(results, dork, fp)
            seeds.extend(new)
            got += len(new)
            if not results or not data.get("next_page"):
                break
        notes.append(f"{dork!r}: {got} candidates")
    return seeds, used, notes


# Portal -> company domain ----------------------------------------------------------------------------------

_NOT_A_SITE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|css|js|ico|woff2?)(?:\?|$)", re.I)


def site_from_portal(html: str, portal_url: str, fp: Fingerprints) -> str | None:
    """The company's own host on its portal page: the most-linked external host that isn't a platform/CDN."""
    tree = HTMLParser(html)
    hosts: Counter[str] = Counter()
    portal_host = bare_host(portal_url)
    for node in tree.css("a[href], link[href], img[src], script[src]"):
        raw = node.attributes.get("href") or node.attributes.get("src") or ""
        url = urljoin(portal_url, raw.strip())
        if not url.startswith("http") or _NOT_A_SITE.search(url) and not url.endswith(".myshopify.com"):
            continue
        host = bare_host(url)
        if not host or host == portal_host:
            continue
        if host.endswith(".myshopify.com"):
            hosts[host] += 3  # the store behind an UpPromote/GoAffPro portal; resolved by redirect below
            continue
        if fp.is_excluded(host) or any(host.endswith(s) for s in ("cdn.com", "fonts.net")):
            continue
        hosts[host] += 2 if node.tag == "a" else 1
    for m in re.finditer(r"https?://([a-z0-9-]+\.myshopify\.com)", html, re.I):  # often only in inline JSON
        hosts[m.group(1).lower()] += 3
    return hosts.most_common(1)[0][0] if hosts else None


async def resolve_portal(seed: Seed, http, fp: Fingerprints | None = None) -> str | None:
    fp = fp or load()
    try:
        resp = await http.get(seed.portal_url)
    except Exception:  # robots refusal, network, 4xx: unresolved, never fatal
        return None
    if resp is None:
        return None
    host = site_from_portal(resp.text, str(resp.url), fp)
    if host and host.endswith(".myshopify.com"):
        try:
            store = await http.get(f"https://{host}/")
        except Exception:
            return None
        final = bare_host(str(store.url)) if store is not None else ""
        return final if final and not final.endswith(".myshopify.com") else None
    return host


def dork_list(cfg: dict) -> list[str]:
    """Portal dorks as-is, plus each niche template x niche (Google stops paginating early; niches go deeper)."""
    dorks = list(cfg.get("portal_dorks") or [])
    for template in cfg.get("niche_dorks") or []:
        dorks.extend(template.format(niche=n) for n in cfg.get("niches") or [])
    return dorks
