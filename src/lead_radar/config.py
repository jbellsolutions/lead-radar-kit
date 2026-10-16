"""Loads config/radar.yaml plus secrets from the environment and a gitignored .env.local.

Secrets are never logged or echoed. `require()` raises MissingSecret naming the key only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "radar.yaml"
DEFAULT_ENV_FILE = REPO_ROOT / ".env.local"

# Canonical name -> accepted aliases (first match wins).
SECRET_ALIASES: dict[str, tuple[str, ...]] = {
    "SCRAPECREATORS_API_KEY": ("SCRAPECREATORS_API_KEY", "SCRAPE_CREATORS_API_KEY", "MCP_SCRAPE_CREATORS_API_KEY"),
    "TYPESAFE_API_KEY": ("TYPESAFE_API_KEY",),
    "OPENROUTER_API_KEY": ("OPENROUTER_API_KEY",),
    "SLACK_BOT_TOKEN": ("SLACK_BOT_TOKEN",),
    "SLACK_WEBHOOK_URL": ("SLACK_WEBHOOK_URL",),
    "RADAR_API_TOKEN": ("RADAR_API_TOKEN",),
    "DATABOX_TOKEN": ("DATABOX_TOKEN",),
    "DATABOX_URL": ("DATABOX_URL",),  # not secret, but read from .env.local like the token
    "BROWSERBOX_TOKEN": ("BROWSERBOX_TOKEN",),
    "BROWSERBOX_URL": ("BROWSERBOX_URL",),  # program lane: `read` for sites that block plain HTTP or render with JS
}


class MissingSecret(RuntimeError):
    def __init__(self, name: str):
        super().__init__(f"missing secret {name} (set it in the environment or .env.local)")
        self.name = name


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if value:
            out[key] = value
    return out


@dataclass
class Settings:
    data: dict
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    state_dir: Path = Path.home() / ".lead-radar"

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def secret(self, name: str) -> str | None:
        return self.secrets.get(name)

    def require(self, name: str) -> str:
        value = self.secrets.get(name)
        if not value:
            raise MissingSecret(name)
        return value

    def has(self, name: str) -> bool:
        return bool(self.secrets.get(name))

    @property
    def db_path(self) -> Path:
        return self.state_dir / "radar.sqlite"


def load(config_path: Path | None = None, env_file: Path | None = None, environ: dict | None = None) -> Settings:
    config_path = config_path or Path(os.environ.get("RADAR_CONFIG", DEFAULT_CONFIG))
    data = yaml.safe_load(config_path.read_text()) if config_path.is_file() else {}
    env = dict(_parse_env_file(env_file or DEFAULT_ENV_FILE))
    env.update({k: v for k, v in (environ if environ is not None else os.environ).items() if v})
    secrets: dict[str, str] = {}
    for canonical, aliases in SECRET_ALIASES.items():
        for alias in aliases:
            if env.get(alias):
                secrets[canonical] = env[alias]
                break
    state_dir = Path(env.get("RADAR_STATE_DIR") or data.get("state_dir") or Path.home() / ".lead-radar").expanduser()
    return Settings(data=data or {}, secrets=secrets, state_dir=state_dir)
