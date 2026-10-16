#!/usr/bin/env bash
# One daily Lead Radar pass: doctor -> hunt (new posts only) -> leads -> export -> verify -> dashboard.
# For a local, CLI-only setup: call it from your own scheduler (cron, launchd or an agent). The dashboard has its own
# daily clock (RADAR_DAILY_AT), so you don't need this when you run the dashboard.
#
#   scripts/radar_daily.sh                      # defaults below
#   RADAR_NAME=x RADAR_BRIEF=config/briefs/x.json RADAR_QUERY="..." scripts/radar_daily.sh
#   RADAR_SKIP_HUNT=1 scripts/radar_daily.sh    # rebuild files for the latest run, no credits spent
#
# Exit codes: 0 ok, 1 a check failed (see output), 2 doctor blocked (missing key / no credits).
# Output folder: $RADAR_OUT (default ~/.lead-radar/daily/<radar>/<YYYY-MM-DD>) with leads.csv, leads.xlsx,
# hunt.log, leads.txt, verify.txt and summary.json. The dashboard is rebuilt at <state dir>/dashboard.html.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NAME="${RADAR_NAME:-lead-gen-referrals}"
BRIEF="${RADAR_BRIEF:-config/briefs/lead-gen-referrals-asks.json}"
QUERY="${RADAR_QUERY:-more leads and referrals for my business}"
DAYS="${RADAR_DAYS:-2}"            # 2-day window + dedupe = only posts not seen on earlier days
PLATFORMS="${RADAR_PLATFORMS:-api}"
DATE="$(date +%Y-%m-%d)"
OUT="${RADAR_OUT:-$HOME/.lead-radar/daily/$NAME/$DATE}"
PY=(env "PYTHONPATH=$ROOT/src" "$ROOT/.venv/bin/python")
mkdir -p "$OUT"

echo "== doctor"
if ! bin/radar doctor; then
  echo "BLOCKED: doctor failed (see above). No credits spent." >&2
  exit 2
fi

if [ -n "${RADAR_SKIP_HUNT:-}" ]; then   # re-export / re-verify the radar's latest run without spending credits
  echo "== hunt skipped (RADAR_SKIP_HUNT)"; hunt_rc=0
else
  echo "== hunt  radar=$NAME days=$DAYS brief=$BRIEF"
  bin/radar hunt "$QUERY" --name "$NAME" --days "$DAYS" --platforms "$PLATFORMS" --brief "$BRIEF" 2>&1 | tee "$OUT/hunt.log"
  hunt_rc=${PIPESTATUS[0]}
fi

RUN="$("${PY[@]}" - "$NAME" <<'EOF'
import sys
from lead_radar import config
from lead_radar.db import Store
store = Store.open(config.load().db_path)
radar = store.get_radar(sys.argv[1])
row = radar and store.conn.execute(
    "SELECT id FROM runs WHERE radar_id=? ORDER BY started_at DESC LIMIT 1", (radar.id,)).fetchone()
print(row["id"] if row else "")
EOF
)"
if [ -z "$RUN" ]; then
  echo "FAIL: hunt produced no run (exit $hunt_rc)." >&2
  exit 1
fi
echo "run=$RUN (hunt exit $hunt_rc)"

echo "== leads"
bin/radar leads "$RUN" | tee "$OUT/leads.txt"

echo "== export"
"${PY[@]}" scripts/radar_export_sheet.py "$NAME" --run "$RUN" \
  --title "Lead Radar $NAME $DATE" --xlsx "$OUT/leads.xlsx" --csv "$OUT/leads.csv" \
  --caveat "Daily pass: only posts not seen on earlier days, last $DAYS days." | tee "$OUT/export.json"

echo "== verify"
"${PY[@]}" scripts/radar_verify_run.py "$NAME" --run "$RUN" --csv "$OUT/leads.csv" | tee "$OUT/verify.txt"
verify_rc=${PIPESTATUS[0]}

"${PY[@]}" - "$OUT" "$RUN" "$NAME" "$hunt_rc" "$verify_rc" <<'EOF'
import json, sys, pathlib
out, run, name, hunt_rc, verify_rc = sys.argv[1:]
exp = json.loads((pathlib.Path(out) / "export.json").read_text() or "{}")
summary = {"radar": name, "run": run, "out": out, "leads": exp.get("leads"),
           "platforms": exp.get("platforms"), "hunt_exit": int(hunt_rc), "verify_exit": int(verify_rc),
           "files": {k: str(pathlib.Path(out) / k) for k in ("leads.xlsx", "leads.csv", "leads.txt")}}
(pathlib.Path(out) / "summary.json").write_text(json.dumps(summary, indent=2))
print("== summary"); print(json.dumps({k: v for k, v in summary.items() if k != "platforms"}))
EOF

echo "== dashboard"
bin/radar dashboard || echo "WARN: dashboard rebuild failed (leads files are still fine)." >&2

[ "$verify_rc" -eq 0 ] || { echo "FAIL: verify did not pass; do not deliver without saying so." >&2; exit 1; }
exit 0
