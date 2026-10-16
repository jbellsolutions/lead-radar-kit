"""Public profiles and link-in-bio pages through ScrapeCreators (1 credit each, GET only).

Each parser reads the documented response shape defensively (docs/radar/scrapecreators-endpoints.md) and returns a
`Profile`: name, bio, links, website, location, company and any contact clues the profile itself publishes.
Reddit has no profile endpoint, so Reddit authors resolve from their post text only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..scrapecreators import OutOfCredits, ScrapeCreators, SkipItem, SourceError
from . import text as tx

SOURCE = "resolve"  # meter key: resolution credits count toward the daily cap like any other


@dataclass
class Profile:
    platform: str
    url: str | None = None
    name: str | None = None
    bio: str = ""
    links: list[str] = field(default_factory=list)
    website: str | None = None
    location: str | None = None
    company: str | None = None
    company_url: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _get(d, *path):
    for p in path:
        if isinstance(d, dict):
            d = d.get(p)
        elif isinstance(d, list) and isinstance(p, int) and len(d) > p:
            d = d[p]
        else:
            return None
    return d


def _links(items, *keys) -> list[str]:
    out = []
    for it in items or []:
        for k in keys:
            u = _s(it.get(k)) if isinstance(it, dict) else _s(it)
            if u.startswith("http") and u not in out:
                out.append(u)
                break
    return out


def parse_instagram(data: dict) -> Profile:
    u = _get(data, "data", "user") or {}
    links = _links(u.get("bio_links"), "url")
    ext = _s(u.get("external_url"))
    if ext and ext not in links:
        links.insert(0, ext)
    city = _get(u, "business_address_json", "city_name")
    return Profile("instagram", url=f"https://www.instagram.com/{_s(u.get('username'))}" if u.get("username") else None,
                   name=_s(u.get("full_name")) or None, bio=_s(u.get("biography")), links=links,
                   location=_s(city) or None, emails=[e for e in [_s(u.get("business_email")) or _s(u.get("public_email"))] if e],
                   phones=[p for p in [_s(u.get("business_phone_number")) or _s(u.get("contact_phone_number"))] if p])


def parse_tiktok(data: dict) -> Profile:
    u = data.get("user") or {}
    link = _s(_get(u, "bioLink", "link"))
    return Profile("tiktok", url=f"https://www.tiktok.com/@{_s(u.get('uniqueId'))}" if u.get("uniqueId") else None,
                   name=_s(u.get("nickname")) or None, bio=_s(u.get("signature")), links=[link] if link else [])


def parse_threads(data: dict) -> Profile:
    return Profile("threads", url=f"https://www.threads.com/@{_s(data.get('username'))}" if data.get("username") else None,
                   name=_s(data.get("full_name")) or None, bio=_s(data.get("biography")),
                   links=_links(data.get("bio_links"), "url"))


def parse_twitter(data: dict) -> Profile:
    leg = data.get("legacy") or {}
    links = _links(_get(leg, "entities", "url", "urls"), "expanded_url", "url")
    links += [u for u in _links(_get(leg, "entities", "description", "urls"), "expanded_url", "url") if u not in links]
    return Profile("x", url=f"https://x.com/{_s(leg.get('screen_name'))}" if leg.get("screen_name") else None,
                   name=_s(leg.get("name")) or None, bio=_s(leg.get("description")), links=links,
                   location=_s(leg.get("location")) or None)


def parse_youtube(data: dict) -> Profile:
    links = _links(data.get("links"), "url", "link")
    return Profile("youtube", url=_s(data.get("channel")) or _s(data.get("url")) or None, name=_s(data.get("name")) or None,
                   bio=_s(data.get("description")), links=links, emails=[e for e in [_s(data.get("email"))] if e])


def parse_facebook(data: dict) -> Profile:
    links = _links(data.get("links"), "url", "link")
    site = _s(data.get("website"))
    return Profile("facebook", url=_s(data.get("url")) or None, name=_s(data.get("name")) or None,
                   bio=_s(data.get("intro")) or _s(data.get("description")), links=links, website=site or None,
                   emails=[e for e in [_s(data.get("email"))] if e], phones=[p for p in [_s(data.get("phone"))] if p])


def parse_linkedin(data: dict) -> Profile:
    exp = (data.get("experience") or [{}])[0] or {}
    return Profile("linkedin", url=_s(data.get("url")) or _s(data.get("linkedInUrl")) or None,
                   name=_s(data.get("name")) or None, bio=_s(data.get("about")), location=_s(data.get("location")) or None,
                   company=_s(exp.get("name")) or None,
                   company_url=_s(exp.get("url")) if "linkedin.com/company/" in _s(exp.get("url")) else None)


def parse_linkedin_company(data: dict) -> Profile:
    loc = data.get("location") or {}
    where = ", ".join(x for x in (_s(loc.get("city")), _s(loc.get("country"))) if x) if isinstance(loc, dict) else _s(loc)
    return Profile("linkedin", name=None, company=_s(data.get("name")) or None, bio=_s(data.get("description")),
                   website=_s(data.get("website")) or None, location=where or None)


def parse_link_in_bio(data: dict, kind: str) -> Profile:
    links = _links(data.get("links"), "url")
    return Profile(kind, name=_s(data.get("name")) or _s(data.get("title")) or None,
                   bio=_s(data.get("description")) or _s(data.get("bio")), links=links,
                   emails=[e for e in [_s(data.get("email_address")) or _s(data.get("email"))] if e])


# platform -> (path, params builder, parser); handle-based unless noted
ENDPOINTS = {
    "instagram": ("/v1/instagram/profile", lambda h: {"handle": h, "trim": "true"}, parse_instagram),
    "tiktok": ("/v1/tiktok/profile", lambda h: {"handle": h}, parse_tiktok),
    "threads": ("/v1/threads/profile", lambda h: {"handle": h}, parse_threads),
    "x": ("/v1/twitter/profile", lambda h: {"handle": h}, parse_twitter),
    "youtube": ("/v1/youtube/channel", lambda h: {"handle": h} if not h.startswith("UC") else {"channelId": h}, parse_youtube),
    "facebook": ("/v1/facebook/profile", lambda url: {"url": url}, parse_facebook),        # url
    "linkedin": ("/v1/linkedin/profile", lambda url: {"url": url}, parse_linkedin),        # url
}
LINK_IN_BIO_PATHS = {"linktree": "/v1/linktree", "komi": "/v1/komi", "pillar": "/v1/pillar", "linkbio": "/v1/linkbio",
                     "linkme": "/v1/linkme"}
CACHE = "7d"  # a cached answer costs 0 credits


async def fetch(sc: ScrapeCreators, path: str, params: dict) -> dict | None:
    """One profile call. None when the profile is gone/private or the call failed (never raises but OutOfCredits)."""
    try:
        return await sc.get(path, {**params, "cache_max_age": CACHE} if path != "/v1/linkedin/profile" else params,
                            source=SOURCE)
    except OutOfCredits:
        raise
    except (SkipItem, SourceError):
        return None


async def profile(sc: ScrapeCreators, platform: str, key: str) -> Profile | None:
    spec = ENDPOINTS.get(platform)
    if not spec or not key:
        return None
    path, params, parse = spec
    data = await fetch(sc, path, params(key.lstrip("@")))
    return parse(data) if data else None


async def linkedin_company(sc: ScrapeCreators, url: str) -> Profile | None:
    data = await fetch(sc, "/v1/linkedin/company", {"url": url})
    return parse_linkedin_company(data) if data else None


async def link_in_bio(sc: ScrapeCreators, url: str) -> Profile | None:
    kind = tx.LINK_IN_BIO.get(tx.host(url))
    if not kind:
        return None
    data = await fetch(sc, LINK_IN_BIO_PATHS[kind], {"url": url})
    return parse_link_in_bio(data, kind) if data else None
