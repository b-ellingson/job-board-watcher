"""
Headless runner — called by Windows Task Scheduler every 15 minutes.
Also used for manual testing via CLI flags.

Usage:
  python run.py                    # normal run: scrape due companies, score, email alerts
  python run.py --dry-run          # scrape + diff + score, print results, no email
  python run.py --company hubspot  # run for one specific company by name (partial match)
  python run.py --force-all        # ignore check_interval, scrape every active company
  python run.py --send-digest      # trigger the daily digest email now
  python run.py --skip-score       # skip Claude scoring
"""
import argparse
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Force UTF-8 stdout so non-ASCII job titles don't crash on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Load .env before anything else
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # dotenv optional; env vars can be set by OS

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from tools.diff_jobs import (
    init_db, migrate_db, is_due, mark_checked, diff_jobs, save_scores,
    get_unsent_jobs, mark_emailed, log_run, get_unscored_jobs,
    get_all_scoreable_jobs,
)
from tools.scrape_jobs import scrape_company
from tools.score_jobs import score_jobs
from tools.send_email import send_digest, send_alert


def load_companies() -> list[dict]:
    path = ROOT / "companies.json"
    if not path.exists():
        sys.exit("companies.json not found. Run: python tools/seed_companies.py")
    return json.loads(path.read_text(encoding="utf-8"))


def active_hours_check() -> bool:
    start = int(os.getenv("ACTIVE_HOURS_START", "7"))
    end   = int(os.getenv("ACTIVE_HOURS_END", "20"))
    now   = datetime.now().hour
    if not (start <= now < end):
        print(f"Outside active hours ({start}:00–{end}:00). Exiting.")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Job Board Watcher runner")
    parser.add_argument("--dry-run",     action="store_true", help="No DB writes, no emails")
    parser.add_argument("--company",     type=str,            help="Run for one company (partial name match)")
    parser.add_argument("--force-all",   action="store_true", help="Ignore check_interval, scrape all active")
    parser.add_argument("--send-digest", action="store_true", help="Send digest email now and exit")
    parser.add_argument("--skip-score",  action="store_true", help="Skip Claude scoring")
    parser.add_argument("--no-hours",    action="store_true", help="Bypass active-hours check (for testing)")
    parser.add_argument("--score-only",  action="store_true", help="Score unscored jobs, no scraping")
    parser.add_argument("--rescore-all", action="store_true", help="Re-score all real jobs (overwrites existing scores)")
    args = parser.parse_args()

    # Active hours guard (skip for manual flags)
    is_manual = (args.dry_run or args.company or args.force_all or args.send_digest
                 or args.no_hours or args.score_only or args.rescore_all)
    if not is_manual and not active_hours_check():
        return

    init_db()
    migrate_db()

    # ── Score-only modes ──────────────────────────────────────────────────
    if args.score_only or args.rescore_all:
        if args.rescore_all:
            jobs_to_score = get_all_scoreable_jobs()
            print(f"Re-scoring {len(jobs_to_score)} real jobs (overwrites existing scores) ...")
        else:
            jobs_to_score = get_unscored_jobs()
            print(f"Scoring {len(jobs_to_score)} unscored jobs ...")

        if not jobs_to_score:
            print("Nothing to score.")
            return

        scored = score_jobs(jobs_to_score)
        save_scores(scored)
        print(f"Done — scored {len(scored)} jobs.")
        return

    # -- Auto-digest check: send digest if current time matches any configured digest time --
    if not args.dry_run and not args.send_digest and not args.company:
        digest_times_raw = os.getenv("DIGEST_TIMES", os.getenv("DIGEST_TIME", "09:00"))
        now_hhmm = datetime.now().strftime("%H:%M")
        now_min  = datetime.now().hour * 60 + datetime.now().minute
        task_interval = int(os.getenv("TASK_INTERVAL_MINUTES", "5"))
        for t in digest_times_raw.split(","):
            t = t.strip()
            if not t:
                continue
            try:
                h, m = t.split(":")
                target_min = int(h) * 60 + int(m)
                if abs(now_min - target_min) <= task_interval:
                    threshold = int(os.getenv("SCORE_THRESHOLD", "6"))
                    unsent = get_unsent_jobs(threshold=threshold)
                    if unsent:
                        print(f"Digest time match ({t}) — sending {len(unsent)} jobs ...")
                        sent = send_digest(unsent, threshold=threshold)
                        if sent:
                            mark_emailed([j["content_hash"] for j in unsent])
                    break
            except ValueError:
                pass

    # -- Send-digest mode --------------------------------------------------
    if args.send_digest:
        threshold = int(os.getenv("SCORE_THRESHOLD", "6"))
        unsent = get_unsent_jobs(threshold=threshold)
        if not unsent:
            print("No unsent jobs above threshold. Nothing to send.")
            return
        sent = send_digest(unsent, threshold=threshold)
        if sent:
            mark_emailed([j["content_hash"] for j in unsent])
        return

    # -- Normal / dry-run scraping -----------------------------------------
    all_companies = load_companies()
    active = [c for c in all_companies if c.get("active", True)]

    if args.company:
        needle = args.company.lower()
        active = [c for c in active if needle in c["name"].lower()]
        if not active:
            print(f"No active company matching '{args.company}'. Check companies.json.")
            return

    due = active if args.force_all else [c for c in active if is_due(c)]

    if not due:
        print(f"No companies due for scraping (checked {len(active)} active). Exiting.")
        return

    print(f"Scraping {len(due)} companies ({'dry-run' if args.dry_run else 'live'}) ...")

    all_scraped = []
    all_new     = []
    companies_checked = 0

    for company in due:
        print(f"  >> {company['name']} [{company.get('platform','?')}] ...")
        try:
            jobs = scrape_company(company)
            all_scraped.extend(jobs)
            companies_checked += 1

            if args.dry_run:
                print(f"    Found {len(jobs)} jobs (dry-run, not saving)")
                all_new.extend(jobs)
            else:
                new_jobs = diff_jobs(jobs)
                all_new.extend(new_jobs)
                status = "success" if jobs else "empty"
                mark_checked(company["name"], status=status, job_count=len(jobs))
                if new_jobs:
                    print(f"    {len(new_jobs)} NEW / {len(jobs)} total")
                else:
                    print(f"    {len(jobs)} jobs, no new ones")

        except Exception as e:
            err_msg = str(e)
            print(f"    ERROR: {err_msg}")
            if not args.dry_run:
                mark_checked(company["name"], status="error", error=err_msg[:500])

    print(f"\nTotal scraped: {len(all_scraped)} | New: {len(all_new)}")

    if not all_new:
        if not args.dry_run:
            log_run(companies_checked, len(all_scraped), 0)
        return

    # -- Scoring ----------------------------------------------------------
    if not args.skip_score:
        print("Scoring new jobs ...")
        scored = score_jobs(all_new, dry_run=args.dry_run)
        if not args.dry_run:
            save_scores(scored)
        all_new = scored

    # -- Print results (always) -------------------------------------------
    print("\n-- New Jobs --------------------------------------------------")
    for j in sorted(all_new, key=lambda x: x.get("score") or 0, reverse=True):
        score_str = f"[{j.get('score', '?')}/10]" if not args.skip_score else ""
        print(f"  {score_str} {j['company']} — {j['title']} | {j.get('location','')}")

    if args.dry_run:
        print("\n(dry-run: no emails sent, no DB changes)")
        return

    # -- Immediate alerts for hot jobs ------------------------------------
    alert_threshold = int(os.getenv("IMMEDIATE_ALERT_THRESHOLD", "9"))

    # Alert on new high-score jobs from this run
    hot_jobs = [j for j in all_new if (j.get("score") or 0) >= alert_threshold]
    for job in hot_jobs:
        sent = send_alert(job)
        if sent:
            mark_emailed([job["content_hash"]])

    # Also alert on any previously-discovered jobs above threshold not yet emailed
    # (covers jobs scored in a prior --score-only run or a previous session)
    for job in get_unsent_jobs(threshold=alert_threshold):
        sent = send_alert(job)
        if sent:
            mark_emailed([job["content_hash"]])

    log_run(companies_checked, len(all_scraped), len(all_new))


if __name__ == "__main__":
    main()
