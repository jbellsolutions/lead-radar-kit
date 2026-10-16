"""Reddit: founders and marketers talking about their OWN affiliate, partner, referral or ambassador program (launching
it, announcing it, recruiting affiliates, hiring an affiliate manager), resolved to the company's own domain.

Discovery is ScrapeCreators search (1 credit per call). A post body is read (1 credit) only when the search result
has none. A post is kept when it reads as the poster's own program, and resolved only when the post, or the OP's
top-level comment, names a domain and the domain's homepage is live, not parked, and clearly the same business:
either the post claims it ("our app acme.io", "I built acme.io", "acme.io is mine") or the homepage's <title> names a
brand the post itself describes in the first person. A domain is never guessed from a username or a competitor
mention, and an affiliate-software vendor is never the company.

Authors are pseudonymous. Usernames are compared in memory only; they are never stored, logged or returned.
"""
from __future__ import annotations

import functools
import html as htmllib
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...scrapecreators import OutOfCredits
from .. import signals as sg
from . import common as c

if TYPE_CHECKING:
    from . import SourceContext

NAME = "reddit"

DEFAULTS = {
    "enabled": True,
    "credits": 15,
    # (subreddit or "" for all of Reddit, query). Ordered by the 2026-10-10 probe's buyer yield (quoted phrases on
    # all of Reddit, and "affiliate program" in the founder subreddits), then the untested forms, then the subreddits
    # the probe found nearly empty. The per-run budget stops the list where it runs out.
    "searches": [
        ["", '"our affiliate program"'],
        ["startups", "affiliate program"],
        ["", '"recruit affiliates"'],
        ["indiehackers", "affiliate program"],
        ["", '"looking for affiliates"'],
        ["microsaas", "affiliate program"],
        ["SaaS", "affiliate program"],
        ["DigitalMarketing", "affiliate program"],
        ["", '"affiliates to promote"'],
        ["", '"referral program" "my saas"'],
        ["", '"partner program" "my startup"'],
        ["", '"affiliate manager" hiring'],
        ["affiliatemarketing", "looking for affiliates for my"],
        ["EntrepreneurRideAlong", "affiliate program"],
        ["hiring", "affiliate manager"],
        ["Entrepreneur", "affiliate program"],
        ["smallbusiness", "affiliate program"],
        ["ecommerce", "affiliate program"],
        ["shopify", "affiliate program"],
        ["marketing", "affiliate program"],
        ["forhire", "affiliate manager"],
    ],
    "reserve_reads": 6,        # searches stop early enough to leave this many credits for body reads
    "max_body_reads": 8,       # /v1/reddit/post calls per run (only for posts whose search result has no body)
    "max_comment_reads": 5,    # /v1/reddit/post/comments calls per run (kept posts with no anchor in the body)
    "max_pages": 1,            # search pages per query (page 2+ costs a credit each)
}

MIN_EPOCH = 946684800   # 2000-01-01 UTC: anything earlier is not a Reddit post date
MAX_EPOCH = 4102444800  # 2100-01-01 UTC
CURLY = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})
OP_UNKNOWN = {"", "[deleted]", "[removed]", "automoderator"}

# ---- text patterns -------------------------------------------------------------------------------------------------

TITLE_TERM = re.compile(r"\b(?:affiliates?|referrals?|partners?|ambassadors?|creators?|resellers?|programs?|programmes?|"
                        r"commissions?|payouts?|recruit\w*)\b", re.I)
PROGRAM_TERM = re.compile(r"\b(?:affiliates?|referrals?|partners?|ambassadors?|creators?|resellers?)\b", re.I)
FIRST_PERSON = re.compile(r"\b(?:we|we're|we've|we'd|we'll|our|ours|us|i|i'm|i've|i'd|i'll|my|mine|me)\b", re.I)
OWN_PROGRAM_FULL = re.compile(
    r"\b(?:our|my)\s+(?:[\w-]+\s+){0,2}?(?:affiliate|referral|partner|ambassador|creator)s?\s+(?:program|programme|network|"
    r"scheme)\b|\b(?:our|my)\s+(?:affiliates|ambassadors)\b", re.I)
AFFILIATE_MANAGER = re.compile(r"\b(?:affiliate|referral|ambassador|creator)\s+(?:manager|lead|director|head|coordinator|"
                               r"specialist)s?\b|\bhead of (?:affiliates?|partnerships?)\b", re.I)
USER_MENTION = re.compile(r"\bu/\w+|/user/|\bu_\w+", re.I)
HIRE_TITLE = re.compile(r"\[hiring\]|\bhiring\b|looking for affiliates for (?:my|our)\b", re.I)
SELF_INTRO = re.compile(r"\b(?:I'm|I am|My name is|my name is|Hi,? I'm|Hey,? I'm)\s+[A-Z][a-z]+")  # case-sensitive

# Program-side drops. Each returns a reason label; text_gate() in common.py covers vertical, MLM, seller, seeker,
# recruiter and individual-affiliate language first.
#
# Seeking: a person looking for a program to join. The need/want branch is exempt for a founder who must still build
# a program for their own product ("we need an affiliate program for our SaaS"), unless the sentence also says join.
SEEKER_NEED = re.compile(
    r"\b(?:looking|searching|hunting|trying|want(?:ing)?|need(?:ing)?) (?:to |for )?(?:an? |any |some |good |best |top |legit )?"
    r"(?:affiliate|referral|partner|ambassador|creator) programs?\b", re.I)
SEEKER_OTHER = re.compile(
    r"\b(?:which|what|best|good|any|top|legit) (?:affiliate |referral |partner )?programs? (?:to join|should i join|for "
    r"(?:beginners|newbies|bloggers|creators|influencers)|that pay)\b"
    r"|\bhow (?:do|can|to|should) i (?:join|get into|get accepted|apply to)\b"
    r"|\b(?:join|apply to|sign up for) (?:the |an? |any |some |this |their |these )?(?:affiliate|referral|partner) programs?\b",
    re.I)
# The poster wanting to join a program ("I want to join our affiliate program"). "we'd love to see you join" is a
# founder's pitch to others, so a subject of you/they/affiliates between the pronoun and the verb does not count.
JOINER = re.compile(r"\b(?:i|we)\b(?:(?!\b(?:you|your|them|they|people|brands|affiliates|creators|partners)\b)[^.!?\n]){0,20}?"
                    r"\b(?:join|apply to|sign up (?:for|to)|get into|get accepted (?:to|into))\b", re.I)
PRODUCT_CONTEXT = re.compile(r"\b(?:for|at|on|in) (?:our|my) (?:[\w-]+ ){0,2}?(?:saas|app|apps|product|products|store|brand|"
                             r"startup|business|platform|software)\b", re.I)
JOIN_WORD = re.compile(r"\b(?:join|joining|apply|applying|sign up|signing up|get accepted|be accepted)\b", re.I)
AFFILIATE_SIDE = re.compile(
    r"\bi(?:'m| am| ve been| have been)? (?:an? )?(?:affiliate|promoter)s?\b(?! (?:program|manager|partner))"
    r"|\bwhere (?:to|can i|do i|should i) (?:find|join|apply)\b[^.!?\n]{0,30}\bprograms?\b"
    r"|\blooking for (?:offers|brands to promote|products to promote|affiliate links?)\b"
    r"|\bmy (?:affiliate |referral )?(?:links?|codes?)\b", re.I)
# Earnings wording is checked sentence by sentence, and not on a founder's own program sentence ("to make money with
# our app" in "we just launched our affiliate program to make money with our app" is the pitch, not the seeker).
EARNINGS = re.compile(r"\bget paid (?:to|per|for)\b|\bside (?:income|hustle)\b|\bpassive income\b|\bmake money\b", re.I)
CONSUMER = re.compile(r"\brefer (?:a |your |my )?friends?\b|\bget \d+ months? free\b|\bresiduals?\b|\bpotential/month\b"
                      r"|\$\d+\s*(?:/|per)\s*(?:day|week)\b|\bdownline\b|\bmlm\b|\bnetwork marketing\b|\bmulti[- ]level\b"
                      r"|\bjoin my team\b", re.I)
TRAFFIC = re.compile(r"\b(?:offer managers?|advertiser offers?|paid traffic|media buy\w*|traffic arbitrage|"
                     r"cpa (?:offers?|networks?)|buy(?:ing)? traffic|run (?:the |our |their |these )?offers?|native traffic|"
                     r"pop-?unders?)\b", re.I)
FOR_HIRE = re.compile(r"\[for hire\]|\bfor hire\b|\bavailable for (?:hire|work|freelance)\b|\bhire me\b|\bopen to work\b"
                      r"|\bi(?:'m| am) (?:a |an )?(?:freelance|freelancer|consultant|contractor)\b", re.I)
SELLER = re.compile(
    r"\b(?:we|i) (?:can|will|'ll|help|offer)\b[^.!?\n]{0,30}\b(?:help|manage|run|set up|setup|build|launch|grow|scale|recruit)"
    r"\b[^.!?\n]{0,40}\b(?:you|your|brands?|businesses|clients?|founders?|companies|merchants?|stores?|startups?|saas)\b"
    r"|\b(?:our|my) (?:agency|consultancy|done[- ]for[- ]you|retainer|service packages?)\b"
    r"|\b(?:we|i) (?:run|manage|build|set up|launch) (?:affiliate|referral|partner|ambassador) programs? for "
    r"(?:clients?|brands?|businesses|companies|others|other)\b"
    r"|\bfree (?:audit|consultation|strategy call|discovery call)\b|\blaunch bundle\b", re.I)
RESEARCH = re.compile(r"\b(?:quick|short|anonymous|\d+[- ]minute) (?:survey|poll|questionnaire)\b"
                      r"|\bcustomer (?:interviews?|discovery|research)\b|\buser (?:interviews?|research)\b"
                      r"|\bvalidat(?:e|es|ing|ion)\b[^.!?\n]{0,30}\b(?:idea|concept|demand|saas|product|niche)\b"
                      r"|\bwhat (?:tools?|software|platforms?|solutions?) (?:do|are) (?:you|people|everyone|founders) (?:use|using)\b"
                      r"|\bwhat (?:consumes|eats up) (?:your|their|the)\b|\bwe(?:'re| are) (?:researching|surveying)\b"
                      r"|\bhow(?:'s| is| are) (?:your|everyone'?s|yours)\b|\bdoes (?:your|everyone'?s) (?:saas|app|product|startup|"
                      r"business|store|brand)\b|\bdo you (?:have|run) an? (?:affiliate|referral) program\b|\bwanted to know\b"
                      r"|\bcurious (?:if|whether|how)\b|\bpoll\b", re.I)

NON_US_SUBS = {"sidehustlesindia", "nepalsocial", "indianstartups", "indiastartups", "pakistan", "nigeria", "kenya",
               "philippines", "southafrica"}
DIRECTORY_HOSTS = {"affiliationlist.com", "anderro.com", "g2.com", "capterra.com", "trustpilot.com", "alternativeto.net",
                   "appsumo.com", "saasworthy.com", "crunchbase.com", "betalist.com", "f6s.com", "github.com",
                   "gist.github.com", "notion.so", "notion.site", "stackoverflow.com", "wikipedia.org", "imgur.com",
                   "redd.it", "i.redd.it", "v.redd.it", "google.com", "docs.google.com", "youtube.com", "x.com",
                   "twitter.com", "medium.com", "substack.com", "producthunt.com", "linkedin.com", "reddit.com"}

# Claim patterns: the domain or brand must sit right after a possessive product noun, a build/own verb, or a "founder
# of" phrase, or be followed by "is mine/ours". Competitor, comparison, tool and adoption words reject the claim.
NOUNS = (r"app|apps|product|products|tool|tools|saas|platform|site|website|store|brand|startup|software|company|shop|"
         r"community|course|service|newsletter|plugin|extension|business|project|game|bot|api|domain|program|programme|"
         r"network")
BRAND_NOUNS = "|".join(n for n in NOUNS.split("|") if n not in ("program", "programme", "network"))
ADJ = r"affiliate|referral|partner|ambassador|creator|new|own|main|free|web|online|b2b|saas|digital|mobile|beta|side"


def _poss(nouns: str) -> re.Pattern:
    return re.compile(rf"\b(?:my|our)\s+(?:(?:{ADJ})\s+){{0,2}}(?:{nouns})s?\s*(?:(?:is|are|at)\s+){{0,2}}"
                      rf"(?:[:,\-–—(=]\s*)?(?:https?://)?\s*$", re.I)


POSS_END = _poss(NOUNS)
POSS_BRAND = _poss(BRAND_NOUNS)  # a brand after "our referral program:" is a vendor name, not the company
VERB_END = re.compile(
    r"\b(?:(?:we|i)(?:'ve|\s+have)?|i'm|i\s+am|we're|we\s+are)\s+(?:built|launched|made|created|started|founded|run|operate|"
    r"own|maintain|designed|running)\s+(?:(?:the|our|my)\s+)?(?:(?:is|at)\s+)?(?:https?://)?\s*$"
    r"|\b(?:i(?:'m| am)|we(?:'re| are))\s+(?:the\s+|a\s+|an\s+|our\s+|my\s+)?(?:founder|co-?founder|creator|developer|maker|"
    r"owner|builder|author|ceo|cto)\s+(?:of|behind)\s+(?:the\s+|our\s+|my\s+)?(?:https?://)?\s*$", re.I)
AFTER_CLAIM = re.compile(r"^\s*(?:is|are)?\s*\(?\s*(?:mine|ours)\b"
                         r"|^\s*(?:[,\-–—]\s*)?(?:is|are)\s+(?:my|our)\s+(?:own\s+)?"
                         r"(?:site|app|product|saas|startup|tool|store|company|project|platform|brand)\b", re.I)
NEG_CLAIM = re.compile(r"\b(?:competitors?|rivals?|alternatives?|vs\.?|versus|like|similar|compared|comparing|instead|than|"
                       r"unlike|via|through|using|use|uses|with|on|from|against|cheaper|better)\b", re.I)
NEG_BRAND = re.compile(r"\b(?:competitors?|rivals?|alternatives?|vs\.?|versus|unlike|compared|comparing|instead of|than|via|"
                       r"through|using|use|uses|like|against|cheaper|better)\b", re.I)
ADOPT = re.compile(r"\b(?:switch(?:ed|ing)?|mov(?:ed|ing)|migrat\w*|chose|chosen|picked|went with|tried|trying|signed up|"
                   r"sign(?:ing)? up|using|adopted|love|loving|recommend\w*|integrat\w*|bought|purchas\w*|powered by|"
                   r"set up on|setting up on)\b", re.I)

URL_RX = re.compile(r"https?://[^\s<>\"'\)\]]+", re.I)
BARE_RX = re.compile(r"(?<![\w@/.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:com|io|co|ai|app|net|org|us|shop|store|biz|"
                     r"finance|capital|health|fit|beauty|so|dev|tech|xyz|me|pro|site|sites|tools|studio|page|cloud|gg|info|"
                     r"online|digital|software|space|live|blog|news|works|systems|link|tv|fm|group|pay|money|cash|bio|team|"
                     r"world|one|ly|sh))(?![\w-]|\.\w)", re.I)
TITLE_RX = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
OG_RX = re.compile(r"<meta[^>]+property=[\"']og:site_name[\"'][^>]*content=[\"']([^\"']+)[\"']", re.I)
BRAND_SPLIT = re.compile(r"\s+[|\-–—:·•]\s+|\s*\|\s*")
GENERIC = {"affiliate", "affiliates", "program", "programs", "partner", "partners", "referral", "referrals", "home",
           "welcome", "login", "log", "sign", "signup", "pricing", "blog", "docs", "help", "support", "faq", "store", "shop",
           "app", "official", "website", "start", "get", "dashboard", "features", "contact", "about", "the", "and", "for",
           "with", "best", "new", "free", "ambassador", "ambassadors", "creator", "creators", "join", "apply", "page"}
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


# ---- pure helpers --------------------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _rx() -> dict:
    """The shared compiled patterns from config (program_post, vendor_names)."""
    return sg.load()["_rx"]


def _plain(text: str) -> str:
    """Text with curly apostrophes made straight, for matching only (quotes stay verbatim)."""
    return (text or "").translate(CURLY)


def _vendor(name: str) -> bool:
    """An affiliate, referral or partner software or network: a vendor, never the company that runs the program."""
    return bool(name) and bool(_rx()["vendor_names"].search(str(name).lower()))


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text or "") if s and s.strip()]


def _clip(text: str, width: int = 400) -> str:
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text
    return text[:width].rsplit(" ", 1)[0]


def _timeframe(days: int) -> str:
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    return "year"


def _int(value) -> int:
    """A count as an int; 0 when the field is not a number ('12 comments', None, inf)."""
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _valid_epoch(value) -> int | None:
    """Epoch seconds from a number (milliseconds are divided down), or None when it is not a plausible post date."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    n = float(value)
    if n != n or n <= 0:
        return None
    if n > 1e11:
        n /= 1000.0
    if MIN_EPOCH <= n <= MAX_EPOCH:
        return int(n)
    return None


def _epoch(raw: dict) -> int | None:
    for key in ("created_utc", "created"):
        epoch = _valid_epoch(raw.get(key))
        if epoch is not None:
            return epoch
    iso = str(raw.get("created_at_iso") or "")
    if iso:
        try:
            return _valid_epoch(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError, OverflowError, OSError):
            return None
    return None


def _norm(raw: dict) -> dict | None:
    """A search hit as a flat record. None when the sub, id or date cannot be read (never invented)."""
    try:
        link = str(raw.get("permalink") or raw.get("url") or "")
        m = re.search(r"/r/([A-Za-z0-9_]{2,30})/comments/([a-z0-9]{4,12})", link, re.I)
        if m:
            sub, pid = m.group(1), m.group(2).lower()
        else:
            pid = str(raw.get("id") or "").removeprefix("t3_").lower()
            sub = str(raw.get("subreddit") or "")
        epoch = _epoch(raw)
        if not re.fullmatch(r"[a-z0-9]{4,12}", pid) or not re.fullmatch(r"[A-Za-z0-9_]{2,30}", sub) or epoch is None:
            return None
        body = raw.get("selftext")
        author = raw.get("author")
        return {"id": pid, "sub": sub, "title": " ".join(str(raw.get("title") or "").split()),
                "body": None if body is None else str(body), "link": str(raw.get("url") or ""),
                "author": str(author) if author else None, "comments": _int(raw.get("num_comments")), "epoch": epoch,
                "iso": datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()}
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


def _external_link(p: dict) -> bool:
    return bool(p["link"]) and "reddit.com" not in p["link"] and "redd.it" not in p["link"]


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())[:80]


def _buyer_title(title: str) -> bool:
    return bool(c.buyer_language(title) or OWN_PROGRAM_FULL.search(title)
                or (FIRST_PERSON.search(title) and PROGRAM_TERM.search(title)))


def _sub_drop(sub: str, title: str) -> str | None:
    """Drops that depend on the subreddit alone (and the title for r/affiliatemarketing)."""
    name = (sub or "").lower()
    if name in NON_US_SUBS:
        return "not US"
    if name == "affiliatemarketing" and not HIRE_TITLE.search(title):
        return "affiliate-side subreddit"
    return None


def _seeks_program(sentence: str) -> bool:
    """The poster is looking for a program to join, not one they own."""
    if JOINER.search(sentence) or SEEKER_OTHER.search(sentence):
        return True
    if SEEKER_NEED.search(sentence):
        founder_building = bool(PRODUCT_CONTEXT.search(sentence)) and not JOIN_WORD.search(sentence)
        return not founder_building
    return False


def _drop_reason(title: str, body: str, sub: str, *, full: bool) -> str | None:
    """Why the post is not a brand talking about its own program. `full` is False for the title-only check made before a
    body read, when the rules that need the whole post are skipped."""
    reason = _sub_drop(sub, title)
    if reason:
        return reason
    text = _plain(f"{title}\n{body or ''}")
    reason = c.text_gate(text)
    if reason:
        return reason
    sentences = _sentences(text)
    if any(_seeks_program(s) for s in sentences):
        return "seeker"
    for label, rx in (("affiliate-side", AFFILIATE_SIDE), ("consumer referral or mlm", CONSUMER),
                      ("traffic arbitrage", TRAFFIC), ("for hire", FOR_HIRE), ("seller", SELLER),
                      ("research or poll", RESEARCH)):
        if rx.search(text):
            return label
    if any(EARNINGS.search(s) and not OWN_PROGRAM_FULL.search(s) for s in sentences):
        return "affiliate-side"
    if full and AFFILIATE_MANAGER.search(text) and not OWN_PROGRAM_FULL.search(text):
        return "hire without own program"
    return None


def _program_quote(text: str, author: str | None = None) -> str | None:
    """The verbatim sentence in which the poster names their own program. It must match config program_post, so that
    lead_score can count it as proof of a program (sg.is_program_post). A sentence that introduces the author by name
    or carries their username is skipped, so a stored quote never names a person."""
    rx = _rx()["program_post"]
    handle = (author or "").strip()
    handle_rx = re.compile(rf"(?<!\w){re.escape(handle)}(?!\w)", re.I) if len(handle) >= 4 else None
    for sentence in _sentences(text):
        plain = _plain(sentence)
        if USER_MENTION.search(plain) or not rx.search(plain):
            continue
        if SELF_INTRO.search(plain) or (handle_rx and handle_rx.search(plain)):
            continue
        if FIRST_PERSON.search(plain) or c.buyer_language(plain):
            return _clip(sentence)
    return None


def _segment_before(text: str, idx: int) -> str:
    head = text[:idx]
    cut = max(head.rfind(ch) for ch in ".!?\n")
    return head[cut + 1:][-100:]


def _segment_after(text: str, idx: int) -> str:
    tail = text[idx:]
    cut = [i for i in (tail.find(ch) for ch in ".!?\n") if i >= 0]
    return tail[:min(cut)][:40] if cut else tail[:40]


def _own_claim(text: str, domain: str) -> bool:
    """True when the text claims the domain as the poster's own: 'our app acme.io', 'I built acme.io',
    'acme.io is mine'. Not 'we pay affiliates via acme.io', 'our competitor acme.io' or 'I use acme.io'."""
    text = _plain(text)
    rx = re.compile(rf"(?<![\w-])(?:https?://)?(?:[\w-]+\.)*{re.escape(domain)}(?![\w-])", re.I)
    for m in rx.finditer(text or ""):
        before = _segment_before(text, m.start())
        after = _segment_after(text, m.end())
        if NEG_CLAIM.search(before) or NEG_CLAIM.search(after):
            continue
        if POSS_END.search(before) or VERB_END.search(before) or AFTER_CLAIM.search(after):
            return True
    return False


def _brand_claim(text: str, brand: str) -> bool:
    """True when a first-person sentence of the post says the brand is the poster's own: the brand follows a possessive
    product noun or a build verb ('our app Acme', 'I'm the developer of Acme'), or is followed by 'is mine/ours'.
    Adoption, comparison and vendor wording reject it ('we switched to Rewardful', 'Our referral program: Acme')."""
    toks = c.tokens(brand)
    if not toks or _vendor(brand):
        return False
    rx = re.compile(r"(?<![a-z0-9])" + r"[^a-z0-9]*".join(re.escape(t) for t in toks) + r"(?![a-z0-9])", re.I)
    for sentence in _sentences(_plain(text)):
        if NEG_BRAND.search(sentence) or ADOPT.search(sentence) or not FIRST_PERSON.search(sentence):
            continue
        for m in rx.finditer(sentence):
            before, after = sentence[:m.start()], sentence[m.end():]
            if POSS_BRAND.search(before) or VERB_END.search(before) or AFTER_CLAIM.search(after):
                return True
    return False


def _page_brands(html: str) -> list[str]:
    """Brand names a homepage gives itself: the parts of its <title> and og:site_name that are not generic words."""
    head = (html or "")[:200_000]
    raw = [m for m in TITLE_RX.findall(head)] + [m for m in OG_RX.findall(head)]
    out: list[str] = []
    for item in raw:
        for part in BRAND_SPLIT.split(htmllib.unescape(item)):
            part = part.strip()
            toks = c.tokens(part)
            if not toks or len(c.norm(part)) < 4 or len(toks) > 4 or all(t in GENERIC for t in toks):
                continue
            if part not in out:
                out.append(part)
    return out


def _domains(text: str, link: str = "") -> list[str]:
    """Company domains the text (or external link) names, in order. Platform and directory hosts are removed."""
    found: list[str] = []

    def add(domain: str | None) -> None:
        if domain and domain not in found and domain not in DIRECTORY_HOSTS:
            found.append(domain)
    for m in URL_RX.finditer(text or ""):
        add(c.host_of(m.group(0).rstrip(".,;:!?")))
    for m in BARE_RX.finditer(text or ""):
        add(c.host_of(m.group(1)))
    if link and "reddit.com" not in link and "redd.it" not in link:
        add(c.host_of(link))
    return found


def _is_top_level(cm: dict) -> bool:
    """A comment that sits directly on the post. Reddit's parent_id is t3_<post> for those and t1_<comment> for replies;
    depth 0 is the same thing. A comment with neither field is not treated as top-level."""
    parent = cm.get("parent_id")
    if isinstance(parent, str) and parent:
        return parent.startswith("t3_")
    depth = cm.get("depth")
    return isinstance(depth, int) and not isinstance(depth, bool) and depth == 0


def _op_top_level(comments: list, op: str) -> list[str]:
    """Bodies of the OP's top-level comments. Replies are never read: an OP reply names a domain in a thread the buyer
    did not start, and the OP's identity on a reply is not evidence about the post."""
    op_name = str(op or "").lower()
    if op_name in OP_UNKNOWN:
        return []
    out: list[str] = []
    for cm in comments or []:
        if not isinstance(cm, dict) or not _is_top_level(cm):
            continue
        author = str(cm.get("author") or "").lower()
        body = cm.get("body")
        if author and author == op_name and body:
            out.append(str(body))
    return out


async def _anchor(ctx, text: str, link: str = "") -> tuple[str, str | None] | None:
    """(domain, company name or None) for the first named domain the text claims or whose homepage the text describes,
    or None. The homepage must be live and not parked; affiliate-software vendors are never accepted."""
    text = _plain(text)
    first_person = any(FIRST_PERSON.search(s) for s in _sentences(text))
    for domain in [d for d in _domains(text, link) if not _vendor(d)][:3]:
        own = _own_claim(text, domain)
        if not own and not first_person:
            continue
        page = await ctx.get(f"https://{domain}/", role="home")
        if page is None or not isinstance(page.status, int) or page.status >= 400 or c.parked(page.html):
            continue
        if c.host_of(page.final_url) is None:  # redirected onto a platform host (link-in-bio, marketplace, portal)
            continue
        brands = [b for b in _page_brands(page.html) if not _vendor(b)]
        if own:
            brand = next((b for b in brands if _brand_claim(text, b)), None)
            return domain, brand
        for brand in brands:
            if _brand_claim(text, brand):
                return domain, brand
    return None


# ---- the run -------------------------------------------------------------------------------------------------------

class _Run:
    def __init__(self, ctx):
        self.ctx = ctx
        self.out = False            # ScrapeCreators credits exhausted: stop calling, keep what was found
        self.reads = 0
        self.comment_reads = 0
        self.drops: dict[str, int] = {}
        self.counted: set[str] = set()

    def drop(self, reason: str) -> None:
        self.drops[reason] = self.drops.get(reason, 0) + 1

    def drop_post(self, pid: str, reason: str) -> None:
        """Counts a post's drop once, however many search passes return it."""
        if pid not in self.counted:
            self.counted.add(pid)
            self.drop(reason)

    async def call(self, path: str, params: dict):
        if self.out or not self.ctx.budget.ok():
            return None
        try:
            return await sg.sc_get(self.ctx.sc, path, params, source="reddit")
        except OutOfCredits:
            self.out = True
            self.ctx.say("reddit: ScrapeCreators credits exhausted, keeping what was found")
            return None

    async def search(self, sub: str, query: str, timeframe: str, pages: int) -> list[dict]:
        out: list[dict] = []
        after = None
        for _ in range(max(1, pages)):
            if sub:
                data = await self.call("/v1/reddit/subreddit/search", {
                    "subreddit": sub, "query": query, "sort": "new", "timeframe": timeframe, "cursor": after})
            else:
                data = await self.call("/v1/reddit/search", {
                    "query": query, "filter": "posts", "sort": "new", "timeframe": timeframe, "after": after})
            if not isinstance(data, dict):
                break
            out += [p for p in data.get("posts") or [] if isinstance(p, dict)]
            after = data.get("after") or data.get("cursor")
            if not after:
                break
        return out

    async def read_post(self, pid: str) -> dict | None:
        data = await self.call("/v1/reddit/post", {"url": f"https://www.reddit.com/comments/{pid}/"})
        if not isinstance(data, dict):
            return None
        return data["post"] if isinstance(data.get("post"), dict) else data

    async def op_anchor(self, p: dict) -> tuple[str, str | None] | None:
        if not p["author"] or str(p["author"]).lower() in OP_UNKNOWN:
            return None
        data = await self.call("/v1/reddit/post/comments",
                               {"url": f"https://www.reddit.com/r/{p['sub']}/comments/{p['id']}/"})
        if not isinstance(data, dict):
            return None
        comments = data.get("comments")
        for body in _op_top_level(comments if isinstance(comments, list) else [], p["author"]):
            found = await _anchor(self.ctx, body)
            if found:
                return found
        return None


def _pool(raw: list[dict], ctx, run: _Run) -> list[dict]:
    """Search hits as records: deduped by id, inside the look-back window, with a program term in the title. Posts the
    subreddit excludes are removed before the title dedupe, so a cross-post in an excluded sub cannot hide the copy the
    buyer posted elsewhere. Buyer-looking titles first, then newest."""
    seen_id: set[str] = set()
    seen_title: set[str] = set()
    out: list[dict] = []
    for item in raw:
        p = _norm(item)
        if p is None or p["id"] in seen_id:
            continue
        seen_id.add(p["id"])
        if not c.within_days(p["iso"], ctx.days, ctx.now) or not TITLE_TERM.search(p["title"]):
            continue
        reason = _sub_drop(p["sub"], p["title"])
        if reason:
            run.drop_post(p["id"], reason)
            continue
        key = _title_key(p["title"])
        if key in seen_title:
            continue
        seen_title.add(key)
        out.append(p)
    out.sort(key=lambda p: (not _buyer_title(p["title"]), -p["epoch"]))
    return out


def _merge(p: dict, full: dict) -> dict:
    q = dict(p)
    q["title"] = " ".join(str(full.get("title") or p["title"]).split())
    q["body"] = str(full.get("selftext") or "")
    author = full.get("author") or p["author"]
    q["author"] = str(author) if author else None
    q["comments"] = _int(full.get("num_comments")) or p["comments"]
    return q


async def collect(ctx: SourceContext) -> list[sg.Candidate]:
    cfg = ctx.cfg
    run = _Run(ctx)
    timeframe = _timeframe(ctx.days)
    max_bodies = int(cfg.get("max_body_reads", 8))
    max_comments = int(cfg.get("max_comment_reads", 5))
    # Pass one searches down to a reserve, so body reads still have credits. Pass two spends what the reads left on
    # the remaining searches (their hits often carry the body, which costs nothing to read). The reserve is at most
    # half the starting budget, so a small run still searches.
    reserve = min(int(cfg.get("reserve_reads", 6)), max(0, ctx.budget.left) // 2)
    queue = [(str(sub or ""), str(query)) for sub, query in cfg.get("searches") or []]
    raw: list[dict] = []
    kept: list[tuple[dict, str]] = []
    done: set[str] = set()
    pool_size = 0

    async def search_until(floor: int) -> None:
        while queue and not run.out and ctx.budget.left > floor:
            sub, query = queue.pop(0)
            hits = await run.search(sub, query, timeframe, int(cfg.get("max_pages", 1)))
            ctx.say(f"reddit: {('r/' + sub) if sub else 'all of Reddit'} {query}: {len(hits)} hits")
            raw.extend(hits)

    async def consider(p: dict) -> None:
        need_read = not _external_link(p) and not p["body"]
        if need_read:
            reason = _drop_reason(p["title"], "", p["sub"], full=False)
            if reason:
                run.drop(reason)
                done.add(p["id"])
                return
            if run.out or run.reads >= max_bodies or not ctx.budget.ok():
                return  # not marked done: a later pass may still read it
            full = await run.read_post(p["id"])
            run.reads += 1
            done.add(p["id"])
            if full is None:
                run.drop("body unreadable")
                return
            p = _merge(p, full)
        else:
            done.add(p["id"])
        reason = _drop_reason(p["title"], p["body"] or "", p["sub"], full=True)
        if reason:
            run.drop(reason)
            return
        quote = _program_quote(f"{p['title']}\n{p['body'] or ''}", p["author"])
        if quote is None:
            run.drop("no program sentence")
            return
        kept.append((p, quote))

    async def read_and_keep() -> None:
        nonlocal pool_size
        pool = _pool(raw, ctx, run)
        pool_size = max(pool_size, len(pool))
        for p in pool:
            if p["id"] in done:
                continue
            try:
                await consider(p)
            except Exception:  # one malformed or failed record must not discard the rest of the run
                run.drop("bad record")
                done.add(p["id"])

    await search_until(reserve)
    await read_and_keep()
    await search_until(0)
    await read_and_keep()

    cands: list[sg.Candidate] = []
    seen_domains: set[str] = set()
    for p, quote in kept:
        try:
            found = await _anchor(ctx, f"{p['title']}\n{p['body'] or ''}", p["link"])
            if found is None and p["comments"] > 0 and run.comment_reads < max_comments and not run.out \
                    and ctx.budget.ok():
                run.comment_reads += 1
                found = await run.op_anchor(p)
        except Exception:
            run.drop("bad record")
            continue
        if found is None:
            run.drop("unresolved")
            continue
        domain, brand = found
        if domain in seen_domains:
            run.drop("same company")
            continue
        seen_domains.add(domain)
        url = f"https://www.reddit.com/r/{p['sub']}/comments/{p['id']}/"
        signal = sg.Signal("community_post", url, quote, p["iso"], None, None, "reddit")
        cands.append(sg.Candidate("affiliate", signal, company_name=brand, domain=domain, require_us=True))

    summary = ", ".join(f"{k} {v}" for k, v in sorted(run.drops.items()))
    ctx.say(f"reddit: {pool_size} in window, {len(kept)} about their own program, {len(cands)} resolved"
            + (f"; dropped: {summary}" if summary else "") + f"; credits left {ctx.budget.left}")
    return cands
