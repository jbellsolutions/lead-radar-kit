"""Resolution: who is behind a lead, and is there enough to enrich them? (free + ScrapeCreators only)

A *subject* is one person or company, keyed across campaigns (`subject_key`). `resolve` gathers clues from the
post text, the author's public profile, a link-in-bio page and, for a LinkedIn author, their company page; at most
`max_credits` ScrapeCreators credits per subject. Then a gate decides whether a paid Data Box `enrich` call is
worth making:

    (a name or a company) AND (a domain, a LinkedIn URL or a company)

Anything that fails the gate is `unresolved` and costs nothing more. Nothing here contacts anyone.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Post
from ..normalize import canonical_url
from ..scrapecreators import ScrapeCreators
from . import profiles as pf
from . import text as tx

HANDLE_PLATFORMS = {"instagram", "tiktok", "threads", "x", "youtube"}
URL_PLATFORMS = {"linkedin", "facebook"}


def subject_key(post: Post) -> str | None:
    """Stable key for the person/company behind a post: platform + handle (listings: board + employer/company)."""
    if post.kind == "job":
        h = (post.author_handle or "").strip().lower()
        if not h:
            return None
        return h if h.split(":", 1)[0] in ("indeed", "wwr") else f"{post.platform}:{h}"
    if post.author_handle:
        return f"{post.platform}:{post.author_handle.strip().lstrip('@').lower()}"
    if post.author_url:
        return f"{post.platform}:url:{canonical_url(post.author_url).lower()}"
    return None


@dataclass
class Resolution:
    key: str
    status: str                      # ready | unresolved
    reason: str = ""
    args: dict = field(default_factory=dict)      # the Data Box `enrich` input
    sources: dict = field(default_factory=dict)   # field -> where it came from (provenance, no values)
    credits: int = 0


def gate(args: dict) -> str | None:
    """None when worth enriching, else the reason it isn't."""
    who = args.get("name") or args.get("company")
    where = args.get("domain") or args.get("linkedin_url") or args.get("company")
    if not who:
        return "no name or company"
    if not where:
        return "no domain, LinkedIn or company"
    return None


class _Acc:
    def __init__(self):
        self.args: dict = {"handles": {}, "emails_seen": [], "phones_seen": [], "hints": {}}
        self.sources: dict = {}
        self.links: list[str] = []

    def set(self, key: str, value, source: str) -> None:
        if value and not self.args.get(key):
            self.args[key] = value
            self.sources[key] = source

    def add(self, key: str, values, source: str) -> None:
        for v in values or []:
            if v and v not in self.args[key]:
                self.args[key].append(v)
                self.sources.setdefault(key, source)

    def absorb_text(self, text: str, source: str, *, own: bool) -> None:
        """own=True for text the person wrote about themselves (a bio, link-in-bio page): every clue counts.
        own=False for a post or listing body: only emails and phones they state; URLs and names in a post are as
        likely to be someone else's (a tool, a competitor) and would send enrichment to the wrong person."""
        c = tx.clues(text)
        self.add("emails_seen", c["emails"], source)
        self.add("phones_seen", c["phones"], source)
        if not own:
            return
        self.set("linkedin_url", (c["linkedin"] or [None])[0], source)
        self.set("website", (c["websites"] or [None])[0], source)
        self.set("company", (c["companies"] or [None])[0], source)
        self.links += [u for u in c["urls"] if u not in self.links]

    def absorb_profile(self, p: pf.Profile, source: str) -> None:
        self.set("name", p.name, source)
        self.set("company", p.company, source)
        if p.platform == "linkedin" and p.url and "/in/" in p.url:
            self.set("linkedin_url", p.url, source)
        self.set("website", p.website, source)
        self.args["hints"].setdefault("location", p.location) if p.location else None
        self.add("emails_seen", p.emails, source)
        self.add("phones_seen", p.phones, source)
        for link in p.links:
            kind = tx.classify_url(link)
            if kind == "linkedin_person":
                self.set("linkedin_url", link, source)
            elif kind == "website":
                self.set("website", link, source)
            if link not in self.links:
                self.links.append(link)
        if p.bio:
            self.absorb_text(p.bio, source + " bio", own=True)


async def resolve(sc: ScrapeCreators | None, posts: list[Post], *, verdict: dict | None = None,
                  max_credits: int = 4) -> Resolution:
    """posts: this subject's posts (newest first). verdict: the listing verdict for job posts."""
    post = posts[0]
    key = subject_key(post) or f"{post.platform}:post:{post.id}"
    acc = _Acc()
    start = sc.meter.credits.get(pf.SOURCE, 0) if sc else 0

    def spent() -> int:
        return (sc.meter.credits.get(pf.SOURCE, 0) - start) if sc else 0

    acc.args["hints"]["source_url"] = post.url
    if post.author_handle:
        acc.args["handles"][post.platform] = post.author_handle
    if post.kind == "job":
        raw = post.raw or {}
        acc.set("company", (verdict or {}).get("company_name") or raw.get("company"), f"{post.platform} listing")
        acc.set("website", raw.get("company_website"), f"{post.platform} listing")
        acc.args["hints"]["role_title"] = (verdict or {}).get("role_title") or post.title
        if raw.get("location"):
            acc.args["hints"]["location"] = raw["location"]
    # Profile sources first (the person's own words about themselves), post text last and only for stated
    # emails/phones: `set` keeps the first value it gets.
    if sc and post.kind != "job":
        prof = None
        if post.platform in HANDLE_PLATFORMS and post.author_handle:
            prof = await pf.profile(sc, post.platform, post.author_handle)
        elif post.platform in URL_PLATFORMS and post.author_url:
            prof = await pf.profile(sc, post.platform, post.author_url)
        if prof:
            acc.absorb_profile(prof, f"{post.platform} profile")
        bio_page = next((u for u in acc.links if tx.classify_url(u) == "link_in_bio"), None)
        if bio_page and spent() < max_credits:
            lib = await pf.link_in_bio(sc, bio_page)
            if lib:
                acc.absorb_profile(lib, "link-in-bio page")
        company_url = prof.company_url if prof else None
        if company_url and not acc.args.get("website") and spent() < max_credits:
            comp = await pf.linkedin_company(sc, company_url)
            if comp:
                acc.set("company", comp.company, "linkedin company page")
                acc.set("website", comp.website, "linkedin company page")
    if post.kind != "job" and post.platform != "reddit":
        acc.set("name", post.author_name, f"{post.platform} post")
    for p in posts:
        acc.absorb_text(p.text, f"{p.platform} {'listing' if p.kind == 'job' else 'post'}", own=False)

    a = acc.args
    if a.get("website"):
        a["website"] = tx.normalize_url(a["website"])
        if tx.classify_url(a["website"]) != "website":  # a storefront / link-in-bio page is not their domain
            a.pop("website")
            acc.sources.pop("website", None)
        else:
            acc.set("domain", tx.domain_of(a["website"]), acc.sources.get("website", ""))
    for e in a["emails_seen"]:
        acc.set("domain", tx.domain_of(e), acc.sources.get("emails_seen", ""))
    if a.get("name"):
        parts = a["name"].split()
        if len(parts) >= 2 and all(x[:1].isalpha() for x in parts[:2]):
            a["first_name"], a["last_name"] = parts[0], parts[-1]
    a = {k: v for k, v in a.items() if v not in (None, "", [], {})}
    reason = gate(a)
    return Resolution(key=key, status="unresolved" if reason else "ready", reason=reason or "", args=a,
                      sources=acc.sources, credits=spent())
