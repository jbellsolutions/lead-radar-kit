#!/usr/bin/env bash
# Put Lead Radar on Railway, always on, with its database on a Railway disk (volume).
#
#   scripts/deploy_railway.sh            # first time: creates the project, the service, the disk, then deploys
#   scripts/deploy_railway.sh --update   # later: re-sends your settings and deploys the code in this folder
#
# Needs: the Railway CLI, logged in (`railway login` opens your browser), and a filled .env.local
# (see INSTALL.md). Your keys go from .env.local to Railway over stdin: they never appear on screen or in
# your shell history. Nothing here is billed to anyone but your own Railway account.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
SERVICE="${RAILWAY_SERVICE:-web}"
PROJECT="${RAILWAY_PROJECT_NAME:-lead-radar}"
UPDATE=0; [ "${1:-}" = "--update" ] && UPDATE=1

say() { printf '\n==> %s\n' "$*"; }
command -v railway >/dev/null || { echo "Install the Railway CLI first: npm i -g @railway/cli  (or: brew install railway)"; exit 2; }
railway whoami >/dev/null 2>&1 || { echo "Log in first: railway login   (opens your browser)"; exit 2; }
[ -f .env.local ] || { echo "No .env.local yet. Follow INSTALL.md step 3 first."; exit 2; }

value_of() {  # the value of KEY in .env.local, or empty
  python3 - "$1" <<'PY'
import re, sys
key = sys.argv[1]
for line in open(".env.local"):
    m = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=(.*)$", line)
    if m and m.group(1) == key:
        print(m.group(2).strip().strip('"').strip("'"), end="")
PY
}
for k in SCRAPECREATORS_API_KEY OPENROUTER_API_KEY RADAR_DASHBOARD_PASSWORD; do
  [ -n "$(value_of "$k")" ] || { echo "$k is empty in .env.local. Set it with: .venv/bin/python scripts/set_key.py $k"; exit 2; }
done

only_workspace() {  # your Railway workspace id when you have exactly one (railway init needs it outside a terminal)
  railway whoami --json 2>/dev/null | python3 -c 'import json, sys
w = json.load(sys.stdin).get("workspaces") or []
print(w[0]["id"] if len(w) == 1 else "", end="")' 2>/dev/null || true
}

if [ "$UPDATE" = 0 ]; then
  if railway status >/dev/null 2>&1; then
    say "This folder is already linked to a Railway project; using it"
  else
    WS="${RAILWAY_WORKSPACE:-$(only_workspace)}"
    say "Creating Railway project '$PROJECT'"
    railway init --name "$PROJECT" ${WS:+--workspace "$WS"} || {
      echo "If you have more than one Railway workspace, name one: RAILWAY_WORKSPACE=<name> scripts/deploy_railway.sh"
      exit 2; }
  fi
  if ! railway service list 2>/dev/null | grep -qw "$SERVICE"; then
    say "Adding service '$SERVICE'"
    railway add --service "$SERVICE"
  fi
  railway service link "$SERVICE" >/dev/null
  if ! railway volume list 2>/dev/null | grep -q "/data"; then
    say "Adding a disk (volume) at /data for the database"
    railway volume add --mount-path /data
  fi
fi

say "Sending settings from .env.local (values are not shown)"
for k in SCRAPECREATORS_API_KEY OPENROUTER_API_KEY RADAR_DASHBOARD_PASSWORD TYPESAFE_API_KEY \
         SLACK_BOT_TOKEN SLACK_WEBHOOK_URL DATABOX_URL DATABOX_TOKEN BROWSERBOX_URL BROWSERBOX_TOKEN \
         RADAR_DAILY_CREDIT_CAP RADAR_DAILY_AT RADAR_PROGRAMS_DAILY RADAR_PROGRAMS_DAILY_CREDITS RADAR_ENRICH; do
  v="$(value_of "$k")"
  [ -n "$v" ] || continue
  printf %s "$v" | railway variable set "$k" --stdin --service "$SERVICE" --skip-deploys >/dev/null
  echo "  set $k"
done

say "Building and deploying (a few minutes the first time)"
railway up --service "$SERVICE" --ci

say "Public address"
railway domain --service "$SERVICE" || true
echo
echo "Open the https address above and log in with RADAR_DASHBOARD_PASSWORD (it's in your .env.local)."
echo "Check it any time: curl -s https://<your-address>/health   -> ok"
