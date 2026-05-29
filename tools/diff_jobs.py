"""
SQLite layer: schema init, diff detection, company check-interval tracking.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent.parent / ".tmp" / "jobs.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            company          TEXT NOT NULL,
            title            TEXT NOT NULL,
            department       TEXT,
            location         TEXT,
            url              TEXT,
            description      TEXT,
            content_hash     TEXT UNIQUE NOT NULL,
            score            INTEGER,
            score_reason     TEXT,
            matched_keywords TEXT,
            first_seen       TEXT NOT NULL,
            last_seen        TEXT NOT NULL,
            emailed          INTEGER DEFAULT 0,
            alert_shown      INTEGER DEFAULT 0,
            user_status      TEXT DEFAULT 'new'
        );

        CREATE TABLE IF NOT EXISTS company_state (
            company_name TEXT PRIMARY KEY,
            last_checked TEXT
        );

        CREATE TABLE IF NOT EXISTS runs (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp          TEXT NOT NULL,
            companies_checked  INTEGER DEFAULT 0,
            jobs_found         INTEGER DEFAULT 0,
            new_jobs_found     INTEGER DEFAULT 0
        );
        """)


def make_hash(company: str, title: str, url: str) -> str:
    key = f"{company.lower().strip()}|{title.lower().strip()}|{(url or '').strip()}"
    return hashlib.sha256(key.encode()).hexdigest()


def is_due(company: dict, interval_override: int | None = None) -> bool:
    """Return True if company hasn't been checked within its check_interval_hours."""
    interval = interval_override or company.get("check_interval_hours", 6)
    with _connect() as con:
        row = con.execute(
            "SELECT last_checked FROM company_state WHERE company_name = ?",
            (company["name"],),
        ).fetchone()
    if not row or not row["last_checked"]:
        return True
    last = datetime.fromisoformat(row["last_checked"])
    return datetime.now() - last >= timedelta(hours=interval)


def mark_checked(company_name: str, status: str = "success",
                 error: str | None = None, job_count: int | None = None):
    with _connect() as con:
        con.execute(
            """INSERT INTO company_state
                   (company_name, last_checked, last_status, last_error, last_job_count)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(company_name) DO UPDATE SET
                   last_checked   = excluded.last_checked,
                   last_status    = excluded.last_status,
                   last_error     = excluded.last_error,
                   last_job_count = excluded.last_job_count""",
            (company_name, datetime.now().isoformat(), status, error, job_count),
        )


def diff_jobs(scraped_jobs: list[dict]) -> list[dict]:
    """
    Compare scraped jobs against DB.
    - New jobs (hash not in DB): insert + return.
    - Existing jobs: update last_seen; also backfill URL if the stored record had none.
    Returns list of new job dicts.
    """
    if not scraped_jobs:
        return []

    now = datetime.now().isoformat()
    new_jobs = []

    with _connect() as con:
        for job in scraped_jobs:
            h = job.get("content_hash") or make_hash(
                job["company"], job["title"], job.get("url", "")
            )
            existing = con.execute(
                "SELECT id FROM jobs WHERE content_hash = ?", (h,)
            ).fetchone()

            if existing:
                con.execute(
                    "UPDATE jobs SET last_seen = ? WHERE content_hash = ?",
                    (now, h),
                )
            else:
                # Hash mismatch — but this may be a URL-enriched version of a job we
                # already have stored with an empty URL (hash was computed with url="").
                # If so, patch the existing record instead of inserting a duplicate.
                url_patched = False
                if job.get("url"):
                    url_less_hash = make_hash(job["company"], job["title"], "")
                    url_less = con.execute(
                        "SELECT id FROM jobs WHERE content_hash = ?", (url_less_hash,)
                    ).fetchone()
                    if url_less:
                        con.execute(
                            "UPDATE jobs SET url = ?, content_hash = ?, last_seen = ? "
                            "WHERE content_hash = ?",
                            (job["url"], h, now, url_less_hash),
                        )
                        url_patched = True

                if not url_patched:
                    con.execute(
                        """INSERT INTO jobs
                           (company, title, department, location, url, description,
                            content_hash, first_seen, last_seen)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            job.get("company", ""),
                            job.get("title", ""),
                            job.get("department", ""),
                            job.get("location", ""),
                            job.get("url", ""),
                            job.get("description", ""),
                            h,
                            now,
                            now,
                        ),
                    )
                    new_jobs.append({**job, "content_hash": h})

    return new_jobs


def save_scores(scored_jobs: list[dict]):
    """Persist Claude scores back to the jobs table."""
    with _connect() as con:
        for job in scored_jobs:
            con.execute(
                """UPDATE jobs SET score = ?, score_reason = ?, matched_keywords = ?
                   WHERE content_hash = ?""",
                (
                    job.get("score"),
                    job.get("score_reason"),
                    json.dumps(job.get("matched_keywords", [])),
                    job["content_hash"],
                ),
            )


def get_unscored_jobs() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE score IS NULL "
            "AND (user_status IS NULL OR user_status != 'not_a_job') "
            "ORDER BY first_seen DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_unsent_jobs(threshold: int = 1) -> list[dict]:
    """Jobs that scored at/above threshold and haven't been emailed yet."""
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE score >= ? AND emailed = 0 ORDER BY score DESC, first_seen DESC",
            (threshold,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_emailed(content_hashes: list[str]):
    with _connect() as con:
        con.executemany(
            "UPDATE jobs SET emailed = 1 WHERE content_hash = ?",
            [(h,) for h in content_hashes],
        )


def log_run(companies_checked: int, jobs_found: int, new_jobs_found: int):
    with _connect() as con:
        con.execute(
            "INSERT INTO runs (timestamp, companies_checked, jobs_found, new_jobs_found) VALUES (?,?,?,?)",
            (datetime.now().isoformat(), companies_checked, jobs_found, new_jobs_found),
        )


def get_recent_jobs(limit: int = 200) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs ORDER BY first_seen DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_run_stats() -> dict:
    with _connect() as con:
        last_run = con.execute(
            "SELECT * FROM runs ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        total_jobs = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        today = datetime.now().date().isoformat()
        today_new = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE first_seen LIKE ?", (f"{today}%",)
        ).fetchone()[0]
    return {
        "last_run": dict(last_run) if last_run else None,
        "total_jobs": total_jobs,
        "today_new": today_new,
    }


def update_job_status(content_hash: str, status: str):
    """User marks a job as 'saved', 'not_interested', or 'new'."""
    with _connect() as con:
        con.execute(
            "UPDATE jobs SET user_status = ? WHERE content_hash = ?",
            (status, content_hash),
        )


def get_hot_jobs_since(since_iso: str, threshold: int) -> list[dict]:
    """Return unacknowledged hot jobs found after since_iso (for live alert polling)."""
    with _connect() as con:
        rows = con.execute(
            """SELECT * FROM jobs
               WHERE score >= ? AND first_seen >= ? AND alert_shown = 0
               ORDER BY score DESC, first_seen DESC""",
            (threshold, since_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_alert_shown(content_hashes: list[str]):
    """Record that the in-app alert was shown for these jobs."""
    with _connect() as con:
        con.executemany(
            "UPDATE jobs SET alert_shown = 1 WHERE content_hash = ?",
            [(h,) for h in content_hashes],
        )


def get_company_states() -> dict:
    """Return {company_name: row_dict} for all tracked companies."""
    with _connect() as con:
        rows = con.execute("SELECT * FROM company_state").fetchall()
    return {r["company_name"]: dict(r) for r in rows}


def get_real_job_count() -> int:
    """Jobs not marked as not_a_job."""
    with _connect() as con:
        return con.execute(
            "SELECT COUNT(*) FROM jobs WHERE (user_status IS NULL OR user_status != 'not_a_job')"
        ).fetchone()[0]


def get_job_counts_by_company() -> dict:
    """Return {company_name: real_job_count} (excludes not_a_job)."""
    with _connect() as con:
        rows = con.execute(
            "SELECT company, COUNT(*) as cnt FROM jobs "
            "WHERE (user_status IS NULL OR user_status != 'not_a_job') "
            "GROUP BY company"
        ).fetchall()
    return {r["company"]: r["cnt"] for r in rows}


def get_all_scoreable_jobs(limit: int = 50000) -> list[dict]:
    """All real jobs — used for force-rescore. Excludes not_a_job."""
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE (user_status IS NULL OR user_status != 'not_a_job') "
            "ORDER BY first_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_companies_with_bad_urls(min_score: int = 5) -> list[str]:
    """Return sorted company names that have at least one job with a bad URL and score >= min_score."""
    with _connect() as con:
        rows = con.execute(
            """SELECT DISTINCT company FROM jobs
               WHERE score >= ?
                 AND (user_status IS NULL OR user_status != 'not_a_job')
                 AND (
                   url IS NULL OR url = ''
                   OR url LIKE 'javascript:%' OR url LIKE 'mailto:%'
                   OR url LIKE 'data:%'   OR url LIKE 'tel:%'
                   OR (url NOT LIKE 'http://%' AND url NOT LIKE 'https://%')
                 )
               ORDER BY company""",
            (min_score,),
        ).fetchall()
    return [r["company"] for r in rows]


def _is_bad_url(url: str | None) -> bool:
    """True for empty, non-HTTP, or pseudo-protocol URLs."""
    if not url:
        return True
    url = url.strip()
    lower = url.lower()
    if lower.startswith(("javascript:", "mailto:", "data:", "tel:", "#")):
        return True
    if not lower.startswith(("http://", "https://")):
        return True
    return False


def fix_bad_urls(min_score: int = 5) -> int:
    """
    Clear bad/non-HTTP URLs from jobs with score >= min_score.
    Resets url to '' and recomputes content_hash without the URL so the
    next force-rescrape can backfill the correct link.
    Returns the count of rows updated.
    """
    with _connect() as con:
        rows = con.execute(
            "SELECT id, content_hash, company, title, url FROM jobs "
            "WHERE score >= ? AND (user_status IS NULL OR user_status != 'not_a_job')",
            (min_score,),
        ).fetchall()
        fixed = 0
        for row in rows:
            if not _is_bad_url(row["url"]):
                continue
            new_hash = make_hash(row["company"], row["title"], "")
            # Guard against producing a duplicate hash
            conflict = con.execute(
                "SELECT id FROM jobs WHERE content_hash = ? AND id != ?",
                (new_hash, row["id"]),
            ).fetchone()
            if conflict:
                # Just clear the URL; leave hash as-is to avoid unique constraint error
                con.execute("UPDATE jobs SET url = '' WHERE id = ?", (row["id"],))
            else:
                con.execute(
                    "UPDATE jobs SET url = '', content_hash = ? WHERE id = ?",
                    (new_hash, row["id"]),
                )
            fixed += 1
    return fixed


def _clean_title_migrate(title: str) -> str:
    """Inline title cleaner for DB migration — mirrors scrape_jobs._clean_title."""
    import re
    if not title:
        return title
    title = re.sub(
        r'(\*+|\[|\]|\bRead More\b|\bRead Less\b|\bLoad More\b|\bSee More\b)',
        '', title, flags=re.IGNORECASE,
    )
    return re.sub(r'\s+', ' ', title).strip()


def clean_existing_titles():
    """One-time cleanup: strip junk chars from titles already in the DB and recompute hashes."""
    with _connect() as con:
        rows = con.execute("SELECT id, content_hash, company, title, url FROM jobs").fetchall()
        for row in rows:
            cleaned = _clean_title_migrate(row["title"])
            if cleaned == row["title"]:
                continue
            new_hash = make_hash(row["company"], cleaned, row["url"] or "")
            existing = con.execute(
                "SELECT id FROM jobs WHERE content_hash = ?", (new_hash,)
            ).fetchone()
            if existing and existing["id"] != row["id"]:
                # Clean version already in DB — remove this dirty duplicate
                con.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
            else:
                con.execute(
                    "UPDATE jobs SET title = ?, content_hash = ? WHERE id = ?",
                    (cleaned, new_hash, row["id"]),
                )


def migrate_db():
    """Add any missing columns for schema evolution without destroying existing data."""
    with _connect() as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
        if "alert_shown" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN alert_shown INTEGER DEFAULT 0")

        cs_cols = {r[1] for r in con.execute("PRAGMA table_info(company_state)").fetchall()}
        for col, definition in [
            ("last_status",    "TEXT"),
            ("last_error",     "TEXT"),
            ("last_job_count", "INTEGER"),
        ]:
            if col not in cs_cols:
                con.execute(f"ALTER TABLE company_state ADD COLUMN {col} {definition}")

    clean_existing_titles()


if __name__ == "__main__":
    init_db()
    migrate_db()
    print(f"Database initialized at {DB_PATH}")
