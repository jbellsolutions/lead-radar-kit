"""Contact clues in free text (a post, a bio, a listing): emails, phones, URLs and "I run X" company phrases.

Conservative on purpose: a wrong clue costs a paid enrichment call, a missed one only costs coverage.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24})(?![\w-])")
URL = re.compile(r"\b((?:https?://|www\.)[^\s<>\"')\]]+|[a-z0-9-]{2,63}\.(?:com|co|io|net|org|ai|biz|agency|co\.uk|com\.au|"
                 r"co\.nz|ca|us|ph)(?:/[^\s<>\"')\]]*)?)", re.I)
PHONE = re.compile(r"(?<![\w/])(\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4})(?![\w/])")
COMPANY = re.compile(
    r"\b(?:I|we)\s+(?:run|own|founded|started|operate|manage)\s+(?:a\s+|an\s+|my\s+|our\s+)?(?:company\s+called\s+)?"
    r"([A-Z][\w&'.-]*(?:\s+[A-Z][\w&'.-]*){0,4})"
    r"|\b(?i:founder|co-founder|owner|ceo|director)\s+(?i:of|at|@)\s+([A-Z][\w&'.-]*(?:\s+[A-Z][\w&'.-]*){0,4})")

SOCIAL_HOSTS = {"instagram.com", "tiktok.com", "threads.net", "threads.com", "x.com", "twitter.com", "facebook.com",
                "fb.com", "youtube.com", "youtu.be", "reddit.com", "pinterest.com", "t.me", "wa.me", "discord.gg",
                "medium.com", "substack.com", "github.com", "calendly.com", "onlinejobs.ph", "weworkremotely.com",
                "freelancer.com", "indeed.com", "upwork.com", "fiverr.com", "google.com", "goo.gl", "bit.ly",
                "linkedin.com", "lnkd.in", "t.co", "apple.com", "spotify.com", "amazon.com", "paypal.me",
                # storefronts, link-in-bio and booking hosts: a page there is not the person's own domain
                "beacons.ai", "stan.store", "bio.link", "carrd.co", "gumroad.com", "kajabi.com", "myshopify.com",
                "shopify.com", "etsy.com", "linktr.ee", "komi.io", "pillar.io", "lnk.bio", "link.me", "wix.com",
                "wixsite.com", "squarespace.com", "wordpress.com", "notion.site", "canva.site", "hubspot.com",
                "gohighlevel.com", "clickfunnels.com", "zoom.us", "whatsapp.com", "telegram.me", "skool.com"}
NOT_COMPANIES = {"facebook", "google", "instagram", "tiktok", "youtube", "linkedin", "amazon", "shopify", "etsy",
                 "upwork", "fiverr", "meta", "twitter", "x", "reddit", "pinterest", "ads", "facebook ads", "google ads",
                 "my business", "a business", "business", "company", "agency"}
LINK_IN_BIO = {"linktr.ee": "linktree", "komi.io": "komi", "pillar.io": "pillar", "lnk.bio": "linkbio",
               "link.me": "linkme"}
FREEMAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com", "proton.me",
            "protonmail.com", "live.com", "msn.com", "gmx.com", "ymail.com", "me.com"}
JUNK_EMAIL = re.compile(r"(noreply|no-reply|donotreply|example\.(com|org)|sentry|wixpress|\.png$|\.jpg$)", re.I)


def host(url: str) -> str:
    u = url if "://" in url else "https://" + url
    h = (urlsplit(u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _base(h: str) -> str:
    parts = h.split(".")
    if len(parts) > 2 and parts[-2] in ("co", "com", "org", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalize_url(url: str) -> str:
    url = url.rstrip(".,;:!?")
    return url if "://" in url else "https://" + url


def emails(text: str) -> list[str]:
    out = []
    for m in EMAIL.findall(text or ""):
        e = m.lower().rstrip(".")
        if not JUNK_EMAIL.search(e) and e not in out:
            out.append(e)
    return out


def phones(text: str) -> list[str]:
    out = []
    for m in PHONE.findall(text or ""):
        digits = re.sub(r"\D", "", m)
        if 8 <= len(digits) <= 15 and m not in out:
            out.append(m.strip())
    return out


def urls(text: str) -> list[str]:
    out = []
    for m in URL.findall(text or ""):
        if "@" in m:
            continue
        u = normalize_url(m)
        if u not in out:
            out.append(u)
    return out


def classify_url(url: str) -> str:
    """linkedin_person | linkedin_company | link_in_bio | social | website."""
    h = host(url)
    path = urlsplit(normalize_url(url)).path.lower()
    if h.endswith("linkedin.com"):
        return "linkedin_person" if path.startswith("/in/") else "linkedin_company" if path.startswith("/company/") else "social"
    if h in LINK_IN_BIO:
        return "link_in_bio"
    if _base(h) in SOCIAL_HOSTS or h in SOCIAL_HOSTS:
        return "social"
    return "website"


def websites(found: list[str]) -> list[str]:
    return [u for u in found if classify_url(u) == "website"]


def domain_of(url_or_email: str) -> str | None:
    s = url_or_email.split("@", 1)[1] if "@" in url_or_email and "://" not in url_or_email else host(url_or_email)
    s = s.lower().strip(".")
    return None if not s or s in FREEMAIL or _base(s) in SOCIAL_HOSTS else _base(s)


def companies(text: str) -> list[str]:
    out = []
    for a, b in COMPANY.findall(text or ""):
        name = re.split(r"[.,;:!?](?:\s|$)", (a or b))[0].strip(" .,'")
        if len(name) >= 3 and name.lower() not in NOT_COMPANIES and name.split()[0].lower() not in NOT_COMPANIES \
                and name not in out:
            out.append(name)
    return out


def clues(text: str) -> dict:
    found = urls(text)
    return {"emails": emails(text), "phones": phones(text), "urls": found,
            "linkedin": [u for u in found if classify_url(u) == "linkedin_person"],
            "linkedin_company": [u for u in found if classify_url(u) == "linkedin_company"],
            "link_in_bio": [u for u in found if classify_url(u) == "link_in_bio"],
            "websites": websites(found), "companies": companies(text)}
