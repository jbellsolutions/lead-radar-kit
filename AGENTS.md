# Lead Radar: agent runbook

Read this before changing code or running hunts. To install, follow [INSTALL.md](INSTALL.md). To use a running
dashboard from an agent, follow [skills/lead-radar/SKILL.md](skills/lead-radar/SKILL.md).

## What it is
One plain-English offer ("a virtual assistant for small business owners") becomes a ranked list of people publicly
asking for that offer. It covers Reddit, YouTube, TikTok, Instagram, Threads, LinkedIn, Facebook groups and Google.
Google `site:` searches also reach X, Skool, Quora and Facebook.

Pipeline:
1. **Search, deterministic.** ScrapeCreators adapters (`src/lead_radar/adapters/`), then normalize, then dedupe (exact
   ID and same-author simhash), then a per-campaign classification queue in SQLite.
2. **Classifier cascade** (`src/lead_radar/classify/`):
   - **Stage 0 rules:** age, length, language, a creator's own posts, seller terms, templated spam.
   - **Stage 1 Jev** (optional, `TYPESAFE_API_KEY`): it only picks among options built in code.
   - **Stage 2 reasoning model** (OpenRouter). Its output is validated against `contracts/lead-intent.v1.json`, and
     evidence must be a verbatim quote.
3. **Job boards** (`adapters/onlinejobs.py`, `weworkremotely.py`, `freelancer.py`, `indeed.py`): businesses posting
   work the offer could do. These are polite, GET-only requests that honour robots.txt. Listings have their own
   classifier (`classify/listings.py`, `contracts/listing-fit.v1.json`).
4. **Contact cards** (`resolve/`, `enrich.py`, `databox.py`), optional: High and Medium leads are resolved for free from
   the post, ScrapeCreators profiles and link-in-bio pages. With `DATABOX_URL`/`DATABOX_TOKEN` set, they are then
   enriched through your own Data Box.
5. **Dashboard** (`server.py`, `dashboard.html`): campaigns, runs, leads and CSV export, password-protected. It has a
   built-in daily clock (`RADAR_DAILY_AT`).
6. **Program lane** (`programs/`), optional: companies that run an affiliate or referral program, verified from their
   own sites.

## Hard rules (do not break)
- **Never post, comment, like, follow or DM** on any platform. `tests/test_no_contact.py` enforces this. Openers are
  drafts only.
- **No secrets in the repo.** Keys live in `.env.local` (gitignored, mode 600) or the environment. Never print, commit
  or paste a key. `scripts/set_key.py` writes keys without echoing them.
- **Contact data stays out of logs, run errors, fixtures, tests and commits.** Enrichment prints counts only.
  Cards are deleted after `RADAR_CONTACT_RETENTION_DAYS` (90 by default). Test data is synthetic, or scrubbed with
  `scripts/scrub_fixtures.py`.
- **No paid enrichment SaaS** (Apollo, Hunter, Clay, ZoomInfo and the like). Contact data comes only from public
  profiles and the lead's own pages, plus your own Data Box if you connect one.
- **Job boards are read, never written:** GET only, robots.txt and crawl delays honoured. Never apply, bid or message.
- **Browser Box is `read` only** (program lane): never `act`, `task`, `computer` or `session`.
- **Model names live only in `config/radar.yaml`.**
- **Spending is capped.** `RADAR_DAILY_CREDIT_CAP` (300 by default) bounds ScrapeCreators credits per day across all
  runs. Don't add code paths that bypass it.
- **Stop and report a missing key.** Never hardcode a fallback.

## Setup
See [INSTALL.md](INSTALL.md). The short version:
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[server,export,dev]"
.venv/bin/python -m pytest -q                       # offline (recorded, scrubbed fixtures); must be all green
.venv/bin/python scripts/set_key.py SCRAPECREATORS_API_KEY
.venv/bin/python scripts/set_key.py OPENROUTER_API_KEY
.venv/bin/python scripts/set_key.py RADAR_DASHBOARD_PASSWORD --generate
bin/radar doctor                                    # keys (names only), credit balance, OpenRouter, state dir
bin/radar serve                                     # dashboard at http://localhost:8080
```
Always run the CLI through **`bin/radar`**. It puts `src/` on the path, because macOS hides venv `.pth` files in some
folders. For scripts, use `PYTHONPATH=src .venv/bin/python scripts/...`.

State lives in `~/.lead-radar/radar.sqlite`. Set `RADAR_STATE_DIR` to put it elsewhere; on Railway or Docker it's `/data`.

## Settings (environment or `.env.local`)
| Variable | Default | What |
|---|---|---|
| `SCRAPECREATORS_API_KEY` | required | All search |
| `OPENROUTER_API_KEY` | required | Search-plan drafting (expander), Stage 2, openers |
| `RADAR_DASHBOARD_PASSWORD` | required for the dashboard | Login and API bearer token |
| `TYPESAFE_API_KEY` | unset | Jev, Stage 1. Without it every post goes to Stage 2 (heuristic mode, flagged in the report). |
| `RADAR_DAILY_CREDIT_CAP` | 300 | ScrapeCreators credits per UTC day across all runs; runs past it are `skipped` |
| `RADAR_DAILY_AT` | 11:00 | UTC time the dashboard queues daily campaigns; `off` disables. `POST /api/run-daily` does the same on demand. |
| `RADAR_STATE_DIR` | `~/.lead-radar` | Database folder |
| `DATABOX_URL`, `DATABOX_TOKEN` | unset | Your own Data Box ([box-kit](https://github.com/jbellsolutions/box-kit)). When unset, Indeed is skipped and contact cards wait as Pending. |
| `RADAR_ENRICH` | on | `off` stops queuing contact-card jobs |
| `RADAR_ENRICH_DAILY_USD`, `RADAR_ENRICH_PER_LEAD_USD`, `RADAR_ENRICH_MAX_PER_RUN` | 3, 0.25, 3 | Data Box spend limits |
| `RADAR_LISTINGS_DAILY_USD` | 2 | Indeed (through Data Box) per day |
| `RADAR_CONTACT_RETENTION_DAYS` | 90 | Cards are deleted after this |
| `RADAR_PROGRAMS_DAILY` | off | `on` adds the program lane's growth run to each daily trigger |
| `RADAR_PROGRAMS_DAILY_CREDITS` | 300 | Credits per daily growth run (also within the daily cap) |
| `BROWSERBOX_URL`, `BROWSERBOX_TOKEN` | unset | Program lane: Browser Box `read` for sites that block plain requests |
| `SLACK_BOT_TOKEN` or `SLACK_WEBHOOK_URL` | unset | Slack digest |

`.env.local` lines are `KEY=value`. Put comments on their own lines, never after a value.

## Dashboard and API
- `bin/radar serve` runs it locally. `scripts/deploy_railway.sh` puts it on Railway: one service, a volume at `/data`,
  and `railway.json` health checks on `/health`. The `Dockerfile` runs anywhere.
- **Tabs:**
  - **Stats:** leads, charts, run log.
  - **Job leads.**
  - **Affiliate programs:** the program lane.
  - **Campaigns:** describe an offer, then draft a plan, edit it, then save and run. Tick "Run it every day" for daily
    runs.
- Jobs run one at a time as `python -m lead_radar.cli hunt ...` subprocesses (`server.py` → `Worker`). A restart marks
  a running job `interrupted`.
- **API** (`Authorization: Bearer <password>`):
  - `GET /api/data`, `GET /api/jobs`
  - `POST /api/campaigns/draft`, `POST /api/campaigns`
  - `GET|PATCH /api/campaigns/{name}`, `POST /api/campaigns/{name}/run`
  - `POST /api/run-daily`
  - `GET /api/export.csv[?campaign=<name>]`
  - `POST /api/admin/import`: a one-time upload of a `radar.sqlite` into an empty store.
  - Program lane: `GET /api/programs`, `GET /api/programs.csv`, `POST /api/programs/run`,
    `GET /api/programs/leads[.csv]?list=affiliate|finance`, `POST /api/programs/import`.

## Run a hunt from the command line
```bash
bin/radar hunt "virtual assistant for small business owners" --name va --days 3 --platforms api \
  --brief config/briefs/virtual-assistant.json
bin/radar leads latest            # buyers High -> Medium -> Low with quote and URL
bin/radar report latest           # per-source table, counts, cost split, leads by platform
```
- **Brief:** leave out `--brief` and the expander writes one, for a few cents of OpenRouter. Two examples are in
  `config/briefs/`. First-person "ask" terms ("need more clients") beat how-to phrasing, which pulls in tutorials.
- **Window:** `--days` is the look-back in whole days. Stage 0 drops anything older than exactly `now - days`.
- **Other flags:**
  - `--dry-run` fetches and counts without storing.
  - `--no-classify` stores and queues only.
  - Another pass on the same `--name` skips posts already seen.
- **Export:** `PYTHONPATH=src .venv/bin/python scripts/radar_export_sheet.py <radar> --xlsx out.xlsx --csv out.csv`. It
  can also write a Google Sheet if the `gws` CLI is logged in.
- **Verify before delivering:** `PYTHONPATH=src .venv/bin/python scripts/radar_verify_run.py <radar> --run <ids> --csv
  out.csv`. It checks that every evidence quote is verbatim in its post and that the credits add up.
- **Judge before delivering.** Never edit verdicts. In your report, flag likely false positives:
  - a job ad or recruiter relay;
  - someone selling, not buying;
  - a how-to question with no need;
  - venting with no ask.

## Program lane (optional)
Finds **companies** whose own site shows an affiliate or partner program: a platform portal link, a tracking script,
or an affiliate page with commission terms. It needs no classifier.
```bash
bin/radar programs seed --max-searches 100   # PartnerStack marketplace (free) + Google searches (1 credit each)
bin/radar programs verify                    # polite GET of each pending site
bin/radar programs report
bin/radar programs export --out programs.csv
bin/radar programs growth --days 30 --budget 300   # dated growth signals: hiring, posts, ads
```
- **Configuration:**
  - platforms are in `config/program_fingerprints.yaml`;
  - signals, sources and exclusions are in `config/program_signals.yaml`. Put your own partners and clients in
    `do_not_prospect` so they are never listed.
- **The two lists:**
  - `affiliate`: companies growing a program;
  - `finance`: US business-funding companies with ISO or broker programs.

## Known limits (state them with any lead delivery)
- The classifier's precision was measured on a VA offer. For other offers, spot-read leads before acting.
- X has no ScrapeCreators search, so it's covered only by Google `site:x.com`. Skool and private Facebook groups aren't
  read directly.
- The Facebook group adapter needs group URLs, in `config/radar.yaml` `communities.facebook` or in the brief.
- Short windows (1–3 days) are thin. Several buyer-phrased passes on the same campaign help.
- Job leads: OnlineJobs.ph and Freelancer hide the employer, so most of their job leads can't be resolved to a company.

## Troubleshooting
- **`ModuleNotFoundError: lead_radar`:** use `bin/radar` or `PYTHONPATH=src`.
- **Out of credits (402):** the run stops and reports it; check `bin/radar doctor`.
- **Heuristic-mode warning:** `TYPESAFE_API_KEY` isn't set. It's optional.
- **A run is `skipped`:** the daily credit cap was reached.

## Data model (SQLite)
| Table | Contents |
|---|---|
| `radars`, `intent_briefs` | Campaigns and versioned briefs |
| `runs`, `run_posts` | Per-run sources, counts, credits and cost; every post each run touched |
| `posts` | One row per unique post |
| `classifications`, `drops` | Verdicts (leads are `role='buyer' AND tier IN ('high','medium','low')`) and Stage 0 drops |
| `classification_queue`, `calibration` | Pending work; Stage 2 double-checks of Jev Highs |
| `listing_queue`, `listing_verdicts` | Job listings and their verdicts |
| `contacts`, `contact_links` | Contact cards (**personal data**) |
| `jobs` | Dashboard hunt and enrich jobs |
| `program_runs`, `program_companies`, `program_signals` | Program lane |

## Where things are
| Path | What |
|---|---|
| `src/lead_radar/cli.py` | `doctor`, `serve`, `hunt`, `classify`, `enrich`, `leads`, `report`, `list`, `dashboard`, `trigger-daily`, `label`, `eval`, `programs ...` |
| `src/lead_radar/server.py`, `dashboard.html` | Dashboard, API, job worker, daily clock |
| `src/lead_radar/hunt.py`, `adapters/`, `scrapecreators.py` | Search |
| `src/lead_radar/classify/` | Cascade; `prompts.py` holds the shared policies |
| `src/lead_radar/golden.py` | Sampling, blind labeling and weighted evaluation for your own golden sets (`bin/radar label`, `bin/radar eval`) |
| `src/lead_radar/programs/` | Program lane |
| `config/radar.yaml` | Models, thresholds, defaults, communities |
| `scripts/` | `set_key.py`, `deploy_railway.sh`, `radar_daily.sh`, `radar_export_sheet.py`, `radar_verify_run.py`, `radar_record_fixtures.py` + `scrub_fixtures.py` |
| `tests/` | Offline test suite |
