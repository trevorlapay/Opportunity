# Project Opportunity

A self-healing, containerized Python application that monitors the Orlando, FL area (and remote equivalents) for career opportunities, professional events, networking groups, and relevant news — filtered for a mid-career management candidate. Runs every 6 hours and delivers a rich HTML email digest only when new findings are detected.

---

## Install in one go

```bash
# 1. Clone, enter, copy env template
git clone <repo-url> opportunity && cd opportunity && cp .env.example .env

# 2. Install dependencies + Playwright browser (local dev)
py -3.11 -m pip install -r requirements.txt && py -3.11 -m playwright install chromium

# 3. Fill in Gmail + Anthropic creds interactively
py -3.11 src/manage.py setup

# 4. Generate filter + sources from USER_PREFS.md, then validate everything
py -3.11 src/manage.py build-sources

# 5. Start the scheduler (runs immediately, then every 6 hours)
py -3.11 src/main.py
```

Prefer Docker? After steps 1 and 3, just run `docker compose up --build`.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Configuration](#configuration)
3. [Running Locally](#running-locally)
4. [Running with Docker](#running-with-docker)
5. [Management CLI](#management-cli)
6. [How It Works](#how-it-works)
7. [Source Coverage](#source-coverage)
8. [Self-Healing](#self-healing)
9. [Adding New Sources](#adding-new-sources)
10. [Project Structure](#project-structure)

---

## Quick Start

### Prerequisites

- Python 3.11
- Docker & Docker Compose (for production)
- A Gmail account with an [App Password](https://myaccount.google.com/apppasswords) configured
- An [Anthropic API key](https://console.anthropic.com/) (optional — used for self-healing LLM lookup and the `research` command)

### 1. Copy the example env file

```bash
cp .env.example .env
```

### 2. Fill in credentials

```bash
python src/manage.py setup
```

This walks you through each required variable interactively and writes your `.env` file.

Or edit `.env` directly:

```env
GMAIL_USER=your_gmail@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
ANTHROPIC_API_KEY=sk-ant-...
RECIPIENT_EMAILS=recipient1@gmail.com,recipient2@gmail.com
```

### 3. Run

```bash
# Locally (Python 3.11)
py -3.11 src/main.py

# Production (Docker)
docker compose up --build
```

The pipeline fires **immediately on startup**, then repeats every 6 hours. If zero new items are found, no email is sent.

---

## Configuration

All behavioral parameters are driven by environment variables. No code changes required.

| Variable | Required | Default | Description |
|---|---|---|---|
| `GMAIL_USER` | Yes | — | Gmail address used to send emails |
| `GMAIL_APP_PASSWORD` | Yes | — | Gmail [App Password](https://myaccount.google.com/apppasswords) (not your account password) |
| `RECIPIENT_EMAILS` | Yes | — | Comma-separated list of digest recipients |
| `ANTHROPIC_API_KEY` | No | — | Used for self-healing LLM lookup and the `research` CLI command. App runs without it but those features are disabled. |
| `SEEN_ITEM_RETENTION_DAYS` | No | `90` | Days before a seen item can resurface |
| `LOG_LEVEL` | No | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

> **Gmail App Password:** In your Google Account, go to Security → 2-Step Verification → App Passwords. Generate one for "Mail" and paste the 16-character code (spaces are fine).

---

## Running Locally

Requires Python 3.11 and all dependencies installed:

```bash
# Install dependencies
py -3.11 -m pip install -r requirements.txt

# Install Playwright browser (for JS-rendered sites like Disney Careers)
py -3.11 -m playwright install chromium

# Validate that your .env is set up
py -3.11 src/manage.py setup

# Check which source URLs are reachable
py -3.11 src/manage.py validate-sources

# Dry run — scrapes everything, no email sent
py -3.11 src/manage.py test-run --verbose

# Start the scheduler (runs now, then every 6 hours)
py -3.11 src/main.py
```

---

## Running with Docker

Docker is the recommended way to run Opportunity in production. It handles the timezone, scheduling, and Playwright browser automatically.

```bash
# Build and start
docker compose up --build

# Run in the background
docker compose up -d --build

# View logs
docker compose logs -f

# Stop
docker compose down
```

The `./data` directory is mounted as a Docker volume, so `seen_items.json`, `sources.json`, and `run_log.json` persist across container restarts.

---

## Management CLI

`src/manage.py` provides all operational tooling without touching source code.

```bash
py -3.11 src/manage.py <command>
```

| Command | Description |
|---|---|
| `setup` | Interactive wizard to create or update `.env` |
| `set-key KEY VALUE` | Update a single env variable (e.g. `set-key ANTHROPIC_API_KEY sk-ant-...`) |
| `list-sources` | Print all sources with their ID, category, strategy, status, and URL |
| `add-source` | Interactively add a new source to `data/sources.json` |
| `update-source ID FIELD VALUE` | Update a single field on an existing source (e.g. `update-source disney_careers status healthy`) |
| `validate-sources` | HEAD-test every source URL and report healthy / unreachable |
| `revive [--id ID] [--older-than DAYS]` | Reset dead sources to `healthy` so the pipeline re-tries them. No args = revive ALL dead. See [Reviving dead sources](#reviving-dead-sources). |
| `test-run [--category jobs] [--verbose]` | Dry-run scrape without sending email |
| `apply-prefs` | Read `USER_PREFS.md` and regenerate the filter config (shows diff, asks to confirm) |
| `research [--add]` | Ask Claude for an **exhaustive**, cluster-by-cluster enumeration of new sources — every major regional employer, every named flagship property, every meta-aggregator. See [Research command](#research-command). |
| `build-sources` | **Run this after editing `USER_PREFS.md`** — applies prefs, researches and adds all new sources, then validates every URL. Full output of everything that changed. |

### After editing `USER_PREFS.md` — run `build-sources`

`build-sources` is the one command to run whenever preferences change. It chains all three update steps in the correct order and runs unattended:

```bash
py -3.11 src/manage.py build-sources
```

**What it does, in order:**

1. **Apply prefs** — reads `USER_PREFS.md`, calls Claude to generate new filter rules, prints a full diff of every pattern added or removed, and writes `data/filter_config.json`
2. **Research** — asks Claude to suggest new sources that fit the updated preferences (skipping anything already in `sources.json`), prints each suggestion with its URL and rationale, then adds all of them automatically
3. **Validate** — HEAD-tests every URL in the final `sources.json` (including newly added sources) and reports which are reachable

Validate runs last by design — it needs to see the complete, final source list after research has added everything.

When it finishes, run `test-run --verbose` to preview what the pipeline will find:

```bash
py -3.11 src/manage.py test-run --verbose
```

### Other examples

```bash
# Only scrape job sources, print all new findings
py -3.11 src/manage.py test-run --category jobs --verbose

# Fix a manually identified broken URL
py -3.11 src/manage.py update-source disney_careers active_url "https://jobs.disneycareers.com/..."

# Restore a dead source after fixing its URL
py -3.11 src/manage.py update-source disney_careers status healthy

# Research new sources and pick which ones to add interactively
py -3.11 src/manage.py research --add
```

### Personalising the pipeline — `USER_PREFS.md`

Open `USER_PREFS.md` in the repo root and fill it in — plain English, no coding knowledge needed. The more detail you give, the better the filter and research suggestions will be. After editing, run `build-sources` (see above).

The file has five sections:

---

#### `## About Me`

Your background. Paste a resume summary, describe your career history, or just write a few sentences. This is used to personalise research suggestions — the more context, the better.

> *Example:* "I'm a senior operations leader with 15 years of experience in hospitality and entertainment. I've managed teams of 50+ and led P&L for regional divisions. I hold an MBA and have a background in HR and project management."

---

#### `## Target Roles`

The job titles and levels you want to see. Be specific about seniority, function, and industry.

> *Example:* "I'm targeting Director, Senior Director, VP, and C-suite roles. I'm open to General Manager, Chief of Staff, Head of Operations, and similar titles. I'm interested in hospitality, entertainment, healthcare, and professional services. I'm not interested in pure technical or engineering management."

---

#### `## Geography`

Where you're willing to work. List cities, counties, or regions. Say whether you're open to remote or hybrid.

> *Example:* "I'm based in Orlando, FL and prefer roles in the Orlando metro area — Orange, Seminole, Osceola, and surrounding counties. I'm open to fully remote or hybrid. I'm not willing to relocate."

---

#### `## Exclusions`

Hard nos. Industries, role types, or topics to filter out entirely.

> *Example:* "No cybersecurity or information security roles. No roles below Manager level. No warehouse, logistics, or frontline management. No staffing or recruiting firms."

---

#### `## Other Preferences` *(optional)*

Anything else: salary range, company size, culture preferences, deal-breakers.

> *Example:* "I prefer companies with 500+ employees. Not interested in startups. Full-time only; no contract or temp."

---

### `research` command

`research` reads `USER_PREFS.md` and asks **claude-opus-4-6** for an **exhaustive** enumeration of opportunity avenues — the pipeline's goal is to be a **superset** of what a motivated human would find by manually browsing LinkedIn, Indeed, and local employer sites. Missing a major local employer or category is treated as a failure.

The research prompt forces Claude to work through a seven-bucket taxonomy:

| Bucket | What it covers |
|---|---|
| **A. Meta-aggregators** | Google Jobs (SerpAPI), JSearch, Adzuna, The Muse, USAJobs, RemoteOK, We Work Remotely, LinkedIn job RSS via third parties |
| **B. Major local employers** | Every employer with 500+ local headcount, **named individually** — e.g. *JW Marriott Orlando Grande Lakes*, *Ritz-Carlton Orlando Grande Lakes*, *Gaylord Palms*, *Signia by Hilton Orlando Bonnet Creek*, *AdventHealth*, *Orlando Health*, *Lockheed Martin Missiles & Fire Control*, *UCF*, etc. Generic brand-level career pages are treated as insufficient — specific property/campus pages are required. |
| **C. Executive search & board / fractional roles** | Heidrick, Russell Reynolds, Spencer Stuart, Korn Ferry, Egon Zehnder, DHR + BoardProspects, Bolster, Catalant, ExecuNet, Chief.com |
| **D. Local / regional job boards & associations** | Chamber of commerce boards, industry association boards (hotel-lodging, hospital, CIO council, CFO council), economic-development agencies |
| **E. Events & networking** | Eventbrite / Meetup filtered for leadership and industry events; chapters of ACG, CHRO/CFO forums; alumni career nights |
| **F. Signal sources** | Local business journals, growth/expansion announcements, exec-departure press releases, SEC 8-K backfill signals, M&A announcements |
| **G. Remote executive boards** | FlexJobs, We Work Remotely, RemoteOK executive, Himalayas, JustRemote, Working Nomads, Remote.co exec |

For each suggestion Claude returns: `id`, `name`, `category`, `url`, `strategy`, `cluster` (which bucket), and `notes`. The `cluster` and `notes` are persisted on each source for later coverage reporting.

Unlike the old 12-source cap, there is no ceiling — if your region has 40 relevant employers, the command will return 40. Output is a JSON array with `max_tokens=16384`.

With `--add`: you pick suggestions by number; selected sources are written to `data/sources.json` immediately and picked up on the next pipeline run.

> **Caveat:** New sources added via `research --add` still have empty `selectors: {}`. They will scrape zero items on the first run and may go dead after three consecutive empty runs. Check Claude's notes for URL uncertainty, fill in selectors (or convert the strategy to `rss_feed` / `workday_api` / `json_api` where possible), and rely on [auto-revive](#dead-sources-and-auto-revive) as the safety net.

---

## How It Works

### Pipeline (per run, every 6 hours)

```
1. Load sources from data/sources.json
2. For each source:
   a. Health check (HTTP HEAD)
   b. If unhealthy → Self-Healing pipeline (see below)
   c. Scrape using the source's configured strategy
   d. If zero results for 3 consecutive runs → Self-Healing pipeline
3. Apply candidate profile filter:
   - Title must match a seniority-level keyword (Manager, Director, VP, Chief, etc.)
   - Title must NOT be primarily cybersecurity (CISO, Security Director, etc.)
   - Location must be Orlando metro, Greater Central Florida, or Remote
4. Deduplicate against data/seen_items.json (SHA-256 fingerprint)
5. If net-new items exist → send HTML digest email
6. Log run metadata to data/run_log.json
```

### Candidate Profile Filter

**Included seniority levels:**
Manager, Senior Manager, Director, Senior Director, VP, SVP, EVP, C-Suite (CTO, COO, CEO, Chief of Staff, etc.), Head of, General Manager, Program Manager, Product Manager

**Excluded (cybersecurity-primary roles):**
CISO, Cybersecurity Manager, Security Director, Information Security Manager — and any role where the description is dominated by SOC, SIEM, penetration testing, threat intelligence, or incident response

**Geography:**
Orlando Metro (Orange, Seminole, Osceola counties), Greater Central Florida (Lake, Volusia, Brevard), within ~50 miles of Orlando, or fully remote. Roles with no location listed are assumed potentially remote and included.

### Scraping Strategies

| Strategy | Used For |
|---|---|
| `rss_feed` | Indeed, Orlando Business Journal, Orlando Sentinel, Florida Trend, PR Newswire |
| `html_list` | Government job boards, event sites, chamber of commerce pages |
| `html_search_result` | LinkedIn, Glassdoor, Built In, Lockheed Martin, Darden, Hilton, etc. |
| `workday_api` | Universal Parks & Resorts, AdventHealth, Orlando Health, NBCUniversal (Workday ATS — uses Playwright to capture the authenticated JSON response) |
| `playwright` | Walt Disney World Careers (JavaScript-rendered via headless Chromium) |
| `json_api` | Generic public JSON endpoints |
| `sitemap` | Sitemap XML URL pattern matching |

### Email Digest

An email is sent **only when at least one net-new item is found.** It groups findings into four sections (Job Postings, Career Events, Networking Groups, News) and includes a direct link for each item.

Subject line format: `[Opportunity] 🗂 X new findings — Day, Month Date, Year`

If nothing new is found across all sources, no email is sent at all.

---

## Source Coverage

### Job Postings (17 sources)
Indeed, LinkedIn, Walt Disney World Careers, Universal Parks & Resorts, NBCUniversal Corporate, AdventHealth, Orlando Health, Lockheed Martin, Siemens, Darden Restaurants, Hilton, Florida Blue, City of Orlando, Orange County Government, State of Florida (People First), Built In, Glassdoor

### Career Events & Expos (6 sources)
Eventbrite Orlando, CareerSource Central Florida, Orlando Economic Partnership, UCF Career Services, Florida Leads, Meetup.com

### Professional Networking (7 sources)
Orlando Young Professionals, Florida CIO Council, ACG Florida, Leadership Orlando, Orlando Regional Chamber, Osceola Chamber, Seminole County Chamber

### News & Articles (5 sources)
Orlando Business Journal, Orlando Sentinel Business, Florida Trend, Built In Orlando, PR Newswire

---

## Self-Healing

When a source fails (dead URL or zero results for 3 consecutive runs), the self-healing pipeline runs automatically:

| Step | Action |
|---|---|
| **1 — Quick retry** | Re-check the URL once after a 5-second pause (catches transient network blips) |
| **2 — Alternate URLs** | Try any `alternate_urls` configured in `sources.json`. The winning alternate is promoted to `active_url` and removed from the alternates list. |
| **3 — LLM lookup** | Ask Claude (`claude-sonnet-4-6`) whether it knows a current replacement URL. If yes → validate by scraping → swap in. If no → mark source `dead`. |
| **4 — Dead** | Source is skipped until the [14-day auto-revive](#dead-sources-and-auto-revive) fires, or until a manual `revive`. Stays in `sources.json` for auditability. |

**Every successful URL swap archives the previous URL** into `alternate_urls` (capped at 10 entries, oldest first is dropped) — so the heal pipeline accumulates fallbacks over time instead of discarding the URL that used to work. Next time the source breaks, Step 2 has a richer list to try before burning an LLM call.

**Persistence.** Every mutation — URL swap, status change, empty-run counter, heal timestamp — is written to `data/sources.json` via `config.save_sources()` immediately. In Docker, `./data` is mounted as a volume, so healed URLs survive container restarts. After one heal, all future runs (and all future containers) use the new URL automatically.

**Token efficiency:** The LLM lookup is intentionally minimal (~80 input tokens per call). It asks one question ("do you know a working URL?") with no web search, no agentic loops. To prevent runaway API spend, each source is subject to a **7-day cooldown** between LLM heal attempts, and no more than 5 LLM calls are made per pipeline run.

**Dead vs. skipped:** If the LLM cap or rate limit is hit during a run, the source is left in its current state and retried next run — it is never marked `dead` just because the cap was reached. Only a confirmed "Claude has no replacement" result triggers the permanent `dead` status.

### Dead sources and auto-revive

`dead` is **not** permanent. Two safety valves prevent the pipeline from slowly strangling itself:

1. **Auto-revive (automatic, no action needed).** Any source that has been `dead` for more than **14 days** is promoted back to `healthy` on the next pipeline run and re-tried. If it still fails, the normal heal pipeline kicks in. This catches the failure mode where a one-off URL hiccup, bot block, or since-fixed selector bug killed a source forever.
2. **Manual revive via CLI (one command).**
    ```bash
    # Revive ALL dead sources
    py -3.11 src/manage.py revive

    # Revive just one
    py -3.11 src/manage.py revive --id disney_careers

    # Revive only sources dead more than 7 days (useful after a config change)
    py -3.11 src/manage.py revive --older-than 7
    ```

Per-run revivals are recorded in `data/run_log.json` under `auto_revived`.

To recover a dead source after fixing its URL:

```bash
# Fix the URL
py -3.11 src/manage.py update-source <source_id> active_url "https://new-url.com"

# Clear the dead status (or just run 'revive --id <source_id>')
py -3.11 src/manage.py update-source <source_id> status healthy
```

---

## Adding New Sources

New sources can be added without any code changes.

### Option 1: AI-assisted discovery

```bash
py -3.11 src/manage.py research --add
```

Claude suggests sources you may have missed, explains why each adds value, and lets you pick which ones to add interactively.

### Option 2: Interactive CLI

```bash
py -3.11 src/manage.py add-source
```

### Option 3: Edit `data/sources.json` directly

Add a new entry following this schema:

```json
{
  "id": "my_new_source",
  "name": "My New Source",
  "category": "jobs",
  "active_url": "https://example.com/careers",
  "alternate_urls": [],
  "strategy": "html_list",
  "selectors": {
    "item_container": ".job-card",
    "title": ".job-title a",
    "location": ".job-location",
    "link": ".job-title a"
  },
  "dead_content_patterns": ["no jobs found", "page not found"],
  "status": "healthy",
  "last_verified": "2026-01-01T00:00:00Z",
  "consecutive_empty_runs": 0
}
```

For Workday-based portals, use `"strategy": "workday_api"` with an `api_config` block:

```json
{
  "strategy": "workday_api",
  "api_config": {
    "base_url": "https://company.wd5.myworkdayjobs.com",
    "endpoint": "/wday/cxs/company/SiteName/jobs",
    "search_text": "manager director",
    "limit": 50,
    "locations": []
  }
}
```

For JS-rendered sites not on Workday, use `"strategy": "playwright"` and add a `"wait_for_selector"` in `selectors`.

---

## Project Structure

```
opportunity/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── USER_PREFS.md            # ← Edit this to personalise filtering and research
├── .env                     # Not committed — contains your secrets
├── .env.example             # Committed — documents required variables
├── README.md
├── SPEC.md                  # Full product specification
├── src/
│   ├── main.py              # Entry point & orchestrator
│   ├── scraper.py           # Scraping engine (7 strategies)
│   ├── healer.py            # Self-healing pipeline (3 steps + dead status)
│   ├── filter.py            # Candidate profile filtering (reads filter_config.json)
│   ├── deduplicator.py      # SHA-256 fingerprinting & state management
│   ├── emailer.py           # HTML digest email via Gmail SMTP
│   ├── scheduler.py         # Run-on-startup + 6-hour scheduler
│   ├── config.py            # Environment variable loading
│   └── manage.py            # Management CLI (apply-prefs, research, test-run, etc.)
├── data/                    # Persistent volume (mounted in Docker)
│   ├── filter_config.json   # Generated by apply-prefs — do not edit by hand
│   ├── seen_items.json      # Fingerprints of all previously surfaced items
│   ├── sources.json         # Source list with health status and active URLs
│   └── run_log.json         # Log of every pipeline run
└── templates/
    └── email_digest.html    # Jinja2 HTML email template
```
