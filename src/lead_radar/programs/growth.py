"""Collect growth signals, build the finance list, expand lookalikes, rank both lists.

Flow (every step resumable from the DB; credits capped per run by `Budget`):
  collect candidates (jobs, staff profiles, posts, finance dorks)
  -> resolve each to its LinkedIn company page (cached in program_companies; 1 credit)
  -> exclusions + US filter -> store company + dated signals
  -> affiliate: program check on its own site (detect.verify, free) ; finance: ISO/broker program check (free)
  -> lead_score -> lists
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..scrapecreators import OutOfCredits, ScrapeCreators
from . import detect, signals as sg
from . import sources as srcs
from .run import _cell, _http, apply_source_evidence, verify as verify_programs
from .sources import common


@dataclass
class GrowthReport:
    candidates: dict[str, int] = field(default_factory=dict)
    resolved: int = 0
    unresolved: int = 0
    by_homepage: int = 0              # resolved from the brand's own site (no LinkedIn company page)
    excluded: dict[str, int] = field(default_factory=dict)
    new_companies: int = 0
    new_signals: dict[str, int] = field(default_factory=dict)
    credits: int = 0
    notes: list[str] = field(default_factory=list)
    sources: dict[str, dict] = field(default_factory=dict)   # per source: candidates, credits, error

    def bump(self, d: dict, k: str, n: int = 1) -> None:
        d[k] = d.get(k, 0) + n


def _window(days: int) -> str:
    return "last-day" if days <= 1 else "last-week" if days <= 7 else "last-month" if days <= 31 else "last-year"


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def _search(sc, budget: sg.Budget, query: str, *, window: str | None, pages: int) -> list[dict]:
    out = []
    for page in range(1, pages + 1):
        if not budget.ok():
            break
        params = {"query": query, "page": page}
        if window:
            params["date_posted"] = window
        data = await sg.sc_get(sc, "/v1/google/search", params, "programs")
        items = (data or {}).get("results") or []
        out.extend(items)
        if not items or not (data or {}).get("next_page"):
            break
    return out


# collectors -----------------------------------------------------------------------------------------------------

async def collect_jobs(sc, budget, cfg, days: int, pages: int = 2) -> list[sg.Candidate]:
    a = cfg["affiliate"]
    sites = " OR ".join(f"site:{s}" for s in a["job_sites"])
    out = []
    for title in a["titles"]:
        for r in await _search(sc, budget, f'"{title}" ({sites})', window=_window(days), pages=pages):
            url, t = r.get("url") or "", r.get("title") or ""
            name = sg.company_from_job(url, t)
            if name and not sg.NON_US_JOB.search(t):
                # the posting date when the snippet shows one; else the day we saw it inside the date window
                when = common.relative_date(r.get("description") or "") or (_today() if days <= 31 else None)
                out.append(sg.Candidate("affiliate", sg.Signal("job", url, sg.job_title(t), when, platform="job_board"),
                                        company_name=name))
    return out


async def collect_staff(sc, budget, cfg, pages: int = 2, finance: bool = False) -> list[sg.Candidate]:
    out = []
    if finance:
        f = cfg["finance"]
        kws = " OR ".join(f'"{k}"' for k in f["keywords"][:4])
        queries = [(t, f'site:linkedin.com/in "{t}" ({kws}) "United States"') for t in f["titles"]]
        kind, lst = "decision_maker", "finance"
    else:
        a = cfg["affiliate"]
        queries = [(t, f'site:linkedin.com/in "{t}" ({a["staff_keywords"]}) "United States"') for t in a["titles"]]
        kind, lst = "affiliate_staff", "affiliate"
    for _, q in queries:
        for r in await _search(sc, budget, q, window=None, pages=pages):
            parsed = sg.staff_from_profile(r.get("title") or "", r.get("description") or "")
            url = (r.get("url") or "").split("?")[0]
            if parsed and "/in/" in url:
                person, role, company = parsed
                out.append(sg.Candidate(lst, sg.Signal(kind, url, role, None, person, url), company_name=company))
    return out


async def collect_posts(sc, budget, cfg, days: int, pages: int = 2) -> list[sg.Candidate]:
    out = []
    for q in cfg["affiliate"]["post_queries"]:
        cursor = None
        for _ in range(pages):
            if not budget.ok():
                return out
            params = {"query": q, "date_posted": _window(days)}
            if cursor:
                params["cursor"] = cursor
            data = await sg.sc_get(sc, "/v1/linkedin/search/posts", params, "programs")
            posts = (data or {}).get("posts") or []
            for p in posts:
                author = p.get("author") or {}
                li = sg.company_url(author.get("url"))
                if not li or not p.get("url"):
                    continue  # person posts: their company isn't in the result, and it is never guessed
                text = p.get("description") or ""
                out.append(sg.Candidate("affiliate", sg.Signal("company_post", p["url"], text[:400], sg.post_date(p)),
                                        linkedin_url=li))
            cursor = (data or {}).get("cursor")
            if not posts or not cursor:
                break
    return out


async def collect_finance(sc, budget, cfg, pages: int = 2) -> list[sg.Candidate]:
    f = cfg["finance"]
    out: list[sg.Candidate] = []
    fp = detect.load()
    # 1) funders advertising an ISO/broker/referral program on their own site
    for kw in f["keywords"]:
        for ph in f["program_phrases"]:
            for r in await _search(sc, budget, f'"{kw}" {ph} -site:linkedin.com', window=None, pages=1):
                host = detect.bare_host(r.get("url") or "")
                if host and not fp.is_excluded(host) and "linkedin.com" not in host:
                    out.append(sg.Candidate("finance", domain=host))
    # 2) LinkedIn company pages, state by state (Google stops paginating early)
    for kw in f["keywords"]:
        for st in [None, *f["states"]]:
            q = f'site:linkedin.com/company "{kw}"' + (f' "{st}"' if st else "")
            for r in await _search(sc, budget, q, window=None, pages=pages if st is None else 1):
                li = sg.company_url(r.get("url"))
                if li:
                    out.append(sg.Candidate("finance", linkedin_url=li))
    return out


# resolution -----------------------------------------------------------------------------------------------------

def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in {"inc", "llc", "co", "the", "ltd", "corp",
                                                                                   "company", "group", "hq"}}


class Resolver:
    def __init__(self, sc, http, store, budget: sg.Budget, cfg: dict):
        self.sc, self.http, self.store, self.budget, self.cfg = sc, http, store, budget, cfg
        self.profiles: dict[str, sg.Profile | None] = {}
        self.by_name: dict[str, str | None] = {}
        self.fp = detect.load()

    async def profile(self, li: str) -> sg.Profile | None:
        if li in self.profiles:
            return self.profiles[li]
        if not self.budget.ok():
            return None
        data = await sg.sc_get(self.sc, "/v1/linkedin/company", {"url": li}, "programs")
        prof = sg.Profile.from_api(li, data) if data else None
        self.profiles[li] = prof
        return prof

    async def linkedin_for_name(self, name: str) -> str | None:
        key = name.lower()
        if key in self.by_name:
            return self.by_name[key]
        li = None
        results = await _search(self.sc, self.budget, f'site:linkedin.com/company "{name}"', window=None, pages=1)
        for r in results[:3]:
            cand = sg.company_url(r.get("url"))
            if cand and common.names_match(name, (r.get("title") or "").split("|")[0].split(" - ")[0]):
                li = cand
                break
        self.by_name[key] = li
        return li

    async def linkedin_for_domain(self, domain: str) -> str | None:
        page = await detect._get(self.http, f"https://{domain}/", "home")
        if page is not None:
            for m in sg.LI_COMPANY.finditer(page.html):
                return sg.company_url(m.group(0))
        if not self.budget.ok():
            return None
        for r in (await _search(self.sc, self.budget, f'site:linkedin.com/company "{domain}"', window=None, pages=1))[:3]:
            cand = sg.company_url(r.get("url"))
            if cand:
                return cand
        return None

    @staticmethod
    def _same_company(prof: sg.Profile, c: sg.Candidate) -> bool:
        """A LinkedIn page found from a candidate's domain belongs to it: same website domain, or the names match."""
        if prof.domain and common.registrable(prof.domain) == common.registrable(c.domain or ""):
            return True
        stem = common.registrable(c.domain or "").split(".")[0]
        return common.names_match(prof.name, c.company_name or stem) or common.norm(prof.name) == common.norm(stem)

    def known(self, c: sg.Candidate) -> dict | None:
        """The stored company a candidate points at (by domain or LinkedIn page), without any request."""
        domain = c.domain or (self.store.program_domain_for_linkedin(c.linkedin_url) if c.linkedin_url else None)
        return self.store.program_company(domain) if domain else None

    async def resolve(self, c: sg.Candidate) -> sg.Profile | None:
        li, via_domain = c.linkedin_url, False
        if not li and c.domain:
            known = self.store.program_company(c.domain)
            li = (known or {}).get("linkedin_url")
            if not li:
                li, via_domain = await self.linkedin_for_domain(c.domain), True
        if not li and c.company_name:
            li = await self.linkedin_for_name(c.company_name)
        if not li:
            return None
        prof = await self.profile(li)
        if prof and via_domain and not self._same_company(prof, c):
            return None  # the homepage linked some other company's page (partner, parent, wrong slug): never guess
        if prof and prof.domain in self.cfg["_shorteners"]:
            prof.domain = await self.unshorten(prof.website)
        if prof and not prof.domain and c.domain:
            prof.domain = c.domain
        return prof

    async def unshorten(self, url: str | None) -> str | None:
        """Follow a shortlink / link-in-bio to the company's own host (plain HTTP); None if it never leaves them."""
        if not url:
            return None
        page = await detect._get(self.http, url if "//" in url else "https://" + url, "home")
        host = detect.bare_host(page.final_url) if page is not None else None
        return host if host and host not in self.cfg["_shorteners"] else None


# the run --------------------------------------------------------------------------------------------------------

async def _finance_check(http, domain: str) -> tuple[sg.Signal | None, bool]:
    """(ISO/broker program signal, direct funder?) from the funder's own homepage and partner pages. Free."""
    home = await detect._get(http, f"https://{domain}/", "home")
    if home is None or home.status >= 400:
        return None, False
    pages = [home]
    for url in sg.finance_links(home.html, home.final_url):
        page = await detect._get(http, url, "linked")
        if page is not None and page.status < 400:
            pages.append(page)
    direct = bool(sg.load()["_rx"]["direct"].search(detect.page_text(home.html)))
    return sg.finance_program(pages), direct


US_STATES = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|"
             "OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC")
US_STATE_NAMES = ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|"
                  "Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|"
                  "Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|"
                  "New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|"
                  "South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|"
                  "Wisconsin|Wyoming")
US_ADDRESS = re.compile(rf"\b(?:{US_STATES}|{US_STATE_NAMES}),?\s+\d{{5}}(?:-\d{{4}})?\b")
US_WORDS = re.compile(r"\b(?:United States|U\.S\.A\.?|USA)\b")
# Signs the company itself is registered elsewhere (a country name alone is not: "we ship to Canada").
NON_US = re.compile(r"\b(?:Ltd\.?|Pty|GmbH|S\.?L\.?U?|B\.V\.|VAT (?:No|number|reg\w*)|Registered in (?:England|"
                    r"Ireland|Scotland|Wales)|Company (?:No|number|registration)|ABN|GSTIN|Companies House)\b|£\s?\d|€\s?\d")
STANDARD_CONTACT_PATHS = ("/policies/contact-information", "/pages/contact", "/contact", "/pages/about-us", "/about")


def homepage_us(text: str) -> bool:
    """A US company by its own site: a US street address (state + ZIP), or 'United States/USA' with no sign of a
    company registered elsewhere."""
    if US_ADDRESS.search(text):
        return True
    return bool(US_WORDS.search(text)) and not NON_US.search(text)


async def homepage_profile(http, c: sg.Candidate) -> sg.Profile | None:
    """For a candidate with a domain but no LinkedIn company page (small brands): a profile from its own homepage,
    accepted only when the page is live, not parked, and names the brand. Country is 'US' only on US evidence."""
    if not c.domain or http is None:
        return None
    page = await detect._get(http, f"https://{c.domain}/", "home")
    if page is None or page.status >= 400 or common.parked(page.html):
        return None
    final = common.host_of(page.final_url)
    if final and common.registrable(final) != common.registrable(c.domain):
        return None  # redirected to another company
    brand = c.company_name or c.domain.split(".")[0]
    if not common.page_names_brand(page.html, brand) and not common.page_names_brand(page.html, c.domain.split(".")[0]):
        return None
    text = detect.page_text(page.html)
    title = re.search(r"<title[^>]*>(.*?)</title>", page.html[:200_000], re.I | re.S)
    name = c.company_name or (title.group(1).split("|")[0].split(" - ")[0].strip() if title else c.domain)
    us = homepage_us(text)
    if not us:  # small brands put their address on the contact/about page: the site's own links, else standard paths
        base = page.final_url.split("?")[0].rstrip("/")
        tries = contact_links(page.html, page.final_url)[:2]
        tries += [base.split("/", 3)[0] + "//" + base.split("/", 3)[2] + p for p in STANDARD_CONTACT_PATHS]
        seen: set[str] = set()
        for url in tries:
            if url in seen or len(seen) >= 3:
                continue
            seen.add(url)
            sub = await detect._get(http, url, "linked")
            if sub is not None and sub.status < 400 and homepage_us(detect.page_text(sub.html)):
                us = True
                break
    return sg.Profile(linkedin_url="", name=re.sub(r"\s+", " ", name)[:120], domain=c.domain,
                      country="US" if us else None, industry="",
                      text=f"{name} {(title.group(1) if title else '')} {text[:3000]}")


def contact_links(html: str, base: str) -> list[str]:
    """Same-site contact / about / legal pages linked from a homepage, contact first."""
    from urllib.parse import urljoin
    root = common.registrable(common.host_of(base) or "")
    found: list[tuple[int, str]] = []
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"']", html[:400_000], re.I):
        url = urljoin(base, m.group(1))
        host = common.host_of(url)
        path = url.split("?")[0].lower()
        if not host or common.registrable(host) != root:
            continue
        rank = 0 if re.search(r"/(?:pages/)?contact", path) else 1 if re.search(r"/(?:pages/)?about", path) else \
            2 if re.search(r"/(?:policies/|pages/)?(?:terms|privacy|legal|imprint)", path) else None
        if rank is not None and url not in [u for _, u in found]:
            found.append((rank, url))
    return [u for _, u in sorted(found)]


async def process(cands: list[sg.Candidate], resolver: Resolver, store, rep: GrowthReport) -> list[str]:
    """Resolve candidates, store companies and signals. Returns the domains touched."""
    touched: list[str] = []
    for c in cands:
        known = resolver.known(c)
        if known and not (c.require_us and known.get("country") is None):
            # already stored: add the new signal without paying for the LinkedIn page again
            if known.get("excluded"):
                rep.bump(rep.excluded, "already excluded")
                continue
            if c.require_us and known.get("country") != "US":
                rep.bump(rep.excluded, "US not confirmed")
                continue
            rep.resolved += 1
            if c.signal and store.add_program_signal(known["domain"], c.signal):
                rep.bump(rep.new_signals, c.signal.kind)
            touched.append(known["domain"])
            continue
        prof = await resolver.resolve(c)
        if (prof is None or not prof.domain) and c.domain:
            prof = await homepage_profile(resolver.http, c)  # no LinkedIn page: the brand's own site, free
            rep.by_homepage += prof is not None
        if prof is None or not prof.domain:
            rep.unresolved += 1
            continue
        if c.require_us and prof.country != "US":
            rep.bump(rep.excluded, "US not confirmed")  # social/community sources: listed only when confirmed US
            continue
        rep.resolved += 1
        domain = prof.domain
        lst = c.list
        if lst == "affiliate" and sg.load()["_rx"]["finance"].search(f"{prof.industry} {prof.text}") \
                and prof.industry in ("Financial Services", "Banking", "Capital Markets"):
            lst = "finance"
        reason = sg.exclusion(prof, lst)
        if reason:
            rep.bump(rep.excluded, reason.split(" (")[0])
        new = store.save_company_profile(domain, lst=lst, source=f"signal:{c.signal.kind if c.signal else lst}",
                                         ref=(c.signal.url if c.signal else prof.linkedin_url)[:300], name=prof.name or None,
                                         linkedin_url=prof.linkedin_url or None, country=prof.country, hq=prof.hq,
                                         size=prof.size, industry=prof.industry, excluded=reason)
        rep.new_companies += new
        sigs = ([c.signal] if c.signal else []) + sg.program_posts(prof, lst)
        for s in sigs:
            if store.add_program_signal(domain, s):
                rep.bump(rep.new_signals, s.kind)
        touched.append(domain)
    return touched


async def run_sources(sc, http, store, res: Resolver, bud: sg.Budget, cfg: dict, rep: GrowthReport,
                      steps: tuple[str, ...], lists: tuple[str, ...], days: int, say) -> list[str]:
    """Run the registered platform sources named in `steps` (sources.REGISTRY order). A source that raises is
    recorded and skipped; running out of credits stops the remaining sources. Candidates of each source are
    resolved and stored before the next source starts, so a later failure never loses them."""
    touched: list[str] = []
    for name in srcs.REGISTRY:
        if name not in steps or not bud.ok():
            continue
        try:
            mod = srcs.load(name)
        except ImportError as exc:
            rep.sources[name] = {"error": f"not available: {exc}"[:200]}
            continue
        conf = srcs.settings(name, mod, cfg)
        if not conf.get("enabled", True) or days < int(conf.get("min_days", 0)):
            continue  # off, or needs a longer look-back than this run (e.g. weekly-only sources)
        sub = bud.sub(int(conf.get("credits", 20)))
        before = sc.meter.total_credits()
        ctx = srcs.SourceContext(sc=sc, http=http, budget=sub, cfg=conf, signals_cfg=cfg, days=days,
                                 known=lambda d: store.program_company(d) is not None, say=say,
                                 http_cap=int(conf.get("http_cap", 120)))
        info: dict = {}
        try:
            got = [c for c in await mod.collect(ctx) if c.list in lists]
            info["candidates"] = len(got)
            rep.bump(rep.candidates, name, len(got))
            touched += await process(got, res, store, rep)
        except OutOfCredits:
            info["error"] = "out of credits"
            rep.sources[name] = info | {"credits": sc.meter.total_credits() - before}
            break
        except Exception as exc:  # one broken source must not fail the run
            info["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        info["credits"] = sc.meter.total_credits() - before
        rep.sources[name] = info
        say(f"{name}: {info.get('candidates', 0)} candidates, {info['credits']} credits"
            + (f", failed ({info['error']})" if info.get("error") else ""))
    return touched


def rescore(store, domains: list[str] | None = None) -> int:
    rows = store.program_companies()
    if domains is not None:
        keep = set(domains)
        rows = [r for r in rows if r["domain"] in keep]
    sigs = store.program_signals([r["domain"] for r in rows])
    for r in rows:
        r["_direct"] = "direct funder" in (r.get("tags") or [])
        store.set_lead_score(r["domain"], sg.lead_score(r, sigs.get(r["domain"], [])))
    return len(rows)


async def run(settings, store, *, days: int = 30, budget: int = 300, lists: tuple[str, ...] = ("affiliate", "finance"),
              steps: tuple[str, ...] = ("jobs", "posts", "staff", "finance", "profile", "verify"),
              profile_limit: int = 400, progress=None) -> GrowthReport:
    cfg = sg.load()
    rep = GrowthReport()
    http = _http()
    say = progress or (lambda msg: None)
    try:
        async with ScrapeCreators(settings.require("SCRAPECREATORS_API_KEY"), concurrency=4) as sc:
            bud = sg.Budget(sc, budget)
            res = Resolver(sc, http, store, bud, cfg)
            cands: list[sg.Candidate] = []
            if "affiliate" in lists:
                if "jobs" in steps:
                    got = await collect_jobs(sc, bud, cfg, days)
                    rep.candidates["job"] = len(got); cands += got; say(f"jobs: {len(got)} postings")
                if "posts" in steps:
                    got = await collect_posts(sc, bud, cfg, days)
                    rep.candidates["company_post"] = len(got); cands += got; say(f"posts: {len(got)} company posts")
                if "staff" in steps:
                    got = await collect_staff(sc, bud, cfg)
                    rep.candidates["affiliate_staff"] = len(got); cands += got; say(f"staff: {len(got)} profiles")
            if "finance" in lists and "finance" in steps:
                got = await collect_finance(sc, bud, cfg)
                got += await collect_staff(sc, bud, cfg, finance=True)
                rep.candidates["finance"] = len(got); cands += got; say(f"finance: {len(got)} candidates")
            # dedupe candidates that point at the same page/company
            seen, uniq = set(), []
            for c in cands:
                key = (c.list, c.signal.url if c.signal else (c.linkedin_url or c.domain or c.company_name))
                if key not in seen:
                    seen.add(key); uniq.append(c)
            touched = await process(uniq, res, store, rep)
            say(f"resolved {rep.resolved}, unresolved {rep.unresolved}, new companies {rep.new_companies}")
            # every other platform: one isolated source at a time, each within its own credit cap
            touched += await run_sources(sc, http, store, res, bud, cfg, rep, steps, lists, days, say)
            # profile already-known affiliate companies (US filter, size, company posts), best first
            if "profile" in steps and "affiliate" in lists:
                todo = [r for r in store.program_companies(["verified"]) if r["list"] == "affiliate"
                        and not r.get("profiled_at")][:profile_limit]
                todo.sort(key=lambda r: -r["score"])
                touched += await process([sg.Candidate("affiliate", domain=r["domain"]) for r in todo], res, store, rep)
                say(f"profiled {len(todo)} known companies")
            # finance: ISO/broker program check on each funder's own site (free)
            fin = [r for r in store.program_companies() if r["list"] == "finance" and not r.get("excluded")
                   and r["domain"] in set(touched)]
            for r in fin:
                sig, direct = await _finance_check(http, r["domain"])
                if sig and store.add_program_signal(r["domain"], sig):
                    rep.bump(rep.new_signals, sig.kind)
                tags = set(r.get("tags") or []) | ({"direct funder"} if direct else set())
                store.conn.execute("UPDATE program_companies SET status='verified', tags=?, "
                                   "platforms=CASE WHEN ? THEN '[\"iso-program\"]' ELSE platforms END WHERE domain=?",
                                   (json.dumps(sorted(tags)), bool(sig), r["domain"]))
            rep.credits = sc.meter.total_credits()
    finally:
        await http.aclose()
    if "verify" in steps:
        await verify_programs(settings, store, concurrency=16)  # new affiliate companies: program check, free
        apply_source_evidence(store)
    rescore(store)
    return rep


async def lookalikes(settings, store, *, budget: int = 200, per_list: int | None = None, progress=None) -> GrowthReport:
    """One hop of LinkedIn 'similar pages' from the top companies of each list; every lookalike is checked like any
    other company (US, exclusions, finance match / program check)."""
    cfg = sg.load()
    per_list = per_list or cfg["lookalikes"]["seeds_per_list"]
    rep = GrowthReport()
    http = _http()
    try:
        async with ScrapeCreators(settings.require("SCRAPECREATORS_API_KEY"), concurrency=4) as sc:
            bud = sg.Budget(sc, budget)
            res = Resolver(sc, http, store, bud, cfg)
            known = {r["linkedin_url"] for r in store.program_companies() if r.get("linkedin_url")}
            cands = []
            for lst in ("affiliate", "finance"):
                top = [r for r in store.program_companies() if r["list"] == lst and r["lead_score"] > 0
                       and r.get("linkedin_url")]
                top.sort(key=lambda r: -r["lead_score"])
                for r in top[:per_list]:
                    prof = await res.profile(r["linkedin_url"])
                    for li in (prof.similar if prof else []):
                        if li not in known:
                            known.add(li)
                            cands.append(sg.Candidate(lst, sg.Signal("lookalike", li, f"similar to {r['name'] or r['domain']}"),
                                                      linkedin_url=li))
            cands = cands[: cfg["lookalikes"]["max_new"]]
            rep.candidates["lookalike"] = len(cands)
            touched = await process(cands, res, store, rep)
            for d in touched:
                row = store.program_company(d)
                if row and row["list"] == "finance" and not row.get("excluded"):
                    sig, direct = await _finance_check(http, d)
                    if sig:
                        store.add_program_signal(d, sig)
                    store.conn.execute("UPDATE program_companies SET status='verified' WHERE domain=?", (d,))
            rep.credits = sc.meter.total_credits()
    finally:
        await http.aclose()
    await verify_programs(settings, store, concurrency=16)
    apply_source_evidence(store)
    rescore(store)
    return rep


# lists ----------------------------------------------------------------------------------------------------------

LIST_COLUMNS = ["rank", "company", "domain", "website", "linkedin", "hq", "size", "industry", "score", "why now",
                "affiliate platform", "program url", "commission / program terms", "partner count", "signal links",
                "person", "person linkedin", "first signal seen"]


def is_new(row: dict, signals: list[dict], since: str) -> bool:
    """New on/after `since` (YYYY-MM-DD): the company itself or any of its signals was first seen then."""
    return row["first_seen"][:10] >= since or any(x["first_seen"][:10] >= since for x in signals)


def top_list(store, lst: str, *, limit: int | None = None, min_score: int = 1, since: str | None = None,
             require_signal: bool | None = None) -> list[dict]:
    """Eligible companies of one list, best first, each with its signals. Affiliate rows need a verified program
    and (by default) at least one growth signal; finance rows are all prime."""
    rows = [r for r in store.program_companies() if r["list"] == lst and not r.get("excluded")
            and r["lead_score"] >= min_score and (r.get("country") in (None, "US"))]
    sigs = store.program_signals([r["domain"] for r in rows])
    need = (lst == "affiliate") if require_signal is None else require_signal
    out = []
    for r in rows:
        s = sigs.get(r["domain"], [])
        if need and not s:  # a lookalike counts: it is listed, but ranks below real growth signals
            continue
        if since and not is_new(r, s, since):
            continue
        out.append({**r, "signals": s})
    out.sort(key=lambda r: (-r["lead_score"], -(r["partner_count"] is not None), r["domain"]))
    return out[:limit] if limit else out


def export_list(store, lst: str, path: Path, *, limit: int | None = None) -> int:
    rows = top_list(store, lst, limit=limit)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(LIST_COLUMNS)
        for i, r in enumerate(rows, 1):
            s = r["signals"]
            person = next((x for x in s if x.get("person_name")), {})
            program = next((x for x in s if x["kind"] == "partner_program"), {})
            w.writerow([_cell(v) for v in (
                i, sg.clean_name(r["name"], r["domain"]), r["domain"], f"https://{r['domain']}", r.get("linkedin_url") or "",
                r.get("hq") or "", r.get("size") or "", r.get("industry") or "", r["lead_score"], sg.why(s),
                ", ".join(r["platforms"]), r["program_url"] or program.get("url", ""),
                r["commission_text"] or program.get("quote", ""),
                r["partner_count"] if r["partner_count"] is not None else "",
                " ".join(x["url"] for x in s[:3]), person.get("person_name", ""), person.get("person_url", ""),
                min((x["first_seen"][:10] for x in s), default=""))])
    return len(rows)
