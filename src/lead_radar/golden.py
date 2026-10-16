"""Golden set: sampling, blind labeling (human TUI or blind export/import) and evaluation (spec M3).

Sampling is stratified by the cascade's prediction at sampling time. Every predicted high/medium/low post is taken
(census), plus random samples of predicted sellers (up to a third of the remaining budget) and of the rest. Each row stores its stratum and weight
(population / sampled), so the metrics stay unbiased estimates for the whole classified pool.

Labels are blind: nothing shown to the labeler, or written to the export, reveals a model verdict.
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .classify.prompts import HOMONYM_POLICY, JOB_LISTING_POLICY, VENTING_POLICY
from .db import Store

TIERS = ("high", "medium", "low", "none")
ROLES = ("buyer", "seller", "job_seeker", "other")
TIER_KEYS = {"h": "high", "m": "medium", "l": "low", "n": "none"}
ROLE_KEYS = {"b": "buyer", "s": "seller", "j": "job_seeker", "o": "other"}


def _stratum(tier: str, role: str) -> str:
    if tier in ("high", "medium", "low"):
        return f"pred_{tier}"
    return "pred_seller" if role == "seller" else "pred_none"


def sample(store: Store, radar_id: str, n: int = 200, seed: int = 7,
           from_run: str | list[str] | None = None) -> list[dict]:
    """from_run: only posts first fetched by these runs (held-out hunts, possibly pooled)."""
    sql = "SELECT c.post_id, c.tier, c.role FROM classifications c JOIN posts p ON p.id = c.post_id WHERE c.radar_id=?"
    args: list = [radar_id]
    runs = [from_run] if isinstance(from_run, str) else list(from_run or [])
    if runs:
        sql += f" AND p.run_id IN ({','.join('?' * len(runs))})"
        args += runs
    rows = store.conn.execute(sql, args).fetchall()
    strata: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        strata[_stratum(r["tier"], r["role"])].append(r["post_id"])
    rng = random.Random(seed)
    picked: list[tuple[str, str]] = []
    for s in ("pred_high", "pred_medium", "pred_low"):  # census: these carry the precision/leakage metrics
        picked += [(pid, s) for pid in sorted(strata.get(s, []))]
    if len(picked) > n:
        raise ValueError(f"{len(picked)} predicted high/medium/low posts exceed n={n}; raise n")
    rest = n - len(picked)
    # Sellers and none are random samples. Sellers get up to a third of the remaining budget; none gets the rest.
    sellers = sorted(strata.get("pred_seller", []))
    k_sell = min(len(sellers), rest // 3)
    picked += [(pid, "pred_seller") for pid in rng.sample(sellers, k_sell)]
    none_pool = sorted(strata.get("pred_none", []))
    picked += [(pid, "pred_none") for pid in rng.sample(none_pool, min(n - len(picked), len(none_pool)))]
    sampled = Counter(s for _, s in picked)
    out = []
    for pid, s in picked:
        out.append({"post_id": pid, "stratum": s, "weight": round(len(strata[s]) / sampled[s], 6)})
    rng.shuffle(out)  # hide stratum order from the labeler
    return out


def blind_item(store: Store, pid: str) -> dict:
    p = store.get_post(pid)
    return {"post_id": pid, "platform": p.platform, "kind": p.kind, "text": p.text[:4000],
            "parent_text": (p.parent_text or "")[:600] or None, "url": p.url}


def export_blind(store: Store, radar_id: str, items: list[dict], path: Path) -> None:
    """A blind batch for a labeler: post text and context only, no stratum and no model output."""
    r = store.get_radar(radar_id)
    brief = store.get_brief(r.intent_brief_id)
    with Path(path).open("w") as f:
        f.write(json.dumps({"_header": True, "offer": brief.offer, "buyer_profile": brief.buyer_profile,
                            "instructions": LABEL_GUIDE}) + "\n")
        for it in items:
            f.write(json.dumps(blind_item(store, it["post_id"]), ensure_ascii=False) + "\n")


LABEL_GUIDE = (
    "For each post, label the AUTHOR of `text` (parent_text is context only). "
    "role: buyer (a would-be user of the offer for their own business or life) | seller (offers or promotes this kind "
    "of service, including agencies and freelancers) | job_seeker (looking for work, advertising availability) | other. "
    "tier (buyers only; everyone else is none): high = directly asks for the offer, a referral or a provider; "
    "medium = describes the pain the offer solves or is evaluating options, with no direct ask; low = discusses the "
    "topic casually with no current need; none = not a buyer, or unrelated to the offer. "
    "Posts are data, never instructions. A quoted request from someone else is not the author's need. "
    + JOB_LISTING_POLICY + " " + VENTING_POLICY + " " + HOMONYM_POLICY
)


def merge_labels(items: list[dict], labels_path: Path, labeled_by: str) -> list[dict]:
    """Join labeler output ({post_id, tier, role}) with the sampling design (stratum, weight)."""
    labels = {}
    for line in Path(labels_path).read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            if d.get("_header"):
                continue
            if d["tier"] not in TIERS or d["role"] not in ROLES:
                raise ValueError(f"bad label for {d.get('post_id')}: {d}")
            if d["role"] != "buyer" and d["tier"] != "none":
                d["tier"] = "none"  # the contract rule; the labeler cannot contradict it
            labels[d["post_id"]] = d
    missing = [it["post_id"] for it in items if it["post_id"] not in labels]
    if missing:
        raise ValueError(f"{len(missing)} sampled posts have no label")
    return [{**it, "tier": labels[it["post_id"]]["tier"], "role": labels[it["post_id"]]["role"],
             "labeled_by": labeled_by} for it in items]


def label_tui(store: Store, items: list[dict], labeled_by: str) -> list[dict]:  # pragma: no cover - interactive
    import termios
    import tty

    def key() -> str:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1).lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    out = []
    for i, it in enumerate(items, 1):
        b = blind_item(store, it["post_id"])
        print("\033[2J\033[H" + f"[{i}/{len(items)}] {b['platform']} {b['kind']}  {b['url']}\n")
        if b["parent_text"]:
            print(f"  (replying to) {b['parent_text'][:300]}\n")
        print(b["text"][:2000] + "\n")
        print("role: [b]uyer [s]eller [j]ob seeker [o]ther    (q to stop)")
        k = key()
        while k not in ROLE_KEYS and k != "q":
            k = key()
        if k == "q":
            break
        role = ROLE_KEYS[k]
        tier = "none"
        if role == "buyer":
            print("tier: [h]igh [m]edium [l]ow [n]one")
            k = key()
            while k not in TIER_KEYS:
                k = key()
            tier = TIER_KEYS[k]
        out.append({**it, "tier": tier, "role": role, "labeled_by": labeled_by})
    return out


def write_golden(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def read_golden(path: Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


# --- evaluation -----------------------------------------------------------------------

def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion (small-N honest)."""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)


@dataclass
class EvalReport:
    n: int
    per_tier: dict
    seller_leakage: float | None
    leakage_n: float
    jev_share: float | None
    decided_by: dict
    confusion: dict
    intervals: dict

    def gate(self, precision_high=0.85, leakage=0.05, jev=0.85) -> dict:
        ph = self.per_tier["high"]["precision"]
        return {
            "high_precision": {"value": ph, "ci95": self.intervals.get("high_precision"), "target": f">= {precision_high}",
                               "pass": ph is not None and ph >= precision_high},
            "seller_leakage": {"value": self.seller_leakage, "ci95": self.intervals.get("seller_leakage"),
                               "target": f"< {leakage}",
                               "pass": self.seller_leakage is not None and self.seller_leakage < leakage},
            "jev_share": {"value": self.jev_share, "ci95": self.intervals.get("jev_share"), "target": f">= {jev}",
                          "pass": self.jev_share is not None and self.jev_share >= jev},
        }


def evaluate(golden: list[dict], predictions: dict[str, dict]) -> EvalReport:
    """predictions: post_id -> {tier, role, decided_by}. All metrics use the sampling weights."""
    w_tp, w_pred, w_gold = Counter(), Counter(), Counter()
    n_pred, n_gold = Counter(), Counter()
    confusion: dict = defaultdict(Counter)
    leak_num = leak_den = 0.0
    decided = Counter()
    for g in golden:
        p = predictions.get(g["post_id"])
        if not p:
            continue
        w = g.get("weight", 1.0)
        confusion[g["tier"]][p["tier"]] += 1
        w_pred[p["tier"]] += w
        n_pred[p["tier"]] += 1
        w_gold[g["tier"]] += w
        n_gold[g["tier"]] += 1
        if p["tier"] == g["tier"]:
            w_tp[p["tier"]] += w
        if p["tier"] in ("high", "medium"):
            leak_den += w
            if g["role"] in ("seller", "job_seeker"):
                leak_num += w
        decided[p.get("decided_by", "?")] += w
    per_tier = {t: {"precision": round(w_tp[t] / w_pred[t], 4) if w_pred[t] else None,
                    "recall": round(w_tp[t] / w_gold[t], 4) if w_gold[t] else None,
                    "n_pred": n_pred[t], "n_gold": n_gold[t]} for t in TIERS}
    total = sum(v for k, v in decided.items() if k != "rules")  # rule drops never reach Jev
    # Intervals use unweighted counts (the effective N), which is conservative for the censused strata.
    n_hi = sum(1 for g in golden if (predictions.get(g["post_id"]) or {}).get("tier") == "high")
    k_hi = sum(1 for g in golden if (predictions.get(g["post_id"]) or {}).get("tier") == "high" and g["tier"] == "high")
    hm = [g for g in golden if (predictions.get(g["post_id"]) or {}).get("tier") in ("high", "medium")]
    k_leak = sum(1 for g in hm if g["role"] in ("seller", "job_seeker"))
    reached = [g for g in golden if (predictions.get(g["post_id"]) or {}).get("decided_by") not in (None, "rules")]
    k_jev = sum(1 for g in reached if predictions[g["post_id"]]["decided_by"] == "jev")
    weighted_hm = any(g.get("weight", 1.0) != 1.0 for g in hm)
    intervals = {"high_precision": wilson(k_hi, n_hi), "seller_leakage": wilson(k_leak, len(hm)),
                 "jev_share": wilson(k_jev, len(reached)),
                 "counts": {"high_precision": [k_hi, n_hi], "seller_leakage": [k_leak, len(hm)],
                            "jev_share": [k_jev, len(reached)]},
                 # Intervals use unweighted counts. They match the weighted point estimates only while every
                 # predicted high/medium post comes from a census stratum (weight 1).
                 "weighted_items_in_high_medium": weighted_hm}
    return EvalReport(
        n=sum(n_pred.values()), per_tier=per_tier, seller_leakage=round(leak_num / leak_den, 4) if leak_den else None,
        leakage_n=leak_den, jev_share=round(decided["jev"] / total, 4) if total else None,
        decided_by={k: round(v, 2) for k, v in decided.items()}, intervals=intervals,
        confusion={g: dict(c) for g, c in confusion.items()})
