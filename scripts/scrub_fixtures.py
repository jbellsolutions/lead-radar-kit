"""Make recorded ScrapeCreators fixtures non-identifying: post text, ids and links are rewritten consistently.
Run it after scripts/radar_record_fixtures.py and before committing fixtures.

- Free text: words outside a small common-word allowlist become pseudo-words of the same length and case, so text
  can't be searched back to its author while sentence shape, numbers and buyer phrases ("need a virtual assistant")
  stay. Identical inputs map to identical outputs, so dedupe tests still see duplicates.
- Ids: digits and letters are replaced with hashed ones of the same class and length (prefixes like t3_ kept).
- URLs: platform links keep host and structural path words; ids and slugs are hashed. Media/CDN and third-party
  links become https://media.example.invalid/<hash>.
"""
import hashlib, json, re, sys, pathlib

import secrets
SALT = secrets.token_hex(16)  # random per run and never saved: the rewrite can't be reversed by guessing
COMMON = set("""a about above after again against all also am an and any anyone anybody are as ask asking at be because
been before being below best better between both but by can could day days did do does doing done down during each
even every few find finding for from get getting give go going good got great had has have having he help helping her
here hers him his how i if in into is it its just know let like little look looking lot made make making many may me
more most much must my need needed needs new no not now of off on once one only or other our out over own people
please really recommend recommendations right same see seeking she should since so some someone something still such
take than thank thanks that the their them then there these they thing things think this those through time to too
two under until up us use using very want wanted wants was way we week weeks well were what when where which while
who why will with without work working would year years yes yet you your hi hello hey anyone's i'm i've we're it's
don't can't
virtual assistant assistants va vas admin administrative remote part full hire hiring hired job jobs freelance freelancer
business businesses small owner owners client clients customer customers lead leads referral referrals sales marketing
email emails inbox calendar schedule scheduling support service services company team agency agencies startup
appointment appointments booking bookings social media manager management data entry bookkeeping hours hour month
monthly weekly daily budget pay paid rate cost price per looking for need help recommend advice tips
grow growth more get qualified prospects prospect outreach cold calls call phone website online""".split())
CDN = re.compile(r"(cdn|ytimg|redditmedia|redd\.it|fbcdn|licdn|tiktokv|muscdn|ggpht|googleusercontent|akamai|"
                 r"scontent|cloudfront|amazonaws|preview\.|external-preview|styles\.|i\.imgur|pbs\.twimg)", re.I)
PLATFORMS = re.compile(r"(^|\.)(reddit|facebook|youtube|youtu\.be|instagram|tiktok|threads|linkedin|x|twitter)\.(com|net|be)$", re.I)
STRUCT = {"r", "comments", "groups", "posts", "permalink", "watch", "shorts", "reel", "reels", "p", "video", "status",
          "in", "company", "feed", "update", "urn:li:activity", "story.php", "photo", "videos", "post", "comment",
          "u", "user", "channel", "c", "hashtag", "explore", "tags", "search", "results", "pulse", "events", "jobs", "view"}
TEXT_KEYS = {"text", "title", "description", "caption", "body", "selftext", "content", "snippet", "commentary",
             "message", "transcript", "desc", "summary", "headline", "comment", "post_text", "postText", "subtitle",
             "accessibility_caption", "original_title", "selftext_html", "body_html", "html", "markdown"}
ID_KEYS = re.compile(r"(^|_)(id|ids|post_?id|aweme_?id|video_?id|shortcode|code|pk|urn|cid|fbid|cursor|"
                     r"continuation_?token|parent_?id|link_?id|name|activity|share_?urn|thread_?id|media_?id)$", re.I)

def h(s, n=12):
    return hashlib.sha256((SALT + s).encode()).hexdigest()[:n]

def same_shape(tok):
    """Replace each letter/digit with a hashed one of the same class; keep case and punctuation."""
    d = hashlib.sha256((SALT + tok).encode()).digest()
    out = []
    for i, ch in enumerate(tok):
        b = d[i % len(d)] ^ (i * 31 % 256)
        if ch.isdigit():
            out.append(str(b % 10))
        elif ch.isalpha() and ch.isascii():
            c = "abcdefghijklmnopqrstuvwxyz"[b % 26]
            out.append(c.upper() if ch.isupper() else c)
        else:
            out.append(ch)
    return "".join(out)

def pseudo_word(w):
    core = re.sub(r"[^A-Za-z]", "", w)
    if not core or core.lower() in COMMON or len(core) <= 2:
        return w
    return same_shape(w)

def scrub_url(u):
    m = re.match(r"(https?://)([^/?#]+)([^?#]*)(\?[^#]*)?(#.*)?$", u)
    if not m:
        return u
    scheme, host, path, query, frag = m.group(1), m.group(2).lower(), m.group(3), m.group(4) or "", m.group(5) or ""
    bare = host[4:] if host.startswith("www.") else host
    if host.endswith(".invalid") or host.endswith(".example") or host.endswith(".test") or "example" in host:
        return u
    if CDN.search(host) or not PLATFORMS.search(bare):
        ext = re.search(r"\.(jpg|jpeg|png|webp|mp4|gif|heic)$", path, re.I)
        return f"https://media.example.invalid/{h(u)}" + (ext.group(0).lower() if ext else "")
    segs = []
    for i, sgm in enumerate(path.split("/")):
        if not sgm or sgm.lower() in STRUCT or (i == 2 and path.startswith("/r/")):  # keep /r/<subreddit>
            segs.append(sgm)
        elif sgm.startswith("@"):
            segs.append("@" + same_shape(sgm[1:]))
        else:
            segs.append(same_shape(sgm))
    q = ""
    if query:
        parts = []
        for kv in query[1:].split("&"):
            k, _, v = kv.partition("=")
            parts.append(f"{k}={same_shape(v)}" if v else k)
        q = "?" + "&".join(parts)
    return scheme + host + "/".join(segs) + q

URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>)\]]+")

def scrub_text(t):
    t = URL_IN_TEXT.sub(lambda m: scrub_url(m.group(0)), t)
    def word(m):
        w = m.group(0)
        if w.startswith(("@", "#")):
            return w[0] + same_shape(w[1:])
        return pseudo_word(w)
    # skip the URLs we just rewrote
    parts = re.split(r"(https?://\S+)", t)
    return "".join(p if p.startswith("http") else re.sub(r"[@#]?[A-Za-z][A-Za-z'’-]*", word, p) for p in parts)

def walk(node, key=""):
    if isinstance(node, dict):
        return {k: walk(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [walk(v, key) for v in node]
    if not isinstance(node, str) or not node:
        return node
    if re.match(r"https?://", node):
        return scrub_url(node)
    if re.match(r"/[A-Za-z@][^\s]*/", node):  # a site-relative link, e.g. a Reddit permalink
        return scrub_url("https://www.reddit.com" + node)[len("https://www.reddit.com"):]
    if re.fullmatch(r"\d{4}-\d\d-\d\d([T ][\d:.]+Z?([+-]\d\d:?\d\d)?)?", node) or re.fullmatch(r"-?\d+(\.\d+)?", node) and not ID_KEYS.search(key):
        return node
    if key in TEXT_KEYS or (" " in node.strip() and len(node) > 30):
        return scrub_text(node)
    opaque = (" " not in node and len(node) >= 16 and re.search(r"\d", node) and re.search(r"[A-Za-z]", node))
    if ID_KEYS.search(key) or re.search(r"(token|Token|cursor|Cursor|key|Key|uri|Uri|uid|Id)$", key) or opaque or (
            re.fullmatch(r"[A-Za-z0-9_-]{6,}", node) and re.search(r"\d", node)):
        if node.startswith("user_"):
            return node  # already pseudonymized
        m = re.match(r"^(t\d_|urn:li:[a-z]+:)", node)
        pre = m.group(0) if m else ""
        return pre + same_shape(node[len(pre):])
    return node

def rehash_users(text):
    """Placeholder names (user_xxxxxxxx) get a fresh salted hash, so a guessed username can't be checked."""
    return re.sub(r"user_([0-9a-f]{8})\b", lambda m: "user_" + h("u" + m.group(1), 8), text)


if __name__ == "__main__":
    # usage: python scripts/scrub_fixtures.py tests/fixtures/sc
    for p in sorted(pathlib.Path(sys.argv[1]).glob("*.json")):
        data = json.loads(p.read_text())
        p.write_text(rehash_users(json.dumps(walk(data), ensure_ascii=False, indent=1)) + "\n")
        print("scrubbed", p.name)
