# SPEC.md — Project Opportunity

## Table of Contents
1. [Overview](#overview)
2. [Candidate Profile & Filtering Criteria](#candidate-profile--filtering-criteria)
3. [Data Sources](#data-sources)
4. [Scraping Architecture](#scraping-architecture)
5. [Self-Healing Mechanism](#self-healing-mechanism)
6. [Deduplication & State Management](#deduplication--state-management)
7. [Scheduling](#scheduling)
8. [Email Notification System](#email-notification-system)
9. [Error Handling & Alerting](#error-handling--alerting)
10. [Containerization](#containerization)
11. [Configuration](#configuration)
12. [Future Enhancements](#future-enhancements)
13. [Glossary](#glossary)

---

## Overview

**Project Opportunity** is a self-healing, containerized Python application that continuously monitors the Orlando, FL area (and remote equivalents) for career opportunities, professional events, career expos, networking groups, and relevant news articles matching a specific mid-career management candidate profile. It runs on an hourly schedule and delivers a rich HTML email digest to a defined recipient list only when new findings are detected.

---

## Candidate Profile & Filtering Criteria

The application filters all findings against the following candidate profile. This profile is baked into the application's keyword and relevance logic.

### Background
| Attribute | Detail |
|---|---|
| Most Recent Role | Senior Director, Cybersecurity — Equinix (5 years) |
| Prior Role | CTO — Project Echo (health hub technology nonprofit, 5 years) |
| Target Level | Management at any level |
| Domain Preference | Tech-adjacent or non-tech; **not** directly cybersecurity |

### Seniority Levels (Job Postings)
The following title tiers are in scope. All others are filtered out.

- Manager / Senior Manager
- Director / Senior Director
- VP / SVP / EVP
- C-Suite (CTO, COO, CEO, Chief of Staff, etc.)

### Title Keyword Inclusions (examples — not exhaustive)
`Manager`, `Director`, `Vice President`, `VP`, `Chief`, `Head of`, `Senior Manager`, `Senior Director`, `General Manager`, `Operations Manager`, `Program Manager`, `Product Manager`, `Managing Director`

### Title Keyword Exclusions
Any posting whose **primary function** is cybersecurity, even at a management level, should be filtered out. Exclusion signals include:

- `Cybersecurity Manager`, `Security Director`, `CISO`, `Information Security Manager`
- Postings whose description is dominated by terms like: `SOC`, `SIEM`, `penetration testing`, `threat intelligence`, `incident response` as the core deliverable

> **Note:** A role like "Director of IT Operations" at a healthcare company that *mentions* security as one of several responsibilities is **in scope**. A role where cybersecurity is the *primary function* is **out of scope**.

### Geography
Job postings must match at least one of the following:

- Orlando Metro (Orange, Seminole, Osceola counties)
- Greater Central Florida (Lake, Volusia, Brevard counties)
- Within approximately 50 miles of Orlando, FL
- Fully remote (any location listed, or no location listed)

### Work Arrangement
All arrangements are acceptable: remote, hybrid, and on-site. No filtering on this dimension.

### Additional Filters
None beyond title keywords and geography. Salary and company size are not filtering criteria.

---

## Data Sources

The application maintains a curated, versioned list of sources organized by category. Each source entry contains a URL, a scraping strategy identifier, a health status, and a last-verified timestamp.

### Category: Job Postings

| Source | Notes |
|---|---|
| LinkedIn Jobs | Search URL filtered by title keywords + Orlando location |
| Indeed | Search URL filtered by title keywords + zip code radius |
| Walt Disney World / Disney Careers | `jobs.disneycareers.com` — specific search pages, must be validated |
| Universal Orlando / Comcast NBCUniversal | Careers portal, filtered by management categories |
| AdventHealth Careers | Healthcare-adjacent; management roles |
| Orlando Health Careers | Healthcare system; management roles |
| Lockheed Martin (Orlando) | Defense/tech; management roles only |
| Siemens (Orlando area) | Tech/industrial; management |
| Darden Restaurants | HQ in Orlando; operations management roles |
| Hilton (corporate, Orlando area) | Hospitality operations management |
| Florida Blue | Insurance/tech; management |
| City of Orlando | Government management roles |
| Orange County Government | Government management roles |
| State of Florida Jobs (People First) | `peoplefirst.myflorida.com` |
| Built In (remote/tech management) | Tech-focused management roles |
| Glassdoor | Secondary/supplementary source |

> **Source list is designed to be extended.** The application should support adding new sources via configuration without code changes.

### Category: Career Expos & Events

| Source | Notes |
|---|---|
| Eventbrite (Orlando) | Search: "career fair", "job expo", "hiring event" in Orlando |
| CareerSource Central Florida | `careersourcecentralflorida.com/events` |
| Orlando Economic Partnership events | Management/executive-level professional events |
| UCF Career Services Events | Public career events open to non-students |
| Florida Leads (statewide leadership events) | Executive-level events |
| Meetup.com | Search: "career", "professional development", "leadership" in Orlando |

### Category: Professional Networking Groups

| Source | Notes |
|---|---|
| Orlando Young Professionals | Event listings and new group announcements |
| Florida CIO Council | CIO/tech executive networking events |
| Association for Corporate Growth (ACG) Florida | M&A and executive networking |
| Leadership Orlando | Program announcements |
| Local chambers of commerce (Orlando, Osceola, Seminole) | Business/professional events |
| LinkedIn Groups (Orlando Leadership, Central FL Tech) | New post/event monitoring where possible |

### Category: News & Articles

| Source | Notes |
|---|---|
| Orlando Business Journal | `bizjournals.com/orlando` — executive appointments, company expansions, org news |
| Orlando Sentinel (Business section) | Local business news relevant to management job market |
| Florida Trend | Statewide business and executive news |
| Built In Orlando | Tech company news, funding rounds, hiring announcements |
| PR Newswire (Orlando region filter) | Press releases about company expansions, new leadership hires |

---

## Scraping Architecture

### Design Philosophy
The scraper is **deterministic-first**. It follows a defined algorithm against known URLs without requiring an external AI API call on each run. The API fallback (self-healing) is invoked only when the deterministic path fails.

### Scraping Pipeline (per source, per run)

```
1. Load source list from sources.json
2. For each source:
   a. Validate URL health (HTTP HEAD request, check status code)
   b. If healthy → execute deterministic scrape strategy for that source type
   c. If unhealthy → trigger Self-Healing Mechanism (see below)
3. Parse results using source-specific extraction rules (CSS selectors / XPath)
4. Apply candidate profile filters (title keywords, geography, seniority)
5. Deduplicate against seen-items store
6. Collect net-new items
7. If net-new items exist → trigger email digest
8. Update state files
```

### Scrape Strategy Types

| Strategy ID | Description |
|---|---|
| `html_list` | Page contains a list of job cards or event cards; extract via CSS selectors |
| `html_search_result` | Paginated search result page; extract top N results per run |
| `rss_feed` | Source exposes an RSS/Atom feed; parse with `feedparser` |
| `json_api` | Source exposes a public (no-auth) JSON endpoint |
| `sitemap` | Parse sitemap XML for new URLs matching a pattern |

Each source in `sources.json` is assigned a strategy ID and a set of CSS selectors or XPath expressions specific to that source's current HTML structure.

### Fingerprinting
Each discovered item is fingerprinted using a hash of: `source_id + title + url`. This fingerprint is used for deduplication.

---

## Self-Healing Mechanism

### Trigger Conditions
Self-healing is triggered for a source when any of the following occur:

- HTTP status code is not 2xx (dead link)
- The page returns a 2xx but the expected content markers (CSS selectors) yield zero results
- The page content matches known "posting not found" / "this job no longer exists" patterns (configurable list of strings)
- Three consecutive runs return zero results from a source that historically returns results

### Self-Healing Algorithm

```
Phase 1 — Retry
  Wait 5 minutes, retry the original URL up to 2 more times.
  If successful → resume normal operation, log recovery.

Phase 2 — Alternate URL Discovery (Deterministic)
  Check sources.json for any configured alternate_urls for this source.
  Try each alternate URL in order.
  If one succeeds → update active URL in state, log the change.

Phase 3 — AI-Assisted Research (API Fallback)
  If Phase 1 and Phase 2 both fail:
    Invoke the Anthropic Claude API (claude-sonnet-4-20250514) with a prompt:
      "The following job/event source URL is broken: [url]. 
       The source is [source name]. 
       Find the current correct URL for their careers/events listing page 
       as of today and return it in JSON format: 
       {\"new_url\": \"...\", \"confidence\": \"high|medium|low\", \"notes\": \"...\"}"
    Parse the API response.
    Validate the returned URL (HTTP HEAD + content marker check).
    If valid → update sources.json with new URL, log the change.
    If invalid or low confidence → mark source as DEGRADED, trigger alert email.

Phase 4 — Alert
  If all phases fail:
    Mark source status as DEGRADED in sources.json.
    Send a self-healing failure alert email (see Error Handling).
    Continue processing all other sources normally.
    Retry DEGRADED sources once per day (not every hour).
```

### sources.json Schema

```json
{
  "sources": [
    {
      "id": "disney_careers",
      "name": "Walt Disney World Careers",
      "category": "jobs",
      "active_url": "https://jobs.disneycareers.com/search-jobs/Orlando%2C%20FL",
      "alternate_urls": [],
      "strategy": "html_search_result",
      "selectors": {
        "item_container": ".job-list-item",
        "title": ".job-title",
        "location": ".job-location",
        "link": "a"
      },
      "dead_content_patterns": [
        "this posting does not exist",
        "job no longer available",
        "page not found"
      ],
      "status": "healthy",
      "last_verified": "2025-01-01T00:00:00Z",
      "consecutive_empty_runs": 0
    }
  ]
}
```

---

## Deduplication & State Management

### Storage Format
All state is stored as flat JSON files in a `/data` directory mounted into the Docker container as a persistent volume.

### Files

| File | Purpose |
|---|---|
| `data/seen_items.json` | Hash fingerprints of all previously surfaced items |
| `data/sources.json` | Source list with health status and active URLs |
| `data/run_log.json` | Log of each hourly run: timestamp, sources checked, items found, emails sent |

### seen_items.json Schema

```json
{
  "items": [
    {
      "fingerprint": "sha256:abc123...",
      "title": "Senior Director of Operations",
      "source_id": "linkedin_jobs",
      "url": "https://linkedin.com/jobs/view/...",
      "first_seen": "2025-06-01T14:00:00Z",
      "category": "jobs"
    }
  ]
}
```

### Retention Policy
Items in `seen_items.json` are retained for **90 days** from `first_seen`. After 90 days, the fingerprint is purged, allowing the item to be re-surfaced if it reappears (e.g., a reposted job).

---

## Scheduling

- The application runs **every hour on the hour** via a cron job defined in the Docker container.
- Each run is a full pipeline execution across all healthy sources.
- If zero net-new items are found, the run completes silently (no email sent).
- DEGRADED sources are retried once every 24 hours, not every hour.

### Cron Expression
```
0 * * * *
```

---

## Email Notification System

### Trigger
An email is sent only when at least one net-new item is found in a given hourly run.

### Recipients
- `justina.rak@gmail.com`
- `trevor.n.lapay@gmail.com`

### Transport
Gmail via SMTP (port 587, STARTTLS). Credentials stored as environment variables:
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD` (Gmail App Password, not the account password)

### Email Format
Rich HTML email with the following structure:

```
Subject: [Opportunity] 🗂 X new findings — {Day, Month Date, Year}

Header:
  - App logo/wordmark ("Opportunity")
  - Run timestamp
  - Summary line: "We found X new item(s) across Y categories."

Body (one section per category, only shown if that category has new items):

  ┌─────────────────────────────────┐
  │ 💼 New Job Postings (N)         │
  ├─────────────────────────────────┤
  │ [Clickable Card]                │
  │   Title: Senior Director, Ops   │
  │   Company: AdventHealth         │
  │   Location: Orlando, FL / Remote│
  │   Source: LinkedIn              │
  │   [View Posting →]              │
  └─────────────────────────────────┘

  ┌─────────────────────────────────┐
  │ 📅 Career Events & Expos (N)    │
  ├─────────────────────────────────┤
  │ [Clickable Card]                │
  │   Event: Central FL Career Fair  │
  │   Date: June 15, 2025           │
  │   Location: Orange County CC    │
  │   [View Event →]                │
  └─────────────────────────────────┘

  ┌─────────────────────────────────┐
  │ 🤝 Networking Groups (N)        │
  └─────────────────────────────────┘

  ┌─────────────────────────────────┐
  │ 📰 News & Articles (N)          │
  └─────────────────────────────────┘

Footer:
  - "Powered by Project Opportunity"
  - Timestamp of run
  - Note: "You are receiving this because new items were found. No email = nothing new."
```

### Self-Healing Alert Email Format
A separate, plaintext-style alert email sent only to the recipient list when self-healing fails:

```
Subject: [Opportunity] ⚠️ Source Degraded: {Source Name}

Body:
  The following data source has failed and could not be automatically repaired:

  Source:    Walt Disney World Careers
  URL:       https://jobs.disneycareers.com/...
  Failure:   Dead link / No content found
  Attempts:  3 retries + AI-assisted URL discovery (no valid replacement found)
  Status:    DEGRADED — this source will be retried in 24 hours

  Action required: Please update the source URL manually in sources.json,
  or wait for the next automated retry.
```

---

## Error Handling & Alerting

| Scenario | Behavior |
|---|---|
| Source URL returns non-2xx | Trigger self-healing pipeline |
| Source returns 2xx but zero results (3 consecutive runs) | Trigger self-healing pipeline |
| Page content matches dead-posting pattern | Mark item invalid, skip, do not fingerprint |
| Self-healing Phase 1–3 all fail | Mark source DEGRADED, send alert email |
| SMTP send failure | Log error, retry once after 60 seconds |
| Claude API call fails during self-healing | Skip Phase 3, go directly to Phase 4 alert |
| Scrape runtime exceeds 45 minutes | Log timeout warning, abort run, resume next hour |
| JSON state file corruption | Log critical error, rebuild from empty state, send alert email |

All errors are written to `data/run_log.json` with full stack traces and timestamps.

---

## Containerization

### Docker Structure

```
opportunity/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env                     # Not committed; contains secrets
├── .env.example             # Committed; documents required vars
├── src/
│   ├── main.py              # Entry point / orchestrator
│   ├── scraper.py           # Scraping engine
│   ├── healer.py            # Self-healing logic
│   ├── filter.py            # Candidate profile filtering
│   ├── deduplicator.py      # Fingerprint + state management
│   ├── emailer.py           # Email composition + SMTP send
│   ├── scheduler.py         # Cron wrapper
│   └── config.py            # Loads env vars and sources.json
├── data/                    # Mounted persistent volume
│   ├── seen_items.json
│   ├── sources.json
│   └── run_log.json
└── templates/
    └── email_digest.html    # Jinja2 HTML email template
```

### docker-compose.yml (outline)

```yaml
version: "3.9"
services:
  opportunity:
    build: .
    restart: always
    env_file: .env
    volumes:
      - ./data:/app/data
    environment:
      - TZ=America/New_York
```

### .env.example

```
GMAIL_USER=your_gmail@gmail.com
GMAIL_APP_PASSWORD=your_app_password_here
ANTHROPIC_API_KEY=your_anthropic_key_here
RECIPIENT_EMAILS=justina.rak@gmail.com,trevor.n.lapay@gmail.com
RUN_TIMEZONE=America/New_York
SEEN_ITEM_RETENTION_DAYS=90
SELF_HEAL_RETRY_INTERVAL_MINUTES=5
SELF_HEAL_MAX_RETRIES=2
LOG_LEVEL=INFO
```

---

## Configuration

All behavioral parameters are driven by environment variables and `sources.json`. No hardcoded values should exist in application logic except defaults. The following are configurable without code changes:

| Parameter | Env Var | Default |
|---|---|---|
| Recipient emails | `RECIPIENT_EMAILS` | (required) |
| Gmail credentials | `GMAIL_USER`, `GMAIL_APP_PASSWORD` | (required) |
| Anthropic API key | `ANTHROPIC_API_KEY` | (required for self-healing Phase 3) |
| Retention period | `SEEN_ITEM_RETENTION_DAYS` | `90` |
| Self-heal retry interval | `SELF_HEAL_RETRY_INTERVAL_MINUTES` | `5` |
| Self-heal max retries | `SELF_HEAL_MAX_RETRIES` | `2` |
| Timezone | `RUN_TIMEZONE` | `America/New_York` |
| Log level | `LOG_LEVEL` | `INFO` |

---

## Future Enhancements

The following features are explicitly out of scope for v1 but should be considered in architectural decisions to avoid painting the project into a corner:

1. **Web Dashboard** — A lightweight read-only UI (e.g., Flask or FastAPI + simple HTML) to browse all findings, filter by category or date, and view source health status. State files (JSON) are already structured to support this without schema changes.

2. **Candidate Profile Configuration via UI** — Allow editing of keyword filters and seniority tiers through a settings page rather than editing source code.

3. **Digest Frequency Control** — Allow recipients to opt into daily digests vs. hourly, or set a quiet-hours window.

4. **Source Health Dashboard** — Visual display of which sources are healthy, degraded, or recovering.

5. **Multiple Candidate Profiles** — Support running the pipeline for more than one candidate, each with their own filters, recipients, and digest.

6. **Slack / SMS notifications** — Parallel delivery channels in addition to email.

---

## Glossary

| Term | Definition |
|---|---|
| **Self-healing** | The application's ability to detect broken sources and automatically find and validate replacement URLs without manual intervention |
| **Deterministic scraping** | Scraping that follows a fixed, repeatable algorithm against known URLs, producing consistent results without randomness or AI inference on each run |
| **Fingerprint** | A SHA-256 hash of a discovered item's source ID + title + URL, used to uniquely identify it for deduplication |
| **DEGRADED** | A source status indicating all self-healing attempts have failed and human review is required |
| **Net-new item** | A discovered item whose fingerprint does not exist in `seen_items.json` |
| **Phase 3 / API Fallback** | The self-healing step that invokes the Claude API to research a replacement URL when all deterministic recovery methods have failed |
