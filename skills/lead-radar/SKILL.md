---
name: lead-radar
description: "Find people publicly asking for an offer (e.g. 'need a virtual assistant', 'need more leads or referrals') across Reddit, YouTube, TikTok, Instagram, Threads, LinkedIn, Facebook groups and Google, plus businesses hiring for that work on job boards, through the user's own Lead Radar dashboard: create campaigns, run them, and report the leads. Triggers: 'run the lead radar', 'daily leads', 'new lead campaign', 'find people asking for X', 'lead radar for <offer>'."
author: "jbellsolutions"
allowed-tools: "Bash Read"
---

# Lead Radar

The user runs their own Lead Radar dashboard (install: https://github.com/jbellsolutions/lead-radar-kit, INSTALL.md).
You need two things from them:
- **`RADAR_APP_URL`:** the dashboard address, e.g. `http://localhost:8080` or `https://<name>.up.railway.app`.
- **`RADAR_DASHBOARD_PASSWORD`:** given to you as a secret, never in chat.

The dashboard holds every campaign, run and lead. Campaigns marked daily run by themselves every day at `RADAR_DAILY_AT`
(11:00 UTC by default), so you don't need to schedule anything.

## Rules (never break)
- **Read-only.** Never post, comment, like, follow or DM anyone. You deliver a list; a human decides who to contact.
- **Never print, paste or commit the password or any API key.** Send the password only in the `Authorization` header.
- **Say what a run costs before starting one.** Every run costs about 90–140 ScrapeCreators credits (about
  $0.20–0.30) plus a few cents of AI. The server refuses runs past its daily credit cap (`RADAR_DAILY_CREDIT_CAP`,
  300 by default); don't try to work around it.
- **Never edit lead verdicts.** Flag doubtful leads in your message instead.
- **Contact cards are personal data.** Put names, emails and phones only in the message to the user who asked; never in
  logs, files you commit, public channels or other tools. Never look people up yourself with paid enrichment services.
- **Job boards are read-only too:** never apply, bid or message an employer.

## Use the API
```bash
U="$RADAR_APP_URL"
H="Authorization: Bearer $RADAR_DASHBOARD_PASSWORD"
curl -s -H "$H" $U/api/data            # campaigns, runs, leads, job leads, credits
curl -s -H "$H" $U/api/jobs            # recent runs: queued / running / done / failed / skipped, with logs

# New campaign: 1) draft a search plan (a few cents of AI, no credits)
curl -s -H "$H" -H "content-type: application/json" -X POST $U/api/campaigns/draft \
  -d '{"offer": "a bookkeeper for my small business in New Zealand", "offer_context": "NZ bookkeeping firm"}'
# 2) save it (send the returned "brief" back, edited if needed) and optionally run now
curl -s -H "$H" -H "content-type: application/json" -X POST $U/api/campaigns \
  -d '{"new": true, "name": "nz-bookkeeper", "offer": "...", "days": 3, "daily": true, "run_now": true, "brief": {...}}'

curl -s -H "$H" -X POST $U/api/campaigns/<name>/run                  # run an existing campaign now (costs credits)
curl -s -H "$H" -H "content-type: application/json" -X PATCH $U/api/campaigns/<name> -d '{"daily": true}'
curl -s -H "$H" "$U/api/export.csv?campaign=<name>" -o leads.csv        # leads + job leads (+ contact columns)
```
- `days` (the first run's look-back window) is one of 1, 2, 3, 7 or 14. Daily runs look back 2 days and add only posts
  not seen before.
- Only one run executes at a time; others wait their turn. A run takes a few minutes: poll `/api/jobs` until its
  status is `done`.
- Read the search plan before saving it:
  - Search terms should be first-person asks ("need a bookkeeper"), not how-to phrases.
  - Use at most 8 terms per platform.
  - Keep at least 5 seller signals.
- **Job boards:** add `"job_boards": true` when saving a campaign. The plan then needs 2–4 job titles under
  `terms.onlinejobs`. Job leads are in `/api/data` → `listings` (`lead: true` = direct employer, high/medium fit).
- **Contact cards** (only when the user connected a Data Box): every finished run queues a `contact cards` job
  (`kind: "enrich"`). Cards are in `/api/data` → `contacts`, keyed by the `contact` field on each lead. Only `verified`
  emails are confirmed mailboxes; `accept_all` means the domain accepts everything.

### Optional: the program lane (companies with affiliate or referral programs)
Off by default. When the user has turned it on:
```bash
curl -s -H "$H" $U/api/programs                                        # verified companies, summary, recent runs
curl -s -H "$H" "$U/api/programs/leads?list=affiliate"                  # ranked, with why-now and dated signals
curl -s -H "$H" -H "content-type: application/json" -X POST $U/api/programs/run -d '{"max_searches": 100}'
```
A company is listed only with evidence from its own site or marketplace listing. A program run costs 1 credit per
search and counts toward the daily cap. `person_name`/`person_url` on signals are personal data.

## Deliver
Send the user:
1. The new leads from the run, by tier, with each lead's quote and URL. Get them from `/api/data` → `leads`, filtered by
   the job's `run_id`, plus job leads (`listings` with `lead: true`) and contacts if any.
2. The run's credits and lead count from `/api/jobs`.
3. Your judgement: list any likely false positive and why. Typical ones:
   - a job ad or recruiter post;
   - someone selling, not buying;
   - a how-to question with no need;
   - venting with no ask.
4. Caveats:
   - The classifier's precision is measured only for a VA offer; spot-read leads for other offers.
   - X is covered only through Google.
   - Skool and private Facebook groups aren't covered.

If a job is `failed` or `skipped`, say so and quote its note.
