"""Signal sources for the program lane: one module per platform family, all with the same contract.

Each module exposes

    NAME: str                      # registry key, also the `--steps` name and the config/program_signals.yaml
                                   # `sources.<NAME>` block (enabled, credits, plus module-specific keys)
    DEFAULTS: dict                 # module settings (queries, phrases, limits); config `sources.<NAME>` overrides keys
    async def collect(ctx: SourceContext) -> list[signals.Candidate]

A collector only finds and filters. It never writes to the store: growth.run() resolves each Candidate to a LinkedIn
company page, applies the exclusions and the US gate, and stores the company and its Signal. A collector must

- check `ctx.budget.ok()` before every ScrapeCreators call and stop cleanly when it is False (each source has its
  own credit cap inside the run's budget);
- fetch public pages only through `ctx.get(url)` (robots.txt honoured, rate-limited, counted against
  `ctx.http_cap`), never log in anywhere, never call Browser Box, never use paid enrichment;
- set Signal.platform, a dated Signal.signal_date where the source gives one (ISO YYYY-MM-DD), a verbatim
  Signal.quote (<= 400 chars) and a canonical Signal.url (no tracking params);
- never log or print a person's name or profile URL (Signal.person_name/person_url are stored, never logged);
- set Candidate.require_us=True for social and community sources (listed only when the company is confirmed US).

A source that raises is recorded as failed for that run; the other sources still run (growth.run isolates them).
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .. import detect, signals as sg

# registry key -> module (relative to this package). Order is the order sources run in a growth run.
REGISTRY = {
    "meta_ads": "meta_ads",
    "hiring_boards": "hiring_boards",
    "finance_iso": "finance_iso",
    "linkedin_people": "linkedin_people",
    "reddit": "reddit",
    "social_meta": "social_meta",
    "social_video": "social_video",
    "community": "community",
}


@dataclass
class SourceContext:
    sc: object                      # ScrapeCreators client (sc.get / sg.sc_get); its meter counts credits
    http: object                    # listings.http.PoliteClient (use ctx.get, which counts requests)
    budget: sg.Budget               # this source's credit allowance (a sub-budget of the run)
    cfg: dict                       # module DEFAULTS merged with config `sources.<NAME>`
    signals_cfg: dict               # the whole signals config (sg.load()): exclusions, regexes, do_not_prospect
    days: int                       # look-back window (daily runs: 3)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    known: Callable[[str], bool] = lambda domain: False  # True when the domain is already in the store
    say: Callable[[str], None] = lambda msg: None        # progress line (no personal data)
    http_cap: int = 120             # free page fetches this source may make this run
    http_used: int = 0

    async def get(self, url: str, role: str = "page") -> detect.Page | None:
        """One polite public GET (robots.txt honoured), or None when refused, failed, or over this run's cap."""
        if self.http_used >= self.http_cap:
            return None
        self.http_used += 1
        return await detect._get(self.http, url, role)


def load(name: str):
    """The source module for a registry key."""
    return importlib.import_module(f".{REGISTRY[name]}", __name__)


def settings(name: str, module, signals_cfg: dict) -> dict:
    merged = dict(getattr(module, "DEFAULTS", {}))
    merged.update((signals_cfg.get("sources") or {}).get(name) or {})
    return merged


Collector = Callable[[SourceContext], Awaitable[list]]
__all__ = ["REGISTRY", "SourceContext", "load", "settings", "Collector"]
