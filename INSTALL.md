# Install Lead Radar

This guide works for a person or for a setup agent (Claude Code, Codex and others). Each step has a **Check**; move
on only when it passes.

## For a setup agent: read this first

- **Do the technical work yourself.** Ask the person only for what only they can do: their keys, signing in to
  Railway, and choosing a plan.
- **Ask for one key at a time.** Say what each key is for and where to get it, using the table in step 4.
- **Prefer that the person types keys at the hidden prompt** in their own terminal (step 4). If they paste a key to
  you instead, save it with the piped form in step 4. Never repeat a key back, print it, or put it in any file other
  than `.env.local`.
- **Make the dashboard password with `--generate --quiet`,** so it isn't shown to you. The person reads it from
  `.env.local`.
- **Never sign in for the person,** whether on Railway, ScrapeCreators or OpenRouter, and never create accounts.
  `railway login` opens their browser; they finish it.
- **Never spend without saying so.** Tests, `bin/radar doctor` and starting the dashboard on a new install are free.
  A campaign run costs about 90–140 ScrapeCreators credits plus a few cents of AI. Ask before the first run. On a
  reinstall, read the note in step 5 first.
- **`bin/radar serve` keeps running.** Start it in the background, run the checks, and leave it running for the
  person, or stop the process you started.
- **Lead Radar never posts, comments, likes or messages anyone.** Don't add anything that does.
- **Finish only when the checklist at the end passes,** then tell the person:
  - where the dashboard is;
  - the full path of their `.env.local`, which holds the password;
  - what each run costs.

## 1. Check the tools

```bash
git --version
python3 --version        # needs 3.12 or newer (3.12, 3.13 and 3.14 all work)
```

**Check:** both print a version, and Python is 3.12 or newer.

If either is missing:
- **macOS:** install [Homebrew](https://brew.sh), then run `brew install git python@3.12`.
- **Linux:** run `sudo apt install git python3 python3-venv` on Debian or Ubuntu. If the system Python is older than
  3.12, install a newer one with [uv](https://docs.astral.sh/uv/) (`uv python install 3.12`).
- **Windows:** use WSL. Run `wsl --install`, open Ubuntu, and follow the Linux steps there. The commands in this
  guide don't work in plain Windows PowerShell.

If `python3` is older than 3.12 but `python3.12` exists, use `python3.12` in step 3.

## 2. Get the code

Pick the folder it will live in, for example your home folder. Don't move or rename it later, because the install
in step 3 points at this folder.

On a Mac, avoid Desktop, Documents and Downloads if you want it to run in the background (step 5): macOS blocks
background jobs from those folders.

```bash
cd ~
git clone https://github.com/jbellsolutions/lead-radar-kit.git
cd lead-radar-kit
```

**Check:** `ls bin/radar INSTALL.md` lists both files. Run every command below from this folder.

## 3. Install and test

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[server,export,dev]"
.venv/bin/python -m pytest -q
```

**Check:** the last line shows tests passed and none failed, for example `918 passed`. The tests run offline and cost
nothing.

## 4. Add the keys

Keys are saved in **`.env.local`** in this folder. `scripts/set_key.py` creates it from `.env.example` the first time,
keeps it private to your user, and Git never uploads it.

| Key | Required? | What it's for | Where to get it |
|---|---|---|---|
| `SCRAPECREATORS_API_KEY` | Yes, for campaigns | Searching Reddit, YouTube, TikTok, Instagram, Threads, LinkedIn, Facebook groups and Google | Sign up at [app.scrapecreators.com](https://app.scrapecreators.com) and copy the API key. It includes 100 free credits; after that, about $10 per 5,000. |
| `OPENROUTER_API_KEY` | Yes, for campaigns | The AI that drafts search plans and reads posts | Sign up at [openrouter.ai](https://openrouter.ai), add a few dollars of credit, then create a key at [openrouter.ai/keys](https://openrouter.ai/keys). |
| `RADAR_DASHBOARD_PASSWORD` | Yes | Logging in to your dashboard | Made for you; see below. |
| `TYPESAFE_API_KEY` | No | A fast first-pass reader (Jev). Without it, every post goes to the OpenRouter model ("Stage 2"), which works but costs a little more. | [typesafe.ai](https://typesafe.ai) |
| `SLACK_WEBHOOK_URL` or `SLACK_BOT_TOKEN` | No | Slack notices | Your Slack workspace |
| `DATABOX_URL` and `DATABOX_TOKEN` | No | Contact cards and Indeed listings, through your own Data Box | [box-kit](https://github.com/jbellsolutions/box-kit) |

The dashboard runs without valid keys; only campaigns need them. So you can finish steps 4 and 5 now and add keys
later.

**Typing keys yourself:** the prompt hides what you paste.

```bash
.venv/bin/python scripts/set_key.py SCRAPECREATORS_API_KEY
.venv/bin/python scripts/set_key.py OPENROUTER_API_KEY
```

**A setup agent that was given a key in chat** pipes it in. The key is then in the agent's command history, just as it
is in the chat:

```bash
printf %s '<the key>' | .venv/bin/python scripts/set_key.py SCRAPECREATORS_API_KEY
```

**The dashboard password:**

```bash
.venv/bin/python scripts/set_key.py RADAR_DASHBOARD_PASSWORD --generate          # a person: shows it once
.venv/bin/python scripts/set_key.py RADAR_DASHBOARD_PASSWORD --generate --quiet  # a setup agent: doesn't show it
```

Either way, it's the `RADAR_DASHBOARD_PASSWORD=` line in `.env.local`.

**Check:** run `bin/radar doctor`. With working keys, it shows:
- **no `FAIL` lines;**
- `ScrapeCreators` with a credit balance;
- `OpenRouter` with `HTTP 200`;
- `ok` for `RADAR_DASHBOARD_PASSWORD`.

The key rows only say whether a key is present. The `ScrapeCreators` and `OpenRouter` rows test that the keys work.

- A `warn` line is optional and fine. That includes `TYPESAFE_API_KEY`, and `gws (Sheet export)`, which only matters
  for Google Sheets export; nobody needs to sign in for it.
- A `FAIL` on the `ScrapeCreators` row that mentions 401, or `OpenRouter HTTP 401`, means that key is wrong. Run
  `set_key.py` for it again.
- `doctor` exits with code 1 while any `FAIL` remains.

## 5. Choose where it runs

**Reinstalling?** If `~/.lead-radar/radar.sqlite` already exists, this computer has run Lead Radar before, and the new
install uses the same campaigns and leads. When the dashboard starts after 11:00 UTC, it may start today's daily
campaigns right away, which spends credits. To prevent that:
- add `RADAR_DAILY_AT=off` to `.env.local` first, or
- point the new install at a separate folder with `RADAR_STATE_DIR=<folder>` in `.env.local`.

### Option A: this computer (free)

```bash
bin/radar serve                  # or: bin/radar serve --port 8090   if 8080 is already in use
```

Open **http://localhost:8080** (or your port) and log in with the password from step 4. The dashboard listens on this
computer only. It keeps running until you press Ctrl+C or stop the process.

**Check:** in another terminal, run `curl -s http://localhost:8080/health`. It should print `ok`. Something else may
already be using that port and answering too, so also log in once in the browser, or run:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $(grep '^RADAR_DASHBOARD_PASSWORD=' .env.local | cut -d= -f2-)" http://localhost:8080/api/data
```

It should print `200`.

- **Daily campaigns** start at 11:00 UTC while it's running. To use another time, set `RADAR_DAILY_AT=HH:MM` (UTC)
  in `.env.local`. If the computer was off at that time, they start when you next run it that day.
- **Your data** is in `~/.lead-radar/radar.sqlite`. To put it elsewhere, set `RADAR_STATE_DIR` in `.env.local`.

**To keep it running in the background on a Mac,** without a terminal open, run this from the `lead-radar-kit`
folder:

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

Stop it with `launchctl bootout gui/$(id -u)/com.leadradar.serve`. The log is in `~/Library/Logs/lead-radar.log`. If
port 8080 is busy, add `<string>--port</string><string>8090</string>` after `serve` in the file before loading it.

### Option B: Railway (always on)

[Railway](https://railway.com) runs your copy on its servers at its own https address, so it works while your
computer is off. The free trial gives $5 of credit for 30 days with no card, which is enough for a month. After the
trial, the Hobby plan ($5 a month, including $5 of usage) keeps it on. The README covers the Free plan and other
no-cost options.

**The quickest way, no computer setup at all:** open the [Deploy on Railway](https://railway.com/deploy/Cb06_G?referralCode=Q-Z5pZ&utm_medium=integration&utm_source=button&utm_campaign=lead-radar) link, sign in, paste the two
keys and click **Deploy**. Railway builds it, adds the `/data` disk and generates the password (the service's
**Variables → `RADAR_DASHBOARD_PASSWORD`**). Your address is under the service's **Settings → Networking**. A setup
agent can't do this part, because it needs the person's Railway sign-in and their keys typed into Railway's page.

**From this computer, with the code you just installed:**

1. Install the Railway CLI: `npm i -g @railway/cli` if you have Node, or `brew install railway` on a Mac.
2. Run `railway login`. It opens the browser, where the person signs up or signs in. A setup agent waits for them.
3. Deploy. This needs the two required keys in `.env.local`.

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

**Check:** `curl -s https://<your-address>/health` prints `ok`, and the password logs in.

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

**Check:** `curl -s http://localhost:8080/health` on that server prints `ok`.

## 6. Run your first campaign

This step spends credits, so a setup agent asks first.

**The quickest start:** your dashboard begins with six example campaigns (a virtual assistant, more leads and
referrals, an SDR or appointment-setting partner, business funding, homeschool help, and a local trade). None runs by
itself. In **Campaigns**, click **Run now** on the closest one, or **Edit plan** to adapt it, then tick **Daily** if you
want it every morning. A run uses roughly 40–140 credits depending on the campaign, so the 100 free ScrapeCreators credits cover about one.

**Your own campaign:** in **Campaigns**, click **New campaign**:
1. Describe what you sell in a sentence.
2. Click **Draft search plan**. This costs a few cents of AI and no credits.
3. Read the plan. The search phrases should sound like a buyer asking for help ("need someone to…"), not like
   how-to questions.
4. Leave **Run it every day** ticked if you want a daily run, then click **Save and run now**. A run takes a few
   minutes; watch it in **Activity**.
5. Read the leads in **Stats**.

You can also run campaigns from the command line. See [AGENTS.md](AGENTS.md).

## Done when

- [ ] `.venv/bin/python -m pytest -q` shows no failures.
- [ ] `bin/radar doctor` shows no `FAIL` lines: ScrapeCreators shows a balance and OpenRouter shows `HTTP 200`. If the
      person has no keys yet, say so and list what's left.
- [ ] The dashboard answers `ok` on `/health` and the password logs in, either on this computer or at the Railway
      address.
- [ ] The person knows:
  - the dashboard address;
  - the full path of `.env.local`, which holds the password;
  - that each run costs about 90–140 ScrapeCreators credits plus a few cents of AI, capped at 300 credits a day
    (`RADAR_DAILY_CREDIT_CAP`).

## If something goes wrong

- **`Address already in use`, or `/health` answers but the login fails:** another program has the port. Use
  `bin/radar serve --port 8090` and that port.
- **`ModuleNotFoundError: lead_radar`:** run commands through `bin/radar`, which sets the path.
- **`bin/radar serve` says the password isn't set:** run the `--generate` command in step 4.
- **`doctor` shows `401` for ScrapeCreators or OpenRouter:** that key is wrong; set it again with `set_key.py`.
- **`doctor` shows `out of credits`:** add credits at app.scrapecreators.com.
- **Railway: `railway whoami` fails:** run `railway login` again.
- **Railway: `railway init` asks for or fails on a workspace:** you have more than one. Run
  `RAILWAY_WORKSPACE=<workspace name> scripts/deploy_railway.sh`.
- **Railway: the deploy fails:** run `railway logs --service web` to see why. Then run `scripts/deploy_railway.sh --update`.
- **Railway: the dashboard is empty after a redeploy:** the disk is missing. Check that `railway volume list` shows a
  volume mounted at `/data`.
- **A run is marked `skipped`:** today's credit cap was reached. It resets at midnight UTC, or you can raise
  `RADAR_DAILY_CREDIT_CAP`.
