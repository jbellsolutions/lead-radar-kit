"""Brand-owned Instagram and Threads posts recruiting affiliates, ambassadors or partners for the brand's OWN program.

Discovery is affiliate-explicit phrase search: ScrapeCreators reel search for Instagram (the default phrases), with a
Threads keyword search kept as an opt-in setting (off by default: it yielded no passing posts). Gates run cheapest
first: the post text (no credits), then ONE profile lookup per unique author (cached, capped at
DEFAULTS.max_profiles, shared by Instagram and Threads). An author passes when it is a professional account (not a
creator or seller category), its own link resolves to a brand domain, and that domain's homepage names the brand.
Nothing here reads a company page; growth.process() resolves each candidate to LinkedIn and applies the US gate
(require_us).

Facebook has no keyword discovery endpoint in ScrapeCreators, so this source emits only instagram and threads.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit

from .. import signals as sg
from . import common

NAME = "social_meta"
SOURCE = "social_meta"  # ScrapeCreators meter label

DEFAULTS = {
    "enabled": True,
    "credits": 15,
    "max_profiles": 10,   # shared cap on profile lookups (1 credit each, cached per author)
    "max_linktree": 2,    # /v1/linktree lookups (1 credit each) for linktr.ee hubs
    # Only the two phrases that produced candidates in the 30-day replay. Each search costs 1 credit per run whatever
    # it returns. "now accepting affiliates", "affiliate program is open", "become a brand partner", "our ambassador
    # program" and "apply to be an ambassador" yielded nothing passing; add them back via sources.social_meta.
    "reel_phrases": ["join our affiliate program", "our affiliate program"],
    # Threads keyword search: "join our affiliate program" returned 0 posts, "affiliate program" 20 posts, none passing
    # the gates. Off by default (0 credits). Set sources.social_meta.threads_queries to re-enable.
    "threads_queries": [],
}

# Text gates (pure) -----------------------------------------------------------------------------------------------

BRAND_VOICE = re.compile(r"\b(?:we|we're|we’re|our|ours|us)\b", re.I)
# Recruiting copy the shared buyer gate misses: "Be part of the nutribullet Affiliate Program!"
BUYER_EXTRA = re.compile(r"\bbe (?:part of|a part of) (?:the |our )?(?:[\w&'’-]+ ){0,3}(?:affiliate|referral|partner|"
                         r"ambassador)s?\b", re.I)
# Creator voice. Only the capital I counts, so "i.e." is not first person, and "AI" or "LALAL.AI" is not "I".
FIRST_PERSON_I = re.compile(r"(?<![\w.])I(?!\w|\.\w)")
FIRST_PERSON_WORDS = re.compile(r"\b(?:my|me|mine)\b", re.I)
# "We're partnering with Zenith": a brand partnering with someone else. "partner with us/our/you" is recruiting copy.
PARTNER_WITH = re.compile(r"\bpartner(?:ing|ed|s)?\s+with\s+(?!(?:us|our|you)\b)", re.I)
AT_MENTION = re.compile(r"(?<![\w.])@([A-Za-z0-9_.]{2,40})")
TLD_TAIL = re.compile(r"\.(?:ai|app|co|com|dev|io|net|org|shop|store|tv|us|xyz)$", re.I)
AFFILIATE_CUE = re.compile(r"\baffiliates?\b|\breferral (?:program|partners?)\b|\bpartner program(?:me)?\b|"
                           r"\bbrand partners?\b|\bbecome an? (?:\w+ )?partner\b", re.I)
AMBASSADOR_CUE = re.compile(r"\bambassadors?\b", re.I)
COMMISSION_CUE = re.compile(r"\d\s?%|\bcommissions?\b|\bearn\b|\brevenue[- ]share\b|\brecurring\b|\bpayouts?\b|"
                            r"\bpoints\b", re.I)
PROGRAM_QUOTE = re.compile(AFFILIATE_CUE.pattern + "|" + AMBASSADOR_CUE.pattern, re.I)
# Marketplace copy that is not the US program lane (Shopee/Lazada/Tokopedia and Indian storefronts).
NON_US_TEXT = re.compile(r"\b(?:shopee|lazada|tokopedia|myntra|flipkart|tiktok shop (?:malaysia|indonesia|philippines))\b",
                         re.I)
# "Become a Runway Affiliate", "join Higfy Affiliate Program": a capitalised name right after a lead-in verb and
# before the program word. Only that lead-in counts, so "Social Media Ambassador" or "our College Ambassador
# Program" name nothing.
NAMED_PROGRAM = re.compile(r"\b(?:[Bb]ecome an?|[Jj]oin|[Bb]e part of(?: the)?)\s+([A-Z][A-Za-z0-9&.'’-]{2,})\s+"
                           r"(?:Affiliate|Ambassador|Partner|Referral)s?\b")
# A program owner written as a possessive ("Zenith's affiliate program") or as a bare name ("Zenith Affiliate
# Program"), with no lead-in verb. The owner must still be the account for the post to pass.
POSSESSIVE_PROGRAM = re.compile(r"\b([A-Z][A-Za-z0-9&.'’-]{2,})['’]s\s+(?:[A-Za-z]+\s+)?"
                                r"(?i:affiliate|referral|partner|ambassador)s?\b")
BARE_PROGRAM = re.compile(r"\b([A-Z][A-Za-z0-9&.'’-]{2,})\s+(?i:affiliate|referral|partner|ambassador)s?\s+"
                          r"(?i:program|programme|network)\b")
NAMED_STOPWORDS = {"Our", "The", "Your", "This", "Brand", "New", "Official", "Affiliate", "Ambassador", "Partner",
                   "Referral", "Creator", "Elite", "Exclusive", "Shop", "Store", "Social", "Campus", "College",
                   "Digital", "Student", "Young", "European", "Media", "Youth", "University", "Local",
                   # Lead-ins and verbs that open a program sentence: "Join Affiliate Program", "Introducing ...".
                   "Join", "Become", "Apply", "Be", "Launch", "Launching", "Introducing", "Welcome", "Earn", "Get",
                   "Start", "Now", "Open", "Sign", "Refer", "Hiring", "Recruiting", "Looking", "We", "Its", "Here",
                   "Program", "Programs", "Partners", "Ambassadors", "Affiliates", "Brands", "Creators", "Team"}
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
# Two capitalised words, e.g. a person's name or another brand, inside the program sentence.
CAP_PAIR = re.compile(r"\b([A-Z][a-z][\w'’-]*)\s+([A-Z][a-z][\w'’-]*)\b")
# Capitalised words that are ordinary copy, not names ("Join Our", "Apply Now", "Affiliate Program").
CAP_STOP = {"A", "An", "Accepting", "Affiliate", "Affiliates", "Ambassador", "Ambassadors", "And", "Apply", "Be",
            "Become", "Best", "Bonus", "Brand", "Brands", "Check", "Click", "Code", "Commission", "Congrats",
            "Congratulations", "Creator", "Creators", "Discount", "Earn", "Elite", "Every", "Exclusive", "For", "Free",
            "Get", "Gold", "Grow", "Hey", "Here", "Hi", "If", "In", "Introducing", "Is", "It", "Join", "Launch", "Learn",
            "Limited", "Link", "Max", "Monthly", "New", "Now", "Of", "On", "Official", "Open", "Our", "Partner",
            "Partners", "Plus", "Pro", "Premium", "Program", "Programme", "Recurring", "Referral", "Sale", "Save", "Share",
            "Shop", "Sign", "Store", "Subscribe", "Summer", "Team", "Thank", "Thanks", "The", "This", "To", "Today", "Top",
            "Up", "Visit", "We", "Welcome", "With", "You", "Your"}
CAPTION_EXCLUDE_KEYS = ("sellers", "vendors", "vendor_names", "verticals")

# Profile gates ---------------------------------------------------------------------------------------------------

CREATOR_CATEGORY = re.compile(r"creator|blogger|influencer|personal|public figure|artist|musician|actor|comedian|"
                              r"athlete|model|photograph|journalist|writer|celebrity", re.I)
SELLER_CATEGORY = re.compile(r"marketing|advertis|consult|agency|education|course|coach|public relations", re.I)
AGGREGATOR_HOSTS = {"linktr.ee", "beacons.ai", "campsite.to", "campsite.bio", "likeshop.me", "shopmy.us", "snif.life",
                    "lnk.bio", "bio.link", "msha.ke", "stan.store", "koji.to", "tap.bio", "solo.to", "linktree.com",
                    "payhip.com", "gumroad.com", "ko-fi.com"}
NON_US_SUFFIXES = (".uk", ".co.uk", ".au", ".com.au", ".nz", ".co.nz", ".ph", ".com.ph", ".my", ".com.my", ".sg",
                   ".in", ".co.in", ".ca", ".de", ".fr", ".es", ".it", ".nl", ".mx", ".com.mx", ".br", ".com.br",
                   ".jp", ".kr", ".cn", ".hk", ".tw", ".ie", ".id", ".co.id", ".vn", ".th", ".ae", ".za", ".co.za")


def _date_posted(days: int) -> str:
    """ScrapeCreators reel search accepts only these windows (there is no last-day)."""
    return "last-week" if days <= 7 else "last-month" if days <= 31 else "last-year"


def _same_brand(a: str | None, b: str | None) -> bool:
    na, nb = common.norm(a or ""), common.norm(b or "")
    if len(na) < 4 or len(nb) < 4:
        return False
    return na == nb or na in nb or nb in na


def _owner_like(name: str, owner: str, full_name: str | None) -> bool:
    """The name is the account's own brand: equal after normalising, or one contains the other at 4+ characters."""
    for ref in (owner, full_name):
        if common.norm(name) and common.norm(name) == common.norm(ref or ""):
            return True
        if _same_brand(name, ref):
            return True
    return False


def _text(value) -> str:
    """Instagram captions are strings; Threads captions are {'text': ...} objects, sometimes null."""
    if isinstance(value, dict):
        value = value.get("text")
    return value if isinstance(value, str) else ""


def named_program(text: str) -> str | None:
    """The brand a caption names as the program owner ('Become a Runway Affiliate' -> 'Runway'), or None."""
    for m in NAMED_PROGRAM.finditer(text or ""):
        if m.group(1) not in NAMED_STOPWORDS:
            return m.group(1)
    return None


def program_owners(text: str) -> list[str]:
    """Every brand a caption attaches to a program, in any of the three forms: 'Become a Zenith Affiliate',
    "Zenith's affiliate program", 'Zenith Affiliate Program'. Stoplisted words are not names."""
    names: list[str] = []
    for rx in (NAMED_PROGRAM, POSSESSIVE_PROGRAM, BARE_PROGRAM):
        names += [m.group(1) for m in rx.finditer(text or "") if m.group(1) not in NAMED_STOPWORDS]
    return names


def program_sentence(caption: str) -> str:
    """The caption sentence that carries the program cue, verbatim with whitespace collapsed, at most 300 characters.
    Line breaks end a sentence as well as full stops, so an unpunctuated caption is not one long sentence."""
    pieces: list[str] = []
    for line in (caption or "").splitlines():
        pieces += SENTENCE_BREAK.split(line.strip())
    for piece in pieces:
        sentence = re.sub(r"\s+", " ", piece).strip()
        if sentence and PROGRAM_QUOTE.search(sentence):
            return sentence[:300]
    return re.sub(r"\s+", " ", caption or "").strip()[:300]


def third_party_name(quote: str, owner: str, full_name: str | None) -> bool:
    """True when the quote names a person or another brand in two capitalised words (a shout-out, a featured
    affiliate). A program sentence names only the brand itself, so a name in it is a reason to drop the post."""
    for first, second in CAP_PAIR.findall(quote or ""):
        if first in CAP_STOP or second in CAP_STOP:
            continue
        pair = f"{first} {second}"
        if not (_same_brand(pair, owner) or _same_brand(pair, full_name)):
            return True
    return False


def foreign_mention(handle: str, owner: str, full_name: str | None) -> bool:
    """A mention of an account other than the brand itself. The brand's own handle may carry a domain tail
    ('@runway.ai' for the runwayapp account)."""
    base = TLD_TAIL.sub("", handle.rstrip("."))
    own = common.norm(base) == common.norm(owner) or _same_brand(base, owner) or _same_brand(base, full_name)
    return not own


def post_gate(caption: str, owner: str, full_name: str | None, rx: dict, dnp: list[str]) -> str | None:
    """Why a post is not a brand recruiting for its own program (text only, no credits), or None."""
    text = caption or ""
    if not text.strip():
        return "no caption"
    if not (common.buyer_language(text) or BUYER_EXTRA.search(text)):
        return "not buyer language"
    gate = common.text_gate(text)
    if gate:
        return gate
    if NON_US_TEXT.search(text):
        return "non-US marketplace"
    if FIRST_PERSON_I.search(text) or FIRST_PERSON_WORDS.search(text):
        return "first person (creator voice)"
    names = program_owners(text)
    if any(not _owner_like(n, owner, full_name) for n in names):
        return "program named is another brand"
    # Brand voice (we/our/us), or a program the caption names as the account's own ('Become a Runway Affiliate').
    if not (BRAND_VOICE.search(text) or names):
        return "not brand voice"
    if not AFFILIATE_CUE.search(text) and not (AMBASSADOR_CUE.search(text) and COMMISSION_CUE.search(text)):
        return "ambassador without commission"
    if PARTNER_WITH.search(text):
        return "partners with another brand"
    if any(foreign_mention(m, owner, full_name) for m in AT_MENTION.findall(text)):
        return "tags another account"
    if third_party_name(program_sentence(text), owner, full_name):
        return "names a third party in the program sentence"
    for key in CAPTION_EXCLUDE_KEYS:
        if rx.get(key) and rx[key].search(text):
            return f"excluded: {key}"
    low = f"{full_name or ''} {owner}".lower().replace(" ", "")
    if any(n and n.replace(" ", "") in low for n in dnp):
        return "do not prospect"
    return None


def post_date(value) -> str | None:
    """ISO date from an ISO timestamp (reels) or epoch seconds (Threads). Never invented: an out-of-range number
    (for example epoch milliseconds) is undated."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        return common.relative_date(value.strip())
    return None


IG_POST = re.compile(r"instagram\.com/(p|reel|reels|tv)/([A-Za-z0-9_-]+)", re.I)


def instagram_url(item: dict) -> str | None:
    """Canonical https://www.instagram.com/p/<code>/ or /reel/<code>/."""
    m = IG_POST.search(item.get("url") or "")
    if m:
        kind = "reel" if m.group(1).lower() in ("reel", "reels") else m.group(1).lower()
        return f"https://www.instagram.com/{kind}/{m.group(2)}/"
    code = item.get("shortcode")
    return f"https://www.instagram.com/p/{code}/" if isinstance(code, str) and code else None


def threads_url(item: dict, username: str) -> str | None:
    code = item.get("code")
    if isinstance(code, str) and code and username:
        return f"https://www.threads.com/@{username}/post/{code}"
    return common.unwrap(item["url"]) if isinstance(item.get("url"), str) else None


# Profile helpers (pure) ------------------------------------------------------------------------------------------

def profile_user(resp: dict | None) -> dict | None:
    """The user object of an Instagram profile response (data.user, else user)."""
    if not isinstance(resp, dict):
        return None
    data = resp.get("data")
    if isinstance(data, dict) and isinstance(data.get("user"), dict):
        return data["user"]
    if isinstance(resp.get("user"), dict):
        return resp["user"]
    return resp if resp.get("username") else None


def business_reason(user: dict) -> str | None:
    """Why an account is not a brand's own professional account, or None when it is one.

    Instagram's business flag is not reliable for brands (a verified affiliate-program brand reports
    is_business_account false, category empty), so a professional account is the gate, and creator and seller
    categories are the denylist. The homepage check then requires the brand's own site to name the brand."""
    if user.get("is_professional_account") is not True and user.get("is_business_account") is not True:
        return "not a professional account"
    cat = " ".join(str(user.get(k) or "") for k in ("category_name", "business_category_name", "overall_category_name"))
    if CREATOR_CATEGORY.search(cat):
        return "creator category"
    if SELLER_CATEGORY.search(cat):
        return "seller category"
    return None


def display_name(name: str | None) -> str:
    """'LALAL.AI - AI music tool' -> 'LALAL.AI'."""
    return re.split(r"\s+[-–|·:]\s+|\s*\|\s*", (name or "").strip())[0].strip()


def _unwrap_lynx(url: str) -> str:
    """l.instagram.com/?u=<target> in bio links: the real target is the u parameter. A malformed URL is returned
    unchanged (it then resolves to no domain)."""
    try:
        parts = urlsplit(url if "//" in url else "https://" + url)
        query = parse_qs(parts.query)
        host = parts.hostname or ""
    except ValueError:
        return url
    if host.endswith("instagram.com") and query.get("u"):
        return unquote(query["u"][0])
    return url


def link_urls(user: dict) -> list[str]:
    urls = [user.get("external_url")] + [b.get("url") for b in user.get("bio_links") or [] if isinstance(b, dict)]
    return [_unwrap_lynx(str(u)) for u in urls if u]


def brand_domain(urls: list[str], primary: str | None = None) -> str | None:
    """The brand's own registrable domain. The profile's external link is the primary one; otherwise the majority
    of its other links (a brand's bio can list many unrelated links, so the majority alone is not enough).
    Platforms, aggregators and forms never count."""
    own = [u for u in urls if (common.host_of(u) or "") and common.host_of(u) not in AGGREGATOR_HOSTS]
    if primary:
        host = common.host_of(_unwrap_lynx(str(primary)))
        if host and host not in AGGREGATOR_HOSTS:
            return host
    return common.brand_root(own, min_share=0.34) if own else None


def _registrable(url: str) -> str:
    try:
        return common.registrable(urlsplit(url if "//" in url else "https://" + url).hostname or "")
    except ValueError:
        return ""


def non_us_domain(domain: str) -> bool:
    return domain.endswith(NON_US_SUFFIXES)


# Collection ------------------------------------------------------------------------------------------------------

LAST_STATS: dict[str, int] = {}


class _Post:
    __slots__ = ("platform", "owner", "full_name", "caption", "url", "date", "quote", "named", "affiliate",
                 "commission")

    def __init__(self, platform, owner, full_name, caption, url, date):
        self.platform, self.owner, self.full_name = platform, owner, full_name
        self.caption, self.url, self.date = caption, url, date
        self.quote = program_sentence(caption)
        self.named = named_program(caption)
        self.affiliate = bool(AFFILIATE_CUE.search(caption))
        self.commission = bool(COMMISSION_CUE.search(caption))


def _items(data, key: str) -> list:
    """The list under `key` in a ScrapeCreators response, or an empty list."""
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, list) else []


def _gate_post(it, ctx, stats, platform: str, rx: dict, dnp: list[str]) -> _Post | None:
    """Free gates for one search result. platform=instagram reads owner/url; threads reads user/code."""
    if not isinstance(it, dict):
        stats["not an object"] += 1
        return None
    if platform == "instagram":
        if it.get("is_paid_partnership") or it.get("is_ad"):
            stats["paid partnership"] += 1
            return None
        owner = it.get("owner") if isinstance(it.get("owner"), dict) else {}
    else:
        owner = it.get("user") if isinstance(it.get("user"), dict) else {}
    username = owner.get("username")
    if not username:
        stats["no owner"] += 1
        return None
    caption = _text(it.get("caption"))
    date = post_date(it.get("taken_at"))
    if not date or not common.within_days(date, ctx.days, ctx.now):
        stats["undated or outside window"] += 1
        return None
    why = post_gate(caption, username, owner.get("full_name"), rx, dnp)
    if why:
        stats[why] += 1
        return None
    url = instagram_url(it) if platform == "instagram" else threads_url(it, username)
    if not url:
        stats["no url"] += 1
        return None
    return _Post(platform, username, owner.get("full_name"), caption, url, date)


def _gate_posts(items, ctx, stats, platform: str) -> list[_Post]:
    """Free gates for one search result list. One malformed item is counted and skipped, never fatal."""
    rx, dnp = ctx.signals_cfg["_rx"], ctx.signals_cfg["_dnp"]
    out = []
    for it in items:
        try:
            post = _gate_post(it, ctx, stats, platform, rx, dnp)
        except (TypeError, ValueError, AttributeError):
            stats["malformed post"] += 1
            continue
        if post is not None:
            out.append(post)
    return out


async def _search(ctx, path: str, params: dict, state: dict, stats, key: str):
    """One ScrapeCreators search (1 credit). Out of credits ends the search phase; posts already gathered are kept."""
    try:
        data = await sg.sc_get(ctx.sc, path, params, SOURCE)
    except sg.OutOfCredits:
        state["out_of_credits"] = True
        stats["out of credits"] += 1
        return None
    stats[key] += 1
    return data


async def _profile(ctx, username: str, cache: dict, state: dict, stats) -> dict | None:
    """One cached Instagram profile lookup per author, within the shared profile cap and the source budget."""
    key = username.lower()
    if key in cache:
        return cache[key]
    if state["profiles"] >= ctx.cfg["max_profiles"] or not ctx.budget.ok():
        return None
    state["profiles"] += 1
    try:
        resp = await sg.sc_get(ctx.sc, "/v1/instagram/profile", {"handle": username}, SOURCE)
    except sg.OutOfCredits:
        # Not cached: the author was never answered, so it is not treated as a non-brand.
        state["out_of_credits"] = True
        stats["out of credits"] += 1
        return None
    cache[key] = profile_user(resp)
    return cache[key]


async def _linktree_urls(ctx, url: str, state: dict, stats) -> list[str]:
    if state["linktree"] >= ctx.cfg["max_linktree"] or not ctx.budget.ok():
        return []
    state["linktree"] += 1
    try:
        resp = await sg.sc_get(ctx.sc, "/v1/linktree", {"url": url}, SOURCE)
    except sg.OutOfCredits:
        state["out_of_credits"] = True
        stats["out of credits"] += 1
        return []
    return [str(x["url"]) for x in (resp or {}).get("links") or [] if isinstance(x, dict) and x.get("url")]


async def _brand_homepage(ctx, domain: str, names: list[str], stats) -> tuple[str | None, str | None]:
    """(final brand domain, the name the homepage uses), or (None, None). One free GET, robots honoured."""
    page = await ctx.get(f"https://{domain}/", role="home")
    if page is None or page.status >= 400 or common.parked(page.html):
        stats["homepage unreachable or parked"] += 1
        return None, None
    final = common.host_of(page.final_url)
    if not final:
        stats["homepage on a platform host"] += 1
        return None, None
    if non_us_domain(final):
        stats["non-US domain"] += 1
        return None, None
    for name in names:
        if name and len(common.norm(name)) >= 4 and common.page_names_brand(page.html, name):
            return final, name
    stats["homepage does not name the brand"] += 1
    return None, None


async def _brand_candidate(ctx, p: _Post, cache: dict, state: dict, stats) -> sg.Candidate | None:
    """The brand candidate for one author's strongest post, or None. Costs at most one profile lookup and, for a
    linktr.ee hub, one linktree lookup."""
    user = await _profile(ctx, p.owner, cache, state, stats)
    if user is None:
        stats["no profile (cap, budget or 404)"] += 1
        return None
    why = business_reason(user)
    if why:
        stats[why] += 1
        return None
    urls = link_urls(user)
    primary = user.get("external_url")
    if brand_domain(urls, primary) is None:
        for hub in [u for u in urls if _registrable(u) == "linktr.ee"][:1]:
            urls += await _linktree_urls(ctx, hub, state, stats)
    domain_guess = brand_domain(urls, primary)
    if domain_guess is None:
        stats["no brand link"] += 1
        return None
    full = user.get("full_name") or ""
    names = [display_name(full), full, user.get("username") or p.owner, p.named]
    if non_us_domain(domain_guess):
        stats["non-US domain"] += 1
        return None
    domain, used_name = await _brand_homepage(ctx, domain_guess, [n for n in names if n], stats)
    if domain is None:
        return None
    return sg.Candidate("affiliate", sg.Signal("social_post", p.url, p.quote, p.date, platform=p.platform),
                        company_name=display_name(used_name) or used_name, domain=domain, require_us=True)


async def collect(ctx) -> list[sg.Candidate]:
    stats: Counter = Counter()
    state = {"profiles": 0, "linktree": 0, "out_of_credits": False}
    cache: dict = {}
    # 1) Searches first (1 credit each), so profile lookups never starve the search phase.
    reels: list[_Post] = []
    threads: list[_Post] = []
    for phrase in ctx.cfg["reel_phrases"]:
        if state["out_of_credits"] or not ctx.budget.ok():
            break
        data = await _search(ctx, "/v2/instagram/reels/search",
                             {"query": phrase, "date_posted": _date_posted(ctx.days)}, state, stats, "reel searches")
        reels += _gate_posts(_items(data, "reels"), ctx, stats, "instagram")
    for query in ctx.cfg["threads_queries"]:
        if state["out_of_credits"] or not ctx.budget.ok():
            break
        data = await _search(ctx, "/v1/threads/search", {"query": query, "trim": True}, state, stats,
                             "threads searches")
        threads += _gate_posts(_items(data, "posts"), ctx, stats, "threads")

    # 2) Profiles, strongest posts first: affiliate wording, then a commission cue, then newest.
    posts = sorted(reels + threads, key=lambda p: p.date, reverse=True)
    posts.sort(key=lambda p: (not p.affiliate, not p.commission))
    best: dict[str, sg.Candidate] = {}
    seen: set[str] = set()
    for p in posts:
        if state["out_of_credits"]:
            break
        if p.owner.lower() in seen:
            continue
        seen.add(p.owner.lower())
        try:
            cand = await _brand_candidate(ctx, p, cache, state, stats)
        except (TypeError, ValueError, AttributeError):
            stats["malformed profile"] += 1
            continue
        if cand is None:
            continue
        domain = cand.domain
        if domain not in best or (cand.signal.signal_date or "") > (best[domain].signal.signal_date or ""):
            best[domain] = cand
        stats["brand candidates"] += 1

    out = list(best.values())
    stats["profiles"] = state["profiles"]
    stats["linktree"] = state["linktree"]
    stats["candidates"] = len(out)
    LAST_STATS.clear()
    LAST_STATS.update(stats)
    ctx.say(f"social_meta: {stats['reel searches']} reel searches, {stats['threads searches']} threads searches, "
            f"{state['profiles']} profile lookups, {len(out)} candidates")
    return out


__all__ = ["NAME", "DEFAULTS", "collect", "post_gate", "named_program", "program_owners", "program_sentence",
           "third_party_name", "instagram_url", "threads_url", "post_date", "profile_user", "business_reason",
           "link_urls", "brand_domain", "display_name", "LAST_STATS"]
