# Install Lead Radar

This guide works for a person or for a setup agent (Claude Code, Codex and others). Every step ends with a check, and
you move on only when it passes.

## For a setup agent: read this first

- **Do the technical work yourself.** Ask the person only for what only they can do: their keys, signing in to
  Railway, and choosing a plan.
- **Ask for one key at a time.** Say what each key is for and where to get it, using the table in step 4.
- **Never repeat a key back, print it or put it in a file other than `.env.local`.** To save a key someone pasted to
  you, pipe it into `scripts/set_key.py` as shown in step 4.
- **Never sign in for the person,** whether on Railway, ScrapeCreators or OpenRouter, and never create accounts.
  `railway login` opens their browser; they finish it.
- **Never spend without saying so.** Tests and `bin/radar doctor` are free. A campaign run costs about 90–140
  ScrapeCreators credits plus a few cents of AI. Ask before the first run.
- **Lead Radar never posts, comments, likes or messages anyone.** Don't add anything that does.
- **Finish only when the checklist at the end passes,** then tell the person:
  - where the dashboard is;
  - where their password is: the `RADAR_DASHBOARD_PASSWORD` line in `.env.local`;
  - what each run costs.

## 1. Check the tools

```bash
git --version
python3 --version        # needs 3.12 or newer (3.12, 3.13 and 3.14 all work)
```

If either is missing:
- **macOS:** install [Homebrew](https://brew.sh), then run `brew install git python@3.12`.
- **Windows:** use WSL (`wsl --install`, then follow the Linux steps inside Ubuntu), or install Python from
  [python.org](https://www.python.org/downloads/) and [Git for Windows](https://git-scm.com/download/win).
- **Linux:** `sudo apt install git python3 python3-venv` on Debian or Ubuntu. If the system Python is older than
  3.12, install a newer one with [uv](https://docs.astral.sh/uv/) (`uv python install 3.12`).

If `python3` is older than 3.12 but `python3.12` exists, use `python3.12` in step 3.

## 2. Get the code

```bash
git clone https://github.com/jbellsolutions/lead-radar-kit.git
cd lead-radar-kit
```

Run every command below from this folder.

## 3. Install and test

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[server,export,dev]"
.venv/bin/python -m pytest -q
```

**Check:** the last line says something like `918 passed`. The tests run offline and cost nothing.

## 4. Add the keys

| Key | Required? | What it's for | Where to get it |
|---|---|---|---|
| `SCRAPECREATORS_API_KEY` | Yes | Searching Reddit, YouTube, TikTok, Instagram, Threads, LinkedIn, Facebook groups and Google | Sign up at [app.scrapecreators.com](https://app.scrapecreators.com) and copy the API key. It includes 100 free credits; after that, about $10 per 5,000. |
| `OPENROUTER_API_KEY` | Yes | The AI that drafts search plans and reads posts | Sign up at [openrouter.ai](https://openrouter.ai), add a few dollars of credit, then create a key at [openrouter.ai/keys](https://openrouter.ai/keys). |
| `RADAR_DASHBOARD_PASSWORD` | Yes | Logging in to your dashboard | Made for you; see below. |
| `TYPESAFE_API_KEY` | No | A fast first-pass reader (Jev). Without it, every post goes to the OpenRouter model, which works but costs a little more. | [typesafe.ai](https://typesafe.ai) |
| `SLACK_WEBHOOK_URL` or `SLACK_BOT_TOKEN` | No | Slack notices | Your Slack workspace |
| `DATABOX_URL` and `DATABOX_TOKEN` | No | Contact cards and Indeed listings, through your own Data Box | [box-kit](https://github.com/jbellsolutions/box-kit) |

**A person** types each key at a hidden prompt:

```bash
.venv/bin/python scripts/set_key.py SCRAPECREATORS_API_KEY
.venv/bin/python scripts/set_key.py OPENROUTER_API_KEY
```

**A setup agent** that was given a key in chat pipes it in, so it doesn't show up on screen:

```bash
printf %s '<the key>' | .venv/bin/python scripts/set_key.py SCRAPECREATORS_API_KEY
```

Then make the dashboard password:

```bash
.venv/bin/python scripts/set_key.py RADAR_DASHBOARD_PASSWORD --generate
```

It prints the password once and saves it in `.env.local`.

**Check:** run `bin/radar doctor`. You should see:
- `ok` for both required keys;
- the ScrapeCreators credit balance;
- `OpenRouter HTTP 200`;
- `ok` for `RADAR_DASHBOARD_PASSWORD`.

A `warn` line is fine: it marks something optional. A `FAIL` line names the key to fix.

## 5. Choose where it runs

### Option A: this computer (free)

```bash
bin/radar serve
```

Open **http://localhost:8080** and log in with the password from step 4. The dashboard listens on this computer only.

- **Daily campaigns** start at 11:00 UTC while it's running. To use another time, set `RADAR_DAILY_AT=HH:MM` (UTC)
  in `.env.local`. If the computer was off at that time, they start when you next run it that day.
- **Stop it** with Ctrl+C. Your leads are kept in `~/.lead-radar/radar.sqlite`.
- **To keep it running in the background on a Mac** without a terminal open:

  ```bash
  mkdir -p ~/Library/LaunchAgents
  cat > ~/Library/LaunchAgents/com.leadradar.serve.plist <<EOF
  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0"><dict>
    <key>Label</key><string>com.leadradar.serve</string>
    <key>ProgramArguments</key><array><string>$PWD/bin/radar</string><string>serve</string></array>
    <key>WorkingDirectory</key><string>$PWD</string>
    <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$HOME/Library/Logs/lead-radar.log</string>
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/lead-radar.log</string>
  </dict></plist>
  EOF
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.leadradar.serve.plist
  ```

  To stop it: `launchctl bootout gui/$(id -u)/com.leadradar.serve`.

**Check:** you can log in at http://localhost:8080.

### Option B: Railway (always on)

[Railway](https://railway.com) runs your copy on its servers at its own https address, so it works while your
computer is off. The free trial gives $5 of credit for 30 days with no card, which is enough for a month. After the
trial, the Hobby plan ($5 a month, including $5 of usage) keeps it on. The README covers the Free plan and other
no-cost options.

1. Install the Railway CLI. Use `npm i -g @railway/cli` if you have Node, or `brew install railway` on a Mac.
2. Run `railway login`. It opens the browser, where the person signs up or signs in. A setup agent waits for them.
3. Deploy:

   ```bash
   scripts/deploy_railway.sh
   ```

   The script:
   1. creates a Railway project called `lead-radar` with one service called `web`;
   2. adds a disk at `/data`, so the database survives redeploys;
   3. copies your settings from `.env.local` without showing them;
   4. builds the included Dockerfile;
   5. prints your public address.

4. Open the `https://...up.railway.app` address and log in with the password from step 4.

**Check:** `curl -s https://<your-address>/health` prints `ok`, and you can log in.

To update later, after `git pull` or changing a setting in `.env.local`:

```bash
scripts/deploy_railway.sh --update
```

Daily campaigns start at 11:00 UTC by themselves. You don't need a second service or a cron job.

### Option C: any server with Docker (for example, an always-free cloud server)

```bash
docker build -t lead-radar .
docker run -d --name lead-radar --restart unless-stopped -p 8080:8080 \
  --env-file .env.local -v lead-radar-data:/data lead-radar
```

The named volume `lead-radar-data` keeps the database. Put the dashboard behind https (a reverse proxy such as Caddy)
before you open it to the internet.

## 6. Run your first campaign

In the dashboard, open **Campaigns**, then **New campaign**:
1. Describe what you sell in a sentence.
2. Click **Draft search plan**. This costs a few cents of AI and no credits.
3. Read the plan. The search phrases should sound like a buyer asking for help ("need someone to…"), not like
   how-to questions.
4. Leave **Run it every day** ticked if you want a daily run, then click **Save and run now**. A run takes a few
   minutes; watch it in **Activity**.
5. Read the leads in **Stats**.

You can also run campaigns from the command line. See [AGENTS.md](AGENTS.md).

## Done when

- [ ] `.venv/bin/python -m pytest -q` passes.
- [ ] `bin/radar doctor` shows `ok` for `SCRAPECREATORS_API_KEY`, `OPENROUTER_API_KEY` and `RADAR_DASHBOARD_PASSWORD`.
- [ ] The dashboard opens and the password logs in: http://localhost:8080, or the Railway address with `/health` returning `ok`.
- [ ] The person knows:
  - where the dashboard is;
  - that the password is in `.env.local`;
  - that each run costs about 90–140 ScrapeCreators credits plus a few cents of AI, capped at 300 credits a day
    (`RADAR_DAILY_CREDIT_CAP`).

## If something goes wrong

- **`ModuleNotFoundError: lead_radar`:** run commands through `bin/radar`, which sets the path.
- **`bin/radar serve` says the password isn't set:** run step 4's `--generate` command.
- **`doctor` shows `ScrapeCreators ... out of credits`:** add credits at app.scrapecreators.com.
- **Railway: `railway whoami` fails:** run `railway login` again.
- **Railway: the deploy fails:** run `railway logs --service web` to see why. Then run `scripts/deploy_railway.sh --update`.
- **Railway: the dashboard is empty after a redeploy:** the disk is missing. Check `railway volume list --service web`
  for a volume mounted at `/data`.
- **A run is marked `skipped`:** today's credit cap was reached. It resets at midnight UTC, or you can raise
  `RADAR_DAILY_CREDIT_CAP`.
