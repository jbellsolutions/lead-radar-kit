"""Finance list: US business funders and brokers recruiting ISOs, brokers and referral partners right now.

Signal kinds: partner_program (a funder's or broker's own page, platform web), company_post (a company page's LinkedIn
post, platform linkedin), person_post (a person's post whose call to join names the funder, platform linkedin).

1. Google, date-windowed (1 credit per query). A result page is fetched once with ctx.get (robots honoured, free) and is
   kept only when recruiting copy sits in its body (navigation, header, footer and aside are stripped first), a funding
   term appears on the page, the page is not parked, blocked, a directory or article, nor a redirect to another host,
   and the page carries a date inside the window (the search snippet's date, else the page's own last-modified date).
   A same-host program link is followed once when the landing page has no body copy; a teaser on a non-program page is
   replaced by its program page when that page passes on its own date.
2. LinkedIn post search, date-windowed (1 credit per query). A company-page author becomes a company_post anchored to
   its LinkedIn company page. A person author becomes a person_post only when the sentence holding the call to join
   names the funder (a domain, or "at <Company>") and is written in the first person. The person's name and profile URL
   are stored on the Signal and never logged.
3. Filters run on identity text (title, description, the text around the CTA, or the post body), not on the whole page:
   vendors that sell leads, UCC data, CRM or software; payments ISOs with no funding term nearby; settlement and defence
   firms; consultants and credit repair; CPA-only referral programs; individual affiliates; ISO-certification
   collisions; non-US pages; the do-not-prospect list; social, forum, review and press-wire hosts.

Only finds and filters. growth.process() resolves each Candidate to a LinkedIn company page (the US gate and the
exclusions apply there). A zero-result query (404) costs nothing.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

from .. import detect, signals as sg
from . import common as c

NAME = "finance_iso"
SRC = "finance_iso"  # ScrapeCreators credit label

DEFAULTS = {
    "enabled": True,
    "credits": 20,          # per-run credit allowance for this source (each query is one credit)
    "http_cap": 90,         # free page fetches per run (follow-links included)
    "max_pages": 40,        # funder/broker search-result pages fetched per run
    "tries_per_domain": 2,  # a domain's pages tried until one is kept (product pages can fail where the program page passes)
    "person_posts": True,   # keep person-authored posts whose call to join names the funder (see judge_post)
    "require_date": True,   # a web hit needs a date inside the window; False accepts Google's date_posted window alone
    "google_queries": [
        '"ISO program" "merchant cash advance"',
        '"become an ISO" funding',
        '"broker partner program" "business loans"',
        '"referral partner program" "working capital"',
        '"ISO partners" "same day funding"',
        '"now accepting ISOs" funding',
        '"broker program" "equipment financing"',
        '"ISO commission" points "merchant cash advance"',
    ],
    "google_exclude": "-site:linkedin.com -site:facebook.com -site:instagram.com -site:debanked.com",
    # LinkedIn post queries are off: the live probe (30-day window, 4 phrases) returned no posts at all, so no post
    # candidate has been verified. Re-enable with the phrases '"now accepting new ISOs" funding', '"seeking ISOs" funding',
    # '"ISO partners wanted" funding', '"brokers welcome" funding' once a live probe keeps a buyer post.
    "post_queries": [],
}

# Recruiting copy: a CTA that asks ISOs, brokers or referral partners to join a program or apply.
CTA = re.compile(
    r"\bbecome an? (?:iso|broker|referral|partner|sub-?iso|funding partner)s?\b"
    r"|\bjoin (?:our|the|a) (?:[a-z-]+ ){0,3}(?:iso|broker|partner|referral)s?\b"
    r"|\b(?:iso|broker|referral|partner) programs?\b"
    r"|\bnow accepting (?:new )?(?:iso|broker|partner|referral)s?\b"
    r"|\b(?:isos?|brokers?|referral partners?|partners?) (?:are )?(?:wanted|welcome(?:d)?)\b"
    r"|\bindependent sales organi[sz]ations?\b"
    r"|\bapply to (?:become|be) (?:an? )?(?:iso|broker|partner|referral)"
    r"|\bpartner with us\b[^.]{0,80}\b(?:iso|broker|referral)s?\b"
    r"|\bupfront commissions?\b|\bresidual commissions?\b"
    r"|\b\d{1,2} points\b[^.]{0,60}\bcommission",
    re.I)
# On a dedicated program path only, a weaker in-body invitation to apply counts.
WEAK_CTA = re.compile(
    r"\b(?:iso|broker|referral)s?\b[^.]{0,60}\b(?:apply|application|onboarding|join|sign ?up|register|submit)\b"
    r"|\b(?:apply|application|onboarding|join|sign ?up|register)\b[^.]{0,60}\b(?:iso|broker|referral)s?\b",
    re.I)
# On a non-program page the recruiting copy must speak for the page's own company.
OWN = re.compile(r"\b(?:our|we|your)\b|\b(?i:with us|join us)\b")
OWN_POST = re.compile(r"\b(?:we|we're|we've|our|my)\b", re.I)
EDITORIAL_SLUG = re.compile(r"/[a-z0-9]+(?:-[a-z0-9]+){4,}/?$")
# A brand named before "... Program" that is not this site's (a third-party program, or an affiliate page promoting it).
BRAND = re.compile(r"\b([A-Z][A-Za-z0-9&]{1,})\s+(?:(?i:referral|partner|iso|broker|affiliate|funding)\s+){0,2}"
                   r"(?i:programs?)\b")
BRAND_STOP = {"iso", "isos", "broker", "brokers", "referral", "partner", "partners", "affiliate", "our", "the", "join",
              "become", "your", "new", "funding", "ambassador", "reseller", "resellers", "loan", "merchant", "free",
              "business", "commission", "fast", "same", "day", "a", "an", "and", "for", "with", "structured", "top",
              "commercial", "equipment", "sba", "small", "lending", "lender", "financing", "factoring", "mca", "cash",
              "advance", "credit", "line", "term", "revenue", "based", "direct", "bank", "cpa", "accountant", "payments",
              "processing", "payment", "loan", "loans", "capital", "group", "how", "what", "why", "who", "when", "where",
              "this", "that", "these", "those", "get", "see", "learn", "check", "read", "start", "explore", "view", "more",
              "all", "we", "you", "is", "are", "it", "one", "two", "best", "top", "strong", "fund",
              # generic adjectives that sell a program without naming a company
              "premier", "strategic", "elite", "trusted", "preferred", "exclusive", "select", "premium", "global",
              "national", "leading", "verified", "official", "dedicated"}  # not brands
PROGRAM_PATH = re.compile(
    r"/(?:isos?|iso-partners?|iso-program|partners?|brokers?|broker-partners?|referral-partners?|partner-program|"
    r"become-a-partner|join|affiliates?)(?:[/-]|$)", re.I)
# Links that lead to a log-in, an application form or an account page, never to a program page.
SKIP_LINK = re.compile(r"login|log-in|signin|sign-in|portal|dashboard|register|signup|sign-up|password|checkout|"
                       r"account|cart|/apply(?:[/-]|$)|/application", re.I)
DIRECTORY = re.compile(r"/(?:lenders?|funders?|directory|profiles?|listings?|reviews?|tools?|calculators?)(?:/|$)"
                       r"|-(?:calculator|forecaster)/?$", re.I)
SUBSCRIPTION = re.compile(r"\b(?:/month|per month|monthly (?:fee|subscription|membership)|subscription plans?|"
                          r"membership (?:level|tier|plans?)|training library)\b", re.I)
FUNDER = re.compile(r"\b(?:iso|isos|brokers?|funders?|funding|lenders?|merchant cash advance|mca|loans?)\b", re.I)
VENDOR = re.compile(
    r"\b(?:ucc data|ucc filings? data|data subscriptions?|broker dashboards?|intake platforms?|lead packages?|"
    r"leads? (?:for|available|packages?|sold)|live (?:mca )?leads|ai (?:fund(?:ing)? )?advisors?|crm (?:built|for)|"
    r"software for|sell(?:ing)? (?:mca |funding )?leads|lead[- ]generation|lead[- ]gen)\b", re.I)
SETTLE = re.compile(
    r"\b(?:debt settlement|mca defen[cs]e|merchant defen[cs]e|defen[cs]e (?:firm|attorney)|reconciliation clause|"
    r"debt relief|lawyers?|attorneys?|law firm|litigation|class action)\b", re.I)
CONSULT = re.compile(
    r"\b(?:credit (?:building|repair|restoration)|funding readiness|business (?:coach|coaching)|"
    r"coaching (?:program|clients?|services)|consult(?:ing|ants?) (?:firm|services|group)|we (?:coach|consult)|"
    r"fractional cfo|bookkeeping services|tax (?:prep|preparation) services)\b", re.I)
INDIVIDUAL = re.compile(
    r"\b(?:my (?:own )?(?:link|code|referral link)|dm me|link in (?:my )?bio|i earn|i make (?:\$|a )?commissions?|"
    r"passive income|side hustle|work from (?:home|anywhere|your phone))\b", re.I)
CPA = re.compile(r"\b(?:cpas?|accountants?|bookkeepers?|tax (?:pros?|professionals?))\b", re.I)
CERT = re.compile(r"\b(?-i:ISO)\s?(?:\d{3,5}\b|certif|standards?|compliance)|\bISO (?:settings?|speeds?)\b|\bcamera\b",
                  re.I)
PAYMENTS = re.compile(
    r"\b(?:merchant services|payment processing|payment processors?|card processing|credit card processing|"
    r"interchange|payfac|payment facilitator|pos (?:systems?|terminals?)|merchant accounts?|high[- ]risk merchant|"
    r"terminal leasing)\b", re.I)
NON_US = re.compile(
    r"\b(?:canada|canadian|ontario|toronto|vancouver|alberta|quebec|montreal|australia|australian|sydney|melbourne|"
    r"united kingdom|london|india|mumbai|bengaluru|hong kong|philippines|manila|singapore|dubai|ireland|"
    r"new zealand)\b|\+(?:44|61|91|63|852|65|971|353|64)[ -]?\d", re.I)
# US evidence: a US state code with ZIP, "United States"/"USA", or a US state or city name. Phone numbers do not count:
# the +1 and (area) formats are shared with Canada.
US_HINT = re.compile(
    r"(?i:\b(?:united states|usa)\b)|\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|"
    r"MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\s\d{5}(?:-\d{4})?\b"
    r"|(?i:\b(?:florida|new york|california|texas|new jersey|illinois|georgia|arizona|nevada|utah|colorado|"
    r"north carolina|pennsylvania|ohio|massachusetts|michigan|washington|connecticut|tennessee|virginia|maryland|"
    r"minnesota|missouri|indiana|oregon|miami|brooklyn|los angeles)\b)")
COUNTRY = re.compile(r"\b(?:united states|canada|mexico|united kingdom|australia|india|philippines|germany|france|spain|"
                     r"brazil|japan|china|italy|netherlands|singapore)\b", re.I)
# Page chrome that a quote must not carry: skip links and a language switcher (two or more language names in a row).
LANG = r"(?:EN|ES|FR|English|Español|Français|Deutsch|Português|Italiano|中文|한국어|Tiếng Việt|日本語)"
CHROME_TEXT = re.compile(r"\bskip to (?:main )?content\b|(?<!\w)" + LANG + r"(?:\s+" + LANG + r")+(?!\w)", re.I)
BLOCKS = "p, li, h1, h2, h3, h4, h5, h6, blockquote, dd, dt, td, th, figcaption, div, section, article"
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# A verb that makes the call itself, not a description of a program ("Join the broker network" vs "a program for brokers").
CALL_VERB = re.compile(r"\bbecome an?\b|\bjoin\b|\bnow accepting\b|\bapply\b|\bwanted\b|\bwelcome(?:d)?\b|"
                       r"\bpartner with us\b|\bsign up\b|\bregister\b|\bget started\b", re.I)
# A sentence that invites the reader to act, right after a call to join ("Apply at x.test").
INVITE = re.compile(r"\s*(?:apply|join|register|sign up|submit|visit|contact|learn more|get started|"
                    r"reach out)\b", re.I)


def us_evidenced(geo: str) -> bool:
    """A US address, state or ZIP on the page, and no non-US place or phone. A page that lists three or more countries
    (a country selector), or names a non-US city next to a US state, is not evidence of where the company is, so it
    needs LinkedIn to confirm the US."""
    if NON_US.search(geo):
        return False
    return bool(US_HINT.search(geo)) and len({m.group(0).lower() for m in COUNTRY.finditer(geo)}) < 3


BLOCK = re.compile(r"just a moment|attention required|access denied|checking your browser|enable javascript|"
                   r"network filter|captcha|are you a robot|verify you are human|403 forbidden|404 not found|"
                   r"page not found", re.I)
AT_NAME = re.compile(r"\b(?:at|with|from|join|Join|here at)\s+((?:[A-Z][\w&'.-]*)(?:\s+[A-Z][\w&'.-]*){0,3})")
FUND_NAME = re.compile(r"\b(?:funding|capital|financ\w*|lending|lenders?|funders?|advance|loans?|business)\b", re.I)
URN = re.compile(r"activity[:\-](\d{15,})")
BOUND = re.compile(r"[.!?|](?=\s)")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SKIP_HOSTS = {"debanked.com", "trustpilot.com", "bbb.org", "yelp.com", "crunchbase.com", "zoominfo.com", "g2.com",
              "capterra.com", "nerdwallet.com", "forbes.com", "bloomberg.com", "reuters.com", "prnewswire.com",
              "businesswire.com", "globenewswire.com", "prweb.com", "wikipedia.org", "fool.com", "investopedia.com"}
CHROME_TAGS = "script, style, noscript, svg, template, iframe, nav, header, footer, aside"
CHROME_ROLES = ("navigation", "banner", "contentinfo", "menu", "menubar", "complementary")
CHROME_CLASSES = {"nav", "navbar", "navigation", "menu", "mega-menu", "megamenu", "footer", "site-footer",
                  "site-header", "topbar", "top-bar", "main-nav", "mainnav", "header-nav", "nav-menu"}
RANK = {"company_post": 0, "person_post": 1, "partner_program": 2}


@dataclass
class Hit:
    kind: str                       # partner_program | company_post | person_post (the Signal kind)
    url: str
    quote: str
    date: str | None
    domain: str | None = None
    linkedin_url: str | None = None
    company_name: str | None = None
    person_name: str | None = None  # stored on the Signal, never logged or returned
    person_url: str | None = None
    us_seen: bool = True            # a US address, state, or phone was found on the page (web hits only)


def _window(days: int) -> str:
    return "last-day" if days <= 1 else "last-week" if days <= 7 else "last-month" if days <= 31 else "last-year"


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _text(node) -> str:
    return _ws(node.text(separator=" ")) if node is not None else ""


def _strip_chrome(text: str) -> str:
    """Whitespace-collapsed text without skip links or a language-switcher run."""
    return _ws(CHROME_TEXT.sub(" ", text or ""))


def _clean(tree: HTMLParser) -> None:
    """Strip page chrome in place: scripts, navigation, header, footer, aside and their role or class equivalents."""
    for node in tree.css(CHROME_TAGS):
        node.decompose()
    for role in CHROME_ROLES:
        for node in tree.css(f'[role="{role}"]'):
            node.decompose()
    for node in tree.css("[class]"):
        if set((node.attributes.get("class") or "").lower().split()) & CHROME_CLASSES:
            node.decompose()


def _identity(tree: HTMLParser) -> str:
    parts = []
    title = tree.css_first("title")
    if title is not None:
        parts.append(title.text())
    for sel in ('meta[name="description"]', 'meta[property="og:description"]', 'meta[property="og:title"]'):
        meta = tree.css_first(sel)
        if meta is not None:
            parts.append(meta.attributes.get("content") or "")
    parts += [h.text() for h in tree.css("h1")]
    return _ws(" ".join(parts))


def _title_of(tree: HTMLParser) -> str:
    node = tree.css_first("title")
    return _ws(node.text()) if node is not None else ""


def _ld_objects(data):
    """Top-level JSON-LD objects: each item of a list and each member of an @graph. Nested objects are not yielded."""
    for item in data if isinstance(data, list) else [data]:
        if not isinstance(item, dict):
            continue
        yield item
        graph = item.get("@graph")
        for member in graph if isinstance(graph, list) else []:
            if isinstance(member, dict):
                yield member


def _page_date(tree: HTMLParser) -> str | None:
    """The page's own last-modified date: an article:modified_time or og:updated_time meta, else dateModified of its
    top-level WebPage or Organization JSON-LD object. Must be read before the scripts are stripped."""
    for sel in ('meta[property="article:modified_time"]', 'meta[property="og:updated_time"]'):
        meta = tree.css_first(sel)
        content = (meta.attributes.get("content") or "") if meta is not None else ""
        if ISO_DATE.match(content):
            return content[:10]
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        for obj in _ld_objects(data):
            kinds = obj.get("@type")
            kinds = [kinds] if isinstance(kinds, str) else kinds if isinstance(kinds, list) else []
            modified = obj.get("dateModified")
            if ({k for k in kinds if isinstance(k, str)} & {"WebPage", "Organization"}
                    and isinstance(modified, str) and ISO_DATE.match(modified)):
                return modified[:10]
    return None


def _snippet_date(desc: str, now: datetime) -> str | None:
    """Date from the leading part of a Google snippet ('6 days ago ...', 'Sep 21, 2026 - ...'), never from the body."""
    lead = re.split(r"\s+(?:—|–|-|\.\.\.|…|\|)\s+", desc or "", maxsplit=1)[0]
    return c.relative_date(lead, now) if 0 < len(lead) <= 30 else None


def _sentence(text: str, m: re.Match, left: int = 80, right: int = 200) -> str:
    """A verbatim excerpt around a match (text is whitespace-collapsed): the sentence where it fits, else a tight window
    of `left`/`right` characters, snapped to word boundaries. Never more than 400 characters."""
    lo = max(0, m.start() - left)
    if lo and text[lo - 1] != " ":
        sp = text.find(" ", lo, m.start())
        lo = sp + 1 if sp != -1 else m.start()
    for b in BOUND.finditer(text, lo, m.start()):
        lo = b.end()
    hi = min(len(text), m.end() + right)
    b = BOUND.search(text, m.end(), hi)
    if b:
        hi = b.start() + 1
    elif hi < len(text):
        sp = text.rfind(" ", m.end(), hi)
        hi = sp if sp > m.end() else hi
    return text[lo:hi].strip()[:400]


def _call_text(text: str, m: re.Match) -> str:
    """The sentence that holds a call to join, plus the sentence right after it when that one is an invitation to act
    ('Apply at x.test', 'Visit x.test to apply'). The funder must be named in this text, not anywhere in the post."""
    start = 0
    for b in BOUND.finditer(text, 0, m.start()):
        start = b.end()
    end_b = BOUND.search(text, m.end())
    if end_b is None:
        return text[start:].strip()
    end = end_b.end()
    next_b = BOUND.search(text, end)
    nxt = text[end:next_b.start() + 1 if next_b else len(text)]
    if INVITE.match(nxt):
        end = next_b.end() if next_b else len(text)
    return text[start:end].strip()


def _cta_quote(tree: HTMLParser, rx: re.Pattern, body: str) -> str | None:
    """The sentence holding the call to join, taken from the innermost block whose own text matches, so page chrome
    never leads the quote. A heading is used only when no body block carries the match ('ISO Partner Program' as a
    heading names the program but is not the call). Falls back to the whole body when no single block holds it."""
    best = None
    for node in tree.css(BLOCKS):
        text = _strip_chrome(_text(node))
        m = rx.search(text)
        if not m:
            continue
        sentence = _sentence(text, m)
        key = (not CALL_VERB.search(sentence), node.tag in HEADINGS, len(text))
        if best is None or key < best[0]:
            best = (key, text, m)
    if best is not None:
        _, text, m = best
        return _sentence(text, m)
    text = _strip_chrome(body)  # no single block holds the match (it spans two blocks): quote from the body
    m = rx.search(text)
    return _sentence(text, m) if m else None


def _third_party_program(window: str, domain: str, title: str) -> str | None:
    """A brand this site does not own, named in front of a program ('The Foo Referral Partner Program'). The site owns
    a name that appears in its domain or its <title>, or as the initials of its <title> ('FCFC' for Fast Cash Funding
    Co). Descriptions and headings are not used: a page that promotes another firm's program mentions that firm there."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", title)[:10]).lower()
    for m in BRAND.finditer(window):
        name = m.group(1)
        low = name.lower()
        if low in BRAND_STOP or low in domain or low in words:  # whole words only: 'foo' is not in 'brokers'
            continue
        if len(low) >= 3 and low in initials:
            continue
        return name
    return None


def _dnp(cfg: dict, text: str) -> bool:
    low = (text or "").lower()
    flat = low.replace(" ", "")
    return any(n in low or n.replace(" ", "") in flat for n in cfg.get("_dnp", []))


def _cpa_only(text: str) -> str | None:
    """A referral programme for accountants (professionals, not ISOs): CPA words with no funding word near them."""
    return "CPA referral only" if CPA.search(text) and not FUNDER.search(text) else None


def identity_drop(ident: str, geo: str, cfg: dict, *, full: bool = True) -> str | None:
    """Why identity text is a seller, vendor, individual, or off-target, or None. `full=False` skips the checks that need
    the whole page (payments, CPA-only, non-US), for pre-fetch screening of a search snippet."""
    for name, rx in c.GATES_RX.items():
        if name != "seller" and rx.search(ident):  # funders say "we help businesses": not a seller signal
            return name
    if cfg["_rx"]["finance_vendors"].search(ident) or VENDOR.search(ident):
        return "vendor (leads, data, CRM or software)"
    if SETTLE.search(ident):
        return "settlement or defence firm"
    if CONSULT.search(ident):
        return "consultant or credit repair"
    if INDIVIDUAL.search(ident):
        return "individual affiliate"
    if CERT.search(ident):
        return "ISO certification collision"
    if SUBSCRIPTION.search(ident):
        return "subscription or membership offer"
    if not full:
        return None
    if PAYMENTS.search(ident) and not cfg["_rx"]["finance"].search(ident):
        return "payments ISO without funding term"
    if NON_US.search(geo) and not US_HINT.search(geo):
        return "not US"
    return None


def judge_page(html: str, status: int, final_url: str, result_url: str, snippet: str, days: int, now: datetime,
               cfg: dict, *, require_date: bool = True) -> tuple[Hit | None, str]:
    """(hit, reason) for one fetched funder or broker page. Pure: no I/O."""
    if status >= 400 or not html:
        return None, "http error"
    if c.parked(html):
        return None, "parked domain"
    tree = HTMLParser(html)
    head = _identity(tree)
    page_date = _page_date(tree)  # before _clean: the JSON-LD lives in script tags that _clean removes
    geo = _text(tree.body)
    _clean(tree)
    body = _text(tree.body)
    if len(body) < 200 or BLOCK.search(head) or BLOCK.search(body[:300]):
        return None, "blocked or thin page"
    domain = c.host_of(final_url)
    if not domain or domain != c.host_of(result_url):
        return None, "redirected off host"
    path = urlsplit(final_url).path
    if detect.is_article(final_url) or DIRECTORY.search(path):
        return None, "article or directory page"
    if _dnp(cfg, f"{domain} {head}"):
        return None, "do not prospect"
    program_page = bool(PROGRAM_PATH.search(path))
    cta_m = CTA.search(body)
    m = cta_m or (WEAK_CTA.search(body) if program_page else None)
    if m is None:
        return None, "no recruiting copy in body"  # a landing page may still lead to its program page (followed once)
    if not program_page and EDITORIAL_SLUG.search(path):
        return None, "article or directory page"
    if not cfg["_rx"]["finance"].search(body):
        return None, "no funding term"
    window = body[max(0, m.start() - 300): m.end() + 300]
    if not program_page and not OWN.search(window) and not m.group(0).lower().startswith("become"):
        return None, "not the company's own recruiting copy"
    if _third_party_program(window, domain, _title_of(tree)):
        return None, "third-party program"
    drop = identity_drop(f"{head} {snippet} {window}", geo, cfg) or _cpa_only(window)
    if drop:
        return None, drop
    sdate = _snippet_date(snippet, now)
    if sdate and not c.within_days(sdate, days, now):
        return None, "stale snippet date"
    date = sdate or (page_date if c.within_days(page_date, days, now) else None)
    if date is None and require_date:
        return None, "undated (no date in window)"  # dated evidence only: an evergreen page is covered by collect_finance
    quote = _cta_quote(tree, CTA if cta_m else WEAK_CTA, body) or _sentence(body, m)
    return Hit("partner_program", c.unwrap(final_url), quote, date, domain=domain,
               us_seen=us_evidenced(geo)), "kept"


def post_url(post: dict) -> str | None:
    """Canonical feed URL. The raw /posts/<author-slug>_activity-... URL carries the author's name, so it is replaced."""
    m = URN.search(post.get("activityUrn") or "") or URN.search(post.get("url") or "")
    return f"https://www.linkedin.com/feed/update/urn:li:activity:{m.group(1)}/" if m else None


def _named_funder(text: str) -> str | None:
    for m in AT_NAME.finditer(text):
        name = m.group(1).strip(" .")
        if FUND_NAME.search(name):
            return name
    return None


def judge_post(post: dict, days: int, now: datetime, cfg: dict) -> tuple[Hit | None, str]:
    """(hit, reason) for one LinkedIn post. Pure: no I/O. A person's post is kept only when the sentence holding the
    call to join is written in the first person and names the funder by domain, or 'at <Company>' appears in the post.
    Employment cannot be checked through the API, so that stays a parent decision (config person_posts)."""
    post = {**post, "activityUrn": post.get("activityUrn") or "", "url": post.get("url") or ""}
    text = _ws(post.get("description") or "")
    url = post_url(post)
    if not url:
        return None, "no activity id"
    author = post.get("author") or {}
    aurl = author.get("url") or ""
    li = sg.company_url(aurl)
    person = li is None and "/in/" in aurl
    if li is None and not person:
        return None, "author is neither a company nor a person"
    m = CTA.search(text)
    if m is None:
        return None, "no recruiting copy"
    if not cfg["_rx"]["finance"].search(text):
        return None, "no funding term"
    if _dnp(cfg, text):
        return None, "do not prospect"
    drop = identity_drop(text, text, cfg) or _cpa_only(text)
    if drop:
        return None, drop
    date = sg.post_date(post)
    if date and not c.within_days(date, days, now):
        return None, "stale post"
    quote = _sentence(text, m)
    if li:
        return Hit("company_post", url, quote, date, linkedin_url=li), "kept"
    call = _call_text(text, m)
    if not OWN_POST.search(call):
        return None, "person post without first-person ownership"
    dom = next(iter(c.domains_in_text(call)), None)
    name = None if dom else _named_funder(text)
    if not dom and not name:
        return None, "person post names no funder"
    return Hit("person_post", url, quote, date, domain=dom, company_name=name,
               person_name=author.get("name") or None, person_url=aurl), "kept"


def _candidate(h: Hit) -> sg.Candidate:
    if h.kind == "partner_program":
        # no US evidence on the page: the LinkedIn company's country must confirm US (growth.process)
        return sg.Candidate("finance", sg.Signal(kind="partner_program", url=h.url, quote=h.quote,
                                                 signal_date=h.date, platform="web"), domain=h.domain,
                            require_us=not h.us_seen)
    if h.kind == "company_post":
        return sg.Candidate("finance", sg.Signal(kind="company_post", url=h.url, quote=h.quote,
                                                 signal_date=h.date, platform="linkedin"),
                            linkedin_url=h.linkedin_url, require_us=True)
    return sg.Candidate("finance", sg.Signal(kind="person_post", url=h.url, quote=h.quote, signal_date=h.date,
                                             person_name=h.person_name, person_url=h.person_url, platform="linkedin"),
                        domain=h.domain, company_name=h.company_name, require_us=True)


def _dedupe(hits: list[Hit]) -> list[Hit]:
    """One candidate per company: its LinkedIn page, else its domain, else its named funder. Stronger kinds first."""
    out, keys = [], set()
    for h in sorted(hits, key=lambda h: RANK[h.kind]):
        key = (("li", h.linkedin_url) if h.linkedin_url else ("dom", h.domain) if h.domain
               else ("name", c.norm(h.company_name or "")))
        if key in keys:
            continue
        keys.add(key)
        out.append(h)
    return out


def _judge_fetched(ctx, stats: Counter, page: detect.Page, result_url: str, snippet: str) -> tuple[Hit | None, str]:
    try:
        return judge_page(page.html, page.status, page.final_url, result_url, snippet, ctx.days, ctx.now,
                          ctx.signals_cfg, require_date=bool(ctx.cfg.get("require_date", True)))
    except Exception as exc:  # one bad page must not discard the source's other candidates
        stats[f"error: {type(exc).__name__}"] += 1
        return None, "error"


def _program_links(page: detect.Page, cfg: dict, *, teaser: bool) -> list[str]:
    """At most one same-host link to follow: login, account and application links removed, program paths first. A
    teaser (a non-program page that carries a call to join) needs a program path; a landing page without copy takes any
    finance link."""
    out = []
    for link in sg.finance_links(page.html, page.final_url, cfg):
        path = urlsplit(link).path
        if link == page.final_url or SKIP_LINK.search(path):
            continue
        program = bool(PROGRAM_PATH.search(path))
        if teaser and not program:
            continue
        out.append((0 if program else 1, link))
    return [link for _, link in sorted(out)][:1]


async def _judged(ctx, stats: Counter, page: detect.Page, result_url: str, snippet: str) -> tuple[Hit | None, str]:
    """Judge a fetched funder page. A landing page with no body copy is replaced by its followed program link. A teaser
    on a non-program page is replaced by its program page when that page passes on its own date, else kept as it is."""
    hit, reason = _judge_fetched(ctx, stats, page, result_url, snippet)
    if hit is None and reason == "no recruiting copy in body":
        links = _program_links(page, ctx.signals_cfg, teaser=False)
        sub = await ctx.get(links[0], role="linked") if links else None
        if sub is not None:
            hit, reason = _judge_fetched(ctx, stats, sub, result_url, "")  # the followed page is dated by its own date only
            stats["followed link: " + ("kept" if hit else reason)] += 1
    elif hit is not None and not PROGRAM_PATH.search(urlsplit(hit.url).path):
        links = _program_links(page, ctx.signals_cfg, teaser=True)
        sub = await ctx.get(links[0], role="linked") if links else None
        if sub is not None:
            prog, why = _judge_fetched(ctx, stats, sub, result_url, "")
            stats["teaser: " + ("program page kept" if prog else why)] += 1
            if prog is not None:
                hit, reason = prog, "kept"
    return hit, reason


async def _google(ctx, hits: list[Hit], stats: Counter) -> None:
    cfg = ctx.cfg
    results: list[dict] = []
    for q in cfg["google_queries"]:
        if not ctx.budget.ok():
            stats["stopped: budget"] += 1
            break
        data = await sg.sc_get(ctx.sc, "/v1/google/search",
                               {"query": f'{q} {cfg["google_exclude"]}', "date_posted": _window(ctx.days)}, SRC)
        results += (data or {}).get("results") or []
    stats["search results"] += len(results)
    done: set[str] = set()          # domains with a kept page: one candidate each
    tries: Counter = Counter()
    pages = 0
    for r in results:
        url = r.get("url") or ""
        domain = c.host_of(url)
        if not domain or domain in c.PLATFORM_HOSTS or domain in SKIP_HOSTS:
            stats["host skipped"] += 1
            continue
        if domain in done or tries[domain] >= int(cfg["tries_per_domain"]):
            stats["duplicate host"] += 1
            continue
        desc = _ws(r.get("description") or "")
        title = _ws(r.get("title") or "")
        pre = identity_drop(f"{title} {desc}", f"{title} {desc}", ctx.signals_cfg, full=False)
        if pre:
            stats[f"snippet: {pre}"] += 1
            continue
        sdate = _snippet_date(desc, ctx.now)
        if sdate and not c.within_days(sdate, ctx.days, ctx.now):
            stats["snippet: stale"] += 1
            continue
        if pages >= int(cfg["max_pages"]):
            stats["stopped: page cap"] += 1
            break
        page = await ctx.get(c.unwrap(url), role="iso")
        if page is None:
            stats["page not fetched"] += 1
            continue
        pages += 1
        tries[domain] += 1
        hit, reason = await _judged(ctx, stats, page, url, desc)
        stats[f"page: {'kept' if hit else reason}"] += 1
        if hit:
            hits.append(hit)
            done.add(domain)


async def _posts(ctx, hits: list[Hit], stats: Counter) -> None:
    for q in ctx.cfg["post_queries"]:
        if not ctx.budget.ok():
            stats["stopped: budget"] += 1
            break
        data = await sg.sc_get(ctx.sc, "/v1/linkedin/search/posts",
                               {"query": q, "date_posted": _window(ctx.days)}, SRC)
        for post in (data or {}).get("posts") or []:
            try:
                hit, reason = judge_post(post, ctx.days, ctx.now, ctx.signals_cfg)
            except Exception as exc:  # one malformed post must not discard the other posts
                stats[f"error: {type(exc).__name__}"] += 1
                continue
            if hit and hit.kind == "person_post" and not ctx.cfg.get("person_posts", True):
                hit, reason = None, "person post (disabled)"
            stats[f"post: {'kept' if hit else reason}"] += 1
            if hit:
                hits.append(hit)


async def collect(ctx) -> list[sg.Candidate]:
    hits: list[Hit] = []
    stats: Counter = Counter()
    for phase in (_google, _posts):
        try:
            await phase(ctx, hits, stats)
        except sg.OutOfCredits:  # checked first: the broad handler below must never swallow it
            stats["stopped: out of credits"] += 1
            break
        except Exception as exc:  # an unexpected failure in one phase keeps the other phase's hits
            stats[f"error: {type(exc).__name__}"] += 1
    out = _dedupe(hits)
    ctx.say("finance_iso: " + str(len(out)) + " candidates; " + ", ".join(f"{k} {v}" for k, v in sorted(stats.items())))
    return [_candidate(h) for h in out]
