"""
Gmail API email sender. Two modes:
  - digest: all unemailed jobs above threshold, grouped by company
  - alert:  single high-score job, sent immediately
"""
import base64
import json
import os
from collections import defaultdict
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).parent.parent

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    Environment = FileSystemLoader = None  # type: ignore

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    HAS_GOOGLE = True
except ImportError:
    HAS_GOOGLE = False

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
TEMPLATE_DIR = ROOT / "templates"


def _get_gmail_service():
    if not HAS_GOOGLE:
        raise RuntimeError("google-api-python-client not installed")

    creds_path  = Path(os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json"))
    token_path  = Path(os.getenv("GMAIL_TOKEN_PATH", "token.json"))

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {creds_path}. "
                    "Run setup.py to complete Gmail OAuth."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)


def _render_template(jobs: list[dict], is_alert: bool = False) -> str:
    if Environment is None:
        raise RuntimeError("jinja2 not installed")

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("email.html")

    # Parse matched_keywords from JSON string if needed
    for job in jobs:
        kw = job.get("matched_keywords")
        if isinstance(kw, str):
            try:
                job["matched_keywords"] = json.loads(kw)
            except Exception:
                job["matched_keywords"] = []

    # Group by company
    grouped: dict[str, list] = defaultdict(list)
    for job in jobs:
        grouped[job["company"]].append(job)
    # Sort companies by their best score descending
    sorted_groups = dict(
        sorted(grouped.items(), key=lambda kv: max(j.get("score") or 0 for j in kv[1]), reverse=True)
    )

    return template.render(
        jobs=jobs,
        grouped_jobs=sorted_groups,
        total_new=len(jobs),
        total_matching=len(jobs),
        companies_checked=len(grouped),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        is_alert=is_alert,
    )


def _send(service, to: str, subject: str, html_body: str):
    sender = os.getenv("ALERT_EMAIL", "")
    msg = MIMEMultipart("alternative")
    msg["From"]    = sender
    msg["To"]      = to
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_digest(jobs: list[dict], threshold: int | None = None) -> bool:
    """Send a digest email of all supplied jobs. Returns True on success."""
    if os.getenv("EMAIL_ENABLED", "true").lower() == "false":
        print("  [email] Email alerts disabled (EMAIL_ENABLED=false) — skipping digest")
        return False

    if not jobs:
        print("  [email] No jobs to send — skipping digest")
        return False

    threshold = threshold or int(os.getenv("SCORE_THRESHOLD", "6"))
    matching  = [j for j in jobs if (j.get("score") or 0) >= threshold]

    if not matching:
        print(f"  [email] No jobs meet score threshold {threshold} — skipping digest")
        return False

    to = os.getenv("ALERT_EMAIL", "")
    if not to:
        print("  [email] ALERT_EMAIL not configured — skipping digest")
        return False

    date_str = datetime.now().strftime("%b %d")
    subject = f"Job Digest — {len(matching)} new role{'s' if len(matching) != 1 else ''} match your profile [{date_str}]"

    html = _render_template(matching, is_alert=False)

    try:
        svc = _get_gmail_service()
        _send(svc, to, subject, html)
        print(f"  [email] Digest sent to {to} ({len(matching)} jobs)")
        return True
    except Exception as e:
        print(f"  [email] Failed to send digest: {e}")
        return False


def send_alert(job: dict) -> bool:
    """Send an immediate alert for a single high-score job."""
    if os.getenv("EMAIL_ENABLED", "true").lower() == "false":
        print("  [email] Email alerts disabled (EMAIL_ENABLED=false) — skipping alert")
        return False

    to = os.getenv("ALERT_EMAIL", "")
    if not to:
        print("  [email] ALERT_EMAIL not configured — skipping alert")
        return False

    subject = f"⚡ Hot Job Alert — {job['title']} at {job['company']} ({job.get('score',0)}/10)"

    html = _render_template([job], is_alert=True)

    try:
        svc = _get_gmail_service()
        _send(svc, to, subject, html)
        print(f"  [email] Alert sent: {job['title']} @ {job['company']}")
        return True
    except Exception as e:
        print(f"  [email] Failed to send alert: {e}")
        return False


def send_test_email() -> tuple[bool, str]:
    """Send a plain test email to verify Gmail auth and config. Returns (success, message)."""
    to = os.getenv("ALERT_EMAIL", "")
    if not to:
        return False, "ALERT_EMAIL is not set in .env — add it in Settings and save."
    if os.getenv("EMAIL_ENABLED", "true").lower() == "false":
        return False, "EMAIL_ENABLED is set to false — toggle 'Email alerts enabled' in Settings."
    try:
        svc = _get_gmail_service()
        html = (
            "<h2 style='font-family:sans-serif'>✅ Job Board Watcher — email is working!</h2>"
            "<p style='font-family:sans-serif'>If you received this, Gmail alerts and digests are configured correctly.</p>"
        )
        _send(svc, to, "Test Email — Job Board Watcher", html)
        return True, f"Test email sent to {to}"
    except Exception as e:
        return False, f"Send failed: {e}"


if __name__ == "__main__":
    # Quick render test (no email sent)
    fake_jobs = [
        {
            "company": "Acme Corp",
            "title": "Senior Product Manager",
            "department": "Product",
            "location": "Remote, US",
            "url": "https://example.com/jobs/1",
            "score": 9,
            "score_reason": "Strong match — aligns with your B2B SaaS PM background.",
            "matched_keywords": '["B2B SaaS", "growth", "analytics"]',
        }
    ]
    html = _render_template(fake_jobs, is_alert=True)
    out = ROOT / ".tmp" / "test_email.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Test email rendered to {out}")
