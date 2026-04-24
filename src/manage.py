"""
manage.py — Project Opportunity management CLI.

Commands:
  setup             Interactive wizard to create/update the .env file
  list-sources      Print all sources with their status and strategy
  add-source        Interactively add a new source to sources.json
  update-source     Update a field on an existing source
  validate-sources  Test each source URL (HEAD request) and report health
  test-run          Run a single scrape pass without sending email (dry run)
  set-key           Update a single env var (e.g., ANTHROPIC_API_KEY)
  apply-prefs       Read USER_PREFS.md and regenerate data/filter_config.json
  research          Ask Claude to suggest new sources not in the pipeline
                    (use --add to interactively add selected suggestions)
  build-sources     First-time setup: apply-prefs → research → add all → validate

Usage:
  python src/manage.py <command> [options]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError for non-ASCII titles)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE_DIR        = Path(__file__).resolve().parent.parent
DATA_DIR        = BASE_DIR / "data"
ENV_FILE        = BASE_DIR / ".env"
SOURCES_FILE    = DATA_DIR / "sources.json"
FILTER_CFG_FILE = DATA_DIR / "filter_config.json"
USER_PREFS_FILE = BASE_DIR / "USER_PREFS.md"

REQUIRED_VARS = [
    ("GMAIL_USER", "Gmail address used to send emails"),
    ("GMAIL_APP_PASSWORD", "Gmail App Password (not your account password)"),
    ("RECIPIENT_EMAILS", "Comma-separated list of recipient emails"),
    ("ANTHROPIC_API_KEY", "Anthropic API key (used for self-healing Phase 3)"),
]

OPTIONAL_VARS = [
    ("SEEN_ITEM_RETENTION_DAYS", "90"),
    ("LOG_LEVEL", "INFO"),
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _write_env(env: dict[str, str]) -> None:
    lines = []
    for k, v in env.items():
        lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n")
    print(f"  ✓ Wrote {ENV_FILE}")


def _load_sources() -> list[dict]:
    if not SOURCES_FILE.exists():
        return []
    with open(SOURCES_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh).get("sources", [])


def _save_sources(sources: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SOURCES_FILE, "w", encoding="utf-8") as fh:
        json.dump({"sources": sources}, fh, indent=2)


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {label}{suffix}: ").strip()
    return val if val else default


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_setup(args) -> None:
    print("\n=== Project Opportunity — Setup Wizard ===\n")
    env = _read_env()

    for var, description in REQUIRED_VARS:
        current = env.get(var, "")
        hint = f"(current: {'***' if current else 'not set'})"
        print(f"  {var} — {description} {hint}")
        val = input(f"    Enter value (press Enter to keep current): ").strip()
        if val:
            env[var] = val

    print("\n  Optional parameters (press Enter to keep defaults):")
    for var, default in OPTIONAL_VARS:
        current = env.get(var, default)
        val = _prompt(f"{var}", current)
        env[var] = val

    _write_env(env)
    print("\nSetup complete. Run 'python src/manage.py validate-sources' to check connectivity.")


def cmd_set_key(args) -> None:
    if not args.key or not args.value:
        print("Usage: python src/manage.py set-key KEY VALUE")
        sys.exit(1)
    env = _read_env()
    env[args.key] = args.value
    _write_env(env)
    print(f"  ✓ {args.key} updated.")


def cmd_list_sources(args) -> None:
    sources = _load_sources()
    if not sources:
        print("No sources found in data/sources.json")
        return

    fmt = "  {id:<35} {category:<12} {strategy:<16} {status:<10} {url}"
    print("\n" + fmt.format(
        id="ID", category="CATEGORY", strategy="STRATEGY",
        status="STATUS", url="ACTIVE URL"
    ))
    print("  " + "-" * 120)
    for s in sources:
        print(fmt.format(
            id=s["id"][:34],
            category=s.get("category", "")[:11],
            strategy=s.get("strategy", "")[:15],
            status=s.get("status", "healthy")[:9],
            url=s.get("active_url", "")[:80],
        ))
    print(f"\n  Total: {len(sources)} sources\n")


def cmd_add_source(args) -> None:
    print("\n=== Add New Source ===\n")
    sources = _load_sources()
    existing_ids = {s["id"] for s in sources}

    source_id = _prompt("Source ID (unique snake_case key)")
    if source_id in existing_ids:
        print(f"  ERROR: Source ID '{source_id}' already exists.")
        sys.exit(1)

    name = _prompt("Display name")
    category = _prompt("Category (jobs/events/networking/news)", "jobs")
    active_url = _prompt("Active URL")
    strategy = _prompt(
        "Strategy (html_list/html_search_result/rss_feed/json_api/playwright/workday_api)",
        "html_list"
    )

    new_source = {
        "id": source_id,
        "name": name,
        "category": category,
        "active_url": active_url,
        "alternate_urls": [],
        "strategy": strategy,
        "selectors": {},
        "api_config": {} if strategy in ("workday_api", "json_api") else None,
        "dead_content_patterns": [],
        "status": "healthy",
        "last_verified": datetime.now(timezone.utc).isoformat(),
        "consecutive_empty_runs": 0,
    }
    if new_source["api_config"] is None:
        del new_source["api_config"]

    sources.append(new_source)
    _save_sources(sources)
    print(f"\n  ✓ Source '{source_id}' added to data/sources.json")
    print("  Tip: Edit data/sources.json to add selectors or api_config details.")


def cmd_update_source(args) -> None:
    sources = _load_sources()
    target = next((s for s in sources if s["id"] == args.source_id), None)
    if not target:
        print(f"  ERROR: Source '{args.source_id}' not found.")
        sys.exit(1)

    field = args.field
    value = args.value

    # Handle nested JSON values
    try:
        parsed = json.loads(value)
        target[field] = parsed
    except (json.JSONDecodeError, ValueError):
        target[field] = value

    _save_sources(sources)
    print(f"  ✓ Source '{args.source_id}' field '{field}' updated.")


def cmd_revive(args) -> None:
    """
    Reset dead sources back to healthy so the pipeline re-attempts them.

    Without arguments: revives EVERY dead source.
    With --id <source_id>: revives just that one.
    With --older-than <days>: only revives sources marked dead more than N days ago.
    """
    sources = _load_sources()
    if not sources:
        print("No sources found.")
        return

    target_id = getattr(args, "id", None)
    older_than = getattr(args, "older_than", None)
    cutoff = None
    if older_than is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(older_than))

    revived = []
    for s in sources:
        if s.get("status") != "dead":
            continue
        if target_id and s["id"] != target_id:
            continue
        if cutoff is not None:
            last = s.get("last_verified", "")
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt > cutoff:
                    continue
            except (ValueError, TypeError):
                pass  # unparseable timestamp → revive anyway
        s["status"] = "healthy"
        s["consecutive_empty_runs"] = 0
        s.pop("last_llm_heal_ts", None)  # clear cooldown so healer can try again
        revived.append(s["id"])

    if not revived:
        print("Nothing to revive.")
        return

    _save_sources(sources)
    print(f"\n  ✓ Revived {len(revived)} source(s):")
    for sid in revived:
        print(f"    - {sid}")
    print(
        "\n  Next run will retry these. Sources with empty selectors will likely\n"
        "  go dead again on the first run — consider adding selectors first, or\n"
        "  running 'build-sources' to let Claude suggest better endpoints.\n"
    )


def cmd_validate_sources(args) -> None:
    import requests

    sources = _load_sources()
    if not sources:
        print("No sources to validate.")
        return

    print(f"\nValidating {len(sources)} sources …\n")
    healthy = 0
    degraded = 0

    for source in sources:
        url = source.get("active_url", "")
        if not url:
            print(f"  [SKIP] {source['id']} — no active_url")
            continue
        try:
            resp = requests.head(url, timeout=10, allow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0 (compatible; OpportunityBot/1.0)"
            })
            if resp.status_code == 405:
                resp = requests.get(url, timeout=10, allow_redirects=True)
            code = resp.status_code
            mark = "✓" if code < 400 else "✗"
            status_label = "HEALTHY" if code < 400 else "DEGRADED"
            print(f"  [{mark}] {source['id']:<40} HTTP {code}  {status_label}")
            if code < 400:
                healthy += 1
            else:
                degraded += 1
        except Exception as exc:
            print(f"  [✗] {source['id']:<40} ERROR: {exc}")
            degraded += 1

    print(f"\nResults: {healthy} healthy, {degraded} degraded/unreachable\n")


def cmd_test_run(args) -> None:
    """Run a single scrape + filter + dedup pass without sending email."""
    # Load .env manually so we don't need all vars set
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)

    import config
    from scraper import scrape_source
    from filter import apply_profile_filter
    from deduplicator import get_new_items

    sources = config.load_sources()
    category_filter = args.category  # optional: "jobs", "events", etc.

    all_raw = []
    for source in sources:
        if category_filter and source.get("category") != category_filter:
            continue
        if source.get("status") == "DEGRADED":
            print(f"  [SKIP] {source['id']} (DEGRADED)")
            continue
        print(f"  Scraping {source['id']} …", end=" ", flush=True)
        results = scrape_source(source)
        print(f"{len(results)} items")
        all_raw.extend(results)

    filtered = apply_profile_filter(all_raw)
    new_items = get_new_items(filtered)

    print(f"\nDry run complete:")
    print(f"  Raw items:    {len(all_raw)}")
    print(f"  After filter: {len(filtered)}")
    print(f"  Net-new:      {len(new_items)}")

    if new_items and args.verbose:
        print("\nNet-new items:")
        for item in new_items:
            d = item.to_dict()
            print(f"  [{d['category']}] {d['title']} | {d['location']} | {d['url']}")


# ── Candidate profile — loaded from USER_PREFS.md ─────────────────────────────

def _load_user_prefs() -> str:
    """Return the contents of USER_PREFS.md, or a fallback notice if missing."""
    if USER_PREFS_FILE.exists():
        return USER_PREFS_FILE.read_text(encoding="utf-8").strip()
    return (
        "No USER_PREFS.md found. "
        "Create one and run apply-prefs to personalise the pipeline."
    )


# ── Shared LLM helpers (used by apply-prefs, research, and build-sources) ──────

def _llm_call(client, prompt: str, label: str, max_tokens: int = 8192) -> tuple[str, str]:
    """Make a streaming Claude API call; echo tokens to stdout as they arrive.
    Returns (raw_text, stop_reason). Exits on failure."""
    import anthropic
    print(f"\n── {label}: streaming response (max_tokens={max_tokens}) ──", flush=True)
    chunks: list[str] = []
    try:
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                chunks.append(text)
                print(text, end="", flush=True)
            final = stream.get_final_message()
    except anthropic.APIError as exc:
        print()
        print(f"ERROR: Anthropic API call failed ({label}): {exc}")
        sys.exit(1)

    print("\n── end stream ──", flush=True)

    stop_reason = getattr(final, "stop_reason", None) or ""
    usage = getattr(final, "usage", None)
    if usage is not None:
        print(f"  usage: input={getattr(usage, 'input_tokens', '?')}  "
              f"output={getattr(usage, 'output_tokens', '?')}  "
              f"stop_reason={stop_reason!r}")

    if stop_reason == "max_tokens":
        print(f"WARNING: {label} response was cut off (max_tokens reached at {max_tokens}).")

    # Prefer the text we streamed — it's what we actually saw. Fall back to
    # final.content in case the stream produced nothing (shouldn't happen).
    raw = "".join(chunks).strip()
    if not raw:
        for block in getattr(final, "content", []) or []:
            if getattr(block, "type", "") == "text":
                raw = (block.text or "").strip()
                break

    if not raw:
        print(f"ERROR: Claude returned no text for {label}.")
        print(f"  stop_reason={stop_reason!r}")
        print(f"  Content blocks: {[getattr(b, 'type', '?') for b in getattr(final, 'content', []) or []]}")
        sys.exit(1)

    return raw, stop_reason


def _build_filter_config(client, prefs: str) -> dict:
    """Call Claude to turn USER_PREFS text into a filter_config dict."""
    prompt = f"""You are configuring a job-opportunity pipeline filter for a specific candidate.

Read the candidate's preferences below and produce a filter_config.json.

CANDIDATE PREFERENCES:
{prefs}

OUTPUT REQUIREMENTS:
Return a single JSON object with exactly these keys:

  title_include_patterns      — list of Python regex strings (case-insensitive).
                                 A job title must match at least one to be included.
                                 Use \\b word boundaries. Be generous — err toward inclusion.

  title_exclude_patterns      — list of Python regex strings. A title matching any of
                                 these is dropped regardless of include matches.

  description_exclude_terms   — list of plain lowercase strings. If a job description
                                 contains this many of these terms, the role is dropped.

  description_exclude_threshold — integer: minimum hits in description_exclude_terms
                                   required to trigger exclusion (default 3).

  geography_include_terms     — list of lowercase place-name strings (city, county, region).
                                 A job must mention one of these (or a remote term) to pass.
                                 Include all cities/counties/regions the candidate named.

  remote_terms                — list of lowercase strings that indicate a remote-friendly
                                 role ("remote", "work from home", etc.).

  geography_exempt_categories — list of category strings that skip geography filtering
                                 entirely (always keep ["events", "networking", "news"]).

RULES:
- Use double-escaped backslashes in regex strings (e.g. "\\\\bmanager\\\\b").
- Do not add a "_comment" key.
- Return JSON only — no markdown, no explanation.

JSON schema:
{{
  "title_include_patterns": [...],
  "title_exclude_patterns": [...],
  "description_exclude_terms": [...],
  "description_exclude_threshold": 3,
  "geography_include_terms": [...],
  "remote_terms": [...],
  "geography_exempt_categories": ["events", "networking", "news"]
}}"""

    raw, _ = _llm_call(client, prompt, "apply-prefs")
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start < 0 or end <= start:
        print("ERROR: Claude did not return a JSON object. Raw:\n")
        print(raw[:2000])
        sys.exit(1)
    try:
        return json.loads(raw[start:end])
    except json.JSONDecodeError as exc:
        print(f"ERROR: Could not parse filter config JSON: {exc}\nRaw:\n{raw}")
        sys.exit(1)


def _write_filter_config(new_cfg: dict) -> None:
    new_cfg["_comment"] = "Generated by manage.py apply-prefs from USER_PREFS.md. Do not edit by hand."
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILTER_CFG_FILE.write_text(json.dumps(new_cfg, indent=2), encoding="utf-8")


def _print_filter_diff(current_cfg: dict, new_cfg: dict) -> None:
    print("=" * 60)
    print("PROPOSED FILTER CHANGES")
    print("=" * 60)

    def _show(label: str, key: str) -> None:
        old = set(current_cfg.get(key, []))
        new = set(new_cfg.get(key, []))
        added, removed = sorted(new - old), sorted(old - new)
        print(f"\n{label}:  {len(new & old)} unchanged", end="")
        if added:
            print(f",  +{len(added)} added")
            for item in added[:8]:
                print(f"    + {item}")
            if len(added) > 8:
                print(f"    … and {len(added) - 8} more")
        if removed:
            print(f",  -{len(removed)} removed")
            for item in removed[:8]:
                print(f"    - {item}")
            if len(removed) > 8:
                print(f"    … and {len(removed) - 8} more")
        if not added and not removed:
            print()

    _show("Title include patterns",    "title_include_patterns")
    _show("Title exclude patterns",    "title_exclude_patterns")
    _show("Description exclude terms", "description_exclude_terms")
    _show("Geography include terms",   "geography_include_terms")
    _show("Remote terms",              "remote_terms")

    old_t = current_cfg.get("description_exclude_threshold", 3)
    new_t = new_cfg.get("description_exclude_threshold", 3)
    if old_t != new_t:
        print(f"\nDescription exclude threshold: {old_t} → {new_t}")


def _fetch_source_suggestions(client, sources: list[dict]) -> list[dict]:
    """Call Claude and return a list of suggested new source dicts.

    The prompt forces an exhaustive enumeration of opportunity avenues —
    major employer clusters (including named properties, not just parent
    brands), meta-aggregators, signal sources, and niche boards — so the
    output is a SUPERSET of what a motivated human would manually check.
    """
    user_prefs = _load_user_prefs()
    existing_summary = "\n".join(
        f"  - {s['name']} ({s.get('category','?')}) — {s.get('active_url','')[:60]}"
        for s in sources
    )
    prompt = f"""You are building an exhaustive opportunity-monitoring pipeline for a candidate.
The pipeline MUST be a SUPERSET of what any motivated human could find by manually
browsing LinkedIn, Indeed, and local employer sites. Missing a major local employer
or category is a failure.

CANDIDATE PREFERENCES (from USER_PREFS.md):
{user_prefs}

SOURCES ALREADY MONITORED (do not duplicate these; suggest ADJACENT or MISSING ones):
{existing_summary}

TASK:
Exhaustively enumerate every source that could surface a matching opportunity.
Do NOT stop at 12 — if the candidate's region has 40 relevant employers, suggest 40.
Think like a headhunter who knows the region intimately.

Work through this taxonomy and suggest sources in EVERY bucket that applies to the
candidate's region and role targets. Name SPECIFIC properties, not just parent brands
(e.g. "JW Marriott Orlando Grande Lakes" and "Ritz-Carlton Orlando Grande Lakes",
not just "Marriott"). Include the actual ATS career page for each.

  A. META-AGGREGATORS (highest leverage — one source, many employers):
     - Google Jobs via SerpAPI or similar
     - JSearch (LinkedIn + Indeed + Glassdoor + ZipRecruiter normalised)
     - Adzuna public API
     - The Muse API, USAJobs API, RemoteOK, We Work Remotely
     - LinkedIn job RSS via third-party feeds
     - Indeed publisher RSS variants

  B. MAJOR LOCAL EMPLOYERS — enumerate by industry cluster. For the candidate's region
     (use the Geography section of USER_PREFS), list every employer with 500+ local
     headcount. Prefer Workday / Greenhouse / Lever / SmartRecruiters / Ashby / Workable
     ATS endpoints over scraping corporate career pages — they return stable JSON.
     Clusters to cover (add/remove based on region):
       • Hospitality & lodging — name EVERY large hotel/resort property individually
         (flagship hotels, convention hotels, destination resorts). Generic brand
         career pages are INSUFFICIENT; also add the property-specific posting page
         where one exists.
       • Theme parks, attractions, entertainment venues, professional sports teams
       • Healthcare systems, hospitals, specialty-care networks, insurers, MCOs
       • Defense, aerospace, modeling & simulation, government contractors
       • Technology employers (SaaS, gaming, ad-tech, fintech) with local offices
       • Higher ed — universities, colleges, research institutes
       • State/county/city government + utility authorities + transit/airport authorities
       • Professional services — Big 4, mid-tier consulting, regional law firms
       • Retail, restaurant, and consumer-brand HQs headquartered in the region
       • Financial services — regional banks, wealth managers, insurance HQs
       • Major non-profits and foundations

  C. EXECUTIVE SEARCH & FRACTIONAL / BOARD ROLES:
     - Named exec-search firms' current-searches / "open assignments" pages filtered
       for the region (Heidrick, Russell Reynolds, Spencer Stuart, Korn Ferry,
       Egon Zehnder, DHR, and regional boutiques)
     - Board-seat and fractional platforms (BoardProspects, Bolster, Catalant,
       ExecuNet, Chief.com events)

  D. LOCAL / REGIONAL JOB BOARDS & ASSOCIATIONS:
     - Regional chamber of commerce job boards
     - Industry association job boards (e.g. hotel & lodging association,
       hospital association, CIO council, CFO council)
     - Economic-development agency hiring pages

  E. EVENTS & NETWORKING (candidate may passively learn of roles here):
     - Eventbrite / Meetup filtered for leadership, career, networking, industry
     - Local chapters of national associations (ACG, CHRO forums, CFO forums)
     - Chamber of commerce signature events
     - University alumni career nights

  F. SIGNAL SOURCES (roles not yet posted — proactive hunting):
     - Local business journal feeds (bizjournals, Orlando Inno, Axios Local)
     - Growth/expansion announcements (new HQ, new office, recent funding)
     - Executive-departure press releases and SEC 8-K filings implying backfills
     - M&A announcements implying org restructuring
     - Major construction / development projects hiring leadership

  G. REMOTE-SPECIFIC BOARDS for executive/director roles:
     - FlexJobs (paid but high quality), We Work Remotely, RemoteOK executive feed,
       Himalayas, JustRemote, Working Nomads, Remote.co exec

For EACH suggestion produce:
  id         — unique snake_case identifier, NO hyphens/spaces; prefix per cluster
               (hosp_*, health_*, defense_*, tech_*, edu_*, gov_*, search_*, signal_*,
               meta_*, remote_*, events_*, board_*)
  name       — human display name including the specific property if applicable
  category   — one of: jobs | events | networking | news
  url        — the MOST SPECIFIC, direct URL to the listings/feed/API endpoint.
               NEVER a homepage. For ATS: use the actual /jobs JSON endpoint if you
               know the tenant. Flag any guessed tenant IDs in notes.
  strategy   — one of: rss_feed | html_list | html_search_result | json_api |
                      playwright | workday_api | sitemap
  cluster    — which taxonomy letter above (A-G) and the named sub-cluster.
               Example: "B-hospitality" or "F-signal".
  notes      — 1-2 sentences: why this fits the candidate; flag URL uncertainty;
               mention the ATS (Workday tenant, Greenhouse slug, etc.) if applicable.

HARD REQUIREMENTS:
- Cover every major employer ≥ 500 local headcount the candidate could plausibly
  target — do NOT omit any because it "seems obvious". Obvious is good.
- Name specific hotel properties, specific hospital campuses, specific government
  authorities. Do not collapse a city's entire hospitality sector into one entry.
- Prefer ATS endpoints (Workday, Greenhouse, Lever) over corporate career pages.
- Suggest meta-aggregators (Category A) even if the candidate might need an API key
  — the pipeline operator will decide whether to wire them up.
- If a source is already in the existing list but its URL looks stale or generic,
  suggest a more specific replacement URL as a new entry (different id, new notes).

OUTPUT FORMAT — THIS IS A HARD REQUIREMENT:
- Your VERY FIRST character MUST be `[`. Do not write ANY preamble, explanation,
  apology, or markdown. No "Here are…", no code fences, no headings. Just the
  JSON array, starting with `[` and ending with `]`.
- Keep each entry's `notes` field to at most 2 short sentences — the response
  budget is finite and long notes push later entries out of the response.
- If you run low on output budget, finish the CURRENT object cleanly, then close
  the array with `]` rather than leaving an object half-written.

Schema: [{{"id": "...", "name": "...", "category": "...", "url": "...", "strategy": "...", "cluster": "...", "notes": "..."}}]"""

    # Larger budget: the taxonomy produces dozens of suggestions.
    raw, stop_reason = _llm_call(client, prompt, "research", max_tokens=32768)
    start = raw.find("[")
    if start < 0:
        print("ERROR: Claude did not return a JSON array (no `[` in response).")
        print(f"  stop_reason={stop_reason!r}  len(raw)={len(raw)}")
        print("  First 3000 chars of response:\n")
        print(raw[:3000])
        print("\n  (If stop_reason is 'max_tokens', the model spent its entire budget on preamble. "
              "Try simplifying USER_PREFS.md or rerun — the stricter output instructions should prevent this.)")
        sys.exit(1)

    # Happy path: well-formed array.
    end = raw.rfind("]") + 1
    if end > start:
        try:
            return json.loads(raw[start:end])
        except json.JSONDecodeError:
            pass  # fall through to salvage

    # Salvage path: response was cut off mid-array. Parse as many complete
    # object entries as we can from the prefix, then return those.
    if stop_reason == "max_tokens":
        print("  Attempting to salvage complete entries from truncated output …")
    salvaged = _salvage_json_array(raw[start:])
    if salvaged:
        print(f"  Recovered {len(salvaged)} complete suggestion(s) from truncated response.")
        return salvaged

    print("ERROR: Could not parse source suggestions JSON. Raw:\n")
    print(raw[:2000])
    sys.exit(1)


def _salvage_json_array(text: str) -> list[dict]:
    """Given a string starting with '[' that may be truncated, return whatever
    complete top-level JSON objects we can decode in order."""
    decoder = json.JSONDecoder()
    i = text.find("[")
    if i < 0:
        return []
    i += 1
    out: list[dict] = []
    n = len(text)
    while i < n:
        while i < n and text[i] in " \t\n\r,":
            i += 1
        if i >= n or text[i] != "{":
            break
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


def _add_sources(suggestions: list[dict], sources: list[dict]) -> list[str]:
    """
    Append suggestions to sources list (skipping duplicates).
    Returns list of added IDs. Caller must call _save_sources.
    """
    existing_ids  = {s["id"] for s in sources}
    existing_urls = {s.get("active_url", "") for s in sources}
    now_ts = datetime.now(timezone.utc).isoformat()
    added = []
    for s in suggestions:
        sid = s.get("id", "").strip()
        url = s.get("url", "").strip()
        if not sid:
            continue
        if sid in existing_ids:
            print(f"  SKIP (duplicate id):  {sid}")
            continue
        if url and url in existing_urls:
            print(f"  SKIP (duplicate url): {sid}")
            continue
        new_entry = {
            "id": sid,
            "name": s.get("name", sid),
            "category": s.get("category", "jobs"),
            "active_url": url,
            "alternate_urls": [],
            "strategy": s.get("strategy", "html_list"),
            "selectors": {},
            "dead_content_patterns": [],
            "status": "healthy",
            "last_verified": now_ts,
            "consecutive_empty_runs": 0,
        }
        # Preserve taxonomy cluster + research notes for coverage reporting.
        if s.get("cluster"):
            new_entry["cluster"] = s["cluster"]
        if s.get("notes"):
            new_entry["research_notes"] = s["notes"]
        sources.append(new_entry)
        existing_ids.add(sid)
        existing_urls.add(url)
        added.append(sid)
    return added


def _validate_urls(sources: list[dict]) -> tuple[int, int]:
    """HEAD-test all source URLs. Prints results. Returns (healthy, unreachable)."""
    import requests
    healthy = unreachable = 0
    for source in sources:
        url = source.get("active_url", "")
        if not url:
            print(f"  [SKIP] {source['id']:<40} no active_url")
            continue
        try:
            resp = requests.head(url, timeout=10, allow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (compatible; OpportunityBot/1.0)"})
            if resp.status_code == 405:
                resp = requests.get(url, timeout=10, allow_redirects=True)
            code = resp.status_code
            ok = code < 400
            mark = "✓" if ok else "✗"
            print(f"  [{mark}] {source['id']:<40} HTTP {code}")
            if ok:
                healthy += 1
            else:
                unreachable += 1
        except Exception as exc:
            print(f"  [✗] {source['id']:<40} ERROR: {exc}")
            unreachable += 1
    return healthy, unreachable


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_apply_prefs(args) -> None:
    """Read USER_PREFS.md, generate new filter config, show diff, confirm before writing."""
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)
    if not USER_PREFS_FILE.exists():
        print(f"ERROR: {USER_PREFS_FILE} not found. Fill it in first.")
        sys.exit(1)

    prefs = USER_PREFS_FILE.read_text(encoding="utf-8").strip()
    current_cfg: dict = {}
    if FILTER_CFG_FILE.exists():
        try:
            current_cfg = json.loads(FILTER_CFG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    print("\nReading USER_PREFS.md and generating new filter config …\n")
    client = anthropic.Anthropic(api_key=api_key)
    new_cfg = _build_filter_config(client, prefs)

    _print_filter_diff(current_cfg, new_cfg)
    print()
    if input("Apply these changes? [y/N]: ").strip().lower() != "y":
        print("Aborted — filter_config.json unchanged.")
        return

    _write_filter_config(new_cfg)
    print(f"\n  ✓ Saved to {FILTER_CFG_FILE}")
    print("  Run 'py -3.11 src/manage.py test-run --verbose' to preview results.")


def cmd_research(args) -> None:
    """Ask Claude to suggest new sources; optionally add selected ones interactively."""
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    sources = _load_sources()

    print("\nAsking Claude to research new sources …\n")
    suggestions = _fetch_source_suggestions(client, sources)

    if not suggestions:
        print("Claude returned no suggestions.")
        return

    # ── Display table ─────────────────────────────────────────────────────────
    existing_ids = {s["id"] for s in sources}
    print(f"{'#':<3}  {'ID':<35} {'CAT':<12} {'STRATEGY':<22} {'NAME'}")
    print("  " + "-" * 100)
    for i, s in enumerate(suggestions, 1):
        sid  = s.get("id", "")[:34]
        dupe = " [DUPLICATE]" if sid in existing_ids else ""
        print(f"{i:<3}  {sid:<35} {s.get('category',''):<12} {s.get('strategy',''):<22} {s.get('name','')}{dupe}")
    print()
    for i, s in enumerate(suggestions, 1):
        print(f"  [{i}] {s.get('name', '')}")
        print(f"       URL:   {s.get('url', '')}")
        print(f"       Notes: {s.get('notes', '')}")
        print()

    if not args.add:
        print("Run with --add to interactively select sources to add.")
        return

    # ── Interactive selection ─────────────────────────────────────────────────
    choice = input("Enter numbers to add (comma-separated), or Enter to skip: ").strip()
    if not choice:
        print("No sources added.")
        return

    to_add = []
    for tok in choice.split(","):
        tok = tok.strip()
        if tok.isdigit():
            idx = int(tok) - 1
            if 0 <= idx < len(suggestions):
                to_add.append(suggestions[idx])

    if not to_add:
        print("No valid selections.")
        return

    added = _add_sources(to_add, sources)
    if added:
        _save_sources(sources)
        print(f"\n  ✓ Added {len(added)} source(s) to data/sources.json.")
        print("  Tip: fill in selectors in data/sources.json before the next pipeline run.")


def cmd_build_sources(args) -> None:
    """
    Full first-time setup: apply prefs → research → add ALL suggestions → validate.
    Runs unattended (no confirmation prompts). Safe to re-run: skips existing sources.
    """
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set. Run: py -3.11 src/manage.py set-key ANTHROPIC_API_KEY <key>")
        sys.exit(1)
    if not USER_PREFS_FILE.exists():
        print(f"ERROR: {USER_PREFS_FILE} not found. Fill it in first.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    prefs  = USER_PREFS_FILE.read_text(encoding="utf-8").strip()

    # ── Step 1: Apply preferences ─────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("STEP 1/3 — Applying preferences from USER_PREFS.md")
    print("=" * 60)
    current_cfg: dict = {}
    if FILTER_CFG_FILE.exists():
        try:
            current_cfg = json.loads(FILTER_CFG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    new_cfg = _build_filter_config(client, prefs)
    _print_filter_diff(current_cfg, new_cfg)
    _write_filter_config(new_cfg)
    print(f"\n  ✓ Written to data/filter_config.json")

    # ── Step 2: Research + add all new sources ────────────────────────────────
    print("\n" + "=" * 60)
    print("STEP 2/3 — Researching new sources")
    print("=" * 60)
    sources = _load_sources()
    suggestions = _fetch_source_suggestions(client, sources)

    if not suggestions:
        print("  Claude returned no suggestions — source list may already be comprehensive.")
    else:
        print(f"\n  Claude suggested {len(suggestions)} source(s):\n")
        for s in suggestions:
            print(f"  {s.get('name', s.get('id', '?'))}")
            print(f"    URL:      {s.get('url', '')}")
            print(f"    Category: {s.get('category', '')}  |  Strategy: {s.get('strategy', '')}")
            print(f"    Notes:    {s.get('notes', '')}")
            print()
        added = _add_sources(suggestions, sources)
        if added:
            _save_sources(sources)
            print(f"  ✓ Added {len(added)} new source(s): {', '.join(added)}")
            skipped = len(suggestions) - len(added)
            if skipped:
                print(f"  Skipped {skipped} duplicate(s).")
        else:
            print("  All suggestions were duplicates — nothing new to add.")

    # ── Step 3: Validate all source URLs ─────────────────────────────────────
    print("\n" + "=" * 60)
    print("STEP 3/3 — Validating all source URLs")
    print("=" * 60 + "\n")
    sources = _load_sources()  # reload to include newly added
    healthy, unreachable = _validate_urls(sources)

    print(f"\n{'=' * 60}")
    print(f"BUILD COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Filter config:    ✓ written to data/filter_config.json")
    print(f"  Sources total:    {len(sources)}")
    if suggestions:
        print(f"  Sources added:    {len(added) if added else 0} new this run")
    print(f"  URLs reachable:   {healthy}")
    print(f"  URLs unreachable: {unreachable}")
    if unreachable:
        print(f"\n  NOTE: {unreachable} unreachable URL(s) above. The self-healing pipeline")
        print(f"  will attempt to fix them automatically on the first run.")
    print(f"\n  Next step: py -3.11 src/manage.py test-run --verbose")


# ── CLI routing ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Project Opportunity — Management CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Interactive .env setup wizard")

    p_key = sub.add_parser("set-key", help="Set a single .env variable")
    p_key.add_argument("key", help="Environment variable name")
    p_key.add_argument("value", help="Value to set")

    sub.add_parser("list-sources", help="List all sources with status")

    sub.add_parser("add-source", help="Interactively add a new source")

    p_upd = sub.add_parser("update-source", help="Update a field on an existing source")
    p_upd.add_argument("source_id", help="Source ID to update")
    p_upd.add_argument("field", help="Field name (e.g. active_url, status)")
    p_upd.add_argument("value", help="New value (JSON or plain string)")

    sub.add_parser("validate-sources", help="HEAD-test all source URLs")

    p_revive = sub.add_parser(
        "revive",
        help="Reset dead sources back to healthy so the pipeline retries them",
    )
    p_revive.add_argument("--id", help="Revive only this source_id")
    p_revive.add_argument(
        "--older-than", type=int, metavar="DAYS",
        help="Only revive sources marked dead more than N days ago",
    )

    p_test = sub.add_parser("test-run", help="Dry-run scrape without sending email")
    p_test.add_argument("--category", help="Only scrape this category (jobs/events/news/networking)")
    p_test.add_argument("--verbose", "-v", action="store_true", help="Print all net-new items")

    sub.add_parser(
        "apply-prefs",
        help="Read USER_PREFS.md and regenerate data/filter_config.json",
    )

    p_research = sub.add_parser(
        "research",
        help="Ask Claude to suggest new sources not already in the pipeline",
    )
    p_research.add_argument(
        "--add", action="store_true",
        help="After displaying suggestions, prompt to add selected ones to sources.json",
    )

    sub.add_parser(
        "build-sources",
        help="First-time setup: apply-prefs → research → add all → validate (unattended)",
    )

    args = parser.parse_args()
    commands = {
        "setup": cmd_setup,
        "set-key": cmd_set_key,
        "list-sources": cmd_list_sources,
        "add-source": cmd_add_source,
        "update-source": cmd_update_source,
        "validate-sources": cmd_validate_sources,
        "revive": cmd_revive,
        "test-run": cmd_test_run,
        "apply-prefs": cmd_apply_prefs,
        "research": cmd_research,
        "build-sources": cmd_build_sources,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
