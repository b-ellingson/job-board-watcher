"""
First-time onboarding wizard. Run once per machine.
Walks through: dependencies, API keys, Gmail OAuth, active hours,
score thresholds, company seeding, Task Scheduler registration, and a test run.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def banner(text: str):
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def ask(prompt: str, default: str = "") -> str:
    display = f"{prompt} [{default}]: " if default else f"{prompt}: "
    val = input(display).strip()
    return val if val else default


def ask_int(prompt: str, default: int, lo: int = 0, hi: int = 23) -> int:
    while True:
        raw = ask(prompt, str(default))
        try:
            v = int(raw)
            if lo <= v <= hi:
                return v
        except ValueError:
            pass
        print(f"  Please enter a number between {lo} and {hi}.")


def write_env(updates: dict):
    env_file = ROOT / ".env"
    env = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    env.update(updates)
    env_file.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n", encoding="utf-8")
    print(f"  .env updated.")


# ── Step 1: Dependencies ────────────────────────────────────────────────────
def step_dependencies():
    banner("Step 1 of 9 — Installing Dependencies")
    packages = [
        "requests", "playwright", "anthropic",
        "google-api-python-client", "google-auth-httplib2", "google-auth-oauthlib",
        "jinja2", "streamlit", "python-dotenv",
    ]
    print("  Installing packages (this may take a minute) ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet"] + packages,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("  WARNING: Some packages may not have installed correctly.")
        print(result.stderr[-500:])
    else:
        print("  Python packages: OK")

    print("  Installing Playwright Chromium browser ...")
    result2 = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True
    )
    if result2.returncode == 0:
        print("  Chromium: OK")
    else:
        print("  WARNING: Chromium install may have failed. Run: playwright install chromium")


# ── Step 2: Anthropic API key ───────────────────────────────────────────────
def step_anthropic():
    banner("Step 2 of 9 — Anthropic API Key")
    print("  Claude scores job postings against your profile.")
    print("  Get your API key at: https://console.anthropic.com/")
    print()
    key = ask("Paste your Anthropic API key (starts with sk-ant-)")
    if not key:
        print("  Skipped. Add ANTHROPIC_API_KEY to .env manually later.")
        return
    write_env({"ANTHROPIC_API_KEY": key})


# ── Step 3: Gmail OAuth ─────────────────────────────────────────────────────
def step_gmail():
    banner("Step 3 of 9 — Gmail OAuth Setup")
    print("  Job alerts are sent via your Gmail account.")
    print()
    print("  To set up Gmail OAuth (one-time):")
    print()
    print("  1. Go to https://console.cloud.google.com/")
    print("     Create a new project (or select an existing one).")
    print()
    print("  2. Enable the Gmail API:")
    print("     Search 'Gmail API' in the top search bar → click it → Enable.")
    print()
    print("  3. Configure the OAuth consent screen:")
    print("     Left menu: APIs & Services → OAuth consent screen")
    print("     - User type: External → Create")
    print("     - Fill in App name (anything, e.g. 'Job Board Watcher') and your email")
    print("     - Scopes page: click 'Add or Remove Scopes' →")
    print("       paste: https://www.googleapis.com/auth/gmail.send → Add")
    print("     - Test users page: add your Gmail address → Save")
    print()
    print("  4. Create OAuth credentials:")
    print("     Left menu: APIs & Services → Credentials")
    print("     → + Create Credentials → OAuth client ID")
    print("     - Application type: Desktop app → Create")
    print("     - Click 'Download JSON' on the new credential")
    print()
    print("  5. Rename the downloaded file to 'credentials.json' and place it here:")
    print(f"     {ROOT}")
    print()

    creds_path = ROOT / "credentials.json"
    if not creds_path.exists():
        input("  Press Enter once credentials.json is in place (or Enter to skip) ...")
        if not creds_path.exists():
            print("  Skipped. Add credentials.json and run setup.py again to complete Gmail setup.")
            return

    print("  Opening browser for Google sign-in ...")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        creds = flow.run_local_server(port=0)
        token_path = ROOT / "token.json"
        token_path.write_text(creds.to_json(), encoding="utf-8")
        print("  Gmail OAuth: OK — token.json saved.")
        write_env({
            "GMAIL_CREDENTIALS_PATH": "credentials.json",
            "GMAIL_TOKEN_PATH": "token.json",
        })
    except Exception as e:
        print(f"  Gmail OAuth failed: {e}")
        print("  You can re-run setup.py later to retry.")


# ── Step 4: Alert email ─────────────────────────────────────────────────────
def step_alert_email():
    banner("Step 4 of 9 — Alert Email Address")
    email = ask("What email address should job alerts be sent to?", "bellings84@gmail.com")
    write_env({"ALERT_EMAIL": email})


# ── Step 5: Active hours ────────────────────────────────────────────────────
def step_active_hours():
    banner("Step 5 of 9 — Active Hours")
    print("  The automation only runs during these hours to avoid noise.")
    print()
    start = ask_int("Start hour (0-23, e.g. 7 for 7am)", default=7)
    end   = ask_int("End hour   (0-23, e.g. 20 for 8pm)", default=20)
    digest_time = ask("Daily digest time (HH:MM, 24h)", "09:00")
    write_env({
        "ACTIVE_HOURS_START": str(start),
        "ACTIVE_HOURS_END":   str(end),
        "DIGEST_TIME":        digest_time,
    })
    print(f"  Active hours: {start}:00 – {end}:00 | Digest: {digest_time}")


# ── Step 6: Score thresholds ────────────────────────────────────────────────
def step_thresholds():
    banner("Step 6 of 9 — Matching Strictness")
    print("  Jobs are scored 1–10 against your resume and preferences.")
    print("  Digest threshold: minimum score to include in the daily email.")
    print("  Alert threshold:  minimum score to trigger an immediate notification.")
    print()
    digest_t = ask_int("Digest threshold (recommended: 6)", default=6, lo=1, hi=10)
    alert_t  = ask_int("Alert threshold  (recommended: 9)", default=9, lo=1, hi=10)
    write_env({
        "SCORE_THRESHOLD":           str(digest_t),
        "IMMEDIATE_ALERT_THRESHOLD": str(alert_t),
    })


# ── Step 7: Seed companies ──────────────────────────────────────────────────
def step_seed():
    banner("Step 7 of 9 — Company Watchlist")
    xlsx = ROOT / "Company List.xlsx"
    companies_file = ROOT / "companies.json"

    if companies_file.exists():
        print(f"  companies.json already exists ({companies_file.stat().st_size // 1024} KB).")
        if ask("Overwrite with a fresh seed from Company List.xlsx? (y/N)", "n").lower() != "y":
            print("  Keeping existing companies.json.")
            return

    if not xlsx.exists():
        print(f"  Company List.xlsx not found at {xlsx}.")
        print("  Skipping — you can add companies via the Streamlit UI.")
        return

    print(f"  Seeding from {xlsx.name} ...")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "seed_companies.py"), "--overwrite"],
        capture_output=True, text=True, cwd=str(ROOT)
    )
    print(result.stdout)
    if result.returncode != 0:
        print("  WARNING:", result.stderr[-300:])


# ── Step 8: Windows Task Scheduler ─────────────────────────────────────────
def step_task_scheduler():
    banner("Step 8 of 9 — Windows Task Scheduler")
    print("  This registers two scheduled tasks:")
    print("  • JobBoardWatcher       — runs run.py every 15 minutes")
    print("  • JobBoardWatcherDigest — sends the daily email digest")
    print()

    if ask("Set up Windows Task Scheduler now? (Y/n)", "y").lower() == "n":
        print("  Skipped. Run setup.py again later to register.")
        return

    python_exe  = sys.executable
    run_script  = str(ROOT / "run.py")
    digest_time = ""
    env_data = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "DIGEST_TIME=" in line:
                digest_time = line.split("=",1)[1].strip()

    digest_hour, digest_min = (digest_time.split(":") if ":" in digest_time else ("9","0"))

    # 15-min scraper task
    cmd1 = [
        "schtasks", "/create", "/f",
        "/tn", "JobBoardWatcher",
        "/tr", f'"{python_exe}" "{run_script}"',
        "/sc", "minute", "/mo", "15",
        "/st", "00:00",
    ]
    # Daily digest task
    cmd2 = [
        "schtasks", "/create", "/f",
        "/tn", "JobBoardWatcherDigest",
        "/tr", f'"{python_exe}" "{run_script}" --send-digest',
        "/sc", "daily",
        "/st", f"{digest_hour.zfill(2)}:{digest_min.zfill(2)}",
    ]

    for cmd, name in [(cmd1, "JobBoardWatcher"), (cmd2, "JobBoardWatcherDigest")]:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  ✓ Task '{name}' registered.")
        else:
            print(f"  ✗ Failed to create '{name}': {result.stderr.strip()}")
            print("    Try running setup.py as Administrator if this fails.")


# ── Step 9: Test run ────────────────────────────────────────────────────────
def step_test():
    banner("Step 9 of 9 — Test Run")
    print("  Running a quick dry-run on 3 companies to verify everything works.")
    print()

    # Pick 3 API-friendly companies from companies.json if available
    companies_file = ROOT / "companies.json"
    test_company = ""
    if companies_file.exists():
        companies = json.loads(companies_file.read_text(encoding="utf-8"))
        api_co = [c for c in companies if c.get("scraping_method") == "api" and c.get("active")]
        if api_co:
            test_company = api_co[0]["name"]

    cmd = [sys.executable, str(ROOT / "run.py"), "--dry-run", "--no-hours", "--skip-score"]
    if test_company:
        cmd += ["--company", test_company]
        print(f"  Testing with: {test_company}")

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    print(result.stdout)
    if result.returncode == 0:
        print("  ✓ Test run passed!")
    else:
        print("  ⚠ Test run had issues:")
        print(result.stderr[-500:])


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "★"*60)
    print("  Job Board Watcher — First-Time Setup")
    print("★"*60)
    print("\nThis wizard will configure everything. Press Ctrl+C to cancel at any time.\n")

    try:
        step_dependencies()
        step_anthropic()
        step_gmail()
        step_alert_email()
        step_active_hours()
        step_thresholds()
        step_seed()
        step_task_scheduler()
        step_test()
    except KeyboardInterrupt:
        print("\n\n  Setup cancelled. Re-run setup.py to continue.")
        sys.exit(1)

    banner("Setup Complete!")
    print("  ✓ All steps finished.")
    print()
    print("  Next steps:")
    print("  1. Open the dashboard:  streamlit run app.py")
    print("  2. Add your resume in the Profile tab")
    print("  3. Review your company watchlist in the Companies tab")
    print("  4. Click 'Run Now' in the Dashboard to get your first results")
    print()


if __name__ == "__main__":
    main()
