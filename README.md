# Job Board Watcher

Monitors company career pages for new job postings, scores them against your resume, and emails you the matches.

---

## Quick Start (First Time)

1. **Install Python 3.10+** if you haven't already
2. Open a terminal in this folder and run:
   ```
   python setup.py
   ```
   This walks you through everything: API keys, Gmail sign-in, active hours, and scheduling.

3. Open the dashboard in your browser:
   ```
   streamlit run app.py
   ```

4. In the **Profile** tab, paste your resume and fill in your job preferences.

5. In the **Dashboard** tab, click **Run Now** to get your first results.

That's it. The automation runs in the background on your schedule.

---

## Daily Use

- **See new jobs:** Open the dashboard (`streamlit run app.py`) and check the **New Jobs** tab
- **Add a company:** Dashboard → **Companies** tab → click "Add a Company"
- **Update your resume:** Dashboard → **Profile** tab → paste and save
- **Adjust settings:** Dashboard → **Settings** tab (hours, thresholds, email)
- **Trigger a run:** Dashboard → **Dashboard** tab → click "Run Now"
- **Send digest now:** Dashboard → **Dashboard** tab → click "Send Digest"

---

## How It Works

Every 15 minutes (while your computer is on and within your active hours), the automation:

1. Checks which companies are due for a scrape
2. Fetches job listings from each company's career page
3. Compares against previously seen jobs — only new ones continue
4. Scores each new job against your resume (1–10)
5. Sends an **immediate email** for any job scoring 9 or 10
6. Accumulates remaining matches for your **daily digest** email

---

## Sharing With Someone Else

1. Give them this folder (or share via GitHub)
2. They run `python setup.py` — it sets up their own API keys and Gmail
3. They open `streamlit run app.py` to add their resume and preferences
4. Done — they have a fully independent setup

Each person needs their own Anthropic API key (~$5/month) and their own Gmail authorization.

---

## File Reference

| File | Purpose |
|---|---|
| `setup.py` | First-time setup wizard |
| `run.py` | Background runner (called by Task Scheduler) |
| `app.py` | Streamlit dashboard (open in browser) |
| `companies.json` | Active company watchlist — edit here, not in the xlsx |
| `profile/resume.md` | Your resume |
| `profile/preferences.md` | Desired roles and criteria |
| `.env` | API keys and settings (never share this file) |
| `Company List.xlsx` | Source research file — **read only, never modify** |
| `.tmp/jobs.db` | SQLite database of all discovered jobs |

---

## Manual Commands

```bash
# Open the dashboard
streamlit run app.py

# Test one company without sending emails
python run.py --company "HubSpot" --dry-run

# Force-scrape all companies right now
python run.py --force-all --no-hours

# Send the digest email immediately
python run.py --send-digest

# Seed companies from the Excel file
python tools/seed_companies.py --overwrite
```
