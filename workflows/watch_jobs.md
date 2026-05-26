# Workflow: watch_jobs

## Objective
Monitor a curated list of company career pages for new job postings, score them against a user profile, and deliver matching roles via Gmail.

## Trigger
- **Scheduled:** Windows Task Scheduler runs `python run.py` every 15 minutes during active hours
- **Manual:** `python run.py --dry-run` or via the Streamlit dashboard Run Now button

## Inputs
- `companies.json` — active company watchlist with platform, URL, and check interval
- `profile/resume.md` — user's resume (plain text)
- `profile/preferences.md` — desired roles, keywords, must-haves, deal-breakers
- `.env` — API keys, active hours, thresholds

## Steps

### 1. Active Hours Check (`run.py`)
- Read `ACTIVE_HOURS_START` and `ACTIVE_HOURS_END` from `.env`
- If current hour is outside range → exit immediately (no cost, no noise)

### 2. Determine Due Companies (`tools/diff_jobs.py: is_due`)
- For each active company in `companies.json`:
  - Check `company_state` table for `last_checked` timestamp
  - If `now - last_checked >= check_interval_hours` → due
- Fast-path companies (Greenhouse/Lever/Ashby): 1-hour interval
- Playwright companies: 6-hour interval
- Skip if not due → most 15-min runs touch only a handful of companies

### 3. Scrape (`tools/scrape_jobs.py: scrape_company`)
- Route by `scraping_method`:
  - `api`: call the public ATS REST API (Greenhouse/Lever/Ashby/Workable/SmartRecruiters)
  - `playwright`: launch headless Chromium, intercept network requests, parse JSON payloads
- Normalize output: `{company, title, department, location, url, description, content_hash}`
- On API failure or empty result → fall back to Playwright automatically
- Mark company as checked in `company_state` table

### 4. Diff (`tools/diff_jobs.py: diff_jobs`)
- For each scraped job: compute `SHA256(company + title + url)` as content hash
- Check against `jobs` table:
  - Hash not found → new job → insert to DB → add to new_jobs list
  - Hash found → existing job → update `last_seen` only → skip
- Returns only new jobs

### 5. Score (`tools/score_jobs.py: score_jobs`)
- Load `profile/resume.md` and `profile/preferences.md`
- Build system prompt with full profile text
- Use `cache_control: ephemeral` on the system prompt (5-min cache TTL)
- For each new job: send title + department + description as user message
- Claude returns JSON: `{score: 1-10, score_reason: "...", matched_keywords: [...]}`
- Store scores in DB via `diff_jobs.save_scores`

### 6. Immediate Alerts (`tools/send_email.py: send_alert`)
- Any job with `score >= IMMEDIATE_ALERT_THRESHOLD` (default: 9):
  - Render single-job HTML email
  - Send via Gmail API
  - Mark as emailed in DB
- Sent within 15 minutes of the posting being discovered

### 7. Daily Digest (`tools/send_email.py: send_digest`)
- Triggered by `python run.py --send-digest` (scheduled at `DIGEST_TIME`)
- Fetches all jobs with `score >= SCORE_THRESHOLD` and `emailed = 0`
- Groups by company, sorts by score descending
- Renders professional HTML email with score badges and View Job buttons
- Sends via Gmail API; marks all included jobs as `emailed = 1`

### 8. Log Run (`tools/diff_jobs.py: log_run`)
- Write to `runs` table: timestamp, companies_checked, jobs_found, new_jobs_found

## Outputs
- New jobs stored in `.tmp/jobs.db`
- Immediate email for hot matches (score ≥ 9)
- Daily digest email at configured time

## Error Handling
- Per-company errors are caught and logged; other companies continue running
- API failures automatically fall back to Playwright
- Scoring errors default score to 0 (excluded from digest)
- Gmail send failures are logged but don't crash the run

## Edge Cases
- **No new jobs:** Run exits after diffing with no email sent
- **Outside active hours:** `run.py` exits at line 1 (no cost)
- **No profile set:** Scoring is skipped with a warning; raw jobs still stored
- **Empty API response:** Playwright fallback is attempted
- **First run:** `company_state` is empty → all companies are "due" → full scrape

## Updating the System
- **Add company:** Use the Streamlit Companies tab or edit `companies.json`
- **Remove company:** Streamlit UI or edit `companies.json` (`"active": false` to pause)
- **Update profile:** Streamlit Profile tab → save → takes effect on next scoring run
- **Change hours/thresholds:** Streamlit Settings tab
- **Source research file:** `Company List.xlsx` is read-only — never modify it
