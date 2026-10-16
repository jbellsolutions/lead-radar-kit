"""Hiring boards: affiliate, referral and partner-program job postings on the boards collect_jobs does not cover.

Three public sources, one module (NAME "hiring_boards"):

- Built In (free): each search page of DEFAULTS["builtin_terms"]. A card gives the job, the company name and slug, a
  relative posted date and the location. The collapsed card text gives the description used by the gates. The
  company's own website comes from its Built In company page (free), confirmed on the homepage.
- Time in Business (free): the MCA funder job board. ISO and broker relationship roles only. The employer, the posted
  date and the declared website come from the detail page. A row without a named employer is dropped.
- Google SERP via ScrapeCreators (one credit per query, DEFAULTS["max_google_queries"] at most): Indeed snippets
  (indeed.com itself is never fetched, robots disallows /viewjob) and Upwork snippets, kept only when the snippet names
  the client (most Upwork clients are anonymous).

A collector only finds and filters. growth.process() resolves each candidate to a LinkedIn company page and applies the
exclusions and the US gate. A domain is attached only when the company's own homepage names the brand (a guessed domain
must match a whole title segment). A declared or guessed domain that does not confirm is not used, and a posting with
no named company is dropped, never guessed. On the affiliate list, a live homepage that sells services to brands drops
the posting. A posting with no US marker in its own text (a state code, or an Upwork client with no location) is listed
only when LinkedIn confirms the company is US. One candidate per company per list per run (the newest posting).
"""
from __future__ import annotations

import asyncio
import html as htmllib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote_plus, urlsplit

from selectolax.parser import HTMLParser

from .. import detect, signals as sg
from . import common

NAME = "hiring_boards"
DEFAULTS = {
    "enabled": True,
    "credits": 12,
    "http_cap": 80,
    "builtin_base": "https://builtin.com",
    "builtin_terms": ["affiliate manager", "affiliate marketing manager", "referral program manager",
                      "partner marketing manager", "channel partner manager"],
    "timeinbusiness_index": "https://www.timeinbusiness.com/jobs",
    "max_tib_details": 8,
    "max_google_queries": 6,
    "retry_pause": 30.0,      # seconds before one retry of a Built In company page that answered 429 (cleared in ~30s)
    # [platform, query]. Indeed via SERP snippets only; Upwork kept only when the snippet names the client.
    "google_queries": [
        ["indeed", 'site:indeed.com/viewjob "affiliate manager"'],
        ["indeed", 'site:indeed.com/viewjob "affiliate marketing manager"'],
        ["indeed", 'site:indeed.com/viewjob "referral program manager" OR "affiliate program manager"'],
        ["indeed", 'site:indeed.com/viewjob "affiliate program" (recruit OR hire OR manage)'],
        ["upwork", 'site:upwork.com/freelance-jobs "affiliate program" (recruit OR hire OR manage)'],
        ["upwork", 'site:upwork.com/freelance-jobs "affiliate manager" OR "affiliate recruiter"'],
    ],
}

INDEED_HOSTS = {"www.indeed.com", "indeed.com"}
AGGREGATOR_HOSTS = ("jobgether.com", "dailyremote.com", "himalayas.app", "himalayas.com", "ziprecruiter.com")
AGGREGATOR_TITLE = re.compile(r"\b(?:jobgether|dailyremote|himalayas|ziprecruiter)\b", re.I)

# role gates ----------------------------------------------------------------------------------------------------------
ROLE_NOUN = re.compile(r"\b(?:manager|managers|director|head|lead|coordinator|specialist|recruiter|assistant|"
                       r"partnerships?|vp)\b", re.I)
AFFILIATE_TITLE = re.compile(r"\b(?:affiliates?|referrals?)\b", re.I)
PARTNER_TITLE = re.compile(r"\b(?:partners?|partnerships?|channel|alliances?|resellers?)\b", re.I)
# Partner/channel titles: a title that is a sales, reseller, distributor or integrator role is not a program owner
# (VAR, GSI, channel sales, account executive); the rest need an affiliate, referral, ambassador or partner-program
# word in the title or the posting body. "channel" alone, in a title or in running text, does not name a program.
PARTNER_EXCLUDE_TITLE = re.compile(r"\b(?:VARs?|value[- ]added|resell\w*|distributors?|distribution|GSI|"
                                   r"system integrators?|sales?|account executives?|AEs?)\b", re.I)
PROGRAM_TITLE = re.compile(r"\b(?:affiliates?|referrals?|partner programs?|ambassadors?)\b", re.I)
PROGRAM_BODY = re.compile(r"\b(?:affiliates?|ambassadors?|referral (?:partners?|programs?|network)|partner programs?)\b",
                          re.I)
TITLE_SELLER = re.compile(r"\b(?:consultants?|consulting|agency|agencies|opm)\b", re.I)
AGENCY_NAME = re.compile(r"\b(?:agency|agencies|media buying)\b", re.I)
TEXT_SELLER = re.compile(r"\bpublisher network\b|\bpay[- ]per[- ]call\b|\blead exchange\b|"
                         r"\bmulti[- ]brand (?:agency|marketing)\b|\btech[- ]enabled\b|\bdigital marketing partner\b|"
                         r"\bwe are an? (?:marketing |performance |affiliate )?agency\b|\bour agency\b|"
                         r"\bagency for (?:brands|clients)\b|\bagency clients?\b|\bclient accounts?\b|"
                         r"\bperformance marketing accounts?\b", re.I)
# A company's own homepage that sells services to brands (an agency or accelerator), not a program owner. Phrase-level
# on purpose: a bare "agency" or "our clients" also matches brands that run a program (HighLevel, Sunday).
SELLER_HOME = re.compile(r"\bwe help (?:brands|companies|businesses|clients|ecommerce|e-commerce|merchants|startups)\b|"
                         r"\bhelping (?:brands|businesses|companies|merchants|ecommerce brands) (?:acquire|grow|scale|"
                         r"launch|get|increase|boost|drive)\b|\baffiliate (?:management|marketing) (?:services|agency|"
                         r"firm|company)\b|\b(?:we are|we're)\s+(?:an?\s+)?(?:[\w&-]+\s+){0,3}agency\b|"
                         r"\bdone[- ]for[- ]you\b|\blive[- ]commerce accelerator\b", re.I)
# Built In industry tags that mark a seller (agency, consultancy, staffing) rather than a brand that runs a program
SELLER_TAGS = {"consulting", "agency", "staffing", "recruiting", "staffing and recruiting", "human resources",
               "advertising agency", "marketing agency"}
VERTICAL_EXTRA = re.compile(r"\b(?:bet365|sports ?betting|esports betting|sportsbook|coinpoker|okx|bitcoin|blockchain|"
                            r"lulabet|sportradar|cpa betting|broker rebate|bitdelta)\b", re.I)
# Multi-level and pyramid pitches in a posting. Common's mlm gate is ignored for jobs because it also matches plain
# "work from home" (every remote job ad), so the job version lists the MLM phrases only.
JOB_MLM = re.compile(r"\b(?:network marketing|multi[- ]level|mlm|direct selling|join my team|my downline|downline|"
                     r"build your (?:own )?team|be your own boss|no cap on earnings|unlimited (?:earning|income) potential|"
                     r"financial freedom|passive income|side hustle|make money (?:online|from home)|"
                     r"dm me ['\"]?(?:info|interested|join))\b", re.I)
# A commission-only pitch is an affiliate-list recruiter pattern ("Commission Only, No Cap on Earnings"). Funder ISO
# postings say "commission" as a pay field (override, split), so this check is affiliate-list only.
JOB_COMMISSION_ONLY = re.compile(r"\bcommission[- ]only\b", re.I)
# A legal suffix that marks a non-US headquarters ("Usercentrics GmbH"). Case-sensitive: "Ag" and "Ltd" are words.
NON_US_SUFFIX = re.compile(r"\b(?:GmbH|AG|Pty|Pte|Ltd|Limited|S\.A\.|SAS|B\.V\.|Oy|AB|ApS|KK|Pvt)\b")
# text_gate reasons that describe a seller's or individual's own pitch, not an employer's job posting. The common
# mlm gate is ignored here (see JOB_MLM).
JOB_IGNORED_GATES = {"mlm", "individual_affiliate", "seeker"}
# A page's <title> and og:site_name, split into the segments a brand name leads
TITLE_RX = re.compile(r"<title[^>]*>(.*?)</title>|property=[\"']og:site_name[\"'][^>]*content=[\"']([^\"']+)",
                      re.I | re.S)
TITLE_SPLIT = re.compile(r"\s*(?:\||–|—|·| - |:)\s*")

# location gates ------------------------------------------------------------------------------------------------------
US_STATES = frozenset("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
                      "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split())
STATE_RX = re.compile(r",\s*([A-Z]{2})\b")
US_MARK = re.compile(r"\bU\.?\s?S(?:\.?A)?(?![A-Za-z])|\bUnited States\b")
NON_US_WORDS = re.compile(r"\b(?:canada|canadian|ontario|toronto|vancouver|calgary|montreal|united kingdom|london|"
                          r"india|philippines|manila|taguig|australia|mexico|costa rica|provincia|europe|emea|apac|"
                          r"latam|dubai|netherlands|israel|uk)\b", re.I)
NON_US_CODES = re.compile(r",\s*(?:ON|AB|BC|QC|NS|MB|SK|NB|NL|NT|YT|NU|PE|UK)\b")

# dates, locations and pay on the boards ------------------------------------------------------------------------------
DATE_FULL = re.compile(r"(?:Reposted\s+)?(?:\d+|One|A|An)\s+(?:Minutes?|Hours?|Days?|Weeks?|Months?)\s+Ago|"
                       r"Yesterday|Today|Just Now", re.I)
LOCATION_LIKE = re.compile(r",|\b(?:USA|US|United States|Canada|UK|Locations?)\b", re.I)
PAY = re.compile(r"annually|hourly|monthly|\$|\d+K?-\d+K", re.I)
LEAD_DATE = re.compile(r"^\s*(\d+\s+(?:minutes?|hours?|days?|weeks?)\s+ago|Yesterday|Today|"
                       r"[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4})\b")

# Time in Business -----------------------------------------------------------------------------------------------------
TIB_PREFILTER = re.compile(r"\bISO\b|\bbrokers?\b", re.I)
# a funder whose homepage sells hard-money / real-estate loans is not a business funder (verified 2026-10-10: the
# Ironwood Finance homepage says "hard money real estate loans" and matched only on "working capital")
TIB_NOT_BUSINESS_FUNDER = re.compile(r"\b(?:hard[- ]money|fix (?:and|&) flip|bridge loans?|real estate (?:loans?|lending))\b",
                                     re.I)
TIB_ROLE_TITLE = re.compile(r"\bISO\b|\bbroker (?:partnerships?|relations?|relationships?|development|channel|division)\b",
                            re.I)
TIB_MCA_TITLE = re.compile(r"\bmca brokers?\b|\bindependent (?:mca )?brokers?\b", re.I)
BROKER_BODY = re.compile(r"\bISO\b|\bindependent\b|\b1099\b|\breferrals?\b|\bpartners?(?:hip)?s?\b|\bbroker network\b",
                         re.I)
TIB_WEBSITE = re.compile(r'website\\?"\s*:\s*\\?"(https?://[^"\\\s]+)')

# Indeed / Upwork snippets ---------------------------------------------------------------------------------------------
INDEED_SUFFIX = re.compile(r"\s*[-|]\s*Indeed(?:\.com)?\s*$", re.I)
LOC_TAIL = re.compile(r"^(?:remote|hybrid|in-office|on-?site|full-?time|part-?time|contract|"
                      r"[A-Za-z .'-]+,\s*[A-Za-z]{2}(?:\s+\d{5})?)$", re.I)
INDEED_COMPANY_END = (r"(?:·|Job details|\d\.\d\b|Pay information|Full-time|Part-time|Permanent|Contract|\$)")
UPWORK_TAIL = re.compile(r"\s+-\s+(?:Freelance Job\b.*|Upwork)\s*$", re.I)
UPWORK_NAV = "Hire an agency"
UPWORK_BRAND = re.compile(r"\bfor (?P<name>[A-Z][\w&'’.-]*(?:\s+[A-Z&][\w&'’.-]*){0,3}),\s+(?:a|an|the|which|that|who)\b")
GENERIC_FIRST = {"us", "our", "your", "a", "an", "the", "this", "that", "freelance", "experienced", "senior", "new", "top"}
GENERIC_WORDS = {"brand", "brands", "company", "business", "client", "startup", "store", "shop", "supplement",
                 "supplements", "ecommerce", "e-commerce", "saas", "agency", "team", "app", "firm"}


@dataclass
class Job:
    lst: str                       # affiliate | finance
    platform: str                  # builtin | timeinbusiness | indeed | upwork
    url: str                       # canonical posting URL
    title: str
    company: str                   # named employer, "" when the source does not name one
    date: str | None               # ISO posted date; None when the source shows none
    location: str | None           # text checked for a US marker; None means no location gate (Upwork)
    text: str                      # description or snippet, used only by the gates (never printed)
    role: bool = False             # a program role (role_ok / tib_role_ok)
    tags: tuple[str, ...] = ()     # Built In industry tags from the card
    slug: str = ""                 # Built In company slug
    website: str = ""              # employer domain declared on the source (Time in Business)
    finance_gate: bool = False     # a confirmed homepage must read like a business funder


# pure helpers ----------------------------------------------------------------------------------------------------------

def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _window(days: int) -> str:
    return "last-day" if days <= 1 else "last-week" if days <= 7 else "last-month" if days <= 31 else "last-year"


def us_location(text: str | None, strict: bool = False) -> bool:
    """True when the text names a US place and no non-US place. A US marker (USA, U.S., United States) always counts.
    A US state after a comma counts only when not strict: 'Bogota, CO' is a state code in a foreign city. Built In
    cards always carry a marker, so Built In is strict; Time in Business and Indeed text is not."""
    if not text:
        return False
    if NON_US_WORDS.search(text) or NON_US_CODES.search(text):
        return False
    if US_MARK.search(text):
        return True
    return not strict and any(code in US_STATES for code in STATE_RX.findall(text))


def role_ok(title: str, text: str = "") -> bool:
    """A program role. An affiliate or referral title with a role noun. A partner or channel title with a role noun,
    not a sales/reseller/distributor/integrator role, and an affiliate, referral, ambassador or partner-program word in
    the title or the posting body (a bare 'channel' does not count). Generic strategic or business-development
    partnership titles are dropped."""
    if not ROLE_NOUN.search(title or "") or PARTNER_EXCLUDE_TITLE.search(title or ""):
        return False
    if AFFILIATE_TITLE.search(title):
        return True
    if PARTNER_TITLE.search(title):
        return bool(PROGRAM_TITLE.search(title) or PROGRAM_BODY.search(text or ""))
    return False


def tib_role_ok(title: str, body: str) -> bool:
    """An ISO or broker relationship role. A plain 'MCA Broker' title needs a body that says the funder recruits
    independent or ISO brokers (otherwise it is an employee sales role)."""
    if TIB_ROLE_TITLE.search(title):
        return True
    if TIB_MCA_TITLE.search(title):
        return bool(BROKER_BODY.search(f"{title} {body}"))
    return False


def drop_reason(title: str, company: str, text: str, cfg: dict, lst: str = "affiliate") -> str | None:
    """Why a posting is not an employer's own program role, or None. Sellers, agencies, software and networks,
    recruiters, excluded verticals and finance lead vendors."""
    rx = cfg["_rx"]
    body = f"{title} {text}"
    if VERTICAL_EXTRA.search(f"{company} {body}") or rx["verticals"].search(body):
        return "excluded vertical"
    gate = common.text_gate(body)
    if gate and gate not in JOB_IGNORED_GATES:
        return f"text: {gate}"
    if JOB_MLM.search(body) or (lst == "affiliate" and JOB_COMMISSION_ONLY.search(body)):
        return "mlm or commission-only pitch"
    if rx["vendor_names"].search((company or "").lower()) or rx["vendors"].search(f"{title} {company}"):
        return "affiliate software or network"
    if rx["recruiters"].search(company or "") or AGENCY_NAME.search(company or ""):
        return "recruiter or agency"
    if TITLE_SELLER.search(title) or TEXT_SELLER.search(text) or rx["sellers"].search(body):
        return "seller or agency"
    if lst == "finance" and rx["finance_vendors"].search(body):
        return "finance lead vendor"
    return None


def _title_key(title: str) -> str:
    t = re.sub(r"\b(?:senior|sr|remote|hybrid|in-?office|united states|us|usa|full-?time|part-?time)\b", " ",
               title.lower())
    return "".join(re.findall(r"[a-z0-9]+", t))


def _lead_date(text: str, now: datetime) -> str | None:
    m = LEAD_DATE.match(text or "")
    return common.relative_date(m.group(1), now) if m else None


def _strip_lead(text: str) -> str:
    m = LEAD_DATE.match(text or "")
    return text[m.end():].lstrip(" .·…") if m else (text or "")


def _ld_descriptions(tree: HTMLParser) -> dict[str, str]:
    """url -> description from the page's JSON-LD (Built In lists every card there)."""
    out: dict[str, str] = {}
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if isinstance(node.get("url"), str) and isinstance(node.get("description"), str):
                    out[node["url"].split("?")[0]] = _clean(node["description"])
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return out


# Built In ---------------------------------------------------------------------------------------------------------------

def _date_text(node) -> str:
    for span in node.css("span"):
        text = _clean(span.text(strip=True))
        if DATE_FULL.fullmatch(text):
            return text
    return ""


def _span_texts(node) -> list[str]:
    return [t for t in (_clean(s.text(strip=True)) for s in node.css("span")) if t]


def _card_for(anchor, href: str):
    """The smallest ancestor that holds exactly this job, its company link and its posted-date span."""
    node = anchor
    for _ in range(14):
        node = node.parent
        if node is None:
            return None
        jobs = {(x.attributes.get("href") or "").split("?")[0] for x in node.css('a[href^="/job/"]')
                if _clean(x.text(strip=True))}
        if len(jobs) > 1:
            return None
        if jobs == {href} and _date_text(node) and node.css_first('a[href^="/company/"]') is not None:
            return node
    return None


def _industry_tags(tree: HTMLParser, href: str) -> tuple[str, ...]:
    """Built In's industry tags for a card ('Consulting • Software'), from the card's collapsed block."""
    jid = href.rsplit("/", 1)[-1]
    node = tree.css_first(f'div[data-job-id="{jid}"]') if jid.isdigit() else None
    if node is None:
        return ()
    for div in node.css("div"):
        text = _clean(div.text(separator=" "))
        if "•" in text and len(text) < 200:
            return tuple(t.strip() for t in text.split("•") if t.strip())
    return ()


def _desc_for(tree: HTMLParser, href: str, fallback: str) -> str:
    jid = href.rsplit("/", 1)[-1]
    if jid.isdigit():
        node = tree.css_first(f'div[data-job-id="{jid}"]')
        if node is not None:
            text = _clean(node.text(separator=" "))
            if text:
                return text
    return _clean(fallback)


def parse_builtin(html: str, base: str, now: datetime) -> list[Job]:
    """Every card on a Built In search page, with its company slug, posted date and location."""
    tree = HTMLParser(html)
    ld = _ld_descriptions(tree)
    out: list[Job] = []
    seen: set[str] = set()
    for anchor in tree.css('a[href^="/job/"]'):
        href = (anchor.attributes.get("href") or "").split("?")[0]
        title = _clean(anchor.text(strip=True))
        if not title or href in seen:
            continue
        card = _card_for(anchor, href)
        if card is None:
            continue
        seen.add(href)
        company_a = next((a for a in card.css('a[href^="/company/"]') if _clean(a.text(strip=True))), None)
        company = _clean(company_a.text(strip=True)) if company_a is not None else ""
        slug = (company_a.attributes.get("href") or "").rstrip("/").rsplit("/", 1)[-1] if company_a is not None else ""
        date_text = _date_text(card)
        date = common.relative_date(re.sub(r"^Reposted\s+", "", date_text, flags=re.I), now) if date_text else None
        location = next((t for t in _span_texts(card) if LOCATION_LIKE.search(t) and not PAY.search(t)), "")
        url = common.unwrap(f"{base}{href}")
        desc = _desc_for(tree, href, ld.get(url, ""))
        out.append(Job(lst="affiliate", platform="builtin", url=url, title=title, company=company, date=date,
                       location=location, text=desc, role=role_ok(title, desc), tags=_industry_tags(tree, href),
                       slug=slug))
    return out


def builtin_company_site(html: str) -> str:
    """The employer's own domain from its Built In company page (the external link Built In tags utm_source=BuiltIn)."""
    for a in HTMLParser(html).css('a[href^="http"]'):
        href = a.attributes.get("href") or ""
        if "utm_source=BuiltIn" in href:
            dom = common.host_of(href)
            if dom:
                return dom
    return ""


# Time in Business -------------------------------------------------------------------------------------------------------

def parse_tib_index(html: str, base: str) -> list[tuple[str, str]]:
    """(detail URL, title) for every job row on the Time in Business index."""
    seen: dict[str, str] = {}
    for a in HTMLParser(html).css('a[href^="/jobs/"]'):
        href = (a.attributes.get("href") or "").split("?")[0].rstrip("/")
        title = _clean(a.text(strip=True))
        if not title or href.count("/") < 2 or href == "/jobs/post":
            continue
        seen.setdefault(href, title)
    return [(base + href, title) for href, title in seen.items()]


def parse_tib_detail(html: str, url: str, title: str, now: datetime) -> Job:
    """One Time in Business detail page. The employer is the funder-directory link; the date is the machine-readable
    <time> element; location and description come from the visible text (scripts are dropped)."""
    tree = HTMLParser(html)
    employer = ""
    for a in tree.css('a[href^="/directory/funders/"]'):
        employer = _clean(a.text(strip=True))
        if employer:
            break
    time_node = tree.css_first("time[datetime]")
    raw_date = (time_node.attributes.get("datetime") or "") if time_node is not None else ""
    date = raw_date[:10] if re.match(r"\d{4}-\d{2}-\d{2}", raw_date) else None
    text = detect.page_text(html)
    i = text.find("About the role")
    desc = text[i:i + 4000] if i >= 0 else ""
    j = text.find(employer) if employer else -1
    head = text[j:j + 200] if j >= 0 else ""
    m = TIB_WEBSITE.search(html)
    website = (common.host_of(m.group(1)) or "") if m else ""
    title = _clean(title)
    return Job(lst="finance", platform="timeinbusiness", url=common.unwrap(url), title=title, company=employer,
               date=date, location=head, text=desc, role=tib_role_ok(title, desc), website=website,
               finance_gate=True)


# Google SERP (Indeed, Upwork) -----------------------------------------------------------------------------------------

def _indeed_title(raw: str) -> tuple[str, str]:
    """(title, location tail) from an Indeed SERP title such as 'Affiliate Manager - Remote - Indeed.com'."""
    t = INDEED_SUFFIX.sub("", raw).strip()
    parts = [p.strip() for p in re.split(r"\s+-\s+", t) if p.strip()]
    tail: list[str] = []
    while len(parts) > 1 and LOC_TAIL.match(parts[-1]):
        tail.insert(0, parts.pop())
    return " - ".join(parts), " ".join(tail)


def _indeed_company(title: str, desc: str) -> str | None:
    """Employer from an Indeed snippet: 'Affiliate Manager. Platinum Commerce, LLC. Job details.'"""
    if not title or not desc:
        return None
    m = re.search(re.escape(title) + r"\.\s+([^.·\n]{2,80}?)\.?\s*" + INDEED_COMPANY_END, desc)
    if not m:
        return None
    co = m.group(1).strip(" ,.")
    if len(co) < 2 or co.lower() == title.lower() or not re.search(r"[A-Za-z]", co):
        return None
    if re.search(r"\b(?:job|jobs|apply|salary|hiring|remote)\b", co, re.I):
        return None
    return co


def _upwork_brand(desc: str) -> str | None:
    """The client named in an Upwork snippet ('... for Horizon Digital Co, a business that ...'). Generic phrases
    ('for US Organic Food Brand') do not count."""
    for m in UPWORK_BRAND.finditer(desc or ""):
        name = m.group("name").strip()
        words = name.split()
        if words[0].lower() in GENERIC_FIRST or any(w.lower().strip(".,") in GENERIC_WORDS for w in words):
            continue
        return name
    return None


def parse_serp_item(platform: str, item: dict, now: datetime) -> Job | None:
    """One Google SERP result as a job, or None when it is not an Indeed/Upwork posting we read (other hosts,
    aggregators, or an Indeed snippet on a non-US Indeed domain)."""
    url = (item.get("url") or "").split("#")[0]
    host = (urlsplit(url).hostname or "").lower()
    raw_title = _clean(item.get("title"))
    raw_desc = _clean(item.get("description"))
    if not raw_title or not url or AGGREGATOR_TITLE.search(raw_title) or any(a in host for a in AGGREGATOR_HOSTS):
        return None
    date = _lead_date(raw_desc, now)
    if platform == "indeed":
        if host not in INDEED_HOSTS:
            return None
        jk = re.search(r"[?&]jk=([0-9A-Za-z]+)", url)
        if not jk:
            return None
        title, tail = _indeed_title(raw_title)
        desc = _strip_lead(raw_desc)
        company = _indeed_company(title, desc) or ""
        return Job(lst="affiliate", platform="indeed", url=f"https://www.indeed.com/viewjob?jk={jk.group(1)}",
                   title=title, company=company, date=date, location=f"{tail} {desc}", text=desc,
                   role=role_ok(title, desc))
    if platform == "upwork":
        if not host.endswith("upwork.com"):
            return None
        title = _clean(UPWORK_TAIL.sub("", raw_title)).strip(" .…")
        desc = _strip_lead(raw_desc.split(UPWORK_NAV)[0])
        company = _upwork_brand(desc) or ""
        return Job(lst="affiliate", platform="upwork", url=f"https://{host}{urlsplit(url).path}", title=title,
                   company=company, date=date, location=None, text=desc, role=role_ok(title, desc))
    return None


# screening, resolution, collection ---------------------------------------------------------------------------------

def _screen(job: Job, ctx) -> str | None:
    """Why a posting is dropped before any domain work, or None to keep it."""
    if not job.role:
        return "not a program role"
    if not job.company:
        return "no named company"
    if not job.date:
        return "undated"
    if not common.within_days(job.date, ctx.days, ctx.now):
        return "older than window"
    if job.location is not None and not us_location(job.location, strict=job.platform == "builtin"):
        return "not US"
    if NON_US_SUFFIX.search(job.company) and not us_marked(job.location):
        return "non-US company name"
    if any(tag.lower() in SELLER_TAGS for tag in job.tags):
        return "seller or agency (industry tag)"
    return drop_reason(job.title, job.company, job.text, ctx.signals_cfg, job.lst)


def us_marked(location: str | None) -> bool:
    """True when the posting itself carries a US marker (USA, U.S., United States), not just a state code."""
    return bool(location and US_MARK.search(location))


async def gather(ctx) -> tuple[list[Job], Counter]:
    """Fetch the three sources, screen every posting, dedupe on (company, title) and keep one posting per company
    per list (the newest). Returns the kept postings and the drop counts (reasons only, never names)."""
    cfg = ctx.cfg
    base = str(cfg["builtin_base"]).rstrip("/")
    tib_index = str(cfg["timeinbusiness_index"])
    tib_base = "{0.scheme}://{0.netloc}".format(urlsplit(tib_index))
    drops: Counter = Counter()
    jobs: list[Job] = []

    for term in cfg["builtin_terms"]:
        page = await ctx.get(f"{base}/jobs?search={quote_plus(term)}", "search")
        if page is None or page.status >= 400:
            drops["builtin page unavailable"] += 1
            continue
        jobs += parse_builtin(page.html, base, ctx.now)

    index = await ctx.get(tib_index, "index")
    rows = []
    if index is not None and index.status < 400:
        rows = [(url, title) for url, title in parse_tib_index(index.html, tib_base) if TIB_PREFILTER.search(title)]
    for url, title in rows[: int(cfg["max_tib_details"])]:
        page = await ctx.get(url, "detail")
        if page is None or page.status >= 400:
            drops["tib detail unavailable"] += 1
            continue
        jobs.append(parse_tib_detail(page.html, url, title, ctx.now))

    for platform, query in cfg["google_queries"][: int(cfg["max_google_queries"])]:
        if not ctx.budget.ok():
            drops["credit budget spent"] += 1
            break
        try:
            data = await sg.sc_get(ctx.sc, "/v1/google/search",
                                   {"query": query, "page": 1, "date_posted": _window(ctx.days)}, NAME)
        except sg.OutOfCredits:
            # sc.exhausted is sticky, so later sources stop too. The free Built In and TIB postings already collected
            # are kept: returning them lets the run store them instead of losing the whole source.
            drops["out of credits"] += 1
            break
        for item in (data or {}).get("results") or []:
            job = parse_serp_item(platform, item, ctx.now)
            if job is not None:
                jobs.append(job)

    kept: list[Job] = []
    seen: set[tuple[str, str]] = set()
    for job in jobs:
        why = _screen(job, ctx)
        if why:
            drops[why] += 1
            continue
        key = (common.norm(job.company), _title_key(job.title))
        if key in seen:
            drops["duplicate posting"] += 1
            continue
        seen.add(key)
        kept.append(job)

    best: dict[tuple[str, str], Job] = {}
    for job in kept:
        key = (job.lst, common.norm(job.company))
        if key not in best:
            best[key] = job
        else:
            drops["company already listed"] += 1
            if job.date > best[key].date:
                best[key] = job
    return list(best.values()), drops


async def _home(ctx, cache: dict, dom: str):
    key = ("home", dom)
    if key not in cache:
        cache[key] = await ctx.get(f"https://{dom}/", "home")
    return cache[key]


async def _builtin_site(ctx, cache: dict, base: str, slug: str) -> str:
    """The employer's domain declared on its Built In company page. A 404 is cached. A 429 or 5xx answer, or no answer,
    is not cached: a cached empty string lost a declared domain for the whole run. Such an answer gets one retry after
    a pause, and the name guesses still carry the company."""
    key = ("company", slug)
    if key in cache:
        return cache[key]
    url = f"{base}/company/{slug}"
    page = await ctx.get(url, "company")
    if page is not None and page.status == 429:
        await asyncio.sleep(float(ctx.cfg.get("retry_pause", 30.0)))
        page = await ctx.get(url, "company")
    if page is None or page.status == 429 or page.status >= 500:
        return ""
    cache[key] = builtin_company_site(page.html) if page.status < 400 else ""
    return cache[key]


def _title_segments(html: str) -> list[str]:
    """The segments a page's <title> and og:site_name lead with ('Acme Health | Care plans' -> 'Acme Health', ...)."""
    out: list[str] = []
    for a, b in TITLE_RX.findall((html or "")[:200_000]):
        text = htmllib.unescape(a or b)
        out += [seg for seg in (s.strip() for s in TITLE_SPLIT.split(text)) if seg]
    return out


def _brand_on_home(html: str, company: str, guessed: bool) -> bool:
    """The homepage names the posting's company. A declared domain needs common.page_names_brand. A guessed domain also
    needs one title segment to be the company name (the substring match let 'Pine' confirm 'Pine Valley Dental')."""
    if not common.page_names_brand(html, company):
        return False
    return not guessed or any(common.names_match(company, seg) for seg in _title_segments(html))


async def _domain(ctx, job: Job, cache: dict, base: str) -> tuple[str, str | None]:
    """(confirmed domain or "", drop reason or None). Declared domains come first (Time in Business website, Built In
    company page), then the slug and name guesses. A domain is confirmed only when its live homepage (2xx, not parked)
    names the company. On the affiliate list, a live homepage that sells services to brands drops the candidate whether
    or not the brand matched: the company is a seller. A finance posting whose confirmed homepage does not read like a
    business funder is dropped."""
    declared: list[str] = []
    if job.website:
        declared.append(job.website)
    if job.platform == "builtin" and job.slug:
        declared.append(await _builtin_site(ctx, cache, base, job.slug))
    guessed: list[str] = []
    if job.platform == "builtin" and job.slug:
        guessed.append(job.slug.replace("-", "") + ".com")
    tokens = "".join(common.tokens(job.company))
    if len(tokens) >= 3:
        guessed.append(tokens + ".com")
    tried: set[str] = set()
    for dom, is_guess in [(d, False) for d in declared] + [(g, True) for g in guessed]:
        dom = (dom or "").lower()
        if not dom or dom in tried or common.is_platform_host(dom):
            continue
        tried.add(dom)
        page = await _home(ctx, cache, dom)
        if page is None or page.status >= 400 or common.parked(page.html):
            continue
        if job.lst == "affiliate" and SELLER_HOME.search(detect.page_text(page.html)[:20000]):
            return "", "homepage reads as a seller"
        if not _brand_on_home(page.html, job.company, is_guess):
            continue
        if job.finance_gate:
            text = detect.page_text(page.html)[:20000]
            if TIB_NOT_BUSINESS_FUNDER.search(text) or not ctx.signals_cfg["_rx"]["finance"].search(text):
                return "", "homepage is not a business funder"
        return (common.host_of(page.final_url) or dom), None
    return "", None


async def resolve(ctx, jobs: list[Job]) -> tuple[list[sg.Candidate], Counter]:
    """Candidates for the postings that pass the domain checks. A posting without a US marker in its own text (a state
    code, or an Upwork client with no location) is listed only when LinkedIn confirms the company is US."""
    base = str(ctx.cfg["builtin_base"]).rstrip("/")
    cache: dict = {}
    drops: Counter = Counter()
    out: list[sg.Candidate] = []
    for job in jobs:
        domain, why = await _domain(ctx, job, cache, base)
        if why:
            drops[why] += 1
            continue
        out.append(sg.Candidate(
            job.lst,
            sg.Signal(kind="job", url=job.url, quote=f"{job.title} @ {job.company}"[:400], signal_date=job.date,
                      platform=job.platform),
            company_name=job.company,
            domain=domain or None,
            require_us=job.location is None or not us_marked(job.location),
        ))
    return out, drops


async def collect(ctx) -> list[sg.Candidate]:
    jobs, drops = await gather(ctx)
    cands, more = await resolve(ctx, jobs)
    drops.update(more)
    ctx.say(f"{NAME}: {len(cands)} candidates from {len(jobs)} postings; dropped {dict(drops)}")
    return cands


__all__ = ["NAME", "DEFAULTS", "collect", "gather", "resolve", "parse_builtin", "parse_tib_index", "parse_tib_detail",
           "parse_serp_item", "role_ok", "tib_role_ok", "us_location", "drop_reason", "builtin_company_site", "Job"]
