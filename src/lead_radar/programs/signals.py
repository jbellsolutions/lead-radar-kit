"""Growth signals and the finance list (config/program_signals.yaml).

Two ranked lists of partnership prospects, built from public data only (no LinkedIn login; nothing runs as any
user's account):

- affiliate: companies that run an affiliate/partner/referral program, ranked by dated signals that they are
  putting resources into it: hiring an affiliate/partner manager, affiliate managers on staff, posts about the
  program (company page or people), few partners.
- finance: business-funding companies (MCA, SBA, equipment, working capital), all prime; ranked first by an
  ISO/broker/referral partner program on their own site (their version of an affiliate program).

Every company is resolved to its LinkedIn company page (1 ScrapeCreators credit, cached) for the US filter, size,
industry, recent posts and LinkedIn's own "similar pages" (lookalikes). Exclusions (sellers of affiliate
management, recruiters, gambling/adult/forex/crypto trading, finance lead vendors, your own `do_not_prospect` list) apply to
both lists. Person names/URLs on signals are contact data: never logged.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..config import REPO_ROOT
from ..scrapecreators import OutOfCredits, SkipItem, SourceError
from . import detect

CONFIG = REPO_ROOT / "config" / "program_signals.yaml"
LI_COMPANY = re.compile(r"https?://(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/company/([A-Za-z0-9_\-%.]+)", re.I)
FRESH_DAYS = {"job": 45, "company_post": 90, "person_post": 60, "paid_ad": 60, "social_post": 30, "community_post": 45}
# Every signal kind program_signals accepts (migration 011). platform says where it came from.
KINDS = ("job", "company_post", "person_post", "affiliate_staff", "decision_maker", "partner_program", "lookalike",
         "paid_ad", "social_post", "community_post")
PLATFORM_LABEL = {"linkedin": "LinkedIn", "job_board": "Job board", "web": "Website", "meta_ads": "Meta ads",
                  "instagram": "Instagram", "threads": "Threads", "facebook": "Facebook", "tiktok": "TikTok",
                  "youtube": "YouTube", "reddit": "Reddit", "hackernews": "Hacker News", "indiehackers": "Indie Hackers",
                  "producthunt": "Product Hunt", "builtin": "Built In", "indeed": "Indeed", "upwork": "Upwork",
                  "timeinbusiness": "Time in Business", "shopify": "Shopify Community", "google": "Web"}


@lru_cache(maxsize=2)
def load(path: Path | str = CONFIG) -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    ex = cfg["exclude"]
    cfg["_rx"] = {k: re.compile(ex[k], re.I) for k in ("verticals", "sellers", "seller_topic", "recruiters",
                                                       "finance_vendors", "vendors")}
    cfg["_rx"]["program_job"] = re.compile(cfg["affiliate"]["program_job"], re.I)
    cfg["_rx"]["program_post"] = re.compile(cfg["affiliate"]["program_post"], re.I)
    cfg["_rx"]["vendor_names"] = re.compile(
        r"(?<![a-z0-9])(?:" + "|".join(re.escape(n.lower()) for n in ex.get("vendor_names") or []) + r")(?![a-z0-9])")
    cfg["_shorteners"] = {d.lower() for d in ex.get("shorteners") or []}
    cfg["_rx"]["finance"] = re.compile(cfg["finance"]["match"], re.I)
    cfg["_rx"]["fin_link"] = re.compile(cfg["finance"]["program_link"], re.I)
    cfg["_rx"]["fin_text"] = re.compile(cfg["finance"]["program_text"], re.I)
    cfg["_rx"]["direct"] = re.compile(cfg["finance"]["direct_funder"], re.I)
    cfg["_rx"]["post"] = re.compile(cfg["affiliate"]["post_keywords"], re.I)
    cfg["_dnp"] = [n.lower() for n in cfg.get("do_not_prospect") or []]
    return cfg


@dataclass
class Signal:
    kind: str
    url: str
    quote: str = ""
    signal_date: str | None = None
    person_name: str | None = None
    person_url: str | None = None
    platform: str = "linkedin"          # where it was seen (PLATFORM_LABEL keys); not part of the id

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.kind}|{self.url}".encode()).hexdigest()


@dataclass
class Candidate:
    """Something that points at a company, before we know its domain."""
    list: str                       # affiliate | finance
    signal: Signal | None = None
    company_name: str | None = None
    linkedin_url: str | None = None
    domain: str | None = None
    require_us: bool = False            # social/community sources: listed only when LinkedIn says the company is US


# parsing (pure) ------------------------------------------------------------------------------------------------

def company_url(url: str | None) -> str | None:
    """Canonical https://www.linkedin.com/company/<slug> for any LinkedIn company link, else None."""
    m = LI_COMPANY.search(url or "")
    return f"https://www.linkedin.com/company/{m.group(1).rstrip('/').lower()}" if m else None


def company_from_job(url: str, title: str) -> str | None:
    """Hiring company from a job-board result. Titles: 'HighLevel - Affiliate Manager - Lever',
    'Affiliate Manager @ Toogeza - Jobs', 'Job Application for X at Acme', 'Acme hiring X in Austin, TX | LinkedIn'."""
    host, path = urlsplit(url).netloc.lower(), urlsplit(url).path.strip("/").split("/")
    title = (title or "").strip()
    if "lever.co" in host:
        name = title.split(" - ")[0] if " - " in title else (path[0] if path else "")
    elif "ashbyhq.com" in host:
        m = re.search(r"@\s*(.+?)(?:\s+-\s+Jobs)?$", title)
        name = m.group(1) if m else (path[0] if path else "")
    elif "greenhouse.io" in host:
        m = re.search(r"\bat\s+(.+?)$", title)
        name = m.group(1) if m else (path[0] if path else "")
    elif "workable.com" in host:  # '<Role> - <Company> - Jobs by Workable' / '... - Application'
        parts = [p.strip() for p in title.split(" - ")
                 if not re.fullmatch(r"(?i)(?:application|apply|jobs by workable|workable|careers?|jobs)", p.strip())]
        name = parts[-1] if len(parts) >= 2 else (path[0] if path else "")
    elif "linkedin.com" in host:
        m = re.match(r"(.+?)\s+hiring\s+", title)
        name = m.group(1) if m else ""
    else:
        name = ""
    name = re.sub(r"\s+", " ", name.replace("-", " ") if name and name == name.lower() and " " not in name else name)
    return name.strip(" |-") or None


NON_US_JOB = re.compile(r"\b(?:EMEA|APAC|APJ|SEA|LATAM|LatAm|ANZ|MENA|DACH|Nordics?|UK|U\.K\.|Europe|European|"
                        r"Asia|India|Germany|France|Spain|Italy|Brazil|Mexico|Japan|Korea|China|Singapore|"
                        r"Australia|Canada|Philippines|Indonesia|Vietnam|Thailand|Nigeria|Kenya|Dubai|UAE|"
                        r"London|Berlin|Paris|Toronto|Sydney|Bangalore|Bengaluru|Manila)\b")
JUNK_NAME = re.compile(r"(?i)^(?:home(?:page)?|access denied|just a moment.*|attention required.*|403.*|404.*|"
                       r"page not found|not found|welcome.*|careers at .*|log ?in|sign ?in|untitled|index)$")


def clean_name(name: str | None, domain: str) -> str:
    """A display name: the stored name unless it is a page title rather than a company name ('Home', 'Access
    Denied', 'Careers at X', a long tagline), else the domain's name ('rula.com' -> 'Rula')."""
    n = re.sub(r"\s+", " ", (name or "")).strip()
    m = re.match(r"(?i)careers at (.+)$", n)
    if m:
        return m.group(1).strip()
    if n and not JUNK_NAME.match(n) and len(n.split()) <= 5 and not re.search(r"\s[|–—]\s|:\s", n):
        return n
    stem = (domain or "").split(".")[0]
    return stem[:1].upper() + stem[1:] if stem else n


def job_title(title: str) -> str:
    return re.sub(r"\s*[|-]\s*(?:Lever|Jobs|LinkedIn)$", "", (title or "").strip())[:160]


SELLER_BIO = re.compile(r"\b(?:i (?:help|launch|grow|build)|helping|freelance|consultant|consulting|agency|"
                        r"for hire|let'?s connect|fractional|open to work|self[- ]employed)\b", re.I)


def staff_from_profile(title: str, snippet: str = "") -> tuple[str, str, str] | None:
    """(person, role, company) from 'Jane Doe - Affiliate Manager - Acme | LinkedIn' or from a headline naming the
    company ('Jane Doe - Senior Affiliate Manager at Acme - LinkedIn', '... @ Acme'). None for sellers or when the
    company isn't named (never guessed)."""
    t = re.sub(r"\s*[|-]\s*LinkedIn.*$", "", (title or "").strip())
    parts = [p.strip() for p in t.split(" - ") if p.strip()]
    if len(parts) < 2:
        return None
    person, role = parts[0], parts[1]
    at = re.match(r"(.+?)\s+(?:at|@)\s+(.+)$", role)
    if at:
        role, company = at.group(1).strip(), at.group(2).strip()
    elif len(parts) >= 3:
        company = parts[-1]
    else:
        return None
    company = re.split(r"\s*(?:\||·|,|\.\.\.|…)\s*", company)[0].strip()
    if not company or SELLER_BIO.search(role) or SELLER_BIO.search(company) or len(company) > 60:
        return None
    return person, role, company


def post_date(post: dict) -> str | None:
    """ISO date of a LinkedIn post: datePublished, else decoded from the activity id (ms = id >> 22)."""
    if post.get("datePublished"):
        return str(post["datePublished"])[:10]
    m = re.search(r"activity[:\-](\d{15,})", post.get("activityUrn", "") + " " + post.get("url", ""))
    if not m:
        return None
    ms = int(m.group(1)) >> 22
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def days_old(iso: str | None, now: datetime | None = None) -> int | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return ((now or datetime.now(timezone.utc)) - d).days


# company profile + exclusions ----------------------------------------------------------------------------------

@dataclass
class Profile:
    linkedin_url: str
    name: str = ""
    domain: str | None = None
    website: str | None = None         # as given on the company page (may be a shortlink; resolved by growth.Resolver)
    country: str | None = None
    hq: str | None = None
    size: str | None = None
    industry: str = ""
    text: str = ""                     # name + slogan + description, for matching
    posts: list[dict] = field(default_factory=list)
    similar: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, url: str, d: dict) -> "Profile":
        loc = d.get("location") or {}
        site = (d.get("website") or "").strip()
        domain = detect.bare_host(site if "//" in site else "https://" + site) if site else None
        return cls(linkedin_url=url, name=d.get("name") or "", domain=domain or None, website=site or None,
                   country=(loc.get("country") or "").upper() or None, hq=d.get("headquarters"), size=d.get("size"),
                   industry=d.get("industry") or "",
                   text=" ".join(filter(None, [d.get("name"), d.get("slogan"), d.get("description")]))[:4000],
                   posts=d.get("posts") or [],
                   similar=[u for u in (company_url(p.get("link")) for p in d.get("similarPages") or []) if u])


def exclusion(p: Profile, lst: str, cfg: dict | None = None) -> str | None:
    """Why this company must never be listed, or None."""
    cfg = cfg or load()
    rx = cfg["_rx"]
    head = f"{p.name} {p.text[:300]}"
    low = f"{p.name} {p.domain or ''}".lower()
    if any(n in low or n.replace(" ", "") in low.replace(" ", "") for n in cfg["_dnp"]):
        return "on the do-not-prospect list"
    if p.country and p.country != "US":
        return f"not US ({p.country})"
    if rx["verticals"].search(f"{p.industry} {p.text[:1500]}"):
        return "excluded vertical"
    if p.industry in cfg["exclude"]["recruiter_industries"] or rx["recruiters"].search(head):
        return "recruiter"
    if rx["sellers"].search(head) or (p.industry in cfg["exclude"]["seller_industries"]
                                      and rx["seller_topic"].search(p.text)):
        return "sells affiliate/partner marketing"
    if rx["vendors"].search(head) or rx["vendor_names"].search(low):
        return "affiliate/partner software or network"
    if lst == "affiliate" and p.industry in cfg["exclude"].get("agency_industries", []):
        return "agency / marketing services"
    if lst == "finance":
        if rx["finance_vendors"].search(head):
            return "finance lead/software vendor"
        if not rx["finance"].search(f"{p.industry} {p.text}"):
            return "not a business-funding company"
        allowed = cfg["exclude"].get("finance_industries")
        if allowed and p.industry and p.industry not in allowed:
            return "not a business-funding company"
        if (p.domain or "").endswith(".org"):
            return "nonprofit lender"
    return None


def program_posts(p: Profile, lst: str, cfg: dict | None = None) -> list[Signal]:
    """Company-page posts about their affiliate/partner program (affiliate) or ISO/broker recruiting (finance)."""
    cfg = cfg or load()
    rx = cfg["_rx"]["post"] if lst == "affiliate" else cfg["_rx"]["fin_text"]
    out = []
    for post in p.posts:
        text = post.get("text") or post.get("description") or ""
        m = rx.search(text)
        if m and post.get("url"):
            start = max(0, m.start() - 80)
            out.append(Signal("company_post", post["url"], text[start:m.end() + 160].strip(), post_date(post)))
    return out


def finance_program(pages: list[detect.Page], cfg: dict | None = None) -> Signal | None:
    """An ISO/broker/referral partner program on a funder's own pages (verbatim quote)."""
    cfg = cfg or load()
    for page in pages:
        text = detect.page_text(page.html)
        m = cfg["_rx"]["fin_text"].search(text)
        if m:
            start = text.find(" ", max(0, m.start() - 80), m.start()) + 1 or max(0, m.start() - 80)
            return Signal("partner_program", page.final_url, text[start:m.end() + 140].strip()[:240], platform="web")
    return None


def finance_links(html: str, base_url: str, cfg: dict | None = None) -> list[str]:
    cfg = cfg or load()
    host = detect.bare_host(base_url)
    out = []
    from selectolax.parser import HTMLParser
    from urllib.parse import urljoin
    for a in HTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:")):
            continue
        if not (cfg["_rx"]["fin_link"].search(href) or cfg["_rx"]["fin_link"].search(a.text() or "")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if detect.bare_host(url) == host and url not in out and not detect.is_article(url):
            out.append(url)
    return out[:3]


# scoring ---------------------------------------------------------------------------------------------------------

SMALL_TO_MID = re.compile(r"^(?:2-10|11-50|51-200|201-500|501-1,000) ")


def is_program_job(sig: dict) -> bool:
    """An affiliate/referral/channel-partner role (proof of a program), not generic BD 'strategic partnerships'."""
    return sig["kind"] == "job" and bool(load()["_rx"]["program_job"].search(sig.get("quote") or ""))


POST_KINDS = ("company_post", "person_post", "social_post", "community_post")


def is_program_post(sig: dict) -> bool:
    return sig["kind"] in POST_KINDS and bool(load()["_rx"]["program_post"].search(sig.get("quote") or ""))


def program_evidence(row: dict, signals: list[dict]) -> bool:
    """Runs an affiliate/partner program: verified on its site, hiring for it, staff on it, paying for ads to recruit
    affiliates, or saying so in a post."""
    return row.get("status") == "verified" or any(
        is_program_job(s) or is_program_post(s) or s["kind"] in ("affiliate_staff", "paid_ad") for s in signals)


def lead_score(row: dict, signals: list[dict], now: datetime | None = None) -> int:
    """0-100 priority for the two lists. Excluded or non-US companies score 0 (never listed)."""
    if row.get("excluded"):
        return 0
    kinds: dict[str, list[int | None]] = {}
    for s in signals:
        kinds.setdefault(s["kind"], []).append(days_old(s.get("signal_date"), now))

    def fresh(kind):
        ages = kinds.get(kind) or []
        return any(a is not None and a <= FRESH_DAYS.get(kind, 365) for a in ages)

    size_ok = bool(row.get("size") and SMALL_TO_MID.match(row["size"] + " "))
    recent_platforms = {s.get("platform") or "linkedin" for s in signals
                        if s["kind"] != "lookalike" and (days_old(s.get("signal_date"), now) or 999) <= 30}
    corroborated = 5 if len(recent_platforms) >= 2 else 0  # talking about it in more than one place
    if row.get("list") == "finance":
        s = 40
        s += 30 if "partner_program" in kinds else 0
        s += 10 if any(fresh(k) for k in POST_KINDS) else (5 if "company_post" in kinds else 0)
        s += 10 if fresh("paid_ad") else 0      # paying to recruit ISOs/brokers
        s += 10 if fresh("job") else 0          # hiring ISO/broker relationship staff
        s += 10 if row.get("_direct") else 0
        s += 5 if size_ok else 0
        return min(100, s + corroborated)
    if not program_evidence(row, signals):
        return 0
    strong_jobs = [days_old(x.get("signal_date"), now) for x in signals if is_program_job(x)]
    s = 30
    if strong_jobs:
        s += 30 if any(a is not None and a <= FRESH_DAYS["job"] for a in strong_jobs) else 15
    elif "job" in kinds:
        s += 10  # partnerships hire that isn't clearly the affiliate/partner program
    s += 15 if "affiliate_staff" in kinds else 0
    s += 25 if fresh("paid_ad") else (10 if "paid_ad" in kinds else 0)  # paying right now to recruit affiliates
    strong_posts = [x for x in signals if x["kind"] == "company_post" and is_program_post(x)]
    if any((days_old(x.get("signal_date"), now) or 999) <= FRESH_DAYS["company_post"] for x in strong_posts):
        s += 15                     # a post naming the program
    elif "company_post" in kinds:
        s += 5                      # a post that only mentions partners/affiliates
    s += 15 if fresh("person_post") or fresh("social_post") else 0
    s += 10 if fresh("community_post") else 0
    s += corroborated
    pc = row.get("partner_count")
    s += 10 if pc is not None and pc < 50 else 0
    s += 5 if set(row.get("platforms") or []) - {"in-house"} else 0
    s += 5 if size_ok else 0
    s += 5 if row.get("commission_text") else 0
    return min(100, s)


def why(signals: list[dict]) -> str:
    """One readable line of reasons, newest first."""
    label = {"job": "hiring", "affiliate_staff": "affiliate staff", "company_post": "company post",
             "person_post": "post", "partner_program": "ISO/broker program", "lookalike": "lookalike",
             "paid_ad": "running ads to recruit affiliates", "social_post": "post", "community_post": "thread"}
    out = []
    for s in sorted(signals, key=lambda s: s.get("signal_date") or "", reverse=True):
        bit = label.get(s["kind"], s["kind"])
        plat = s.get("platform")
        if s["kind"] in ("social_post", "community_post", "person_post") and plat in PLATFORM_LABEL:
            bit = f"{PLATFORM_LABEL[plat]} {bit}"
        if s["kind"] == "job":
            bit += f": {s['quote'][:60]}"
        if s.get("signal_date"):
            bit += f" ({s['signal_date']})"
        if bit not in out:
            out.append(bit)
    return "; ".join(out[:4])


class Budget:
    """ScrapeCreators credits for one run; collectors stop cleanly when it is spent."""

    def __init__(self, sc, limit: int):
        self.sc, self.limit = sc, limit

    @property
    def left(self) -> int:
        return self.limit - self.sc.meter.total_credits()

    def ok(self, need: int = 1) -> bool:
        return self.left >= need and not self.sc.exhausted

    def sub(self, cap: int) -> "Budget":
        """A source's own allowance inside this run: stops at `cap` credits spent from now, or when the run's
        budget is spent, whichever comes first."""
        return _SubBudget(self, cap)


class _SubBudget(Budget):
    def __init__(self, parent: Budget, cap: int):
        super().__init__(parent.sc, parent.limit)
        self.parent, self.cap, self.start = parent, cap, parent.sc.meter.total_credits()

    @property
    def spent(self) -> int:
        return self.sc.meter.total_credits() - self.start

    @property
    def left(self) -> int:
        return min(self.parent.left, self.cap - self.spent)

    def ok(self, need: int = 1) -> bool:
        return self.left >= need and not self.sc.exhausted


async def sc_get(sc, path: str, params: dict, source: str) -> dict | None:
    try:
        return await sc.get(path, params, source=source)
    except SkipItem:
        return None
    except SourceError:
        return None


__all__ = ["Signal", "Candidate", "Profile", "load", "company_url", "company_from_job", "staff_from_profile",
           "post_date", "exclusion", "program_posts", "finance_program", "finance_links", "lead_score", "why",
           "Budget", "sc_get", "OutOfCredits", "KINDS", "PLATFORM_LABEL", "program_evidence", "is_program_post",
           "is_program_job", "clean_name"]
