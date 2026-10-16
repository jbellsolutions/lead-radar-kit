# Lead Radar

**Free leads: find people who are publicly asking for what you sell.**

You describe your offer in plain English, for example "a virtual assistant for small business owners". Lead Radar searches
Reddit, YouTube, TikTok, Instagram, Threads, LinkedIn, Facebook groups, Google and job boards. AI reads every post it
finds, and you get a private dashboard of the people asking for help, each with their own words as proof and a link to
the post.

**It never posts, comments, likes or messages anyone.** You read the leads and decide who to contact. A test in the
code (`tests/test_no_contact.py`) makes sure of that.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/Cb06_G?referralCode=Q-Z5pZ&utm_medium=integration&utm_source=button&utm_campaign=lead-radar)

Website: **[jbellsolutions.github.io/lead-radar-kit](https://jbellsolutions.github.io/lead-radar-kit/)**

## The checklist: what to get, what to do

### What to get

| | Need it? | Cost | Deal |
|---|---|---|---|
| **[ScrapeCreators](https://app.scrapecreators.com) key** | Yes: it does the searching | Free to start, then about $10 per 5,000 credits | **100 free credits** when you sign up, no card: about one free run. The smaller examples (local trade, business funding) use the fewest. |
| **[OpenRouter](https://openrouter.ai) key** | Yes: the AI that reads posts | Add $5 of credit; each run uses a few cents | No public discount code exists; $5 lasts many runs. |
| **[Railway](https://railway.com) account** | For the always-on, one-click version | Free trial ($5 of credit, 30 days, no card), then Hobby at $5 a month | Sign up through the **Deploy on Railway** button above for **$20 of Railway credit** under Railway's referral program.* |
| Claude Code or Codex | Only to install on your own computer instead | Your existing plan | |

\* The Railway button is a referral link: Railway gives you $20 in credit on its terms, and the author earns a
commission from Railway. It costs you nothing extra.

### What to do (about 10 minutes)

- [ ] **Get your ScrapeCreators key.** Sign up at [app.scrapecreators.com](https://app.scrapecreators.com) and copy
      the API key from your dashboard.
- [ ] **Get your OpenRouter key.** Sign up at [openrouter.ai](https://openrouter.ai), add $5 under **Credits**, then
      create a key at [openrouter.ai/keys](https://openrouter.ai/keys).
- [ ] **Click [Deploy on Railway](https://railway.com/deploy/Cb06_G?referralCode=Q-Z5pZ&utm_medium=integration&utm_source=button&utm_campaign=lead-radar).** Sign up or sign in (GitHub sign-in is quickest), paste the two keys,
      and click **Deploy**. Your dashboard password is made for you.
- [ ] **Open your dashboard.** In Railway, click the service, then **Settings → Networking** shows your
      `...up.railway.app` address (if none shows, click **Generate Domain** there). The password is under
      **Variables → `RADAR_DASHBOARD_PASSWORD`**.
- [ ] **Run an example.** Your dashboard starts with six example campaigns: a virtual assistant, more leads and
      referrals, an SDR or appointment-setting partner, business funding, homeschool help, and a local trade. Click
      **Run now** on the one closest to you, or click **New campaign** and describe what you sell.
- [ ] **Turn on Daily** for the campaigns you want every morning (11:00 UTC). Each daily run adds only new posts.

Lead Radar stops at 300 search credits a day by default (`RADAR_DAILY_CREDIT_CAP`), so a mistake can't run up a bill.

### Or install it on your own computer (free)

Paste this repository's link into Claude Code or Codex and say:

> **Install Lead Radar on this computer. Follow INSTALL.md.**

The agent installs it, checks that it works, and asks for your keys one at a time. **[INSTALL.md](INSTALL.md)** has every
step if you'd rather do it yourself.

## How it works

1. **You describe what you sell.** Lead Radar drafts a search plan: the phrases a buyer would use ("need a VA",
   "looking for someone to manage my inbox"), where to look, and the signs of a seller rather than a buyer. You can
   edit the plan before it runs.
2. **It searches.** It looks back 1 to 14 days across the platforms, and job boards too if you want businesses
   that are hiring.
3. **AI sorts the posts.** People asking for what you sell become leads, rated High, Medium or Low. Sellers,
   job seekers, how-to questions and spam are set aside. Every lead comes with a word-for-word quote from the post.
4. **You read the dashboard.** You see leads by platform, each with its quote and link, plus the run log and costs.
   Mark a campaign "daily" and it runs every morning, adding only posts it hasn't seen before.

There's also an optional **program lane** that finds companies running affiliate or referral programs, for
partnership outreach. It's off unless you turn it on.

Optional extras, none needed:
- **[TypeSafe](https://typesafe.ai)**, a fast first-pass AI reader.
- **Slack** notices.
- Your own **Data Box** ([box-kit](https://github.com/jbellsolutions/box-kit)), for contact cards and Indeed listings.

Keys stay in your own Railway variables, or in a private `.env.local` file on your computer that is never uploaded.

## Where it runs: your computer or Railway

**Your computer (free).** Run `bin/radar serve` and open http://localhost:8080. Daily campaigns start at 11:00 UTC
while it's running. If the computer was asleep then, they start when it next runs that day.

**Railway (always on).** [Railway](https://railway.com) is a hosting service: it runs your copy of Lead Radar on its
servers, so the dashboard stays online at its own web address and daily runs happen even when your computer is
off. The **Deploy on Railway** button sets it up in one click. If you installed on your computer first,
`scripts/deploy_railway.sh` moves it to Railway. You pay Railway for what the app uses: it bills by memory, CPU and
disk, per second.

### Railway at no cost, and what it really costs

| Railway plan | Price | Enough for Lead Radar? |
|---|---|---|
| **Free trial** | $0: a one-time $5 credit for 30 days, no card needed | Yes. A month of Lead Radar uses well under $5. |
| **Free plan** (after the trial) | $0, with $1 of usage a month (0.5 GB memory, 0.5 GB disk) | Only for trying it out. An always-on copy uses about $1.50–2 a month (a live install measured about 170 MB of memory), so it will pause before the month ends. |
| **Hobby** | $5 a month, which includes $5 of usage | Yes, comfortably. Best for daily use. |

Prices are from [railway.com/pricing](https://railway.com/pricing) as of October 2026; check there for current terms.

**Other no-cost ways to keep it always on:**
- **A computer that stays on**, such as an old laptop or a Mac mini, running `bin/radar serve`.
- **An always-free cloud server.** Oracle Cloud and Google Cloud both offer a small one. Run the included
  Dockerfile there with a disk for `/data`. INSTALL.md has the one Docker command.

Hosting is only part of the cost. **Searches and AI are billed by ScrapeCreators and OpenRouter** wherever it runs.
Expect about $0.20–0.35 per campaign run, or about $6–10 a month for one daily campaign.

## Install by hand

```bash
cd ~                                                                 # anywhere you won't move it from later
git clone https://github.com/jbellsolutions/lead-radar-kit.git
cd lead-radar-kit
python3 -m venv .venv
.venv/bin/pip install -e ".[server,export,dev]"
.venv/bin/python -m pytest -q                                        # about 900 offline tests, all should pass
.venv/bin/python scripts/set_key.py SCRAPECREATORS_API_KEY          # paste; it's hidden
.venv/bin/python scripts/set_key.py OPENROUTER_API_KEY
.venv/bin/python scripts/set_key.py RADAR_DASHBOARD_PASSWORD --generate
bin/radar doctor                                                     # checks your keys and credits
bin/radar serve                                                      # http://localhost:8080 (--port 8090 if busy)
```

To put it on Railway after that, run `railway login` and then `scripts/deploy_railway.sh`. See
[INSTALL.md](INSTALL.md#option-b-railway-always-on).

## Let an agent use it every day

[`skills/lead-radar/SKILL.md`](skills/lead-radar/SKILL.md) is a skill any agent can follow: Claude Code, Codex, Hermes and
others. It uses your dashboard's API to create campaigns, run them and report new leads, and it lists the rules (read
only, never contact anyone). Give the agent your dashboard address and, as a secret, your dashboard password.

## For developers

[AGENTS.md](AGENTS.md) is the runbook: hard rules, the command line, settings, the API, the data model and the tests.

## License

[FSL-1.1-ALv2](LICENSE). You can use it, change it and run it for yourself or your clients for free. You can't sell it as
a competing hosted service. Each version becomes Apache 2.0 two years after release. Search providers, AI providers and
hosting bill you separately, on your own accounts.
