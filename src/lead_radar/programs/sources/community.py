"""Founder and operator communities: Hacker News, Indie Hackers, Product Hunt and Shopify Community.

Finds companies talking about launching, growing, promoting or recruiting for THEIR OWN affiliate, partner, referral
or ambassador program, and Hacker News "Who is hiring?" rows for affiliate and referral roles. Public data only.

- Hacker News: the free Algolia API (story search by date, and the newest 'Ask HN: Who is hiring?' threads). Algolia
  answers application/json, which ctx.get does not return, so JSON is read through ctx.http with the same robots,
  rate and per-run page cap. Firebase user profiles are refused by robots.txt for this client, so no HN profile
  is read.
- Indie Hackers, Product Hunt, Shopify Community: ScrapeCreators Google search (1 credit per query with results),
  date-windowed and site-scoped. Indie Hackers bodies are never fetched (snippet only). A Product Hunt product page
  and a Shopify thread are read through ctx.get (robots honoured) to find the company's own domain.

A domain is accepted only when the post itself names its brand (the brand label appears as whole words in the post
title or URL slug, never only in the body), its homepage is live, not parked, on the same registrable domain, names
that brand in its own title, and does not name itself as a seller or software vendor. The LinkedIn page is taken from
the homepage only when its slug names the brand; otherwise the growth Resolver finds it from the domain.
Every candidate is require_us: it is listed only when LinkedIn confirms a US company.
"""
from __future__ import annotations

import html as htmllib
import re
from datetime import timedelta
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from ...listings.http import Disallowed
from ...scrapecreators import OutOfCredits
from .. import detect, signals as sg
from . import common

NAME = "community"
DEFAULTS = {
    "enabled": True,
    "credits": 10,             # ScrapeCreators credits per run: Google queries only (1 each, free when no results)
    "http_cap": 120,           # free page and API reads per run (Algolia, homepages, product and thread pages)
    "hn_queries": ["our affiliate program", "launched affiliate program", "affiliate program saas",
                   "referral program launched", "partner program launched"],
    "hn_hits": 50,
    "hiring_threads": 2,       # newest "Ask HN: Who is hiring?" threads read each run (keyed by thread id)
    "hiring_days": 45,         # a hiring row stays a signal this long
    "max_google_queries": 6,
    "google_queries": [
        'site:indiehackers.com "we launched" "affiliate program"',
        'site:indiehackers.com "I built" "affiliate program"',
        'site:indiehackers.com "our referral program" OR "our partner program" OR "our ambassador program"',
        'site:producthunt.com "we launched" "affiliate program"',
        'site:producthunt.com "our referral program" OR "our affiliate program"',
        'site:community.shopify.com "we launched" "affiliate program"',
    ],
    "max_candidates": 40,
}

ALGOLIA = "https://hn.algolia.com/api/v1/"
HN_ITEM = "https://news.ycombinator.com/item?id={}"
HIRING_TITLE = re.compile(r"^\s*Ask HN: Who is hiring\? \((?:January|February|March|April|May|June|July|August|"
                          r"September|October|November|December) \d{4}\)\s*$", re.I)
PLATFORM_OF = {"indiehackers.com": "indiehackers", "producthunt.com": "producthunt", "community.shopify.com": "shopify"}
POST_PATH = {
    "indiehackers": re.compile(r"^/post/[^/]+/?$"),
    "producthunt": re.compile(r"^/(?:products|posts)/[^/]+/?$|^/p/[^/]+(?:/[^/]+)?/?$"),
    "shopify": re.compile(r"/td-p/\d+/?$|^/t/[^/]+/\d+/?$"),
}
# Words of a platform's own URL structure, left out of the slug that may name a brand.
PATH_WORDS = {"post", "posts", "products", "product", "p", "c", "t", "td", "shopify", "discussion"}
LISTING = re.compile(r"/(?:alternatives|competitors|leaderboard|compare|categories|reviews?)(?:/|$)|/best-", re.I)
# Hosts that carry code, docs, app stores or hosted demos rather than a company's own site.
DENY_HOSTS = {
    "github.com", "github.io", "gitlab.com", "gitlab.io", "bitbucket.org", "npmjs.com", "pypi.org", "crates.io",
    "huggingface.co", "arxiv.org", "wikipedia.org", "notion.so", "notion.site", "gitbook.io", "readthedocs.io",
    "vercel.app", "netlify.app", "netlify.com", "pages.dev", "workers.dev", "herokuapp.com", "onrender.com",
    "fly.dev", "glitch.me", "replit.app", "framer.website", "webflow.io", "carrd.co", "web.app", "firebaseapp.com",
    "appspot.com", "cloudfront.net", "shopify.dev", "shopifycdn.com", "shopifycloud.com", "gravatar.com",
    "googleusercontent.com", "imgur.com", "giphy.com", "loom.com", "figma.com", "canva.com", "docs.google.com",
    # news and media: a launch reported by a publication names the publication's own site, never the company's
    "techcrunch.com", "theverge.com", "wired.com", "forbes.com", "nytimes.com", "bloomberg.com", "reuters.com",
    "wsj.com", "theguardian.com", "bbc.co.uk", "bbc.com", "cnbc.com", "businessinsider.com", "inc.com",
    "entrepreneur.com", "fastcompany.com", "venturebeat.com", "zdnet.com", "cnet.com", "engadget.com", "techradar.com",
    "mashable.com", "axios.com", "fortune.com", "yahoo.com", "huffpost.com", "time.com", "usatoday.com",
    "marketwatch.com", "investopedia.com", "nerdwallet.com", "theinformation.com", "protocol.com", "sifted.eu",
    "tech.eu",
}
ANCHOR = re.compile(r'<a\b[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
TAG = re.compile(r"<[^>]+>")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Errors from one malformed item (a bad URL, an unexpected field type) drop that item only, never the whole stage.
ITEM_ERRORS = (ValueError, TypeError, AttributeError, KeyError, IndexError)
# The program noun, singular or plural, program or programme, owned by an affiliate, referral, partner, ambassador,
# reseller or creator scheme.
PROGRAM = re.compile(r"\b(?:affiliate|referral|partner(?:ship)?|ambassador|reseller|creator)s? programm?e?s?\b", re.I)
SEEK = re.compile(r"\b(?:looking|searching|seeking|hunting|which|what|best|any|good|recommend\w*)\b[^.!?\n]{0,30}?"
                  r"\b(?:affiliate|referral|partner|ambassador)s? programm?e?s?\b", re.I)
OWN = re.compile(r"\b(?:our|my)\b[^.!?\n]{0,30}\b(?:affiliate|referral|partner|ambassador)s? programm?e?s?\b|"
                 r"\bwe (?:just |recently |finally |now )?(?:launched|launching|have|built|run|started|are running)\b",
                 re.I)
# A buyer verb (launching, joining, recruiting, accepting, growing) must share a sentence with the program noun.
BUYER_VERB = re.compile(r"\b(?:launch(?:es|ed|ing)?|join|now accepting|accepting|hiring|looking for|seeking|"
                        r"grow(?:s|ing)?|scal(?:e|es|ed|ing)|apply now|become an?|introduc(?:e|es|ed|ing)|"
                        r"rolling out|rolled out|open(?:ing)? (?:up|applications)|(?:is|are|now) (?:live|open)|"
                        r"wanted|recruit\w*)\b", re.I)
# Educational and listicle titles are guides about a program, not a company running one.
EDU = re.compile(r"\b(?:how to|guide|tips|ways to|best|templates?|examples?|checklist|playbook|what is|what are)\b", re.I)
# Affiliate, referral and partner software: these sell the program, they do not run one.
VENDOR = re.compile(r"\b(?:(?:affiliate|referral|partner(?:ship)?|ambassador) programm?e?s? (?:software|platform|tool|"
                    r"builder|tracking|management)|(?:software|platform|tool)s? for (?:affiliate|referral|partner(?:ship)?|"
                    r"ambassador) programm?e?s?)\b", re.I)
PARTNERSHIP_ROLE = re.compile(r"\bpartnerships?\b|\bpartner manager\b", re.I)
EMPLOYEE_REF = re.compile(r"\breferral (?:bonus(?:es)?|fees?|rewards?|program for (?:employees|staff))\b|"
                          r"\bemployee referrals?\b|\binternal referrals?\b|\breferrals? (?:are )?welcome\b|"
                          r"\brefer (?:a|an|your|friends?|candidates?|colleagues?)\b|\bcandidate referrals?\b", re.I)
FUNDING = re.compile(r"\b(?:merchant cash|mca|funding|loans?|lending|lenders?|sba|equipment financ\w*|working capital|"
                     r"isos?|brokers?)\b", re.I)
US_MARK = re.compile(r"\b(?:US|USA|U\.S\.|United States|NYC|New York|SF|San Francisco|Seattle|Denver|Boulder|Austin|"
                     r"Chicago|Boston|Los Angeles|Miami|Atlanta|Dallas|Houston|Portland|Phoenix|Brooklyn|"
                     r"Chattanooga|Remote \(US\)|Remote within (?:the )?(?:US|USA))\b")
NON_US = re.compile(r"\b(?:EU|Europe|European|UK|United Kingdom|London|Berlin|Paris|Amsterdam|Canada|Toronto|Vancouver|"
                    r"Montreal|CET|CEST|GMT|BST|India|Bangalore|Bengaluru|Zug|Switzerland|Zurich|Germany|France|Spain|"
                    r"Australia|Sydney|Melbourne|Singapore|Israel|Tel Aviv|Poland|Warsaw|Ireland|Dublin|Remote \(EU\))\b")
LEAD_DATE = re.compile(r"^\s*(\d+\s*(?:minutes?|hours?|days?|weeks?)\s+ago|yesterday|today|"
                       r"[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+20\d\d)\b", re.I)


# pure helpers -----------------------------------------------------------------------------------------------------

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _scrub(s: str) -> str:
    """Whitespace-collapsed text with contact email addresses removed (they never reach a quote)."""
    return _clean(EMAIL.sub("", s or ""))


def html_text(s: str) -> str:
    """Plain text of an HTML fragment: paragraph and line breaks kept as newlines, tags removed, entities decoded."""
    s = re.sub(r"(?i)<br\s*/?>|</p\s*>|</li\s*>|<p[^>]*>", "\n", s or "")
    return htmllib.unescape(TAG.sub("", s))


def _denied(root: str | None) -> bool:
    return not root or root in DENY_HOSTS


def canonical(url: str) -> str | None:
    """https://<host><path> for an Indie Hackers, Product Hunt or Shopify Community URL (no query, fragment or
    tracking), else None. A malformed URL is None, never an exception."""
    try:
        parts = urlsplit(common.unwrap(url or ""))
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if detect.bare_host(host) not in PLATFORM_OF:
        return None
    return f"https://{host}{parts.path.rstrip('/')}"


def lead_date(desc: str, now) -> tuple[str | None, int]:
    """ISO date of a leading relative or absolute date in a search snippet ('6 days ago ...', 'Sep 30, 2026 ...'),
    and where that prefix ends. Dates inside the body are never used (None, 0 when there is no leading date)."""
    m = LEAD_DATE.match(desc or "")
    if not m:
        return None, 0
    return common.relative_date(m.group(1), now), m.end()


def window(days: int) -> str:
    return "last-day" if days <= 1 else "last-week" if days <= 7 else "last-month" if days <= 31 else "last-year"


def _names(text: str, label: str) -> bool:
    """True when the brand label is one name in the text: its whole words run together to the label, so 'Widget Co'
    names 'widgetco' and 'Shipping' does not name 'ship'. Never a substring match. False for an empty label."""
    if not label:
        return False
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    for i in range(len(toks)):
        acc = ""
        for t in toks[i:i + 4]:
            acc += t
            if acc == label:
                return True
            if len(acc) >= len(label):
                break
    return False


def _home_names(html: str) -> str:
    """The text a homepage names itself with: its <title> and og:site_name."""
    found = re.findall(r"<title[^>]*>(.*?)</title>|property=[\"']og:site_name[\"'][^>]*content=[\"']([^\"']+)",
                       (html or "")[:200_000], re.I | re.S)
    return " | ".join(_clean(htmllib.unescape(TAG.sub(" ", a or b))) for a, b in found)


def _seller_home(html: str, cfg: dict) -> bool:
    """True when the homepage names itself as a seller, a software vendor, an excluded vertical, or a guide."""
    said = _home_names(html)
    return bool(VENDOR.search(said) or _excluded_text(said, cfg["_rx"]) or common.text_gate(said))


def post_gate(text: str, cfg: dict, title: str = "") -> str | None:
    """Why a post is not a company talking about its own program, or None when it passes.

    The program noun must be named, and a buyer verb (launching, joining, recruiting, accepting) must share a sentence
    with it. A Show HN post gets no exemption: the maker's own launch passes on the same rule. Seekers, individual
    affiliates, sellers, vendors, verticals and educational titles are dropped."""
    rx = cfg["_rx"]
    if not PROGRAM.search(text):
        return "no program named"
    if SEEK.search(text) and not OWN.search(text):
        return "seeker"
    reason = common.text_gate(text)
    if reason:
        return reason
    if VENDOR.search(text):
        return "affiliate or referral software"
    reason = _excluded_text(text, rx)
    if reason:
        return reason
    if EDU.search(title or text.split(". ")[0]):
        return "educational title"
    if not any(PROGRAM.search(s) and BUYER_VERB.search(s) for s in re.split(r"[.!?;\n]+", text)):
        return "no buyer verb next to the program"
    return None


def _excluded_text(text: str, rx: dict) -> str | None:
    """Which exclusion a text trips, or None. vendor_names is compiled without re.I in signals.load(), so it is
    matched on lowercased text, as signals.exclusion() does."""
    for key in ("verticals", "sellers", "vendors"):
        if rx[key].search(text):
            return key
    if rx["vendor_names"].search(text.lower()):
        return "vendor_names"
    return None


def hiring_parts(comment_html: str) -> tuple[str, str, str, list[str]] | None:
    """(company, role line, first-line text, URLs) from a 'Company | Role | Location' first paragraph, else None."""
    first = ""
    for seg in re.split(r"(?i)<p[^>]*>|</p\s*>|<br\s*/?>", comment_html or ""):
        if html_text(seg).strip():
            first = seg
            break
    line = _clean(html_text(first))
    if "|" not in line:
        return None
    urls = re.findall(r'href="(https?://[^"]+)"', first) + re.findall(r"https?://[^\s<>\"()]+", html_text(first))
    parts = [p.strip() for p in line.split("|")]
    name = re.sub(r"\(\s*https?://[^)]*\)|https?://\S+", "", parts[0]).strip(" -–—:()[]")
    role = " | ".join(parts[1:])
    return name, role, line, [htmllib.unescape(u) for u in urls]


def hiring_gate(name: str, role: str, line: str, body: str, cfg: dict) -> str | None:
    """Why a hiring row is not an affiliate or referral role at a US company, or None when it passes."""
    rx = cfg["_rx"]
    if not name or not role:
        return "no company or role"
    if FUNDING.search(body):
        return "funding context (finance lane)"
    role_c, body_c = EMPLOYEE_REF.sub(" ", role), EMPLOYEE_REF.sub(" ", body)  # 'referral bonus' is not a referral role
    if not rx["program_job"].search(role_c):
        if not (PARTNERSHIP_ROLE.search(role_c) and PROGRAM.search(body_c)):
            return "not an affiliate or referral role"
    if NON_US.search(line) and not US_MARK.search(line):
        return "not US"
    if VENDOR.search(body_c):
        return "affiliate or referral software"
    return _excluded_text(body_c, rx) or common.text_gate(body_c)


def _label(domain: str) -> str:
    """The brand a registrable domain names: 'get.snugcv.com' -> 'snugcv' (normalised, '' when too short)."""
    label = common.norm(common.registrable(domain).split(".")[0])
    return label if len(label) >= 4 else ""


def _linkedin_for(page: detect.Page, domain: str) -> str | None:
    """The homepage's LinkedIn company page whose slug names the domain's brand (the first such link), else None."""
    label = _label(domain)
    if not label:
        return None
    for m in sg.LI_COMPANY.finditer(page.html or ""):
        li = sg.company_url(m.group(0))
        if li and _names(li.rsplit("/", 1)[-1].replace("-", " "), label):
            return li
    return None


def _hits(data) -> list[dict]:
    """The dict rows of an Algolia 'hits' list; anything else is no rows."""
    hits = data.get("hits") if isinstance(data, dict) else None
    return [h for h in hits if isinstance(h, dict)] if isinstance(hits, list) else []


def _results(data) -> list[dict]:
    """The dict rows of a ScrapeCreators Google 'results' list; anything else is no rows."""
    res = data.get("results") if isinstance(data, dict) else None
    return [r for r in res if isinstance(r, dict)] if isinstance(res, list) else []


async def _get_json(ctx, url: str):
    """One JSON GET through the polite client, counted against ctx.http_cap. None when refused, failed, not an
    object, or over the cap."""
    if ctx.http_used >= ctx.http_cap:
        return None
    ctx.http_used += 1
    try:
        resp = await ctx.http.get(url)
    except (Disallowed, httpx.HTTPError, ValueError):
        return None
    if resp is None or resp.status_code >= 400:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _verify_home(ctx, domain: str) -> detect.Page | None:
    """The company's own homepage: live, not parked, on the same registrable domain, naming the domain's brand in its
    own title or site name, and not naming itself as a seller or vendor."""
    label = _label(domain)
    if not label:
        return None
    page = await ctx.get(f"https://{domain}/", "home")
    if page is None or page.status >= 400 or not page.html:
        return None
    if common.registrable(detect.bare_host(page.final_url)) != common.registrable(domain):
        return None
    if common.parked(page.html) or not _names(_home_names(page.html), label):
        return None
    if _seller_home(page.html, ctx.signals_cfg):
        return None
    return page


async def _root(ctx, root: str, post: str | None) -> tuple[str, str | None] | None:
    """(domain, LinkedIn page) for a domain a post points at. When `post` is given (the domain came from the post's
    text or link), its title or slug must name the brand, even for a domain already in the store. A Show HN's own
    link is the one case taken without a post check, and its homepage must still name the brand."""
    if _denied(root):
        return None
    label = _label(root)
    if post is not None and not _names(post, label):
        return None
    if ctx.known(root):
        return root, None
    page = await _verify_home(ctx, root)
    return (root, _linkedin_for(page, root)) if page else None


async def _external_roots(ctx, page_url: str, page: detect.Page, visit_only: bool) -> list[str]:
    """Company domains a Product Hunt or Shopify page links to. visit_only keeps 'Visit website' links (Product
    Hunt). Product Hunt's /r/ redirects are followed to the site they land on."""
    roots: list[str] = []
    for href, label in ANCHOR.findall(page.html[:3_000_000]):
        try:
            href = htmllib.unescape(href)
            if visit_only and not re.search(r"visit|website", html_text(label), re.I):
                continue
            if not href.startswith("http") and not href.startswith("/r/"):
                continue
            if "producthunt.com/r/" in href or href.startswith("/r/"):
                follow = await ctx.get(urljoin(page_url, href), "linked")
                if follow is not None and follow.status < 400:
                    root = common.host_of(follow.final_url)
                    if root and not _denied(root) and root not in roots:
                        roots.append(root)
                continue
            root = common.host_of(href)
            if root and not _denied(root) and root not in roots:
                roots.append(root)
        except ITEM_ERRORS:
            continue
        if len(roots) >= 5:
            break
    return roots


def _quote(text: str) -> str:
    return common.quote_around(text, PROGRAM, 400)


# collectors -------------------------------------------------------------------------------------------------------

async def _hn_stories(ctx, cfg: dict) -> list[sg.Candidate]:
    """Hacker News stories (Show HN, Ask HN, links) from makers and operators who name their own program."""
    since = int((ctx.now - timedelta(days=ctx.days)).timestamp())
    out: list[sg.Candidate] = []
    seen: set[str] = set()
    counts = {"hits": 0, "dropped": 0, "unresolved": 0, "errors": 0}
    for q in cfg["hn_queries"]:
        url = ALGOLIA + "search_by_date?" + urlencode({"query": q, "tags": "story", "hitsPerPage": int(cfg["hn_hits"]),
                                                       "numericFilters": f"created_at_i>{since}"})
        for hit in _hits(await _get_json(ctx, url)):
            try:
                oid = str(hit.get("objectID") or "")
                created = (hit.get("created_at") or "")[:10]
                if not oid or oid in seen or not common.within_days(created, ctx.days, ctx.now):
                    continue
                seen.add(oid)
                counts["hits"] += 1
                title = _clean(hit.get("title") or "")
                text = _scrub(f"{title}. {html_text(hit.get('story_text') or '')}")
                maker = title.lower().startswith("show hn")
                if post_gate(text, ctx.signals_cfg, title=title):
                    counts["dropped"] += 1
                    continue
                got = None
                story_url = hit.get("url") or ""
                root = common.host_of(story_url) if story_url else None
                if root:  # a Show HN links the maker's own site; any other story must name the brand in its title
                    got = await _root(ctx, root, None if maker else title)
                for d in ([] if got else common.domains_in_text(text)):
                    got = await _root(ctx, d, title)  # the brand must be named in the title, not just mentioned
                    if got:
                        break
                if not got:
                    counts["unresolved"] += 1
                    continue
                domain, li = got
                out.append(sg.Candidate(list="affiliate", linkedin_url=li, domain=domain, require_us=True,
                                        signal=sg.Signal("community_post", HN_ITEM.format(oid), _quote(text),
                                                         created or None, platform="hackernews")))
            except ITEM_ERRORS:
                counts["errors"] += 1
    ctx.say(f"community: hackernews stories {counts['hits']} read, {len(out)} resolved, {counts['dropped']} "
            f"dropped by gates, {counts['unresolved']} without a verified domain, {counts['errors']} malformed")
    return out


async def _hn_hiring(ctx, cfg: dict) -> list[sg.Candidate]:
    """Affiliate and referral roles in the newest 'Ask HN: Who is hiring?' threads (keyed by thread id)."""
    threads: list[str] = []
    found = await _get_json(ctx, ALGOLIA + "search_by_date?" + urlencode(
        {"query": "Who is hiring?", "tags": "author_whoishiring,story", "hitsPerPage": 8}))
    for hit in _hits(found):
        try:
            created = (hit.get("created_at") or "")[:10]
            if HIRING_TITLE.match(hit.get("title") or "") and common.within_days(created, int(cfg["hiring_days"]),
                                                                                  ctx.now):
                threads.append(str(hit.get("objectID")))
        except ITEM_ERRORS:
            continue
    threads = threads[: int(cfg["hiring_threads"])]
    out: list[sg.Candidate] = []
    seen: set[str] = set()
    counts = {"rows": 0, "errors": 0}
    for tid in threads:
        dump = await _get_json(ctx, ALGOLIA + "search?" + urlencode(
            {"query": "", "tags": f"comment,story_{tid}", "hitsPerPage": 1000}))
        for c in _hits(dump):
            try:
                oid = str(c.get("objectID") or "")
                if str(c.get("parent_id") or "") != tid or oid in seen:
                    continue
                seen.add(oid)
                cdate = (c.get("created_at") or "")[:10]
                if not common.within_days(cdate, int(cfg["hiring_days"]), ctx.now):
                    continue
                parts = hiring_parts(c.get("comment_text") or "")
                if not parts:
                    continue
                name, role, line, urls = parts
                body = _scrub(html_text(c.get("comment_text") or ""))
                if hiring_gate(name, role, line, body, ctx.signals_cfg):
                    continue
                domain = next((r for r in (common.host_of(u) for u in urls) if r and not _denied(r)), None)
                line = _scrub(line)
                signal = sg.Signal("job", HN_ITEM.format(oid), line[:400], cdate, platform="hackernews")
                if domain:
                    got = await _root(ctx, domain, name)  # the brand is the company name on the first line
                    if not got:
                        continue
                    domain, li = got
                    out.append(sg.Candidate(list="affiliate", signal=signal, domain=domain, linkedin_url=li,
                                            require_us=True))
                elif len(common.norm(name)) >= 5 and US_MARK.search(line):
                    # a name-only row goes to the Resolver's name search, so its US location must be stated here
                    out.append(sg.Candidate(list="affiliate", signal=signal, company_name=name, require_us=True))
                else:
                    continue
                counts["rows"] += 1
            except ITEM_ERRORS:
                counts["errors"] += 1
    ctx.say(f"community: hiring threads {len(threads)} read, {counts['rows']} affiliate or referral roles, "
            f"{counts['errors']} malformed")
    return out


async def _google_posts(ctx, cfg: dict) -> list[sg.Candidate]:
    """Indie Hackers, Product Hunt and Shopify Community posts from the company's own side, found by Google."""
    out: list[sg.Candidate] = []
    counts = {"results": 0, "kept": 0, "errors": 0}
    for q in cfg["google_queries"][: int(cfg["max_google_queries"])]:
        if not ctx.budget.ok():
            break
        try:
            data = await sg.sc_get(ctx.sc, "/v1/google/search", {"query": q, "date_posted": window(ctx.days)}, NAME)
        except OutOfCredits:
            ctx.say("community: ScrapeCreators credits spent, forum and launch search stopped early")
            break
        for r in _results(data):
            counts["results"] += 1
            try:
                cand = await _google_candidate(ctx, r)
            except ITEM_ERRORS:
                counts["errors"] += 1
                continue
            if cand:
                counts["kept"] += 1
                out.append(cand)
    ctx.say(f"community: forum and launch search {counts['results']} results, {counts['kept']} with a verified domain, "
            f"{counts['errors']} malformed")
    return out


async def _google_candidate(ctx, r: dict) -> sg.Candidate | None:
    url = canonical(r.get("url") or "")
    if not url:
        return None
    parts = urlsplit(url)
    platform = PLATFORM_OF[detect.bare_host(parts.hostname or "")]
    if not POST_PATH[platform].search(parts.path) or LISTING.search(parts.path):
        return None
    title = _clean(r.get("title") or "")
    desc = _clean(r.get("description") or "")
    iso, end = lead_date(desc, ctx.now)
    if not common.within_days(iso, ctx.days, ctx.now):
        return None
    body = _scrub(desc[end:].lstrip(" .…-") if end else desc)
    text = f"{title}. {body}"
    if post_gate(text, ctx.signals_cfg, title=title):
        return None
    # the brand must be named in the title or the URL slug, never only in the body
    slug = " ".join(t for t in re.findall(r"[a-z0-9]+", parts.path.lower()) if t not in PATH_WORDS and not t.isdigit())
    post = f"{title} {slug}"
    if platform == "indiehackers":
        roots = common.domains_in_text(text)  # snippet only: Indie Hackers bodies are not fetched
    elif platform == "producthunt":
        page = await ctx.get(url, "page")
        roots = await _external_roots(ctx, url, page, visit_only=True) if page and page.status < 400 else []
    else:
        page = await ctx.get(url, "page")
        roots = await _external_roots(ctx, url, page, visit_only=False) if page and page.status < 400 else []
    for root in roots:
        got = await _root(ctx, root, post)
        if got:
            domain, li = got
            return sg.Candidate(list="affiliate", domain=domain, linkedin_url=li, require_us=True,
                                signal=sg.Signal("community_post", url, _quote(body), iso, platform=platform))
    return None


# the module contract ----------------------------------------------------------------------------------------------

async def collect(ctx) -> list[sg.Candidate]:
    """The three stages, each de-duplicated, then interleaved so one noisy stage cannot starve the others when the
    run is capped at max_candidates."""
    cfg = ctx.cfg
    keys: set[tuple[str, str]] = set()
    lanes: list[list[sg.Candidate]] = []
    for stage in (_hn_stories, _hn_hiring, _google_posts):
        try:
            got = await stage(ctx, cfg)
        except OutOfCredits:
            raise
        except Exception as exc:  # one stage failing must not drop the others
            ctx.say(f"community: {stage.__name__.lstrip('_')} failed ({type(exc).__name__})")
            continue
        lane = []
        for c in got:
            key = ((c.domain or common.norm(c.company_name or "")), c.signal.platform if c.signal else "")
            if key[0] and key not in keys:
                keys.add(key)
                lane.append(c)
        lanes.append(lane)
    out: list[sg.Candidate] = []
    for i in range(max((len(lane) for lane in lanes), default=0)):
        for lane in lanes:
            if i < len(lane):
                out.append(lane[i])
    return out[: int(cfg["max_candidates"])]


__all__ = ["NAME", "DEFAULTS", "collect", "canonical", "lead_date", "post_gate", "hiring_parts", "hiring_gate",
           "html_text", "window"]
