"""Shared helpers for program-lane sources: URL/host handling, brand-domain picking, name matching, dates, and the
deterministic buyer-intent gates. Pure functions (no I/O) unless the name says otherwise."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from .. import detect

# Hosts that are never a company's own domain: social networks, link-in-bio and shortlinks, hosted affiliate
# program portals, job boards / ATSs, marketplaces, and Google/Meta redirectors.
PLATFORM_HOSTS = {
    "facebook.com", "fb.me", "fb.com", "l.facebook.com", "instagram.com", "l.instagram.com", "threads.net",
    "threads.com", "tiktok.com", "youtube.com", "youtu.be", "x.com", "twitter.com", "t.co", "linkedin.com",
    "lnkd.in", "reddit.com", "redd.it", "pinterest.com", "snapchat.com", "whatsapp.com", "api.whatsapp.com",
    "wa.me", "t.me", "telegram.me", "discord.gg", "discord.com", "medium.com", "substack.com", "news.ycombinator.com",
    "indiehackers.com", "producthunt.com", "linktr.ee", "linkin.bio", "beacons.ai", "bio.link", "bio.site",
    "campsite.bio", "carrd.co", "msha.ke", "tap.bio", "solo.to", "hoo.be", "stan.store", "koji.to", "lnk.bio",
    "bit.ly", "bitly.com", "tinyurl.com", "goo.gl", "ow.ly", "buff.ly", "rebrand.ly", "cutt.ly", "shorturl.at",
    "docs.google.com", "forms.gle", "google.com", "sites.google.com", "typeform.com", "jotform.com", "calendly.com",
    "uppromote.com", "af.uppromote.com", "rekomi.com", "goaffpro.com", "refersion.com", "partnerstack.com",
    "impact.com", "tapfiliate.com", "firstpromoter.com", "getrewardful.com", "shareasale.com", "awin.com",
    "cj.com", "referpro.com", "socialsnowball.io", "linkmink.com", "tolt.io",
    "indeed.com", "glassdoor.com", "ziprecruiter.com", "lever.co", "greenhouse.io", "ashbyhq.com", "workable.com",
    "builtin.com", "wellfound.com", "upwork.com", "fiverr.com", "freelancer.com", "onlinejobs.ph",
    "amazon.com", "etsy.com", "ebay.com", "shopify.com", "myshopify.com", "apps.apple.com", "play.google.com",
    "wix.com", "wixsite.com", "squarespace.com", "wordpress.com", "blogspot.com", "godaddysites.com",
}
TWO_LEVEL_TLDS = {"co.uk", "com.au", "co.nz", "com.br", "co.za", "com.mx", "co.in", "co.jp", "com.sg"}
CORP_WORDS = {"inc", "llc", "ltd", "co", "corp", "corporation", "company", "the", "group", "hq", "official", "us",
              "usa", "holdings", "brand", "brands", "store", "shop", "app", "io", "ai", "com"}


def unwrap(url: str) -> str:
    """Strip redirect wrappers (l.facebook.com/l.php?u=, google /url?q=) and tracking params."""
    url = (url or "").strip()
    for _ in range(3):
        parts = urlsplit(url)
        q = parse_qs(parts.query)
        if parts.hostname and parts.hostname.endswith(("facebook.com", "instagram.com")) and parts.path.startswith("/l"):
            url = unquote((q.get("u") or [""])[0]) or url
        elif parts.hostname and parts.hostname.endswith("google.com") and parts.path == "/url":
            url = unquote((q.get("q") or q.get("url") or [""])[0]) or url
        else:
            break
    parts = urlsplit(url)
    keep = "&".join(p for p in parts.query.split("&") if p and not re.match(r"(utm_|fbclid|gclid|ref=|igsh|si=)", p))
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, keep, ""))


def registrable(host: str) -> str:
    """'shop.acme.co.uk' -> 'acme.co.uk', 'get.dripcapital.com' -> 'dripcapital.com'."""
    host = detect.bare_host(host or "")
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LEVEL_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def is_platform_host(host: str) -> bool:
    host = detect.bare_host(host or "")
    return not host or host in PLATFORM_HOSTS or registrable(host) in PLATFORM_HOSTS


def host_of(url: str) -> str | None:
    """Registrable domain of a URL, or None when it is a platform host."""
    try:
        host = urlsplit(unwrap(url) if "//" in (url or "") else "https://" + (url or "")).hostname or ""
    except ValueError:
        return None
    root = registrable(host)
    return None if is_platform_host(host) or "." not in root else root


def brand_root(urls: list[str], min_share: float = 0.34) -> str | None:
    """The brand's own domain from the links a page/account uses: the most common non-platform registrable domain,
    accepted only when it holds at least `min_share` of those links (a page that sends traffic to many unrelated
    hosts is not anchored to one brand)."""
    hosts = [h for h in (host_of(u) for u in urls if u) if h]
    if not hosts:
        return None
    root, n = Counter(hosts).most_common(1)[0]
    return root if n / len(hosts) >= min_share else None


DOMAIN_IN_TEXT = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|co|io|ai|app|net|org|us|shop|store|biz|finance|capital|"
                            r"health|fit|beauty|so|dev|tech|xyz|me))\b", re.I)


def domains_in_text(text: str) -> list[str]:
    """Bare domains mentioned in free text ('try acme.com'), platform hosts removed, in order, deduped."""
    out = []
    for m in DOMAIN_IN_TEXT.finditer(text or ""):
        root = host_of(m.group(1).lower())
        if root and root not in out:
            out.append(root)
    return out


def tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in CORP_WORDS]


def norm(name: str) -> str:
    return "".join(tokens(name))


def names_match(a: str, b: str) -> bool:
    """Same company name: equal after normalising (case, punctuation, Inc/LLC/Co...), or two or more shared
    tokens. One shared common word is not enough (the cause of wrong-company matches)."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shared = set(tokens(a)) & set(tokens(b))
    return len(shared) >= 2


def page_names_brand(html: str, brand: str) -> bool:
    """True when a homepage's <title> or og:site_name contains the brand (normalised), e.g. to accept a guessed
    or linked domain for a brand name."""
    want = norm(brand)
    if not want or not html:
        return False
    found = re.findall(r"<title[^>]*>(.*?)</title>|property=[\"']og:site_name[\"'][^>]*content=[\"']([^\"']+)",
                       html[:200_000], re.I | re.S)
    hay = " ".join(a or b for a, b in found)
    return want in norm(hay) or names_match(brand, hay.split("|")[0].split(" - ")[0])


PARKED = re.compile(r"\b(?:domain (?:is )?for sale|buy this domain|parked (?:free|domain)|sedoparking|"
                    r"hugedomains|this domain may be for sale)\b", re.I)


def parked(html: str) -> bool:
    head = re.findall(r"<title[^>]*>(.*?)</title>|name=[\"']description[\"'][^>]*content=[\"']([^\"']+)",
                      (html or "")[:100_000], re.I | re.S)
    return bool(PARKED.search(" ".join(a or b for a, b in head)))


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], 1)}


def relative_date(text: str, now: datetime | None = None) -> str | None:
    """ISO date from 'N days ago', 'N hours ago', 'yesterday', 'today', 'a week ago', 'N weeks ago', 'Sep 21, 2026',
    '2026-09-21' or an epoch-seconds number; None when there is no date. 'One Month Ago'-style coarse ages return
    None (unknown age is not recent)."""
    now = now or datetime.now(timezone.utc)
    t = (text or "").strip().lower()
    if not t:
        return None
    if re.fullmatch(r"\d{9,11}", t):
        return datetime.fromtimestamp(int(t), tz=timezone.utc).date().isoformat()
    m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)(?!\d)", t)
    if m:
        return m.group(0)
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2}),? (20\d\d)\b", t)
    if m:
        return f"{m.group(3)}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    if re.search(r"\b(?:just now|today|\d+\s*(?:minutes?|mins?|hours?|hrs?|h)\s+ago)\b", t):
        return now.date().isoformat()
    if "yesterday" in t:
        return (now - timedelta(days=1)).date().isoformat()
    m = re.search(r"\b(\d+|a|an|one)\s*(day|week)s?\s+ago\b", t)
    if m:
        n = 1 if m.group(1) in ("a", "an", "one") else int(m.group(1))
        return (now - timedelta(days=n * (7 if m.group(2) == "week" else 1))).date().isoformat()
    return None


def within_days(iso: str | None, days: int, now: datetime | None = None) -> bool:
    if not iso:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        d = datetime.fromisoformat(iso[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return timedelta(0) <= now - d <= timedelta(days=days) or d.date() == now.date()


# Deterministic buyer-intent gates (from the 2026-10-10 platform sweep). A post passes when it is about the
# author's OWN program and none of the drop gates fire.
PROGRAM_NOUN = r"(?:affiliate|referral|partner|ambassador|creator|reseller|iso|broker)s?(?: (?:partner )?program(?:me)?)?"
BUYER_VERB = (r"(?:launch(?:ed|ing)?|recruit(?:ing)?|join|now accepting|accepting (?:new )?|looking for|seeking|"
              r"hiring|apply|become an?|grow(?:ing)?|scale|scaling|activate|wanted|open(?:ing)? (?:up|applications))")
BUYER = re.compile(rf"\b{BUYER_VERB}\b[^.!?\n]{{0,60}}\b{PROGRAM_NOUN}\b|\b{PROGRAM_NOUN}\b[^.!?\n]{{0,40}}\b"
                   rf"(?:{BUYER_VERB}|is (?:live|open)|now open)\b|\bour (?:affiliate|referral|partner|ambassador|iso|"
                   rf"broker) (?:program|programme|network)\b", re.I)
GATES = {
    "mlm": r"\b(?:join my team|downline|network marketing|make money (?:online|from home)|side hustle|passive income|"
           r"work from (?:home|your phone)|financial freedom|be your own boss|dm me ['\"]?(?:info|interested))\b",
    "individual_affiliate": r"\b(?:my (?:affiliate |referral )?(?:link|code)|use (?:my )?code|link in (?:my )?bio to "
                            r"(?:shop|buy)|commission(?:s)? (?:on|from) (?:my|these) links?|i earn (?:a )?commission)\b",
    "seeker": r"\b(?:which|what|best|good|any) (?:affiliate )?programs? (?:to join|should i join|for beginners|that "
              r"pay)|\blooking (?:to join|for an?) (?:affiliate|referral) program(?:s)? to (?:join|promote)\b",
    "seller": r"\b(?:we help (?:brands|companies|businesses|clients)|i help (?:brands|companies|businesses)|done[- ]for"
              r"[- ]you|(?:affiliate|partner) (?:marketing )?agency|launch your (?:own )?(?:affiliate|referral) "
              r"program|affiliate (?:software|course|training|playbook)|book a (?:free )?(?:strategy|discovery) call)\b",
    "recruiter": r"\b(?:recruiting (?:candidates|for (?:a |our )?clients?)|staffing agency|we place|my client is "
                 r"(?:hiring|looking)|on behalf of (?:a|my|our) client)\b",
    "vertical": r"\b(?:casinos?|gambling|igaming|sportsbooks?|betting|poker|adult|onlyfans|forex|cfds?|"
                r"binary options|crypto(?:currency)?|web3|defi|memecoins?|prop (?:firm|trading)|payday|"
                r"research use only|not for human consumption|research peptides?)\b",
}
GATES_RX = {k: re.compile(v, re.I) for k, v in GATES.items()}


def text_gate(text: str) -> str | None:
    """Why a post is not a buyer talking about its own program, or None."""
    for reason, rx in GATES_RX.items():
        if rx.search(text or ""):
            return reason
    return None


def buyer_language(text: str) -> bool:
    """A program noun next to a buyer verb ('now accepting new ISOs', 'join our affiliate program', 'we launched our
    referral program'). A bare 'join' or 'affiliate' does not pass."""
    return bool(BUYER.search(text or ""))


def quote_around(text: str, rx: re.Pattern | None = None, width: int = 400) -> str:
    """A verbatim excerpt (whitespace collapsed) centred on the first match of rx, else the start."""
    text = re.sub(r"\s+", " ", text or "").strip()
    m = rx.search(text) if rx else None
    if not m:
        return text[:width]
    start = max(0, m.start() - width // 3)
    return text[start:start + width].strip()


__all__ = ["PLATFORM_HOSTS", "unwrap", "registrable", "is_platform_host", "host_of", "brand_root", "domains_in_text",
           "tokens", "norm", "names_match", "page_names_brand", "parked", "relative_date", "within_days",
           "text_gate", "buyer_language", "quote_around"]
