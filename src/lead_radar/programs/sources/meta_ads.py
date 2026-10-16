"""Meta Ad Library: brands paying for Facebook/Instagram/Messenger/WhatsApp/Threads ads that recruit affiliates,
ambassadors, partners, creators or ISOs/brokers for their own program.

Flow: one ScrapeCreators keyword search per phrase (1 credit each; US delivery, ACTIVE, most recent first). An ad is
kept only when it is active, started inside the window, is not on a person/profile or non-commercial page, reads as a
buyer (recruiting copy, a hosted program portal, or a program landing path with program wording in the copy) and passes
the seller, vertical and MLM gates. Kept ads are grouped by page. The brand domain comes from the page's own landing
links (majority non-platform host), else a domain-like caption, and is accepted only when it shares a name token with
the page; when the search rows give none, one company-ads pull (1 credit, capped per run) adds the page's other links.
One Candidate per brand; the Signal (URL, date and quote) comes from the newest ad that has recruiting copy. Before a
brand is listed, its own landing page is read once (polite ctx.get) and the brand is dropped only when that page reads
as a restricted vertical. growth.py resolves the LinkedIn page and applies the US gate (require_us).
Person names and profile fields are never stored, printed or logged.
"""
from __future__ import annotations

import html as htmllib
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .. import signals as sg
from . import common as c

NAME = "meta_ads"
SEARCH_PATH = "/v1/facebook/adLibrary/search/ads"
COMPANY_ADS_PATH = "/v1/facebook/adLibrary/company/ads"
AD_URL = "https://www.facebook.com/ads/library/?id={}"
MAX_WINDOW = 60                       # days: the ad window never exceeds this, whatever the run's look-back

# (query, exact phrase?). Affiliate, ambassador, partner, creator recruiting first, then ISO/broker/referral funding.
QUERIES = [
    ("join our affiliate program", True),
    ("become an affiliate", True),
    ("affiliate program commission", False),
    ("become an ambassador", True),
    ("ambassador program", True),
    ("partner program apply", False),
    ("earn commission affiliate", False),
    ("creator program", True),
    ("become an ISO", True),
    ("ISO partners", True),
    ("broker partners funding", False),
    ("referral partner program", True),
    ("business funding", True),
]

DEFAULTS = {
    "enabled": True,
    "credits": 30,
    "queries": [list(q) for q in QUERIES],
    "company_ads_max": 8,             # company-ads pulls per run, for pages whose search rows give no brand domain
}

PROFILE_KEYS = ("page_profile_uri", "page_profile_picture_url", "page_profile_picture")
PERSON_CATEGORIES = {"digital creator", "public figure", "personal blog", "entrepreneur", "profile", "speaker",
                     "podcast", "musician", "artist", "coach", "video creator", "gamer"}
NON_COMMERCIAL_CATEGORIES = {"nonprofit organization", "non-profit organization", "charity organization",
                             "community organization", "education website", "school", "university", "college",
                             "political organization", "government organization"}
PORTALS = {"uppromote.com": "UpPromote", "rekomi.com": "Rekomi", "goaffpro.com": "GoAffPro",
           "partnerstack.com": "PartnerStack", "refersion.com": "Refersion", "impact.com": "Impact",
           "tapfiliate.com": "Tapfiliate", "firstpromoter.com": "FirstPromoter",
           "socialsnowball.io": "Social Snowball", "referpro.com": "ReferPro"}
# Program landing paths. A path alone is not a buyer signal (a product or careers page can sit under /partner or
# /creators); it counts only with program wording in the copy (PROGRAM_COPY or an ISO term). /apply is not a program
# path at all: loan applications land there.
BUYER_PATH = re.compile(r"/(?:affiliates?|partners?|ambassadors?|creators?|collab|join|isos?(?=[/?#._]|$)|"
                        r"iso-(?:partners?|program|resources|network|brokers?)|refer(?!-a-friend))", re.I)
PROGRAM_COPY = re.compile(r"\b(?:affiliates?|ambassadors?|partners?|partnership|referrals?|refer|commissions?|"
                          r"creators?|brokers?)\b", re.I)
ISO_CAPS = re.compile(r"\bISOs?\b")                  # case-sensitive: bare 'iso' hits 'ISO 9001' copy
CONSUMER_REFER = re.compile(r"\brefer (?:a |your |an )?(?:friends?|family|neighbou?rs?)\b|\binvite (?:your )?friends\b|"
                            r"\bgive \$\d+,? get \$\d+|\bget \$\d+ (?:for each|per) (?:friend|referral)\b|"
                            r"\$\d+ (?:gift card|visa)\b", re.I)
BUSINESS_WORD = re.compile(r"\b(?:affiliates?|ambassadors?|partners?|partnership|isos?|brokers?|resellers?|dealers?)\b",
                           re.I)
SELLER_COPY = re.compile(
    r"launch your (?:own )?(?:affiliate|referral|partner|ambassador)s? program|"
    r"affiliate (?:software|course|training|playbook|platform|mastery|academy|coaching)|"
    r"(?:online|video|paid|our|free) (?:course|training|webinar|masterclass|coaching)|\bmasterclass\b|"
    r"\bdone[- ]for[- ]you\b|affiliate income|tiktok shop (?:training|agency|course|coaching|affiliate income)|"
    r"(?:affiliate|marketing|influencer|tiktok shop|performance) agency|"
    r"we (?:manage|run|build) (?:your|affiliate|partner|referral)|"
    r"book a (?:free )?(?:strategy|discovery|demo) call|\bplaybook\b|"
    r"\b(?:creator|influencer) (?:discovery|outreach|management)\b|\bautomate (?:your )?outreach\b|\bGMV\b|"
    r"\b\d+x (?:your )?(?:ad )?spend\b", re.I)
MLM_COPY = re.compile(r"\b(?:join my team|sell ads|passive income|financial freedom|income opportunity|"
                      r"be your own boss|downline|network marketing|make money (?:online|from home))\b", re.I)
VERTICAL_COPY = re.compile(r"\bfunded trader|challenge account\b", re.I)
# Restricted verticals on the brand's own landing page (read once per brand; the ad copy is not used for this).
VERTICAL_PAGE = re.compile(r"research use only|not for human consumption|research (?:peptides?|chemicals?)|"
                           r"\bcasinos?\b|sportsbooks?|igaming|\bgambling\b|\bforex\b|binary options", re.I)
FIN_TERM = re.compile(r"merchant cash advance|\bMCAs?\b|business funding|working capital|\bSBA\b|equipment financing|"
                      r"revenue[- ]based|business loans?|business financing|small business loans?|\blenders?\b|"
                      r"funded deals?|alternative (?:and traditional )?funding|funding partners?", re.I)
ISO_WORDS = re.compile(r"independent sales organi[sz]ation|\bbrokers?\b|referral partners?|channel partners?|"
                       r"funding partners?|\brefer(?:ring)? (?:them|clients?|a client|business owners?)\b", re.I)
PROGRAM_FIN = re.compile(r"\b(?:affiliates?|referral partners?|partner program|ambassadors?)\b", re.I)
QUOTE_CENTER = re.compile(r"affiliate|ambassador|partner|creator|\bISOs?\b|brokers?|\brefer", re.I)
NAME_NOISE = {"the", "and", "of", "for", "inc", "llc", "co", "com"}


# pure helpers --------------------------------------------------------------------------------------------------------

def _snap(row: dict) -> dict:
    snap = row.get("snapshot")
    return snap if isinstance(snap, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _strs(value) -> list[str]:
    return [x.strip() for x in _list(value) if isinstance(x, str) and x.strip()]


def _id(value) -> str:
    """A ScrapeCreators id as text; '' for anything that is not a plain string or number."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def _raw_rows(data, key: str) -> list:
    """The list under `key` of a ScrapeCreators response; anything else (error, list body, missing) is empty."""
    return _list(data.get(key)) if isinstance(data, dict) else []


def _true(value) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def scrub(row: dict) -> dict:
    """Drop personal-profile fields from a raw ad row before anything else sees it."""
    for key in PROFILE_KEYS:
        row.pop(key, None)
    snap = _snap(row)
    for key in PROFILE_KEYS:
        snap.pop(key, None)
    return row


def start_iso(row: dict) -> str | None:
    """Ad start date (ISO, UTC) from the epoch start_date (seconds, or milliseconds), else start_date_string; None when
    undated, out of range, or not a real date."""
    value = row.get("start_date")
    epoch = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        epoch = value
    elif isinstance(value, str) and value.strip().isdigit():
        epoch = value.strip()
    if epoch is not None:
        try:
            seconds = float(epoch)
            if not math.isfinite(seconds):
                return None
            if seconds > 1e11:                       # epoch milliseconds
                seconds /= 1000
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = row.get("start_date_string")
    if isinstance(text, str) and re.match(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return datetime.fromisoformat(text[:10]).date().isoformat()
        except ValueError:
            return None
    return None


def page_name(row: dict) -> str:
    value = row.get("page_name") or _snap(row).get("page_name")
    return value.strip() if isinstance(value, str) else ""


def _unwrapped(url: str) -> str:
    """Canonical form of an ad link, or '' when it cannot be parsed."""
    try:
        return c.unwrap(url)
    except ValueError:
        return ""


def landing(row: dict) -> str:
    link = _snap(row).get("link_url")
    return _unwrapped(link.strip()) if isinstance(link, str) and link.strip() else ""


def link_urls(row: dict) -> list[str]:
    snap = _snap(row)
    out = []
    for url in [snap.get("link_url"), *_list(snap.get("extra_links"))]:
        if isinstance(url, str) and url.strip():
            canon = _unwrapped(url.strip())
            if canon:
                out.append(canon)
    return out


def _fields(row: dict) -> list[str]:
    snap = _snap(row)
    body = snap.get("body")
    body = body.get("text") if isinstance(body, dict) else body
    return [x.strip() for x in (snap.get("link_description"), snap.get("title"), body)
            if isinstance(x, str) and x.strip()]


def ad_text(row: dict) -> str:
    caption = _snap(row).get("caption")
    parts = _fields(row) + ([caption] if isinstance(caption, str) else [])
    return " ".join(parts)


def categories(row: dict) -> list[str]:
    return _strs(_snap(row).get("page_categories"))


def excerpt(text: str, rx: re.Pattern | None, width: int = 400) -> str:
    """Verbatim excerpt (whitespace collapsed, <= width chars) centred on the first match, cut at word boundaries."""
    text = " ".join(text.split())
    m = rx.search(text) if rx else None
    start = max(0, m.start() - width // 3) if m else 0
    if start > 0 and not text[start - 1].isspace():
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < (m.start() if m else len(text)) else start
    end = min(len(text), start + width)
    if end < len(text) and not text[end].isspace():
        space = text.rfind(" ", start, end)
        end = space if space > start else end
    return text[start:end].strip()


def quote_for(row: dict) -> str:
    """Verbatim excerpt (<= 400 chars) from link description, title or body, centred on the recruiting wording."""
    fields = _fields(row)
    for text in fields:
        if QUOTE_CENTER.search(text):
            return excerpt(text, QUOTE_CENTER)
    return excerpt(fields[0], None) if fields else ""


def signal_row(keep: list[dict]) -> dict | None:
    """The newest kept ad whose copy carries recruiting wording, else the newest kept ad with any copy. The Signal's
    URL, date and quote all come from this one ad."""
    texted = [r for r in keep if _fields(r)]
    if not texted:
        return None
    return next((r for r in texted if any(QUOTE_CENTER.search(t) for t in _fields(r))), texted[0])


def portal_of(url: str) -> str | None:
    """Hosted program portal (UpPromote, Rekomi, ...) behind a landing URL, else None."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return None
    return PORTALS.get(c.registrable(host))


def name_tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(t) >= 3 and t not in NAME_NOISE]


def _label(root: str) -> str:
    return re.sub(r"[^a-z0-9]", "", root.split(".")[0].lower())


def root_matches(root: str, name: str) -> bool:
    """The brand domain must carry a name token of its page ('sportsgearswag.com' for 'Sports Gear Swag')."""
    label, toks = _label(root), name_tokens(name)
    if not label or not toks:
        return False
    joined = "".join(re.findall(r"[a-z0-9]+", (name or "").lower()))
    return any(t in label for t in toks) or (len(label) >= 4 and label in joined)


COMPANY_WORDS = {"labs", "lab", "capital", "funding", "fund", "funds", "financial", "group", "golf", "beauty", "apparel",
                 "clothing", "supply", "supplies", "wear", "gear", "goods", "outfitters", "health", "wellness",
                 "telehealth", "media", "studio", "fitness", "co", "company", "brand", "shop", "store", "home", "design",
                 "wheels", "motors", "foods", "coffee", "games", "software", "tech", "technologies", "solutions",
                 "systems", "services", "partners", "network", "ventures", "holdings", "realty", "insurance", "medical",
                 "clinic", "pharma", "sports", "kids", "baby", "pet", "pets", "boutique", "collective", "club", "global",
                 "digital", "marketing", "central", "looks", "cosmetics", "skin", "nutrition", "supplements", "bank",
                 "credit", "loans", "lending", "payments", "trading", "outdoor", "outdoors", "wine", "water", "energy"}


def looks_personal(name: str) -> bool:
    """Two or three title-cased words with no company word ('Pat Example', 'Mary Ann Smith'). Dropped, even when a
    domain matches: a personal site named after the person is still a person's page."""
    words = (name or "").split()
    return (len(words) in (2, 3) and all(w.isalpha() and w[0].isupper() and w[1:].islower() for w in words)
            and not any(w.lower() in COMPANY_WORDS for w in words))


def brand_root_for(urls: list[str], captions: list[str], name: str) -> str | None:
    """Brand domain: majority non-platform host of the page's links, else a domain named in a caption; None when it
    does not carry the page's name (never guessed)."""
    root = c.brand_root(urls)
    if root is None:
        for caption in captions:
            found = c.domains_in_text(caption)
            if found:
                root = found[0]
                break
    return root if root and root_matches(root, name) else None


def buyer_signal(row: dict) -> bool:
    """A hosted program portal, recruiting copy, or a program landing path with program wording in the copy."""
    url = landing(row)
    try:
        path = urlsplit(url).path if url else ""
    except ValueError:
        path = ""
    copy = ad_text(row)
    if portal_of(url) or c.buyer_language(copy):
        return True
    return bool(BUYER_PATH.search(path) and (ISO_CAPS.search(copy) or PROGRAM_COPY.search(copy)))


def row_gate(row: dict, window: int, now: datetime, rx: dict) -> str | None:
    """Why an ad is not a buyer's recruiting ad, or None when it is."""
    if not _true(row.get("is_active")):
        return "inactive ad"
    if _true(row.get("page_is_deleted")) or _true(_snap(row).get("page_is_deleted")):
        return "page deleted"
    start = start_iso(row)
    if not start or not c.within_days(start, window, now):
        return "outside window"
    cats = [x.lower() for x in categories(row)]
    if any(cat in PERSON_CATEGORIES for cat in cats):
        return "person page"
    if any(cat in NON_COMMERCIAL_CATEGORIES for cat in cats):
        return "non-commercial page"
    text = ad_text(row)
    if CONSUMER_REFER.search(text) and not BUSINESS_WORD.search(text):
        return "consumer refer-a-friend"
    why = c.text_gate(text)
    if why:
        return why
    if SELLER_COPY.search(text) or rx["sellers"].search(text):
        return "seller"
    if MLM_COPY.search(text):
        return "mlm"
    if VERTICAL_COPY.search(text):
        return "vertical"
    if not buyer_signal(row):
        return "no buyer language"
    return None


def iso_wording(copy: str) -> bool:
    return bool(ISO_CAPS.search(copy) or ISO_WORDS.search(copy))


def page_text(html: str) -> str:
    """Visible text of a landing page: script, style and noscript blocks removed, tags stripped, entities decoded."""
    body = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", html[:200_000])
    return htmllib.unescape(" ".join(re.sub(r"<[^>]+>", " ", body).split()))


def landing_target(row: dict, root: str) -> str:
    """The landing page to read: the ad's own landing when it is on the brand domain, else the brand homepage."""
    url = landing(row)
    return url if url and c.host_of(url) == root else f"https://{root}/"


# the collector ---------------------------------------------------------------------------------------------------------

async def _restricted_vertical(ctx, target: str) -> bool:
    """True when the brand's landing page reads as a restricted vertical. A failed or refused fetch is not evidence, so
    it returns False and the brand stays on the strength of its ad copy."""
    page = await ctx.get(target)
    html = getattr(page, "html", None) if page is not None else None
    return bool(isinstance(html, str) and html and VERTICAL_PAGE.search(page_text(html)))


async def _company_links(ctx, page_id: str) -> list[str]:
    """Links of a page's other ads (company-ads endpoint, 1 credit): only used to find the brand domain."""
    if not ctx.budget.ok():
        return []
    data = await sg.sc_get(ctx.sc, COMPANY_ADS_PATH,
                           {"pageId": page_id, "status": "ACTIVE", "sort_by": "relevancy_monthly_grouped"}, NAME)
    out: list[str] = []
    for row in _raw_rows(data, "results"):
        if isinstance(row, dict):
            out += link_urls(scrub(row))
    return out


async def collect(ctx) -> list[sg.Candidate]:
    cfg = ctx.cfg or {}
    rx = ctx.signals_cfg["_rx"]
    window = min(int(ctx.days or MAX_WINDOW), MAX_WINDOW)
    now = ctx.now
    stats: Counter = Counter()

    seen: dict[str, dict] = {}
    searches = 0
    for query, exact in cfg.get("queries") or QUERIES:
        if not ctx.budget.ok():
            stats["budget stop"] += 1
            break
        searches += 1
        data = await sg.sc_get(ctx.sc, SEARCH_PATH, {
            "query": query, "country": "US", "status": "ACTIVE", "sort_by": "relevancy_monthly_grouped",
            "search_type": "keyword_exact_phrase" if exact else "keyword_unordered"}, NAME)
        for row in _raw_rows(data, "searchResults"):
            if not isinstance(row, dict):
                stats["malformed row"] += 1
                continue
            ad_id = _id(row.get("ad_archive_id"))
            if not re.fullmatch(r"\d+", ad_id):
                stats["bad ad id"] += 1
                continue
            if ad_id not in seen:
                row["ad_archive_id"] = ad_id
                seen[ad_id] = scrub(row)

    by_page: dict[str, list[dict]] = defaultdict(list)
    for row in seen.values():
        by_page[_id(row.get("page_id"))].append(row)

    kept: dict[str, list[dict]] = {}
    for pid, rows in by_page.items():
        if not pid:
            stats["no page id"] += len(rows)
            continue
        for row in rows:
            try:
                why = row_gate(row, window, now, rx)
            except (TypeError, ValueError, AttributeError):
                why = "malformed row"
            if why:
                stats[why] += 1
            else:
                kept.setdefault(pid, []).append(row)

    order = sorted(kept, key=lambda p: max(start_iso(r) or "" for r in kept[p]), reverse=True)
    cands: list[sg.Candidate] = []
    roots: set[str] = set()
    company_calls = landing_reads = 0
    company_max = int(cfg.get("company_ads_max", 0))
    for pid in order:
        keep = sorted(kept[pid], key=lambda r: start_iso(r) or "", reverse=True)
        name = page_name(keep[0])
        if looks_personal(name):
            stats["person-like page name"] += 1
            continue
        if rx["vendor_names"].search(name.lower()) or rx["sellers"].search(name):
            stats["vendor or seller name"] += 1
            continue
        page_rows = by_page[pid]
        captions = [str(_snap(r).get("caption") or "") for r in page_rows]
        urls = [u for r in page_rows for u in link_urls(r)]
        root = brand_root_for(urls, captions, name)
        if root is None and company_calls < company_max and ctx.budget.ok():
            company_calls += 1
            urls += await _company_links(ctx, pid)
            root = brand_root_for(urls, captions, name)
        if root is None:
            stats["no brand domain"] += 1
            continue
        if root in roots:
            stats["same brand as another page"] += 1
            continue
        copy = " ".join(ad_text(r) for r in keep)
        cats = " ".join(" ".join(categories(r)) for r in keep)
        is_finance = bool(FIN_TERM.search(f"{copy} {name} {cats}")) and (
            iso_wording(copy) or bool(PROGRAM_FIN.search(copy)))
        if is_finance and rx["finance_vendors"].search(copy):
            stats["finance vendor"] += 1
            continue
        row = signal_row(keep)
        if row is None:
            stats["no ad copy"] += 1
            continue
        landing_reads += 1
        if await _restricted_vertical(ctx, landing_target(row, root)):
            stats["restricted vertical landing page"] += 1
            roots.add(root)
            continue
        signal = sg.Signal("paid_ad", AD_URL.format(row["ad_archive_id"]), quote_for(row), start_iso(row),
                           platform="meta_ads")
        cands.append(sg.Candidate("finance" if is_finance else "affiliate", signal, company_name=name,
                                  domain=root, require_us=True))
        roots.add(root)

    ctx.say(f"meta_ads: {searches} searches, {len(seen)} ads, {len(by_page)} pages, {len(cands)} brands, "
            f"{company_calls} company-ads pulls, {landing_reads} landing pages checked; dropped: "
            + (", ".join(f"{k} {v}" for k, v in stats.most_common()) or "none"))
    return cands
