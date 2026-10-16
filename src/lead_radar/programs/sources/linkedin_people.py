"""Posts by people about THEIR OWN company's affiliate / partner / referral program, and funder staff recruiting ISOs or
brokers (source "linkedin_people").

Company-page posts are growth.collect_posts' job; this source keeps posts whose author is a person (a /in/ profile):
founders, marketers, partnership and affiliate managers. Per run:

1. Search posts by phrase (ScrapeCreators /v1/linkedin/search/posts, date-windowed). The first page of every phrase
   runs first, best phrase first, but never spends the credits reserved for profile lookups. Further pages run only
   while the budget can still pay for the lookups the survivors need.
2. Gate each post on its text alone: person author, first-person ownership of the program (a possessive
   "<Name>'s affiliate program" is never ownership), buyer language, an affiliate / referral / commission term, not
   advice, not a seller, recruiter, vertical, job repost, seeker or side project, US only. ISO and broker wording
   routes to the finance list and must name a business-funding term.
3. Anchor the company, stopping at the first that works:
   (a) free: a brand domain tied to the program sentence whose homepage names the brand, or a 'role at Company'
       phrase in the post;
   (b) one Google search on the author's public profile slug (1 credit; cached per slug; capped per run). Accepted only
       when the result URL holds the slug, the title starts with the author's name, the title/description names exactly
       one company, and the role is an owner-type role (founder, CEO, marketing, growth, partnerships, affiliate...).
   A company is never guessed.
Candidates are require_us: growth.process lists one only when LinkedIn confirms a US company page.

Signal.url is the canonical feed/update URL of the post (no author handle), Signal.quote is one sentence of the post
with no person names, and Signal.person_name / person_url are stored as contact data. They are never logged or printed.
"""
from __future__ import annotations

import html as htmllib
import re
import unicodedata
from urllib.parse import unquote

from .. import signals as sg
from . import common

NAME = "linkedin_people"
SEARCH = "/v1/linkedin/search/posts"
GOOGLE = "/v1/google/search"

DEFAULTS = {
    "enabled": True,
    "credits": 25,
    "http_cap": 40,              # homepage checks for brand domains named in posts (free GETs)
    "max_author_lookups": 12,    # Google slug lookups per run (1 credit each)
    # (list, phrase, pages). The phrases are the evidence-backed set from the 2026-10-10 probe: a phrase with no
    # results (404) costs 0 credits. Dropped: 'partner program apply now' (campus/design-partner noise), and
    # '"partnership manager" hiring' (8 of 10 hits were non-affiliate roles).
    # Four of these phrases are also run by growth.collect_posts; that duplicate spend needs a shared per-run search
    # memo (growth.py is outside this source's files).
    "queries": [
        {"list": "affiliate", "q": '"our affiliate program"', "pages": 2},
        {"list": "affiliate", "q": '"join our affiliate program"', "pages": 1},
        {"list": "affiliate", "q": '"launched our affiliate program"', "pages": 1},
        {"list": "affiliate", "q": '"our affiliates" commission', "pages": 1},
        {"list": "affiliate", "q": '"affiliate manager" hiring', "pages": 2},
        {"list": "affiliate", "q": '"looking for affiliates"', "pages": 1},
        {"list": "affiliate", "q": '"recruiting affiliates"', "pages": 1},
        {"list": "affiliate", "q": '"grow our affiliate program"', "pages": 1},
        {"list": "affiliate", "q": '"referral partners" "we are looking for"', "pages": 1},
        {"list": "affiliate", "q": '"affiliate program is now open"', "pages": 1},
        {"list": "finance", "q": '"now accepting new ISOs"', "pages": 1},
        {"list": "finance", "q": '"ISO partners" join', "pages": 1},
    ],
}

# ---- text gates (post text only; the author's headline is not in the search payload) ---------------------------

# first-person ownership of the program: 'our <program noun>', 'my company's <noun>', or a first-person recruit/launch/
# hire verb next to a program noun ("we're recruiting affiliates"). A possessive "<Name>'s affiliate program" is NOT
# ownership: it is usually a third-party brand (an affiliate promoting someone else's program).
OWN = re.compile(
    r"\bour\b[^.!?\n]{0,50}?\b(?:affiliates?|referrals?|partners?|ambassadors?|resellers?|isos?|brokers?|creators?)\b"
    r"|\bmy (?:company|brand|startup|team|business|agency)'?s?\b[^.!?\n]{0,50}?\b(?:affiliates?|referrals?|partners?|"
    r"ambassadors?|isos?|brokers?)\b"
    r"|\bwe(?:'re|\s+are|\s+have|\s+just|\s+now|\s+also|\s+ve|'ve)?\s+(?:now\s+|just\s+|also\s+|currently\s+|actively\s+|"
    r"officially\s+)?(?:recruit\w*|hiring|looking|accepting|seeking|launch\w*|open\w*|grow\w*|scal\w*|expand\w*|invit\w*)"
    r"\b[^.!?\n]{0,60}?\b(?:affiliates?|referrals?|partners?|ambassadors?|resellers?|isos?|brokers?|creators?)\b",
    re.I)

# commentary about someone else's program, or the author joining one (not the author's own program)
THIRD = re.compile(
    r"\b(?:we|i)(?:\s+(?:just|have|had|recently|finally))?\s+(?:joined|became|signed up|applied|were accepted|"
    r"got accepted|are now (?:an? )?(?:official )?partners?)\b"
    r"|\bjoined (?:the|their|his|her|its|a)\b"
    r"|\b(?:congrat\w*|shout ?outs?|props to|kudos|love (?:how|this)|impressed by)\b"
    r"|\b(?:certified|official (?:partner|reseller)|(?:aws|google|microsoft|hubspot|shopify|meta|salesforce) partner)\b"
    r"|\bpartner of\b", re.I)

# advice, or a client / DM pitch rather than a brand's own program
ADVICE = re.compile(
    r"\b(?:how to|tips?|ways to|steps to|mistakes|lessons|guide|checklist|here'?s how|here are \d|you should|"
    r"you need to|you'll need|most (?:brands|companies|founders|businesses)|top \d+|"
    r"\d+ (?:tips|ways|steps|mistakes|reasons|lessons|things)|what (?:is|are) an? (?:affiliate|referral)|"
    r"best (?:affiliate|referral) programs?|your (?:affiliate|referral|partner) (?:program|strategy|channel|marketing)|"
    r"if you(?:'re| are) (?:a|an|looking|thinking)|dm me|comment \w+|my clients?|for (?:our|my) clients?|"
    r"clients? (?:we|who))\b", re.I)

# job-board repost wording (the author is an aggregator, so the company would be wrong)
REPOST = re.compile(
    r"(?i:\b(?:job alerts?|new job|view (?:the |this )?(?:job|role|position|opening)|job board|vacancy|"
    r"job opening at|apply on (?:indeed|glassdoor|linkedin|wellfound)|posted (?:a )?(?:new )?job)\b)|\bjobs? at [A-Z]")

SELLER_TEXT = re.compile(
    r"\b(?:agency|agencies|consultant\w*|consulting|fractional|freelanc\w*|done[- ]for[- ]you|our clients?|my clients?|"
    r"clients? we|we (?:manage|run|build|launch|grow|scale|set up|setup) (?:affiliate|referral|partner)\w* "
    r"(?:programs?|for)|affiliate management|opm|outsourc\w*|we (?:help|helped|work with|partner with) "
    r"(?:brands?|companies|businesses|creators|clients?)|helping (?:brands|businesses|companies)|"
    r"book a (?:free )?(?:call|strategy|discovery)|coaching|mentorship)\b", re.I)

# the author's own seeking or past-tense career talk ("open to new opportunities", "my next role", "I grew ...")
SEEKER = re.compile(
    r"\b(?:open to (?:new )?(?:opportunities|roles?|work|offers)|"
    r"(?:looking|searching) for (?:my |a |an )?(?:next|new)\b(?:\s+\w+){0,2}\s+(?:role|job|opportunity|position|gig)|"
    r"my next (?:\w+ ){0,2}(?:role|job|opportunity|position|gig)|formerly|"
    r"former (?:affiliate|partner|referral|head|manager|director|growth|marketing)|previously (?:at|with)|laid off|"
    r"my last (?:role|job|company)|I (?:grew|built|ran|led|scaled))\b", re.I)

SIDE_PROJECT = re.compile(
    r"\b(?:side (?:project|hustle|business|gig)|personal (?:project|brand|blog|newsletter)|hobby|weekend project|"
    r"for fun)\b", re.I)

VERTICAL_EXTRA = re.compile(
    r"\b(?:revshare|cpa network|igaming|i-gaming|sportsbooks?|betting|casinos?|poker|sweepstakes)\b", re.I)

NON_US_WORD = re.compile(
    r"\b(?:lagos|nigeria\w*|naira|dubai|uae|abu dhabi|saudi\w*|riyadh|doha|qatar|egypt\w*|cairo|kenya|nairobi|"
    r"cape town|johannesburg|south africa|africa|mena|emea|apac|latam|latin america|hyderabad|bangalore|bengaluru|"
    r"mumbai|delhi|chennai|pune|india|pakistan\w*|karachi|philippines|manila|vietnam\w*|indonesia\w*|singapore|"
    r"hong kong|bangkok|malaysia\w*|kuala lumpur|australia\w*|sydney|melbourne|new zealand|canad\w*|toronto|"
    r"vancouver|montreal|uk|united kingdom|london|manchester|edinburgh|britain|british|england|scotland|ireland|"
    r"irish|dublin|europe\w*|eu|germany|berlin|munich|france|paris|spain|madrid|barcelona|portugal|lisbon|porto|"
    r"italy|milan|rome|netherlands|amsterdam|belgium|sweden|stockholm|denmark|norway|finland|poland|warsaw|romania|"
    r"bucharest|bulgaria|sofia|greece|athens|turkey|istanbul|ukraine|kyiv|armenia\w*|yerevan|mali|brazil\w*|"
    r"s[aã]o paulo|mexico city|japan\w*|tokyo|korea\w*|seoul|china|shanghai|beijing)\b", re.I)
NON_US_CUR = re.compile(r"[£€₦₹]|\b(?:AED|GBP|EUR|INR|NGN|ZAR|AUD)\b|\b(?:lakhs?|crores?)\b")
ISO_STANDARD = re.compile(r"\bISO\s?(?:9001|27001|14001|13485|45001|20000|22301|37001)\b", re.I)

# the program is about affiliates, referrals or commission. ISO and broker wording is NOT an affiliate term: it routes
# to the finance list, where a business-funding term must be present.
AFFILIATE_TERM = re.compile(
    r"\b(?:affiliates?|referrals?|referral partners?|commissions?|affiliate managers?|per (?:sale|referral|lead|funded)|"
    r"rev[- ]?shares?|residuals?|upfront)\b", re.I)
COMMISSION = re.compile(r"\b(?:commissions?|per sale|earn|affiliates?|referrals?|payouts?|paid)\b", re.I)
NONCOMMISSION_AMBASSADOR = re.compile(
    r"\b(?:campus|student|college|university|youth|community|developer|city|club|chapter|volunteer)\s+ambassadors?\b",
    re.I)
HIRING = re.compile(
    r"\b(?:we(?:'re| are) hiring|now hiring|hiring an? |join (?:our|the) team|apply (?:now|today|here)|job opening|"
    r"open role|we(?:'re| are) looking for an? )\b", re.I)
AFF_ROLE = re.compile(r"\b(?:affiliates?|referrals?|referral partners?|affiliate managers?)\b", re.I)
FIN_PARTNER = re.compile(r"\b(?:isos?|brokers?)\b", re.I)
# funder posts that are about another regulated product line (mortgage, insurance, real estate, freight)
OFF_FIN = re.compile(
    r"\b(?:mortgages?|home loans?|insurance|insurers?|real estate|realtors?|freight|logistics|brokerage|auto loans?|"
    r"student loans?)\b", re.I)
# a sentence that welcomes, meets or congratulates someone is about that person, not the program
NAME_CUE = re.compile(r"\b(?:welcom\w*|meet|congrat\w*)\b", re.I)

# ---- company anchors ------------------------------------------------------------------------------------------------

FREEMAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com", "proton.me",
            "protonmail.com", "me.com", "live.com", "msn.com", "mail.com", "gmx.com", "ymail.com"}
TOOL_DOMAINS = {"apollo.io", "systeme.io", "publer.com", "calendly.com", "hubspot.com", "notion.so", "canva.com",
                "loom.com", "typeform.com", "beehiiv.com", "mailchimp.com", "klaviyo.com", "gumroad.com", "kit.com",
                "convertkit.com", "zapier.com", "make.com", "clickup.com", "monday.com", "airtable.com",
                "webflow.io", "lnk.bio", "linkin.bio"}
PROGRAM_WORDS = {"affiliate", "affiliates", "program", "programs", "programme", "team", "partners", "partner",
                 "referral", "referrals", "network", "community", "launch", "week", "day", "event", "summit",
                 "conference", "friday", "monday", "black", "cyber", "marketplace", "ambassador", "ambassadors",
                 "creator", "creators", "influencer", "influencers", "manager", "director", "founder", "ceo", "head",
                 "growth", "marketing"}
GENERIC_CO = {"we", "our", "us", "the", "this", "my", "me", "you", "linkedin", "google", "meta", "instagram", "tiktok",
              "facebook", "youtube", "twitter", "x", "scale", "checkout", "work", "home", "today", "shopify", "amazon",
              "reddit", "stripe", "notion", "hubspot", "salesforce", "slack", "zoom", "apple", "microsoft", "openai",
              "self-employed", "self employed", "freelance", "freelancer", "independent", "stealth", "stealth startup",
              "n/a", "none", "student", "retired", "family", "various", "multiple", "clients", "client", "my own",
              "own business", "consultant", "shop", "store", "online", "official", "website", "welcome", "partners",
              "affiliate", "affiliates"}
REJECT_TAG = re.compile(
    r"\b(?:help(?:s|ing|ed)?|consult\w*|agency|agencies|freelanc\w*|coach(?:es|ing)?|mentor\w*|"
    r"advis(?:or|ors|er|ers|ory)|strategist|fractional|recruit\w*|talent|staffing|headhunt\w*|for hire|open to work|"
    r"self[- ]employed|outsourc\w*|we (?:work|grow|build)|i (?:help|grow|build|launch|coach|consult)|"
    r"founders who|taught|teach\w*|operator)\b", re.I)
NAME_RUN = r"[A-Z][\w&\-]*(?:\s+[A-Z][\w&\-]*){0,3}"
ROLE_WORDS = (r"(?:founder|co-?founder|ceo|cmo|coo|owner|president|head of [a-z]+(?: [a-z]+)?|"
              r"director of [a-z]+(?: [a-z]+)?|vp,? (?:of )?[a-z]+|partnerships? (?:manager|lead|director)|"
              r"affiliate (?:manager|lead|director)|marketing (?:manager|director|lead)|growth (?:lead|manager))")
# a first-person company claim in the post text: 'Founder at Acme', 'here at Acme', 'We at Acme'. Possessive forms
# ("Acme's affiliate program") are not used: they name a third party as often as the author's company.
ROLE_AT = re.compile(
    r"(?i:\b" + ROLE_WORDS + r"\s+(?:at|@)\s+)(" + NAME_RUN + r")"
    r"|(?i:\b(?:here at|we at|join us at)\s+)(" + NAME_RUN + r")")
# owner-type roles: the anchor must be the company the author runs or markets, not one they work at as an engineer
OWNER_ROLE = re.compile(
    r"\b(?:founder|co-?founder|ceo|cmo|coo|owner|president|managing director|general manager|"
    r"(?:vp|vice president|head|director|chief)\b[^|·]{0,30}?\b(?:marketing|growth|partnerships?|affiliates?|referrals?|"
    r"business development|brand|e-?commerce|sales|revenue)"
    r"|(?:partnerships?|affiliates?|referrals?|marketing|growth|brand|revenue)\s+(?:manager|lead|director|head|specialist|"
    r"owner))\b", re.I)
REJECT_ROLE = re.compile(
    r"\b(?:engineer\w*|developer|programmer|software|designer|intern|student|support|customer success|account executive|"
    r"sdr|bdr|sales (?:rep|representative|associate|engineer)|legal|counsel|accountant|recruit\w*|human resources|hr|"
    r"data|scientist|analyst|side project|personal|freelanc\w*|independent|side hustle)\b", re.I)
BRAND_SPLIT = re.compile(r"\s*[|–—·]\s*|\s+-\s+|:\s+")
EXPERIENCE = re.compile(r"Experience:\s*([^·|\n]+)", re.I)
PERSON = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/in/([^/?#\s]+)", re.I)
ACTIVITY = re.compile(r"activity[:\-](\d{15,})")
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
THANKS = re.compile(r"\b(?:thanks?|thank you|shout ?outs?|congrat\w*|big shout)\b", re.I)
# two or more capitalised words in a row: a person's name or a brand name (checked against the anchor company)
RUN = re.compile(r"\b[A-Z][\w&'\-]*(?:\s+[A-Z][\w&'\-]*)+")
STOP = {"the", "our", "we", "my", "this", "that", "new", "now", "join", "today", "and", "for", "with", "from", "just",
        "all", "you", "your", "a", "an", "to", "in", "of", "on", "is", "are", "it", "its", "here", "there", "what", "how",
        "why", "who", "if", "as", "at", "by", "or", "but", "so", "not", "be", "has", "have", "out", "up", "more", "most",
        "very", "any", "every", "one", "two", "they", "them", "their", "his", "her", "she", "he", "i", "me", "us", "when",
        "then", "also", "only", "even", "please", "hi", "hey", "earn", "get", "sign", "apply", "launch", "launched"}


# ---- helpers --------------------------------------------------------------------------------------------------------

def _window(days: int) -> str:
    return "last-day" if days <= 1 else "last-week" if days <= 7 else "last-month" if days <= 31 else "last-year"


def _fold(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower())


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def _list(v) -> list:
    return v if isinstance(v, list) else []


def _slug(url: str | None) -> str | None:
    m = PERSON.search(unquote(url or ""))
    return m.group(1).strip("/").lower() if m else None


def _activity(post: dict) -> str | None:
    m = ACTIVITY.search(f"{post.get('activityUrn') or ''} {post.get('url') or ''}")
    return m.group(1) if m else None


def _name(raw: str | None) -> str:
    return re.sub(r"\s+", " ", (raw or "").split(",")[0]).strip()


def _name_tokens(raw: str | None) -> list[str]:
    return [t for t in re.findall(r"[^\W\d_]+", _name(raw)) if len(t) >= 3]


def _buyer(text: str) -> bool:
    """Buyer language judged per clause: a ';' ends a clause, so 'program at Acme; now open' is not one buyer phrase."""
    return any(common.buyer_language(part) for part in re.split(r";", text or ""))


def _unknown_runs(sentence: str) -> list[str]:
    """Capitalised word runs that are not stop words or program words: a person's or a brand's name."""
    out = []
    for m in RUN.finditer(sentence or ""):
        words = [_fold(w) for w in m.group(0).split()]
        if any(w and w not in STOP_FOLD and w not in PROGRAM_FOLD for w in words):
            out.append(m.group(0))
    return out


STOP_FOLD = {_fold(w) for w in STOP}
PROGRAM_FOLD = {_fold(w) for w in PROGRAM_WORDS}


def _quote_names_ok(sentence: str, company: str | None, domain: str | None) -> bool:
    """True when every name run in the quote is the anchor company or its brand label (no third-party names)."""
    label = _fold((domain or "").split(".")[0])
    for run in _unknown_runs(sentence):
        f = _fold(run)
        if f and ((company and f in _fold(company)) or (label and f in label)):
            continue
        return False
    return True


def _program_sentence(text: str, names: list[str]) -> str | None:
    """One sentence of the post that carries the program (buyer language, ideally with ownership), with no author name,
    no @-mention, no thank-you / shout-out / welcome wording. Verbatim, at most 400 characters. Prefers a sentence with
    no name run. None when nothing is clean."""
    found = []
    for raw in SENTENCE.split(text or ""):
        s = re.sub(r"\s+", " ", raw).strip(" -•|")
        if len(s) < 12 or "@" in s or THANKS.search(s) or NAME_CUE.search(s):
            continue
        if any(re.search(rf"\b{re.escape(t)}\b", s, re.I) for t in names):
            continue
        if _buyer(s):
            found.append(s[:400])
    if not found:
        return None
    return min(found, key=lambda s: (not OWN.search(s), bool(_unknown_runs(s))))


def _screen(text: str, query_list: str, cfg: dict) -> tuple[str, str | None]:
    """(list, drop reason or None) for one post's text."""
    rx = cfg["_rx"]
    if not text:
        return query_list, "empty"
    gate = common.text_gate(text)
    if gate:
        return query_list, gate
    checks = [("vertical", VERTICAL_EXTRA), ("vertical", rx["verticals"]), ("seller", rx["sellers"]),
              ("seller", SELLER_TEXT), ("vendor", rx["vendors"]), ("third_party", THIRD), ("advice", ADVICE),
              ("repost", REPOST), ("non_us", NON_US_WORD), ("non_us", NON_US_CUR), ("iso_standard", ISO_STANDARD),
              ("seeker_post", SEEKER), ("side_project", SIDE_PROJECT)]
    for reason, pattern in checks:
        if pattern.search(text):
            return query_list, reason
    if not _buyer(text):
        return query_list, "no_buyer_language"
    if not OWN.search(text):
        return query_list, "not_own_program"
    if FIN_PARTNER.search(text) or query_list == "finance":
        if not rx["finance"].search(text):
            return "finance", "not_funding"
        if rx["finance_vendors"].search(text):
            return "finance", "finance_vendor"
        if OFF_FIN.search(text):
            return "finance", "non_funding_vertical"
        return "finance", None
    if not AFFILIATE_TERM.search(text):
        return query_list, "no_affiliate_term"
    if NONCOMMISSION_AMBASSADOR.search(text) and not COMMISSION.search(text):
        return query_list, "ambassador_noncommission"
    if HIRING.search(text) and not AFF_ROLE.search(text):
        return query_list, "hiring_not_program"
    return "affiliate", None


def _usable_domain(d: str, cfg: dict, shorteners: set[str]) -> bool:
    return not (d in FREEMAIL or d in TOOL_DOMAINS or d in shorteners or cfg["_rx"]["vendor_names"].search(d)
                or cfg["_rx"]["sellers"].search(d))


def _title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", (html or "")[:200_000], re.I | re.S)
    return re.sub(r"\s+", " ", htmllib.unescape(m.group(1))).strip() if m else ""


def _meta(html: str) -> str:
    m = re.search(r"<meta[^>]+name=[\"']description[\"'][^>]*content=[\"']([^\"']*)", (html or "")[:200_000], re.I)
    return htmllib.unescape(m.group(1)) if m else ""


def _brand_from_page(page, domain: str, cfg: dict) -> str | None:
    """The brand a homepage names when its title's first segment IS the domain's brand (the same name, not a word
    that merely appears in the domain). None otherwise."""
    if page is None or page.status >= 400 or common.host_of(page.final_url) != domain or common.parked(page.html):
        return None
    head = f"{_title(page.html)} {_meta(page.html)}"
    if cfg["_rx"]["sellers"].search(head) or SELLER_TEXT.search(head) or REJECT_TAG.search(head):
        return None
    brand = BRAND_SPLIT.split(_title(page.html))[0].strip()
    if not brand or len(brand) > 40 or len(brand.split()) > 5 or not _name_ok(brand, cfg):
        return None
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    name = _fold(brand)
    if len(label) < 4 or not common.tokens(brand):
        return None
    if name == label or (len(common.tokens(brand)) >= 2 and name in label):
        return brand
    return None


async def _brand_domain(ctx, s: dict, cache: dict, shorteners: set[str]) -> tuple[str, str] | None:
    """(domain, brand) for a brand domain the post ties to its program sentence (or a /affiliate, /partner, /referral,
    /join path), when its homepage names the brand. One homepage per domain."""
    cfg = ctx.signals_cfg
    doms = [d for d in common.domains_in_text(s["text"]) if _usable_domain(d, cfg, shorteners)]
    if not doms:
        return None
    tied = [d for d in doms if d in common.domains_in_text(s["sentence"])]
    if not tied:
        tied = [d for d in doms if re.search(re.escape(d) + r"/(?:affiliates?|partners?|referrals?|ambassadors?|join)\b",
                                             s["text"], re.I)]
    if not tied:
        return None
    d = tied[0]
    if d not in cache:
        cache[d] = _brand_from_page(await ctx.get(f"https://{d}/", "home"), d, cfg)
    return (d, cache[d]) if cache[d] else None


def _name_ok(name: str, cfg: dict, strict: bool = True) -> bool:
    rx = cfg["_rx"]
    tokens = {t.lower() for t in re.findall(r"[A-Za-z]+", name)}
    return (3 <= len(name) <= 40 and name.lower() not in GENERIC_CO and not (strict and tokens & PROGRAM_WORDS)
            and not REJECT_TAG.search(name) and not common.text_gate(name) and not NON_US_WORD.search(name)
            and not rx["verticals"].search(name) and not rx["sellers"].search(name)
            and any(c.islower() for c in name))


def _text_company(text: str, cfg: dict) -> str | None:
    """A company the post names with first-person framing: 'Founder at Acme', 'here at Acme', 'We at Acme'."""
    for m in ROLE_AT.finditer(text or ""):
        name = next((g for g in m.groups() if g), "").strip()
        if name and _name_ok(name, cfg):
            return name
    return None


def _experience(desc: str) -> list[str]:
    return [x.strip(" .") for x in EXPERIENCE.findall(desc or "") if x.strip(" .")]


def _company_from_result(title: str, desc: str, name: str, cfg: dict) -> str | None:
    """Company from one Google result for a profile, only when the result also gives an owner-type role: 'Name - Role at
    Company - LinkedIn', 'Name - Role - Company - LinkedIn' with an Experience line naming the company, or a description
    'Role @ Company'. A title cut off by an ellipsis is trusted only when the Experience line names the same company."""
    title, desc = title or "", desc or ""
    if REJECT_TAG.search(f"{title} {desc}"):
        return None
    t = re.sub(r"\s*[|]\s*LinkedIn\s*$|\s+-\s+LinkedIn\s*$", "", title.strip(), flags=re.I)
    rest = [p.strip() for p in re.split(r"\s+[-–—]\s+", t) if p.strip()][1:]
    exp = _experience(desc)
    truncated = bool(re.search(r"\.\.\.|…", title))
    role = co = None
    if len(rest) == 1:
        m = re.match(r"^(.+?)\s+(?:at|@)\s+(.+)$", rest[0], re.I)
        if m:
            role, co = m.group(1), m.group(2)
    elif len(rest) == 2 and exp and _fold(rest[1]) == _fold(exp[0]):
        role, co = rest[0], exp[0]
    if co and truncated and not (exp and _fold(exp[0]) == _fold(co)):
        role = co = None
    if not co:
        seg = re.split(r"\s*·\s*", desc.strip())[0]
        m = re.match(r"^(.+?)\s+(?:@|at)\s+(.+?)\.?$", seg, re.I)
        if m:
            role, co = m.group(1), m.group(2)
    if not co or not role:
        return None
    co = re.split(r"\s*(?:\||·|,|…|\.\.\.)\s*", co)[0].strip(" .")
    if not (2 <= len(co) <= 60) or co.lower() in GENERIC_CO or "@" in co or "http" in co.lower():
        return None
    if (REJECT_ROLE.search(role) or not OWNER_ROLE.search(role) or sg.SELLER_BIO.search(role)
            or REJECT_TAG.search(role)):
        return None
    if sg.SELLER_BIO.search(co) or REJECT_TAG.search(co) or _fold(co) == _fold(name) or not _name_ok(co, cfg, strict=False):
        return None
    return co


def _company_from_results(results, slug: str, name: str, cfg: dict) -> str | None:
    want = _fold(name)
    if not want or not slug:
        return None
    url_rx = re.compile(r"linkedin\.com/in/" + re.escape(slug) + r"(?:[/?#]|$)", re.I)
    found = []
    for r in _list(results)[:5]:
        if not isinstance(r, dict):
            continue
        url, title, desc = _str(r.get("url")), _str(r.get("title")), _str(r.get("description"))
        if not url_rx.search(unquote(url)) or not _fold(title).startswith(want):
            continue
        co = _company_from_result(title, desc, name, cfg)
        if co:
            found.append(co)
    return found[0] if found and len({_fold(c) for c in found}) == 1 else None


def _count(st: dict, reason: str) -> None:
    st["dropped"][reason] = st["dropped"].get(reason, 0) + 1


def _one_post(raw, spec: dict, ctx, seen_ids: set[str], st: dict) -> dict | None:
    """Gate one search result. Raises TypeError/AttributeError/ValueError on a malformed payload (caller counts it)."""
    if not isinstance(raw, dict):
        raise TypeError("post is not an object")
    author = _obj(raw.get("author"))
    post = {"activityUrn": _str(raw.get("activityUrn")), "url": _str(raw.get("url")),
            "datePublished": _str(raw.get("datePublished"))}
    slug = _slug(_str(author.get("url")))
    aid = _activity(post)
    if not slug or not aid:
        _count(st, "not_person_or_no_id")
        return None
    if aid in seen_ids:
        return None
    seen_ids.add(aid)
    date = sg.post_date(post)
    if not date or not common.within_days(date, ctx.days, ctx.now):
        _count(st, "no_date_or_outside_window")
        return None
    text = re.sub(r"\s+", " ", _str(raw.get("description"))).strip()
    lst, reason = _screen(text, spec["list"], ctx.signals_cfg)
    if reason:
        _count(st, reason)
        return None
    sentence = _program_sentence(text, _name_tokens(_str(author.get("name"))))
    if not sentence:
        _count(st, "no_clean_sentence")
        return None
    return {"aid": aid, "url": f"https://www.linkedin.com/feed/update/urn:li:activity:{aid}", "date": date,
            "slug": slug, "person_url": f"https://www.linkedin.com/in/{slug}",
            "name": _name(_str(author.get("name"))), "list": lst, "sentence": sentence, "text": text,
            "company": None, "domain": None, "how": None}


def _ingest(data, spec: dict, ctx, seen_ids: set[str], st: dict) -> list[dict]:
    """Gate one search page. Returns the surviving posts (not yet anchored). A malformed post is counted and skipped."""
    out = []
    for raw in _list(_obj(data).get("posts")):
        st["posts"] += 1
        try:
            item = _one_post(raw, spec, ctx, seen_ids, st)
        except (TypeError, AttributeError, ValueError):
            _count(st, "malformed")
            continue
        if item:
            out.append(item)
    return out


async def _anchor(ctx, items: list[dict], brand_cache: dict, shorteners: set[str], st: dict) -> None:
    for s in items:
        hit = await _brand_domain(ctx, s, brand_cache, shorteners)
        if hit:
            s["domain"], s["company"], s["how"] = hit[0], hit[1], "domain"
            st["how"]["domain"] += 1
            continue
        name = _text_company(s["text"], ctx.signals_cfg)
        if name:
            s["company"], s["how"] = name, "text"
            st["how"]["text"] += 1


def _pending(items: list[dict], cache: dict, cap: int, used: int) -> int:
    slugs = {s["slug"] for s in items if not s["company"] and s["name"] and s["slug"] not in cache}
    return min(len(slugs), max(0, cap - used))


async def collect(ctx) -> list[sg.Candidate]:
    cfg = ctx.cfg
    shorteners = set(ctx.signals_cfg.get("_shorteners") or ())
    queries = list(cfg.get("queries") or [])
    cap = int(cfg.get("max_author_lookups", 12))
    # credits kept back for profile lookups during the first-page phase: half of what is left, at most `cap`
    reserve = min(cap, max(0, ctx.budget.left // 2))
    st = {"posts": 0, "searches": 0, "lookups": 0, "how": {"domain": 0, "text": 0, "profile": 0}, "dropped": {},
          "unresolved": 0, "stopped": None}
    survivors: list[dict] = []
    seen_ids: set[str] = set()
    brand_cache: dict = {}
    lookup_cache: dict[str, str | None] = {}
    cursors: dict[int, str | None] = {}
    out: list[sg.Candidate] = []
    try:
        # 1) first page of every phrase, best first, stopping before the lookup reserve
        for qi, spec in enumerate(queries):
            if not ctx.budget.ok(reserve + 1):
                break
            data = await sg.sc_get(ctx.sc, SEARCH, {"query": spec["q"], "date_posted": _window(ctx.days)}, NAME)
            st["searches"] += 1
            cursors[qi] = _str(_obj(data).get("cursor")) or None
            new = _ingest(data, spec, ctx, seen_ids, st)
            await _anchor(ctx, new, brand_cache, shorteners, st)
            survivors += new
        # 2) further pages, only while the budget can still pay for the lookups the survivors need
        for qi, spec in enumerate(queries):
            for _ in range(int(spec.get("pages", 1)) - 1):
                need = _pending(survivors, lookup_cache, cap, st["lookups"])
                if not cursors.get(qi) or not ctx.budget.ok(need + 1):
                    break
                data = await sg.sc_get(ctx.sc, SEARCH, {"query": spec["q"], "date_posted": _window(ctx.days),
                                                        "cursor": cursors[qi]}, NAME)
                st["searches"] += 1
                cursors[qi] = _str(_obj(data).get("cursor")) or None
                new = _ingest(data, spec, ctx, seen_ids, st)
                await _anchor(ctx, new, brand_cache, shorteners, st)
                survivors += new
                if not _list(_obj(data).get("posts")):
                    break
        # 3) profile slug lookups for survivors no free path anchored
        for s in survivors:
            if s["company"] or not s["name"]:
                continue
            if s["slug"] in lookup_cache:
                s["company"] = lookup_cache[s["slug"]]
            else:
                if st["lookups"] >= cap or not ctx.budget.ok():
                    continue
                st["lookups"] += 1
                data = await sg.sc_get(ctx.sc, GOOGLE, {"query": f"site:linkedin.com/in/{s['slug']}"}, NAME)
                lookup_cache[s["slug"]] = _company_from_results(_obj(data).get("results"), s["slug"], s["name"],
                                                                ctx.signals_cfg)
                s["company"] = lookup_cache[s["slug"]]
            if s["company"]:
                s["how"] = "profile"
                st["how"]["profile"] += 1
    except sg.OutOfCredits:
        st["stopped"] = "out of credits"  # keep what was resolved; the other sources stop too (sticky)
    seen_keys: set[tuple[str, str]] = set()
    for s in survivors:
        if not s["company"]:
            st["unresolved"] += 1
            continue
        if not _quote_names_ok(s["sentence"], s["company"], s["domain"]):
            _count(st, "name_in_quote")
            continue
        key = (s["list"], s["domain"] or _fold(s["company"]))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        sig = sg.Signal("person_post", s["url"], s["sentence"], s["date"], s["name"] or None, s["person_url"], "linkedin")
        out.append(sg.Candidate(s["list"], sig, company_name=s["company"], domain=s["domain"], require_us=True))
    ctx.say(f"{NAME}: {st['searches']} searches, {st['posts']} posts, {len(survivors)} passed the gates, "
            f"{st['lookups']} profile lookups, {len(out)} candidates, dropped {dict(sorted(st['dropped'].items()))}"
            + (f", stopped ({st['stopped']})" if st["stopped"] else ""))
    return out


__all__ = ["NAME", "DEFAULTS", "collect"]
