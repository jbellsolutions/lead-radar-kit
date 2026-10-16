"""Dedupe (spec §4): exact on Post.id; near-duplicate when simhash Hamming distance <= 3 for the same author."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Callable, Iterable

MAX_HAMMING = 3
_TOKEN = re.compile(r"[a-z0-9']+")
_URL = re.compile(r"https?://\S+|www\.\S+")
_MASK = (1 << 64) - 1


def _signed(v: int) -> int:
    """Map to the signed 64-bit range so the value fits an SQLite INTEGER."""
    return v - (1 << 64) if v >= 1 << 63 else v


def simhash(text: str) -> int:
    """64-bit simhash over word unigrams + bigrams, ignoring case, punctuation and links.

    With the spec's Hamming <= 3 threshold this catches cross-posts (same text re-posted with different
    formatting, links or a short sign-off), not merely similar posts.
    """
    tokens = _TOKEN.findall(_URL.sub(" ", text.lower()).replace("&", " and "))
    if not tokens:
        return 0
    features = Counter(tokens) + Counter(" ".join(tokens[i:i + 2]) for i in range(len(tokens) - 1))
    weights = [0] * 64
    for f, count in features.items():
        h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += count if h >> bit & 1 else -count
    return _signed(sum(1 << bit for bit in range(64) if weights[bit] > 0))


def hamming(a: int, b: int) -> int:
    return bin((a & _MASK) ^ (b & _MASK)).count("1")


def dedupe(posts: Iterable, prior_for_author: Callable[[str, str], list[tuple[str, int]]] | None = None):
    """Returns (kept, dropped) where dropped is a list of (post, reason).

    prior_for_author(platform, handle) -> [(post_id, simhash)] lets cross-run cross-posts be caught.
    A post whose ID already exists in `prior` is kept; exact-ID handling is the store's job.
    """
    kept, dropped = [], []
    seen_ids: set[str] = set()
    by_author: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for p in posts:
        if p.id in seen_ids:
            dropped.append((p, "duplicate_id"))
            continue
        key = (p.platform, p.author_handle) if p.author_handle else None
        if key and p.simhash:
            if key not in by_author:
                by_author[key] = list(prior_for_author(*key)) if prior_for_author else []
            near = next((pid for pid, h in by_author[key] if pid != p.id and h and hamming(h, p.simhash) <= MAX_HAMMING), None)
            if near:
                dropped.append((p, f"near_duplicate_of:{near}"))
                continue
            by_author[key].append((p.id, p.simhash))
        seen_ids.add(p.id)
        kept.append(p)
    return kept, dropped


TEMPLATE_MIN_WORDS = 20
TEMPLATE_MIN_AUTHORS = 3
TEMPLATE_MAX_HAMMING = 10   # looser than same-author dedupe: campaigns vary a prefix (country, emoji) per account
TEMPLATE_MAX_POSTS = 6000   # pairwise is O(n^2); the newest posts of a radar are what matter


def templated_campaigns(posts: Iterable, min_authors: int = TEMPLATE_MIN_AUTHORS,
                        min_words: int = TEMPLATE_MIN_WORDS, max_hamming: int = TEMPLATE_MAX_HAMMING) -> set[str]:
    """IDs of posts whose near-identical long text was posted by >= min_authors distinct authors.

    Genuine buyers write their own ask; the same long text under many accounts is a spam or recruiting campaign
    (e.g. one "My Singapore company is looking for a VA" post under 7 accounts). On 835 real VA posts, threshold
    10 found exactly 2 clusters, both campaigns; unrelated posts on the same topic sit at Hamming 25+.
    """
    items = [p for p in posts if p.author_handle and p.simhash and len(p.text.split()) >= min_words]
    items = items[-TEMPLATE_MAX_POSTS:]
    parent = list(range(len(items)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    hashes = [p.simhash & _MASK for p in items]
    for i in range(len(items)):
        hi = hashes[i]
        for j in range(i + 1, len(items)):
            if bin(hi ^ hashes[j]).count("1") <= max_hamming:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)
    out: set[str] = set()
    for members in groups.values():
        if len({(items[i].platform, items[i].author_handle) for i in members}) >= min_authors:
            out.update(items[i].id for i in members)
    return out
