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
  research          Ask Claude to suggest new sources and auto-add them
                    to sources.json (duplicates are skipped)
  log-outreach      Record a conversation you initiated
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
    """Run a single scrape + filter + dedup pass. Sends nothing unless --send.

    Scraping is the expensive-in-*time* step (network I/O, no tokens);
    evaluation is the expensive-in-*money* step (real API calls). Without
    --send this stops after the free part and prints counts, matching the
    command's name. --send continues past that point into evaluation and
    (if anything passes) the send — reusing the SAME scrape rather than
    discarding it, which is the whole reason this flag exists: the plain
    preview has no way to hand its results to a later process, since nothing
    about it is persisted to disk.
    """
    # Load .env manually so we don't need all vars set
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)

    import config
    from scraper import scrape_source
    from filter import apply_profile_filter
    from deduplicator import find_new

    sources = config.load_sources()
    category_filter = args.category  # optional: "jobs", "events", etc.

    all_raw = []
    for source in sources:
        if category_filter and source.get("category") != category_filter:
            continue
        # Match the pipeline: skip only dead sources. Degraded ones are still
        # scraped, so a dry run that skipped them would misreport coverage.
        if str(source.get("status", "")).lower() == "dead":
            print(f"  [SKIP] {source['id']} (dead)")
            continue
        print(f"  Scraping {source['id']} …", end=" ", flush=True)
        results = scrape_source(source, enrich=True)
        print(f"{len(results)} items")
        all_raw.extend(results)

    filtered = apply_profile_filter(all_raw, sources)
    # find_new(), not get_new_items()/mark_seen(): nothing is marked seen here.
    # If --send later processes only some of these (evaluator's per-run cap),
    # deduplicator.mark_seen() below persists only what was actually judged —
    # same rule main.py's real pipeline follows, for the same reason: an
    # item nobody looked at must stay a candidate for next time, not vanish.
    new_items = find_new(filtered)

    print(f"\nDry run complete:")
    print(f"  Raw items:    {len(all_raw)}")
    print(f"  After filter: {len(filtered)}")
    print(f"  Net-new:      {len(new_items)}")

    if new_items and args.verbose:
        print("\nNet-new items:")
        for item in new_items:
            d = item.to_dict()
            print(f"  [{d['category']}] {d['title']} | {d['location']} | {d['url']}")

    if not getattr(args, "send", False):
        return
    if not new_items:
        print("\n--send requested, but there's nothing new to evaluate.")
        return

    print(f"\n{'=' * 60}")
    print(f"--send: now calling the Anthropic API to evaluate {len(new_items)} item(s).")
    print("This is the point where tokens actually get spent — everything above was free.")
    print(f"{'=' * 60}\n")

    import evaluator
    import synthesis
    import emailer
    import main as pipeline  # reuse its run_log helpers, not duplicate them

    from deduplicator import mark_seen

    run_start = datetime.now(timezone.utc)
    main_list, watchlist, eval_stats, processed = evaluator.evaluate(new_items)
    mark_seen(processed)

    summary = {}
    try:
        summary = synthesis.summarize(
            main_list, watchlist, eval_stats,
            last_run_iso=pipeline._previous_run_timestamp(),
        )
    except Exception as exc:
        print(f"  (synthesis failed, continuing without it: {exc})")

    email_sent = False
    if main_list or watchlist:
        to_dicts = lambda items: [i.to_dict() if hasattr(i, "to_dict") else i for i in items]
        email_sent = emailer.send_digest(
            to_dicts(main_list), run_start,
            watchlist=to_dicts(watchlist), eval_stats=eval_stats, summary=summary,
        )
        print(f"\nEmail sent: {email_sent}")
    else:
        print(f"\n{eval_stats['evaluated']} item(s) evaluated, none passed the match test. No email sent.")

    # Record this as a real run so run_log.json / the pattern flag / future
    # "since last run" math stay accurate — a manual --send run is still a run.
    pipeline._append_run_log({
        "timestamp": run_start.isoformat(),
        "source": "test-run --send",
        "raw_items_found": len(all_raw),
        "new_items_after_filter": len(filtered),
        "new_items_after_dedup": len(new_items),
        "evaluated": eval_stats["evaluated"],
        "main_list": eval_stats["main"],
        "watchlist": eval_stats["watchlist"],
        "rejected_by_evaluator": eval_stats["rejected"],
        "evaluator_degraded": eval_stats["degraded"],
        "evaluator_skipped": eval_stats.get("skipped", 0),
        "email_sent": email_sent,
    })


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
Enumerate sources that will surface opportunities matching this candidate. The
brief defines five search tracks in its Section 3. Cover every one of them.
A track with no sources is a track that returns nothing.

Judge a source by the roles it actually produces, not by employer size. A single
hotel property's careers page posts housekeepers and banquet managers, and will
essentially never post a Chief of Staff. That is a bad source here no matter how
many people the property employs. An employer belongs in this list when it has a
corporate, executive, or strategic function that posts senior operating roles.

GENERATE THE TRACKS IN THIS EXACT ORDER, AND RESPECT THE COUNT CAP ON EACH ONE.
Tracks C, D, and E are the ones a headhunter's first pass usually skips, so they
come first here on purpose: if you run out of output budget, it must happen
inside Track A, not before you have covered everything else.

  TRACK C: FRACTIONAL AND INTERIM EXECUTIVE (prefix: c_) — target 10-15 sources
    Bolster, Continuum, Go Fractional, Fractional Jobs, Graphite, Catalant,
    Business Talent Group, Chief of Staff Network, Toptal executive, Lauber
    Business Partners and comparable regional interim-leadership practices, SIM
    and other regional interim networks.

  TRACK D: BOARD AND ADVISORY SEATS (prefix: d_) — target 8-12 sources
    BoardAssist, Nonprofit Board Match, BoardProspects, LinkedIn board postings,
    startup advisory board calls, foundation advisory committees, Orlando and
    Central Florida nonprofit board openings, health system community boards,
    arts and education boards. Prefer boards with a real committee structure
    over ceremonial ones.

  TRACK E: CONSULTING RFPs AND SPEAKING (prefix: e_) — target 10-15 sources
    RFPs: foundation and nonprofit procurement pages, GrantStation, Instrumentl,
    state and municipal solicitation portals, health system and university
    procurement, targeting organizational assessment, strategic planning,
    organizational design, change management, and AI readiness.
    Speaking and facilitation: Sessionize, PaperCall, and CFPs for AI and org
    design, nonprofit technology (NTEN NTC), healthcare innovation, HR and
    people ops (HR Transform, Culture First), Chief of Staff Association
    events, SXSW, regional business and university events, TEDx network.

  WATCHLIST SOURCES: INFOSEC AND DATA CENTER (prefix: watch_) — target 10-15
    The brief's Section 6 keeps a separate market-intelligence list, so these
    are wanted. Target the OPERATING layer, not engineering: chief-of-staff and
    business-operations roles inside CISO and CTO offices, and COO or business
    operations roles at data center, colocation, and AI infrastructure
    companies. Loudoun County (Ashburn, Sterling) is the densest data center
    market in the world and belongs here rather than in Track A.

  SIGNAL SOURCES: ROLES NOT YET POSTED (prefix: signal_) — target 6-10 sources
    Regional business journals (bizjournals, Orlando Inno, Axios Local DC).
    Growth, funding, new-HQ, and expansion announcements. Executive departure
    announcements implying a backfill. Post-merger and restructuring
    announcements. The brief's first match-test signal is a leader with a
    mandate and no infrastructure, and these announcements are where that
    shows up before a posting exists.

  BONUS VECTOR: NORDIC AND POLISH (prefix: nordic_) — target 5-8 sources
    The brief calls this underused and almost nobody searches it. US
    operations, US expansion, and US-facing leadership roles at Danish,
    Swedish, Norwegian, and Polish organizations: Novo Nordisk Foundation and
    its US grantees, Lego, Maersk, Orsted, Danish-American Chamber of
    Commerce, Nordic Innovation House, EU-funded health and climate
    initiatives with US arms.

  TRACK B: NONPROFIT AND PHILANTHROPIC (prefix: b_) — target 15-20 sources
    Sector boards: Bridgespan, Koya Partners and Diversified Search, Talent
    Citizen, Chronicle of Philanthropy, Philanthropy News Digest, Work for
    Good, Foundation List, NTEN community, Idealist (senior filter only).
    Priority per the brief: GRANTEE organizations and portfolio companies of
    the Gates, Rockefeller, Skoll, and MacArthur foundations, not only the
    foundations themselves. Name specific grantees where you know them.
    Health equity, global health delivery, and education redesign
    organizations. Nonprofits hiring a first COO or Chief of Staff.

  TRACK A: EMPLOYED ROLES, FOR-PROFIT (prefix: a_) — target 35-45 sources,
  SPLIT ROUGHLY EVENLY BETWEEN ORLANDO AND DC METRO. This track goes last and
  has the most headroom to enumerate individual employers, but the cap still
  applies: stop at the target rather than exhausting every employer you know.
    - Company career pages directly. The brief rates these above aggregators.
      Prefer the ATS endpoint (Workday, Greenhouse, Lever, SmartRecruiters,
      Ashby, Workable) over the marketing careers page.
    - Startup and tech boards: Wellfound, Y Combinator work-at-a-startup,
      Built In (regional editions), Otta.
    - Orlando and Central Florida employers with real corporate functions:
      theme park and entertainment parent companies (not individual parks),
      health systems, defense and simulation, higher education, utilities and
      authorities, professional services, regional HQs.
    - DC metro employers: mission-driven tech, health policy and health systems,
      AI policy organizations, foundations, national associations and membership
      organizations, universities and academic medical centers, federal
      contractors with civilian missions, and Tysons and Reston HQ companies.
    - Meta-aggregators where a senior filter is possible: Google Jobs via
      SerpAPI, JSearch, Adzuna, The Muse, USAJobs.

If you are approaching your output budget, finish the track you are on and
move to the next rather than exhausting the current one. Every track above
having at least a few entries beats one track having fifty.

GEOGRAPHY. Three zones, all in scope:
  1. Orlando and Central Florida: Orange, Seminole, Osceola, Lake counties.
  2. Remote (US).
  3. Washington DC metro, meaning the full DMV and not just the District.
     Northern Virginia (Arlington, Alexandria, Falls Church, Fairfax, Tysons,
     McLean, Vienna, Reston, Herndon, Loudoun, Prince William) and suburban
     Maryland (Montgomery and Prince George's counties). Postings frequently
     name only the suburb, so build searches on jurisdiction names rather than
     on the phrase "Washington DC".
  Tracks C, D, and E are location-flexible; do not constrain them by zone.

DO NOT SUGGEST sources whose output is dominated by roles the brief hard-excludes:
individual hotel and restaurant properties, retail and gym locations, construction
and AE firms, financial services branch or lending roles, and any board whose
postings are hourly or entry-level. Adding these is the failure mode being fixed:
the previous configuration returned 614 items of which roughly 5 were relevant.

For EACH suggestion produce:
  id         — unique snake_case identifier, NO hyphens/spaces. Prefix by TRACK,
               matching the track list above exactly: a_ b_ c_ d_ e_ watch_
               signal_ nordic_ (e.g. "a_adventhealth_careers", "watch_equinix",
               "c_bolster"). Do not invent other prefixes — a source's track is
               how the pipeline understands what it is, and a per-employer or
               per-cluster prefix instead of the track prefix breaks that.
  name       — human display name including the specific property if applicable
  category   — one of: jobs | events | networking | news
  url        — the MOST SPECIFIC, direct URL to the listings/feed/API endpoint.
               NEVER a homepage. For ATS: use the actual /jobs JSON endpoint if you
               know the tenant. Flag any guessed tenant IDs in notes.
  strategy   — one of: rss_feed | html_list | html_search_result | json_api |
                      playwright | workday_api | sitemap.
               STRONGLY prefer rss_feed / workday_api / json_api over html_* —
               they don't need CSS selectors and won't go dark when a site
               redesigns. Only use html_list / html_search_result when nothing
               structured exists.
  selectors  — REQUIRED when strategy is html_list, html_search_result, or
               playwright. Must be an object with keys: item_container, title,
               link, and optionally location, date. Empty selectors = the source
               returns 0 items on every run and goes dead in ~3 days. If you
               don't know them, pick a different strategy or omit the source.
  api_config — REQUIRED when strategy is workday_api or json_api. For Workday:
               {{"base_url": "https://TENANT.wd5.myworkdayjobs.com",
                 "endpoint": "/wday/cxs/TENANT/SITE/jobs",
                 "search_text": "director manager", "limit": 50,
                 "locations": []}}
  cluster    — the track and sub-cluster. Example: "A-orlando-health",
               "B-foundation-grantee", "D-board", "watch-datacenter".
  company    — REQUIRED for a single-employer source (a company's own careers
               page or ATS tenant). The employer name as a reader should see it,
               e.g. "AdventHealth". Omit for aggregators and multi-employer
               boards. Without it every posting from that source is anonymous.
  notes      — 1-2 sentences: why this fits the candidate; flag URL/tenant
               uncertainty; explain any educated guess in selectors or api_config.

HARD REQUIREMENTS:
- Every one of the five tracks must be represented. Tracks C, D, and E are the
  ones most often skipped, and skipping them is a failure.
- Cover both Orlando and the DC metro for Track A. A source list that only
  covers Florida silently drops a third of the search.
- Prefer ATS endpoints (Workday, Greenhouse, Lever) over corporate career pages.
- Where an ATS supports a keyword or level filter, target senior operating roles
  in the URL rather than pulling the employer's entire req list.
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

Schema: [{{"id": "...", "name": "...", "category": "...", "url": "...",
           "strategy": "...", "cluster": "...", "notes": "...", "company": "...",
           "selectors": {{}} | {{"item_container":"...","title":"...","link":"...","location":"...","date":"..."}},
           "api_config": {{}} | {{"base_url":"...","endpoint":"...","search_text":"...","limit":50,"locations":[]}}}}]"""

    # Larger budget: the taxonomy produces dozens of suggestions.
    # 48k gives headroom over the ~33k a full 7-track, capped run needs; the
    # per-track caps in the prompt are what actually prevent truncation now —
    # this ceiling is a backstop, not the primary control.
    raw, stop_reason = _llm_call(client, prompt, "research", max_tokens=48000)
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
        strategy = s.get("strategy", "html_list")
        new_entry = {
            "id": sid,
            "name": s.get("name", sid),
            "category": s.get("category", "jobs"),
            "active_url": url,
            "alternate_urls": [],
            "strategy": strategy,
            "selectors": s.get("selectors") or {},
            "dead_content_patterns": [],
            "status": "healthy",
            "last_verified": now_ts,
            "consecutive_empty_runs": 0,
        }
        # api_config is only meaningful for workday_api / json_api — pass it
        # through when the research output provided one. Prevents Workday and
        # JSON API suggestions from landing with empty configs.
        if strategy in ("workday_api", "json_api") and s.get("api_config"):
            new_entry["api_config"] = s["api_config"]
        # Single-employer sources (a company's own careers page or ATS tenant)
        # never print the employer on the card, because it is the whole site.
        # scraper.py uses this as the fallback company for every item, so
        # dropping it here would leave those postings anonymous.
        if s.get("company"):
            new_entry["company"] = str(s["company"]).strip()
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
    """Ask Claude to suggest new sources and auto-add them to sources.json."""
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
        print(f"  ✓ Added {len(added)} new source(s) to data/sources.json.")
        skipped = len(suggestions) - len(added)
        if skipped:
            print(f"  Skipped {skipped} duplicate(s).")
        print("  Tip: fill in selectors in data/sources.json before the next pipeline run.")
    else:
        print("  All suggestions were duplicates — nothing new to add.")


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

def cmd_log_outreach(args) -> None:
    """Record a conversation you initiated, for the digest's pattern flag."""
    import synthesis

    entry = synthesis.log_outreach(args.who, args.note or "")
    total = len(synthesis._load_outreach())
    print(f"  Logged: {entry['who']}" + (f" - {entry['note']}" if entry["note"] else ""))
    print(f"  {total} conversation(s) recorded in total.")


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
    p_test.add_argument(
        "--send", action="store_true",
        help="After finding net-new items, also evaluate them against USER_PREFS.md "
             "and send the digest if anything passes. Costs API tokens (evaluator + "
             "synthesis calls). Without this flag, test-run only previews counts and "
             "never calls the model or sends anything.",
    )

    sub.add_parser(
        "apply-prefs",
        help="Read USER_PREFS.md and regenerate data/filter_config.json",
    )

    sub.add_parser(
        "research",
        help="Ask Claude to suggest new sources and auto-add them to sources.json",
    )

    p_out = sub.add_parser(
        "log-outreach",
        help="Record a conversation you initiated (feeds the digest pattern flag)",
    )
    p_out.add_argument("who", help="Person or organization you contacted")
    p_out.add_argument("--note", default="", help="Optional context")

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
        "log-outreach": cmd_log_outreach,
        "build-sources": cmd_build_sources,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
