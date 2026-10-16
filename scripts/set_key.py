#!/usr/bin/env python3
"""Save one setting in .env.local without showing it on screen.

    .venv/bin/python scripts/set_key.py SCRAPECREATORS_API_KEY        # paste when asked (hidden), then Enter
    .venv/bin/python scripts/set_key.py RADAR_DASHBOARD_PASSWORD --generate   # makes a strong password, shows it once
    printf %s "$VALUE" | .venv/bin/python scripts/set_key.py OPENROUTER_API_KEY   # for scripts and setup agents

.env.local is created from .env.example if it doesn't exist, kept private to your user (chmod 600), and never
committed (.gitignore). The value is never printed, except a password made with --generate, which you need to log in.
"""
from __future__ import annotations

import getpass
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env.local"
EXAMPLE = ROOT / ".env.example"


def save(key: str, value: str) -> None:
    if not ENV.exists():
        ENV.write_text(EXAMPLE.read_text() if EXAMPLE.exists() else "")
    os.chmod(ENV, 0o600)
    lines = ENV.read_text().splitlines()
    line = f"{key}={value}"
    for i, old in enumerate(lines):
        if re.match(rf"\s*(export\s+)?{re.escape(key)}\s*=", old):
            lines[i] = line
            break
    else:
        lines.append(line)
    ENV.write_text("\n".join(lines) + "\n")
    os.chmod(ENV, 0o600)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1 or not re.fullmatch(r"[A-Z][A-Z0-9_]*", args[0]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    key = args[0]
    if "--generate" in sys.argv:
        value = secrets.token_urlsafe(18)
        save(key, value)
        print(f"{key} saved in .env.local. Your password (also in .env.local): {value}")
        return 0
    if sys.stdin.isatty():
        value = getpass.getpass(f"Paste {key} (hidden), then press Enter: ")
    else:
        value = sys.stdin.read()
    value = value.strip()
    if not value or any(c in value for c in "\n\r"):
        print(f"{key}: nothing saved (empty or more than one line).", file=sys.stderr)
        return 1
    save(key, value)
    print(f"{key} saved in .env.local ({len(value)} characters).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
