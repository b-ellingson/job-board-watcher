# Job Board Watcher

Monitors company career pages for new job postings, scores them against a user profile (Claude Haiku or local Ollama), and sends email alerts via Gmail API. Runs on Windows (Task Scheduler) or Docker (Linux/Unraid).

## Architecture

```
app.py              # Streamlit dashboard — 5 tabs: Dashboard, New Jobs, Companies, Profile, Settings
run.py              # Headless CLI runner — called by Task Scheduler every N minutes
setup.py            # First-time setup wizard
tools/
  scrape_jobs.py    # Hybrid scraper: REST API (Greenhouse/Lever/Ashby/Workable/SmartRecruiters) + Playwright
  diff_jobs.py      # SQLite DB layer — all queries: diff, score, state, runs, user_status
  score_jobs.py     # LLM scoring: Claude Haiku (default) or Ollama; uses prompt caching
  send_email.py     # Gmail API: immediate single-job alerts + daily HTML digest
  runner.py         # Non-blocking subprocess manager for the Streamlit UI (state in .tmp/)
  seed_companies.py # Seeds companies.json from Company List.xlsx
workflows/
  watch_jobs.md     # Living SOP: scrape → diff → score → alert pipeline, step by step
profile/
  resume.md         # User resume (loaded into scoring prompt)
  preferences.md    # Desired roles, keywords, must-haves, deal-breakers
```

## Key Data Files

| File | Notes |
|---|---|
| `companies.json` | Active watchlist: `{name, platform, ats_slug, careers_url, scraping_method, check_interval_hours, active}` |
| `.tmp/jobs.db` | SQLite: `jobs`, `company_state`, `runs` tables |
| `.env` | API keys and all runtime settings — never commit, never echo contents |
| `Company List.xlsx` | Read-only research source — never modify directly |

## Run Commands

```bash
streamlit run app.py                         # Open dashboard
python run.py                                # Normal scheduled run (respects active hours)
python run.py --dry-run                      # Scrape + score, no DB writes, no email
python run.py --company hubspot              # Single company (partial name match, comma-separated OK)
python run.py --force-all --no-hours         # Force-scrape every active company now
python run.py --send-digest                  # Send digest email immediately
python run.py --score-only                   # Score unscored jobs, no scraping
python run.py --rescore-all                  # Re-score all real jobs (overwrites existing scores)
python tools/seed_companies.py --overwrite   # Re-seed companies.json from Excel
```

## Env Variables (`.env`)

| Key | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude scoring |
| `OLLAMA_BASE_URL` | — | Set to use local Ollama instead of Claude |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Ollama model name |
| `ALERT_EMAIL` | — | Recipient for alerts/digest |
| `ACTIVE_HOURS_START` / `_END` | `7` / `20` | 24h ints; automation only runs in this window |
| `SCORE_THRESHOLD` | `6` | Min score to include in digest |
| `IMMEDIATE_ALERT_THRESHOLD` | `9` | Min score to send immediate single-job alert |
| `DIGEST_TIMES` | `09:00` | Comma-separated `HH:MM` digest send times |
| `TASK_INTERVAL_MINUTES` | `5` | Task Scheduler fire rate |
| `FAST_CHECK_INTERVAL_MIN` | `30` | API companies check interval |
| `SLOW_CHECK_INTERVAL_MIN` | `120` | Playwright companies check interval |
| `EMAIL_ENABLED` | `true` | Master email kill switch |
| `TZ` | `America/Chicago` | IANA timezone for display and Docker |

## Scraping

`tools/scrape_jobs.py` routes by `scraping_method`:
- `api` → direct REST call to ATS (Greenhouse/Lever/Ashby/Workable/SmartRecruiters) — fast, light
- `playwright` → headless Chromium via `playwright` library — slower, CPU-intensive

On API failure or empty result, falls back to Playwright automatically. Platform detection and interval assignment happen in `app.py:detect_platform()` when adding a company.

## Scoring

`tools/score_jobs.py` uses `claude-haiku-4-5-20251001` with `cache_control: ephemeral` on the system prompt (profile cached for 5 min to reduce cost). If `OLLAMA_BASE_URL` is set it routes to that instead. Estimated cost: ~$0.0003/job via Claude Haiku.

Returns: `{score: 1–10, score_reason: str, matched_keywords: list}`.

## Database Schema

`jobs`: `id, company, title, department, location, url, description, content_hash (unique SHA256), score, score_reason, matched_keywords, first_seen, last_seen, emailed, alert_shown, user_status (new/saved/not_interested/not_a_job)`

`company_state`: `company_name, last_checked, last_status, last_error, last_job_count`

`runs`: `id, timestamp, companies_checked, jobs_found, new_jobs_found`

## How to Operate (WAT Pattern)

**Use tools in `tools/` — don't inline logic that belongs there.** New capabilities get a tool script; the workflow documents how to use it.

**Before running scripts that call Claude API or Gmail:** Check with the user — real cost/side effects.

**`workflows/watch_jobs.md` is the living SOP.** Update it when you discover rate limits, better approaches, or new edge cases. Don't create or overwrite workflows without asking.

**`.tmp/` is fully disposable.** Durable user config: `companies.json`, `profile/`, `.env`.

**Docker:** `Dockerfile` + `docker-compose.yml` handle Linux/Unraid deployment. The scraper loop runs via `entrypoint.sh`; the `build: .` context is set in `docker-compose.yml`.
