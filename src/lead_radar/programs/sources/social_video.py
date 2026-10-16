"""Brand-owned TikTok and YouTube videos that announce or recruit for the brand's own affiliate, ambassador, creator
or partner program (list "affiliate", platforms tiktok and youtube).

Strict on purpose: the 2026-10-10 probe measured low precision here (individual creators, MLM, TikTok Shop noise,
ambassador perks, listicles and course sellers). A video is kept only when

1. its caption or description is brand-voiced buyer language about the brand's own program, with no seller, MLM,
   individual-affiliate, listicle or guide, course or tutorial, platform (TikTok Shop), non-business or perks-only
   ambassador rule firing (text_drop);
2. the account is a brand account by profile: TikTok needs an organisation flag, or a commerce or TikTok Shop seller
   flag whose bio domain carries the account's name; YouTube needs a channel whose country is the United States
   (1 credit per account or channel);
3. the caption or title names the account's brand (named_in), and the account's own link resolves to a non-aggregator
   site whose homepage names the account and either links back to it or shows a program page that names the brand
   (verify_site). A homepage that links a different account on the same platform is rejected. An unresolved account
   is dropped, never guessed;
4. no caption or description link carries an affiliate tracking tag to another company (a third-party promo).

Legs: YouTube runs first (it produced every candidate in the live probe), then TikTok under its own sub-budget, so
a TikTok spend cannot starve YouTube. One candidate per company (its newest passing video), require_us=True:
growth.process lists it only when the LinkedIn company page is US. Shorts are skipped: they carry no channel data.
"""
from __future__ import annotations

import html as htmllib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from .. import detect, signals as sg
from . import common as c

NAME = "social_video"
DEFAULTS = {
    "enabled": False,               # recommended off until measured (config sources.social_video overrides)
    "credits": 15,                  # per-run allowance (config sources.social_video overrides)
    "http_cap": 60,                 # free homepage / program-page GETs per run
    "tiktok_queries": ["join our affiliate program", "now accepting affiliates", "ambassador program apply"],
    "youtube_queries": ["join our affiliate program", "introducing our affiliate program", "partner program apply now"],
    "tiktok_credits": 6,            # the TikTok leg's share of the run: 3 searches + 3 profile calls
    "max_profiles": 8,              # TikTok profile calls per run (only for videos that pass the text gates)
    "max_channels": 5,              # YouTube channel calls per run (same rule)
    "max_domains_per_account": 2,   # account links tried before giving up on an account
}

MIN_NAME = 5                        # normalised characters a brand name needs before it can anchor anything
TT_URL = re.compile(r"tiktok\.com/@([A-Za-z0-9._]{2,40})/video/(\d{10,})", re.I)
YT_HANDLE_URL = re.compile(r"youtube\.com/(@[A-Za-z0-9._\-]{2,60})", re.I)
YT_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
YT_ACCOUNT = re.compile(r"youtube\.com/(channel/UC[\w-]{10,}|@[\w.\-]{2,60})", re.I)
TT_ACCOUNT = re.compile(r"tiktok\.com/@([\w.]{2,40})", re.I)
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
URL_IN_TEXT = re.compile(r"https?://[^\s)\"'<>]+", re.I)
AFF_TAG = re.compile(r"[?&](?:ref|via|aff|affid|affiliate|affiliate_id|fpr|rfsn|irclickid|a_aid|partner_id|pid)=", re.I)
US_NAMES = {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}

# Site builders and hosting platforms: a subdomain here belongs to whoever built it, so it never anchors a company
# (matched on the raw host, before registrable-domain reduction collapses trainersbox.webnode.page to webnode.page).
HOSTING = {
    "webnode.page", "webnode.com", "webnode.cz", "weebly.com", "wixsite.com", "squarespace.com", "carrd.co",
    "notion.site", "framer.website", "framer.app", "vercel.app", "netlify.app", "github.io", "pages.dev",
    "herokuapp.com", "glitch.me", "replit.app", "lovable.app", "webflow.io", "tilda.ws", "bubbleapps.io",
    "myshopify.com", "shopify.com", "godaddysites.com", "blogspot.com", "wordpress.com", "sites.google.com",
}

# Link-in-bio, shop and form hosts that are never a brand's own site (a homepage GET on them proves nothing).
AGGREGATORS = {
    "campsite.to", "campsite.bio", "likeshop.me", "shopmy.us", "shopmy.com", "snif.life", "lnk.bio", "linkme.bio",
    "linkbio.co", "komi.io", "pillar.io", "linktr.ee", "linktree.com", "beacons.ai", "bio.link", "bio.site", "stan.store",
    "koji.to", "msha.ke", "tap.bio", "solo.to", "hoo.be", "carrd.co", "linkin.bio", "forms.gle", "docs.google.com",
    "google.com", "gumroad.com", "payhip.com", "lovable.app", "rebrand.ly", "bit.ly", "tinyurl.com", "goo.gl", "t.co",
}

# Text rules on top of common.text_gate and common.buyer_language.
BRAND_VOICE = re.compile(r"\b(?:we|we're|we've|we'll|our|ours|us)\b", re.I)
FIRST_PERSON = re.compile(r"\b(?:i|i'm|i've|i'll|i'd|me|my|mine)\b", re.I)
MLM = re.compile(r"\bmake money\b|\bextra income\b|\bfree products\b|\bwork from (?:anywhere|home|your phone)\b|"
                 r"\bhigh[- ]ticket\b|\bonline business\b|\bdm (?:me|us) (?:for|to|if)\b|\bpassive income\b|"
                 r"\bside hustle\b|\bjoin my team\b|\bdownline\b|\bcomment ['\"]?\w+['\"]? (?:for|to)\b", re.I)
SELLER = re.compile(r"\b(?:affiliate|referral|partner) (?:software|platform|saas|network|tracking|management)\b|"
                    r"\bwe(?:'re| are) (?:an? |the |your )?(?:\w+ )?agency\b|\bour agency\b|\bdone[- ]for[- ]you\b|"
                    r"\bwe (?:build|set up|manage|run|launch) your\b", re.I)
GUIDE = re.compile(r"\b(?:top|best) \d+\b|\b(?:top|best) (?:\w+ )?(?:affiliate |referral )?(?:programs|networks|platforms)\b|"
                   r"\baffiliate programs\b|\bwe (?:ranked|reviewed|tested|compared|rated)\b|\bfor beginners\b", re.I)
COURSE = re.compile(r"\baffiliate marketing\b|\bmasterclass\b|\bwebinar\b|\bebook\b|\bcourse\b|\bcoaching\b|"
                    r"\btutorial\b|\bacademy\b|\bbootcamp\b", re.I)
NON_BUSINESS = re.compile(r"\b(?:youth|student|campus|city|community) ambassadors?\b|\bfully funded\b|"
                          r"\buniversit(?:y|ies|at)\b|\bnon-?profit\b|\bboard (?:of|member)|\bgovernment\b", re.I)
TIKTOK_SHOP = re.compile(r"\btik ?tok shop\b.{0,40}\baffiliates?\b|\btts affiliate\b|"
                         r"\btiktok shop (?:creator )?(?:pilot|affiliate program)\b|\btiktokshopaffiliate\b|"
                         r"\bcreator search insight\b", re.I)
OWN_PROGRAM = re.compile(r"\bour (?:own |separate )?(?:affiliate|ambassador|referral|partner|creator) program\b", re.I)
AMBASSADOR = re.compile(r"\bambassadors?\b|\bcreator (?:program|partners?|collabs?)\b", re.I)
AFFILIATE_CLASS = re.compile(r"\baffiliates?\b|\breferral (?:program|partners?)\b|\bpartner (?:program|programme)\b|"
                             r"\bcommissions?\b", re.I)
COMMISSION = re.compile(r"\bcommissions?\b|\bpayouts?\b|\brevenue[- ]share\b|\bbount(?:y|ies)\b|\brecurring\b|"
                        r"\bearn (?:up to )?(?:\$|£|€)?\d|\bper (?:sale|referral|sign[- ]?up|order)\b", re.I)

# Program pages on the brand's own site, probed in order (a 404 costs nothing but one polite GET). A program page
# needs program-specific terms; a bare "commission" (an affiliate-disclosure page) is not enough.
PROGRAM_PATHS = ("/affiliate", "/affiliates", "/affiliate-program", "/become-an-affiliate", "/ambassador",
                 "/ambassadors")
PROGRAM_PAGE = re.compile(r"\b(?:affiliate|ambassador|referral|partner) program(?:me)?\b|"
                          r"\bbecome an? (?:affiliate|ambassador|partner)\b", re.I)
MAX_PROGRAM_PROBES = 4


@dataclass
class Found:
    platform: str           # tiktok | youtube
    domain: str             # the brand's verified canonical domain
    name: str               # the account's display name (the brand)
    url: str                # canonical video URL
    quote: str              # verbatim caption or description excerpt (<= 400 chars)
    date: str               # ISO YYYY-MM-DD from the platform's publish time


# pure helpers ----------------------------------------------------------------------------------------------------

def tiktok_window(days: int) -> str:
    return "yesterday" if days <= 1 else "this-week" if days <= 7 else "this-month" if days <= 31 else "last-3-months"


def youtube_window(days: int) -> str:
    return "today" if days <= 1 else "this_week" if days <= 7 else "this_month" if days <= 31 else "this_year"


def tiktok_handle(url: str) -> tuple[str, str] | None:
    """(handle, video id) from a TikTok video URL, or None."""
    m = TT_URL.search(url or "")
    return (m.group(1), m.group(2)) if m else None


def tiktok_video_url(handle: str, video_id: str) -> str:
    return f"https://www.tiktok.com/@{handle}/video/{video_id}"


def tiktok_date(item: dict) -> str | None:
    """ISO date of a TikTok search item from create_time (unix seconds), else its create_time_utc string."""
    try:
        ts = int(item.get("create_time") or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts > 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    s = str(item.get("create_time_utc") or "")
    return s[:10] if ISO_DATE.match(s) else None


def youtube_date(publish: str | None) -> str | None:
    """Day of a YouTube publishDate ('2026-10-06T03:00:00-07:00' -> '2026-10-06'). publishedTime is not used: it is a
    fixed time stamp on long-form items."""
    s = str(publish or "")
    return s[:10] if ISO_DATE.match(s) else None


def youtube_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _flag(value) -> bool:
    return value is True or value == 1 or str(value).lower() in ("1", "true")


def tiktok_owner(user: dict) -> str:
    """'organisation' or 'seller' when TikTok's own flags make the account a brand, else ''. Verified alone is not
    used: a real brand (FigureLabs in the probe) can be unverified. A 'seller' flag also marks individual TikTok Shop
    creators, so the caller must check its bio domain against the account name."""
    if _flag(user.get("isOrganization")):
        return "organisation"
    commerce = user.get("commerceUserInfo") or {}
    if _flag(commerce.get("commerceUser")) or _flag(user.get("ttSeller")):
        return "seller"
    return ""


def is_us(country: str | None) -> bool:
    """True only for an explicit United States country. An unknown country is not a US channel."""
    return (country or "").strip().upper() in US_NAMES


def aggregator(domain: str) -> bool:
    return c.registrable(domain) in AGGREGATORS


def hosted(host: str) -> bool:
    """True for a site-builder or hosting host, or any subdomain of one."""
    h = (host or "").lower().rstrip(".")
    return h in HOSTING or any(h.endswith("." + suffix) for suffix in HOSTING)


def display_name(name: str) -> str:
    """'TRAINER'S BOX® -No.1 Channel | Coaches & Trainers' -> "TRAINER'S BOX®" (the account name before any tagline)."""
    head = re.split(r"\s*\|\s*|\s+[-–—]", (name or "").strip())[0]
    return head.strip(" -–—®™")


def named_in(name: str, text: str) -> bool:
    """The account's brand is named in a caption, title or page: its tokens in order, its joined form as one token
    ('Harbor Tools' in 'harbortools.example'), or its distinctive first token ('Northwind' for 'Northwind Lamps').
    The homepage and program-page checks in verify_site still require the full name."""
    want = c.tokens(name)
    joined = c.norm(name)
    if len(joined) < MIN_NAME or not want:
        return False
    have = re.findall(r"[a-z0-9]+", (text or "").lower())
    n = len(want)
    if any(have[i:i + n] == want for i in range(len(have) - n + 1)):
        return True
    return joined in have or (len(want[0]) >= 4 and want[0] in have)


def label_names(name: str, domain: str) -> bool:
    """The domain's first label carries the account's name ('northwind-lamps.example' for 'Northwind Lamps')."""
    want = c.norm(name)
    label = re.sub(r"[^a-z0-9]", "", c.registrable(domain).split(".")[0].lower())
    return len(want) >= MIN_NAME and want in label


def names_homepage(html: str, name: str) -> bool:
    """True when a <title> or og:site_name segment is exactly the account's name (whole tokens). 'Kaza' does not match
    'Kazaam SEO Agency' and 'Brandco' does not match 'Brandco Agency Pro Services'."""
    want = c.tokens(name)
    if not want or not html:
        return False
    raw = re.findall(r"<title[^>]*>(.*?)</title>|property=[\"']og:site_name[\"'][^>]*content=[\"']([^\"']+)",
                     html[:200_000], re.I | re.S)
    for a, b in raw:
        for seg in re.split(r"\s*\|\s*|\s+[-–—]\s*|\s*:\s+", htmllib.unescape(a or b)):
            if c.tokens(seg) == want:
                return True
    return False


def site_domains(urls: list[str], text: str = "") -> list[str]:
    """Company-candidate domains from account links (in order), then bare domains in the bio text. Platform hosts,
    link-in-bio aggregators and site-builder hosts are never returned."""
    out: list[str] = []
    for url in urls:
        if not url:
            continue
        if hosted(detect.bare_host(url if "//" in url else "https://" + url)):
            continue
        host = c.host_of(url)
        if host and host not in out and not aggregator(host) and not hosted(host):
            out.append(host)
    for host in c.domains_in_text(text):
        if host not in out and not aggregator(host) and not hosted(host):
            out.append(host)
    return out


def tiktok_ids(handle: str) -> set[str]:
    """The identifiers a TikTok account goes by on a homepage ('@handle', lower case)."""
    return {f"@{handle.lower()}"}


def youtube_ids(channel_id: str, handle: str | None) -> set[str]:
    """The identifiers a YouTube channel goes by on a homepage: its channel path and its @handle, lower case."""
    out = {f"channel/{channel_id.lower()}"}
    if handle:
        out.add(handle.lower())
    return out


def linked_ids(html: str, platform: str) -> set[str]:
    """Every account of this platform the homepage links to, in the same identifier form as the *_ids helpers."""
    if platform == "tiktok":
        return {f"@{m.lower()}" for m in TT_ACCOUNT.findall(html or "")}
    return {m.lower() for m in YT_ACCOUNT.findall(html or "")}


def third_party_tag(text: str, canon: str) -> bool:
    """True when a link in the caption or description carries an affiliate tracking tag to a host other than the
    brand's own: the video promotes someone else's program."""
    for url in URL_IN_TEXT.findall(text or ""):
        if AFF_TAG.search(url) and c.host_of(url) != canon:
            return True
    return False


def text_drop(text: str) -> str | None:
    """Why a caption or description is not a brand recruiting for its own program, or None when it is."""
    text = text or ""
    reason = c.text_gate(text)
    if reason:
        return reason
    if not c.buyer_language(text):
        return "no buyer language"
    if FIRST_PERSON.search(text):
        return "individual voice"
    if GUIDE.search(text):
        return "listicle or guide"
    if COURSE.search(text):
        return "course or tutorial"
    if not BRAND_VOICE.search(text):
        return "no brand voice"
    if MLM.search(text):
        return "mlm"
    if SELLER.search(text):
        return "seller"
    if NON_BUSINESS.search(text):
        return "non-business ambassador"
    if TIKTOK_SHOP.search(text) and not OWN_PROGRAM.search(text):
        return "platform program (TikTok Shop)"
    if AMBASSADOR.search(text) and not AFFILIATE_CLASS.search(text) and not COMMISSION.search(text):
        return "ambassador perks only"
    return None


def quote_for(text: str) -> str:
    return c.quote_around(text, c.BUYER, 400)


# site verification (free GETs through ctx.get) -------------------------------------------------------------------

async def verify_site(ctx, domain: str, name: str, platform: str, ours: set[str]) -> tuple[str | None, str]:
    """(canonical domain, reason). The domain is accepted when the brand's own homepage names the account and either
    links back to the account (reason 'backlink') or, with no link to any account of this platform, shows a program
    page that names the brand (reason 'program page'). A homepage that links a different account of this platform is
    rejected. Probes at most MAX_PROGRAM_PROBES program paths. A None domain carries the reason it failed."""
    if len(c.norm(name)) < MIN_NAME:
        return None, "name too short"
    home = await ctx.get(f"https://{domain}/", "home")
    if home is None or home.status >= 400:
        return None, "homepage unreachable"
    canon = c.host_of(home.final_url)
    if not canon or aggregator(canon) or hosted(detect.bare_host(home.final_url)):
        return None, "redirected to a platform"
    if c.parked(home.html):
        return None, "parked domain"
    if not names_homepage(home.html, name):
        return None, "homepage does not name the account"
    linked = linked_ids(home.html, platform)
    if linked & ours:
        return canon, "backlink"
    if linked:
        return None, "homepage links another account"
    for path in PROGRAM_PATHS[:MAX_PROGRAM_PROBES]:
        page = await ctx.get(urljoin(f"https://{canon}/", path.lstrip("/")), "linked")
        if page is None or page.status >= 400:
            continue
        text = detect.page_text(page.html)
        if PROGRAM_PAGE.search(text) and named_in(name, text):
            return canon, "program page"
    return None, "no backlink or program page"


# platform legs ---------------------------------------------------------------------------------------------------

async def _youtube(ctx, now: datetime, found: list[Found], stats: Counter) -> None:
    videos: list[dict] = []
    for query in ctx.cfg["youtube_queries"]:
        if not ctx.budget.ok():
            break
        data = await sg.sc_get(ctx.sc, "/v1/youtube/search",
                               {"query": query, "uploadDate": youtube_window(ctx.days), "includeExtras": "true"}, NAME)
        got = (data or {}).get("videos") or []
        videos.extend(got)
        ctx.say(f"youtube: {len(got)} videos for one query")
    newest: dict[str, dict] = {}
    for v in videos:
        vid = str(v.get("id") or "")
        channel = v.get("channel") or {}
        cid = str(channel.get("id") or "")
        if not YT_VIDEO_ID.fullmatch(vid) or not cid:
            stats["youtube: no channel id"] += 1
            continue
        date = youtube_date(v.get("publishDate"))
        if not c.within_days(date, ctx.days, now):
            stats["youtube: outside window"] += 1
            continue
        text = f"{v.get('title') or ''}. {v.get('description') or ''}"
        why = text_drop(text)
        if why:
            stats[f"youtube text: {why}"] += 1
            continue
        if cid not in newest or date > newest[cid]["date"]:
            newest[cid] = {"id": vid, "date": date, "text": text, "title": channel.get("title") or ""}
    ctx.say(f"youtube: {len(newest)} channels past text gates")
    lookups = 0
    for cid, row in sorted(newest.items(), key=lambda kv: kv[1]["date"], reverse=True):
        if lookups >= ctx.cfg["max_channels"] or not ctx.budget.ok():
            break
        lookups += 1
        ch = await sg.sc_get(ctx.sc, "/v1/youtube/channel", {"channelId": cid}, NAME) or {}
        if not ch:
            stats["youtube: no channel data"] += 1
            continue
        if not is_us(ch.get("country")):
            stats["youtube: channel not US"] += 1
            continue
        name = display_name(ch.get("name") or row["title"])
        if not named_in(name, row["text"]):
            stats["youtube: brand not named in video"] += 1
            continue
        handle_m = YT_HANDLE_URL.search(ch.get("channel") or "")
        handle = handle_m.group(1) if handle_m else None
        links = [ch.get("store")] + list(ch.get("links") or [])
        domains = site_domains([x for x in links if x], ch.get("description") or "")
        if not domains:
            stats["youtube: no channel site"] += 1
            continue
        canon, why = None, "no site"
        for domain in domains[: ctx.cfg["max_domains_per_account"]]:
            canon, why = await verify_site(ctx, domain, name, "youtube", youtube_ids(cid, handle))
            if canon:
                break
        if not canon:
            stats[f"youtube site: {why}"] += 1
            continue
        if third_party_tag(row["text"], canon):
            stats["third-party tag"] += 1
            continue
        found.append(Found("youtube", canon, name, youtube_video_url(row["id"]), quote_for(row["text"]), row["date"]))


async def _tiktok(ctx, now: datetime, found: list[Found], stats: Counter) -> None:
    budget = ctx.budget.sub(int(ctx.cfg.get("tiktok_credits", 6)))
    items: list[dict] = []
    for query in ctx.cfg["tiktok_queries"]:
        if not budget.ok():
            break
        data = await sg.sc_get(ctx.sc, "/v1/tiktok/search/keyword",
                               {"query": query, "date_posted": tiktok_window(ctx.days), "sort_by": "date-posted",
                                "trim": "true"}, NAME)
        got = (data or {}).get("search_item_list") or []
        items.extend(got)
        ctx.say(f"tiktok: {len(got)} videos for one query")
    newest: dict[str, dict] = {}
    for item in items:
        parsed = tiktok_handle(item.get("url") or "")
        if not parsed:
            stats["tiktok: no video url"] += 1
            continue
        handle, url_id = parsed
        if str(item.get("region") or "").upper() != "US":
            stats["tiktok: region not US"] += 1
            continue
        date = tiktok_date(item)
        if not c.within_days(date, ctx.days, now):
            stats["tiktok: outside window"] += 1
            continue
        desc = item.get("desc") or ""
        why = text_drop(desc)
        if why:
            stats[f"tiktok text: {why}"] += 1
            continue
        if handle not in newest or date > newest[handle]["date"]:
            newest[handle] = {"id": str(item.get("aweme_id") or url_id), "date": date, "desc": desc,
                              "nick": (item.get("author") or {}).get("nickname") or ""}
    ctx.say(f"tiktok: {len(newest)} accounts past text gates")
    lookups = 0
    for handle, row in sorted(newest.items(), key=lambda kv: kv[1]["date"], reverse=True):
        if lookups >= ctx.cfg["max_profiles"] or not budget.ok():
            break
        lookups += 1
        data = await sg.sc_get(ctx.sc, "/v1/tiktok/profile", {"handle": handle}, NAME)
        user = (data or {}).get("user") or {}
        if not user:
            stats["tiktok: no profile"] += 1
            continue
        name = display_name(user.get("nickname") or row["nick"])
        if not named_in(name, row["desc"]):
            stats["tiktok: brand not named in caption"] += 1
            continue
        owner = tiktok_owner(user)
        if not owner:
            stats["tiktok: no brand flag"] += 1
            continue
        bio = (user.get("bioLink") or {}).get("link") or ""
        domains = site_domains([bio], user.get("signature") or "")
        if owner == "seller":
            domains = [d for d in domains if label_names(name, d)]
        if not domains:
            stats["tiktok: no brand site"] += 1
            continue
        canon, why = None, "no site"
        for domain in domains[: ctx.cfg["max_domains_per_account"]]:
            canon, why = await verify_site(ctx, domain, name, "tiktok", tiktok_ids(handle))
            if canon:
                break
        if not canon:
            stats[f"tiktok site: {why}"] += 1
            continue
        if third_party_tag(row["desc"], canon):
            stats["third-party tag"] += 1
            continue
        found.append(Found("tiktok", canon, name, tiktok_video_url(handle, row["id"]), quote_for(row["desc"]),
                           row["date"]))


async def gather(ctx) -> tuple[list[Found], Counter]:
    """Both legs, YouTube first. Each leg keeps what it found when ScrapeCreators runs out of credits. Returns the
    found brand accounts and a count of every drop reason (no names, handles or links)."""
    found: list[Found] = []
    stats: Counter = Counter()
    legs = (("youtube", _youtube), ("tiktok", _tiktok))
    for label, leg in legs:
        if not ctx.budget.ok():
            break
        try:
            await leg(ctx, ctx.now, found, stats)
        except sg.OutOfCredits:
            ctx.say(f"{label}: credits exhausted, keeping {len(found)} found so far")
            break
    return found, stats


async def collect(ctx) -> list[sg.Candidate]:
    if not ctx.budget.ok():
        return []
    found, stats = await gather(ctx)
    best: dict[str, Found] = {}
    for f in found:
        if f.domain not in best or f.date > best[f.domain].date:
            best[f.domain] = f
    ctx.say(f"social_video: {len(best)} brand candidates; drops {dict(sorted(stats.items()))}")
    return [
        sg.Candidate("affiliate",
                     sg.Signal("social_post", f.url, f.quote, f.date, None, None, f.platform),
                     company_name=f.name, domain=f.domain, require_us=True)
        for f in best.values()
    ]
