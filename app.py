"""
Streamlit dashboard — the day-to-day UI for Job Board Watcher.
Open with: streamlit run app.py
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import streamlit as st
from datetime import timezone as _tz

import time as _time

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from tools import runner as _runner


def _fmt_local(iso_str: str) -> str:
    """Convert a UTC ISO timestamp stored in the DB to the local time display string."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str).replace(tzinfo=_tz.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str[:16].replace("T", " ")

from tools.diff_jobs import (
    init_db, migrate_db, get_recent_jobs as _get_recent_jobs, get_run_stats, update_job_status,
    get_unsent_jobs, mark_emailed, get_hot_jobs_since, mark_alert_shown,
    get_unscored_jobs, get_company_states, get_real_job_count,
    get_job_counts_by_company,
)

# Cache the full job list so repeated reruns don't re-query the DB every time.
# Call get_recent_jobs.clear() after any DB mutation to force a fresh load.
@st.cache_data(ttl=60, show_spinner=False)
def get_recent_jobs(limit: int = 10000) -> list[dict]:
    return _get_recent_jobs(limit)


# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Job Board Watcher",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="collapsed",
)

init_db()
migrate_db()

COMPANIES_FILE = ROOT / "companies.json"
PROFILE_DIR    = ROOT / "profile"
ENV_FILE       = ROOT / ".env"

API_PLATFORMS  = {"greenhouse", "lever", "ashby", "workable", "smartrecruiters"}

# careers_url values that indicate a company has no scrapeable URL
_INVALID_URLS = {"", "N/A", "X", "x", "n/a"}

# Default intervals (local-optimised — faster than the original cloud defaults)
DEFAULT_FAST_MIN    = 30   # API companies: every 30 min
DEFAULT_SLOW_MIN    = 120  # Playwright: every 2 hours
DEFAULT_TASK_MIN    = 5    # Task Scheduler fires every 5 min


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_companies() -> list[dict]:
    if not COMPANIES_FILE.exists():
        return []
    return json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))


def save_companies(companies: list[dict]):
    COMPANIES_FILE.write_text(json.dumps(companies, indent=2, ensure_ascii=False), encoding="utf-8")


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def save_env(updates: dict):
    env = load_env()
    env.update(updates)
    ENV_FILE.write_text(
        "\n".join(f"{k}={v}" for k, v in env.items()) + "\n",
        encoding="utf-8",
    )


def score_badge(score) -> str:
    if score is None:
        return "–"
    s = int(score)
    return f"🟢 {s}/10" if s >= 8 else (f"🟡 {s}/10" if s >= 6 else f"⚪ {s}/10")


def detect_platform(url: str) -> tuple[str, str | None]:
    patterns = [
        (r"boards\.greenhouse\.io/([^/?#\s]+)",     "greenhouse"),
        (r"job-boards\.greenhouse\.io/([^/?#\s]+)", "greenhouse"),
        (r"jobs\.lever\.co/([^/?#\s]+)",            "lever"),
        (r"jobs\.ashbyhq\.com/([^/?#\s]+)",         "ashby"),
        (r"apply\.workable\.com/([^/?#\s]+)",        "workable"),
        (r"jobs\.smartrecruiters\.com/([^/?#\s]+)", "smartrecruiters"),
        (r"myworkdayjobs\.com",                      "workday"),
        (r"workday\.com",                            "workday"),
        (r"icims\.com",                              "icims"),
        (r"ashbyhq\.com",                            "ashby"),
        (r"greenhouse\.io",                          "greenhouse"),
        (r"lever\.co",                               "lever"),
    ]
    for pat, platform in patterns:
        m = re.search(pat, url, re.IGNORECASE)
        if m:
            slug = m.group(1).rstrip("/") if m.lastindex else None
            method = "api" if platform in API_PLATFORMS and slug else "playwright"
            return platform, slug
    return "custom", None


def update_task_scheduler(task_interval_min: int):
    """Re-register the Windows Task Scheduler task with a new interval."""
    python_exe  = sys.executable
    run_script  = str(ROOT / "run.py")
    cmd = [
        "schtasks", "/create", "/f",
        "/tn", "JobBoardWatcher",
        "/tr", f'"{python_exe}" "{run_script}"',
        "/sc", "minute", "/mo", str(task_interval_min),
        "/st", "00:00",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


# ── Audio alert JavaScript ────────────────────────────────────────────────────

AUDIO_SCRIPTS = {
    "Gentle Bell": """
        var ctx = new (window.AudioContext || window.webkitAudioContext)();
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        osc.connect(gain); gain.connect(ctx.destination);
        osc.type = 'sine';
        osc.frequency.setValueAtTime(800, ctx.currentTime);
        gain.gain.setValueAtTime(0.25, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 1.8);
        osc.start(ctx.currentTime); osc.stop(ctx.currentTime + 1.8);
    """,
    "Two-Tone Ping": """
        var ctx = new (window.AudioContext || window.webkitAudioContext)();
        [0, 0.22].forEach(function(delay, i) {
            var osc = ctx.createOscillator();
            var gain = ctx.createGain();
            osc.connect(gain); gain.connect(ctx.destination);
            osc.type = 'sine';
            osc.frequency.setValueAtTime(i === 0 ? 660 : 990, ctx.currentTime + delay);
            gain.gain.setValueAtTime(0.28, ctx.currentTime + delay);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + delay + 0.9);
            osc.start(ctx.currentTime + delay); osc.stop(ctx.currentTime + delay + 0.9);
        });
    """,
    "Ascending Chime": """
        var ctx = new (window.AudioContext || window.webkitAudioContext)();
        [523, 659, 784].forEach(function(freq, i) {
            var osc = ctx.createOscillator();
            var gain = ctx.createGain();
            osc.connect(gain); gain.connect(ctx.destination);
            osc.type = 'sine';
            osc.frequency.setValueAtTime(freq, ctx.currentTime + i * 0.18);
            gain.gain.setValueAtTime(0.22, ctx.currentTime + i * 0.18);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i * 0.18 + 1.1);
            osc.start(ctx.currentTime + i * 0.18);
            osc.stop(ctx.currentTime + i * 0.18 + 1.1);
        });
    """,
    "Soft Pulse": """
        var ctx = new (window.AudioContext || window.webkitAudioContext)();
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        osc.connect(gain); gain.connect(ctx.destination);
        osc.type = 'sine';
        osc.frequency.setValueAtTime(440, ctx.currentTime);
        gain.gain.setValueAtTime(0.35, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
        osc.start(ctx.currentTime); osc.stop(ctx.currentTime + 0.35);
    """,
}


def inject_alert(jobs: list[dict], sound: str, visual: str):
    """Inject JavaScript to play audio + show browser notification for hot jobs."""
    if not jobs:
        return

    job = jobs[0]
    title = f"Hot Job: {job['title']}"
    body  = f"{job['company']} — Score {job.get('score','?')}/10"

    audio_js = AUDIO_SCRIPTS.get(sound, "") if sound != "Off" else ""

    notif_js = ""
    if visual in ("Browser Notification", "Both"):
        notif_js = f"""
        if (window.Notification) {{
            if (Notification.permission === 'granted') {{
                new Notification({json.dumps(title)}, {{
                    body: {json.dumps(body)},
                    icon: 'https://cdn.jsdelivr.net/npm/twemoji@14/assets/72x72/1f50d.png'
                }});
            }} else if (Notification.permission !== 'denied') {{
                Notification.requestPermission().then(function(p) {{
                    if (p === 'granted') {{
                        new Notification({json.dumps(title)}, {{body: {json.dumps(body)}}});
                    }}
                }});
            }}
        }}
        """

    js = f"<script>{audio_js}\n{notif_js}</script>"
    st.components.v1.html(js, height=0)


def request_notification_permission():
    st.components.v1.html("""
    <script>
    if (window.Notification && Notification.permission === 'default') {
        Notification.requestPermission();
    }
    </script>
    """, height=0)


def auto_refresh_js(seconds: int):
    st.components.v1.html(
        f"<script>setTimeout(function(){{window.location.reload();}},{seconds*1000});</script>",
        height=0,
    )


def run_streaming(cmd: list[str], log_area, cwd: str) -> int:
    """Run cmd, stream stdout line-by-line into log_area. Returns exit code."""
    lines: list[str] = []
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", cwd=cwd,
    ) as proc:
        for raw in proc.stdout:
            line = raw.rstrip()
            lines.append(line)
            log_area.code("\n".join(lines[-35:]))
        proc.wait()
    return proc.returncode


# ── Main layout ───────────────────────────────────────────────────────────────

st.title("🔍 Job Board Watcher")
tabs = st.tabs(["📊 Dashboard", "💼 New Jobs", "🏢 Companies", "👤 Profile", "⚙️ Settings"])
env = load_env()


# ════════════════════════════════════════════════════════════════════════
# TAB 1: Dashboard
# ════════════════════════════════════════════════════════════════════════
with tabs[0]:
    stats = get_run_stats()
    last_run = stats.get("last_run")

    # ── Live monitoring controls ─────────────────────────────────────────
    live_col, sound_col, visual_col = st.columns([1, 1, 1])
    with live_col:
        live_mode = st.toggle(
            "Live Monitoring",
            value=st.session_state.get("live_mode", False),
            help="Auto-refreshes the dashboard and triggers alerts when hot jobs are found.",
        )
        st.session_state["live_mode"] = live_mode
    with sound_col:
        sound_choice = st.selectbox(
            "Alert sound",
            options=["Ascending Chime", "Gentle Bell", "Two-Tone Ping", "Soft Pulse", "Off"],
            index=0,
            label_visibility="visible",
        )
    with visual_col:
        visual_choice = st.selectbox(
            "Alert style",
            options=["Both", "Browser Notification", "Banner Only"],
            index=0,
        )

    # ── Metrics ──────────────────────────────────────────────────────────
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Jobs Found", stats["total_jobs"])
    col2.metric("Real Jobs", get_real_job_count(),
                help="Excludes items marked 'Not a Job'")
    col3.metric("New Today", stats["today_new"])
    col4.metric("Companies Watched", sum(1 for c in load_companies() if c.get("active", True)))
    col5.metric("Last Run", _fmt_local(last_run["timestamp"]) if last_run else "Never")

    st.divider()

    # ── Live alert check ─────────────────────────────────────────────────
    alert_threshold = int(env.get("IMMEDIATE_ALERT_THRESHOLD", "9"))
    poll_secs = int(env.get("ALERT_POLL_SECONDS", "60"))

    if live_mode:
        request_notification_permission()
        # Check for new hot jobs since last session check
        last_check = st.session_state.get("last_alert_check", "2000-01-01T00:00:00")
        hot = get_hot_jobs_since(last_check, alert_threshold)
        st.session_state["last_alert_check"] = datetime.utcnow().isoformat()

        if hot:
            inject_alert(hot, sound_choice, visual_choice)
            mark_alert_shown([j["content_hash"] for j in hot])

            if visual_choice in ("Banner Only", "Both"):
                st.error(
                    f"🚨 **{len(hot)} HOT JOB{'S' if len(hot)>1 else ''} FOUND!**   "
                    + "   |   ".join(f"{j['title']} @ {j['company']} ({j.get('score','?')}/10)" for j in hot[:3]),
                    icon="🚨",
                )

        auto_refresh_js(poll_secs)
        st.caption(f"Live mode ON — refreshing every {poll_secs}s | Alert threshold: {alert_threshold}/10")

    # ── Background Task Status ────────────────────────────────────────────
    _bg = _runner.get_status()
    _bg_running = bool(_bg and _bg.get("state") == "running")

    if _bg:
        with st.container(border=True):
            st.markdown("#### ⚙️ Background Task")
            if _bg_running:
                _started_dt = datetime.fromisoformat(_bg["started_at"])
                _elapsed    = int((datetime.now() - _started_dt).total_seconds())
                _mins, _secs = divmod(_elapsed, 60)
                _info_col, _stop_col = st.columns([4, 1])
                with _info_col:
                    st.info(f"**Running:** {_bg['label']} — {_mins}m {_secs}s elapsed")
                with _stop_col:
                    if st.button("⏹ Stop", type="secondary", key="stop_bg"):
                        _runner.stop()
                        st.rerun()
                with st.expander("Live log", expanded=True):
                    st.code(_runner.get_log_tail(35))
            else:
                _state = _bg.get("state", "")
                _dur   = _bg.get("duration_s")
                _dur_s = f" in {_dur // 60}m {_dur % 60}s" if _dur is not None else ""
                if _state == "done":
                    st.success(f"✅ **{_bg['label']}** completed{_dur_s}")
                elif _state == "stopped":
                    st.warning(f"⏹ **{_bg['label']}** was stopped{_dur_s}")
                with st.expander("Last run log"):
                    st.code(_runner.get_log_tail(35))

        _history = _runner.get_history()
        if _history:
            with st.expander("📊 Run History & Timing Benchmarks"):
                _rows = []
                for _h in reversed(_history[-15:]):
                    _d = _h.get("duration_s")
                    _rows.append({
                        "Task":     _h.get("label", ""),
                        "Started":  _fmt_local(_h.get("started_at", "")),
                        "Status":   _h.get("state", ""),
                        "Duration": f"{_d // 60}m {_d % 60}s" if _d is not None else "—",
                    })
                st.dataframe(_rows, use_container_width=True, hide_index=True)

    st.divider()

    # ── Run Now ──────────────────────────────────────────────────────────
    st.subheader("Run Now")
    col_run, col_digest = st.columns([1, 1])

    with col_run:
        run_company = st.text_input("Company filter (leave blank for all due)")
        c1, c2, c3 = st.columns(3)
        force_all  = c1.checkbox("Force all")
        dry_run    = c2.checkbox("Dry run")
        skip_score = c3.checkbox("Skip scoring")

        if st.button("▶ Start Run", type="primary", disabled=_bg_running):
            cmd = [sys.executable, str(ROOT / "run.py"), "--no-hours"]
            if run_company: cmd += ["--company", run_company]
            if force_all:   cmd.append("--force-all")
            if dry_run:     cmd.append("--dry-run")
            if skip_score:  cmd.append("--skip-score")
            _runner.start("Scrape & Score" + (f" ({run_company})" if run_company else ""), cmd)
            st.rerun()

    with col_digest:
        st.subheader("Send Digest Now")
        st.caption("Sends all unsent matching jobs to your alert email.")
        if st.button("📧 Send Digest", disabled=_bg_running):
            cmd = [sys.executable, str(ROOT / "run.py"), "--send-digest", "--no-hours"]
            _runner.start("Send Digest", cmd)
            st.rerun()

    st.divider()

    # ── Scoring ───────────────────────────────────────────────────────────
    st.subheader("Scoring")
    unscored = get_unscored_jobs()
    all_real  = get_real_job_count()

    sc_col1, sc_col2 = st.columns(2)

    _using_ollama = bool(env.get("OLLAMA_BASE_URL", "").strip())

    with sc_col1:
        st.caption(f"**{len(unscored)}** unscored real jobs in database")
        if unscored:
            if _using_ollama:
                st.info(f"Scoring {len(unscored)} jobs locally via Ollama (free)")
            else:
                est = len(unscored) * 0.0003
                st.info(f"Estimated cost: ~${est:.2f} (Claude Haiku, ~$0.0003/job)")
            if st.button(f"⚡ Score {len(unscored)} Unscored Jobs", disabled=_bg_running):
                cmd = [sys.executable, str(ROOT / "run.py"), "--score-only", "--no-hours"]
                _runner.start(f"Score {len(unscored)} Jobs", cmd)
                st.rerun()
        else:
            st.success("All jobs are scored.")

    with sc_col2:
        if _using_ollama:
            st.info(
                f"**Rescore All** will re-score all {all_real:,} real jobs using Ollama (free, local).  \n"
                "Use this after changing your resume or preferences."
            )
        else:
            est_all = all_real * 0.0003
            st.warning(
                f"**Rescore All** will re-score all {all_real:,} real jobs using the Claude API, "
                f"overwriting existing scores.  \nEstimated cost: **~${est_all:.2f}**  \n"
                "Use this after changing your resume or preferences."
            )
        confirm_rescore = st.checkbox(
            "I understand this will overwrite existing scores" if _using_ollama else "I understand this will use API credits",
            key="confirm_rescore",
        )
        if st.button("🔄 Rescore All Real Jobs", disabled=not confirm_rescore or _bg_running):
            cmd = [sys.executable, str(ROOT / "run.py"), "--rescore-all", "--no-hours"]
            _runner.start(f"Rescore All ({all_real:,} jobs)", cmd)
            st.rerun()

    st.divider()
    st.subheader("Recent Jobs (Last 48h)")
    jobs = get_recent_jobs(limit=50)
    cutoff = (datetime.utcnow() - timedelta(hours=48)).isoformat()
    recent = [j for j in jobs if j.get("first_seen", "") >= cutoff]
    _dash_company_lookup = {
        c["name"]: c.get("careers_url", "")
        for c in load_companies()
        if c.get("careers_url", "") not in _INVALID_URLS
    }
    if recent:
        for j in recent:
            sc = score_badge(j.get("score"))
            with st.expander(f"{sc}  **{j['title']}** — {j['company']}  |  {j.get('location','')}"):
                if j.get("score_reason"):
                    st.caption(j["score_reason"])
                if j.get("url"):
                    st.markdown(f"[View Job →]({j['url']})")
                else:
                    fb = _dash_company_lookup.get(j.get("company", ""), "")
                    if fb:
                        st.markdown(f"[View Company Jobs ↗]({fb})")
                st.caption(_fmt_local(j.get("first_seen", "")))
    else:
        st.info("No jobs found in the last 48 hours. Run a scrape to get started.")

    # Auto-refresh Dashboard while a background task is running (JS-based, non-blocking)
    if _bg_running:
        auto_refresh_js(3)


# ════════════════════════════════════════════════════════════════════════
# TAB 2: New Jobs
# ════════════════════════════════════════════════════════════════════════

# ── Non-job detection ─────────────────────────────────────────────────────────

# Explicit title substrings that are never job postings
_NON_JOB_PATTERNS = [
    # Talent pipeline / alerts
    "talent community", "talent pool", "talent network",
    "join our network", "stay connected", "get notified",
    "job alert", "set up alert", "create an alert", "subscribe to",
    "sign up for", "email me jobs",
    # Referral / recruitment programs
    "employee referral", "refer a friend", "referral program",
    "campus recruit", "university recruit", "early careers",
    "graduate program", "internship program", "students & grad",
    # Hiring process / culture info pages
    "how we hire", "our hiring", "interview process", "application process",
    "recruitment process", "hiring faq", "recruitment faq",
    "life at ", "working at ", "work at ", "working here",
    "our culture", "company culture", "culture & values", "culture and values",
    "why work", "why join", "why us",
    "diversity & inclusion", "diversity and inclusion", "dei at",
    "belonging at", "equity & inclusion",
    "benefits & perks", "total rewards", "compensation & benefits",
    # Navigation / page sections
    "skip to", "back to top", "return to",
    "explore all teams", "explore teams", "all departments",
    "all job categories", "browse jobs", "search jobs",
    "view all jobs", "view open", "see all positions",
    "job categories", "career areas", "functional areas",
    # Military / disability / accessibility
    "military & veteran", "us military", "veteran opportunit",
    "disability accommodat", "accessibility statement",
    # Scams / legal / compliance
    "avoid recruiting scam", "recruitment fraud", "fraud alert",
    "privacy notice", "privacy policy", "cookie policy", "terms of use",
    "legal notice",
    # Generic filler
    "general application", "future opportunit", "expression of interest",
    "evergreen", "coming soon", "no current opening", "no open position",
    "no jobs found", "placeholder", "powered by", "about us",
    # Common named nav sections (add company-specific ones here)
    "how we recruit", "ai & hiring", "hiring experience",
    "explore all", "meet our team", "our story", "about genesys",
]

# Words that appear in virtually all real job titles.
# A title with NONE of these is almost certainly a navigation item.
_JOB_WORDS = {
    "engineer", "engineering", "developer", "programmer", "architect",
    "manager", "director", "coordinator", "administrator", "supervisor",
    "analyst", "analytics", "analysis",
    "specialist", "generalist", "consultant", "advisor", "strategist",
    "designer", "design",
    "scientist", "researcher", "research",
    "executive", "officer", "president", "vp", "vice president",
    "lead", "principal", "head", "fellow", "associate",
    "recruiter", "recruiting", "sourcer", "talent acquisition",
    "accountant", "accounting", "controller", "auditor", "actuary",
    "attorney", "counsel", "paralegal", "compliance",
    "technician", "mechanic", "installer", "operator",
    "writer", "editor", "copywriter", "journalist",
    "producer", "project", "program",
    "sales", "account", "business development", "bd",
    "marketing", "brand", "growth", "demand generation",
    "customer success", "customer support", "customer experience",
    "data", "cloud", "security", "network", "platform", "infrastructure",
    "devops", "sre", "qa", "quality assurance", "test",
    "product manager", "product owner",
    "supply chain", "logistics", "procurement", "buyer",
    "pharmacist", "nurse", "physician", "therapist", "clinician",
    "instructor", "trainer", "professor", "teacher",
    "intern", "co-op", "apprentice",
    "representative", "agent", "liaison",
    "finance", "financial", "treasury", "tax",
    "operations", "ops",
    "legal", "risk", "governance",
    "communications", "pr", "public relations",
    "it ", "information technology", "helpdesk", "support",
}


def _is_non_job(j: dict) -> bool:
    title = (j.get("title") or "").lower().strip()
    desc  = (j.get("description") or "").strip()
    dept  = (j.get("department") or "").strip()

    # Layer 1: explicit pattern match
    if any(p in title for p in _NON_JOB_PATTERNS):
        return True

    # Layer 2: heuristic — short title with no supporting content and no job word
    # Real job postings almost always have either a description, a department,
    # or a recognizable job-level word in the title.
    words = title.split()
    if len(words) <= 5 and not desc and not dept:
        if not any(jw in title for jw in _JOB_WORDS):
            return True

    return False


with tabs[1]:
    st.subheader("All Discovered Jobs")
    all_jobs = get_recent_jobs(limit=10000)
    companies_list = sorted(set(j["company"] for j in all_jobs))

    # Build fallback URL lookup: company name → careers page URL (for jobs with no job-specific URL)
    _INVALID_URLS = {"", "N/A", "X", "x", "n/a"}
    company_careers_lookup = {
        c["name"]: c.get("careers_url", "")
        for c in load_companies()
        if c.get("careers_url", "") not in _INVALID_URLS
    }

    # ── View mode ────────────────────────────────────────────────────────
    view_col, _ = st.columns([2, 3])
    with view_col:
        view_mode = st.radio(
            "View mode", ["Triage", "Detail"], horizontal=True,
            help="Triage = compact one-click review | Detail = full info with expanders",
        )

    # ── Filters ──────────────────────────────────────────────────────────
    col_f1, col_f2, col_f3, col_f4 = st.columns([2, 1, 1, 1])
    filter_company = col_f1.multiselect("Filter by company", companies_list)
    min_score      = col_f2.slider("Min score", 0, 10, 0)
    status_filter  = col_f3.selectbox(
        "Status filter",
        ["active", "saved", "not_interested", "not_a_job"],
        help="'active' shows new + saved; hides items marked Not a Job.",
    )
    sort_jobs_by = col_f4.selectbox(
        "Sort by",
        ["Score (high→low)", "Date (newest first)", "Date (oldest first)", "Company (A→Z)"],
    )

    filtered = all_jobs
    if filter_company:
        filtered = [j for j in filtered if j["company"] in filter_company]
    if min_score > 0:
        filtered = [j for j in filtered if (j.get("score") or 0) >= min_score]
    if status_filter == "active":
        filtered = [j for j in filtered if j.get("user_status", "new") != "not_a_job"]
    else:
        filtered = [j for j in filtered if j.get("user_status", "new") == status_filter]

    if sort_jobs_by == "Score (high→low)":
        filtered.sort(key=lambda x: (x.get("score") or 0), reverse=True)
    elif sort_jobs_by == "Date (newest first)":
        filtered.sort(key=lambda x: x.get("first_seen", ""), reverse=True)
    elif sort_jobs_by == "Date (oldest first)":
        filtered.sort(key=lambda x: x.get("first_seen", ""))
    else:  # Company A→Z
        filtered.sort(key=lambda x: x.get("company", "").lower())

    # ── Auto-clean + counts ───────────────────────────────────────────────
    not_a_job_count   = sum(1 for j in all_jobs if j.get("user_status") == "not_a_job")
    auto_candidates   = [
        j for j in all_jobs
        if j.get("user_status", "new") != "not_a_job" and _is_non_job(j)
    ]

    ac_col, info_col = st.columns([2, 3])
    with ac_col:
        if auto_candidates:
            if st.button(f"🧹 Auto-Clean — {len(auto_candidates)} obvious non-jobs", type="secondary"):
                for j in auto_candidates:
                    update_job_status(j["content_hash"], "not_a_job")
                get_recent_jobs.clear()
                st.success(f"Marked {len(auto_candidates)} items as Not a Job.")
                st.rerun()
        else:
            st.caption("✓ No obvious non-jobs detected")
    with info_col:
        parts = [f"{len(filtered)} jobs shown"]
        if not_a_job_count and status_filter == "active":
            parts.append(f"{not_a_job_count} hidden")
        st.caption("  ·  ".join(parts))

    # ── Pagination ───────────────────────────────────────────────────────
    pg_col1, pg_col2, pg_col3 = st.columns([1, 1, 2])
    page_size  = pg_col1.selectbox("Per page", [25, 50, 100, 200], index=1)
    total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
    # Apply any pending navigation from a previous form submit (must happen before
    # the number_input renders, since widget keys can't be set after render)
    if "_triage_pending_page" in st.session_state:
        st.session_state["triage_page"] = st.session_state.pop("_triage_pending_page")
    if "triage_page" not in st.session_state:
        st.session_state["triage_page"] = 1
    if st.session_state["triage_page"] > total_pages:
        st.session_state["triage_page"] = total_pages
    page_num = pg_col2.number_input("Page", min_value=1, max_value=total_pages,
                                     step=1, key="triage_page")
    pg_col3.caption(f"Page {page_num} of {total_pages}")

    page_start = (page_num - 1) * page_size
    page_jobs  = filtered[page_start : page_start + page_size]

    st.divider()

    # ── Triage view (form — no refresh until submit) ─────────────────────
    if view_mode == "Triage":
        if not page_jobs:
            st.info("Nothing to review on this page.")
        else:
            # Column headers
            h1, h2 = st.columns([0.5, 9.5])
            h1.caption("✓")
            h2.caption("Score · Title · Company · Location")
            st.markdown("<hr style='margin:2px 0;border:none;border-top:2px solid #ddd'>",
                        unsafe_allow_html=True)

            with st.form("triage_form", clear_on_submit=False):
                # Action bar at TOP
                tb1, tb2, tb3, tb4, tb5 = st.columns([2, 2, 1.2, 1.2, 2.6])
                sub_notjob_top = tb1.form_submit_button("🚫 Not a Job", type="primary")
                sub_save_top   = tb2.form_submit_button("⭐ Save Selected")
                sub_prev_top   = tb3.form_submit_button("⬅ Prev", disabled=(page_num <= 1))
                sub_next_top   = tb4.form_submit_button("➡ Next", disabled=(page_num >= total_pages))
                tb5.caption(f"pg {page_num}/{total_pages} · check rows then act, or navigate to auto-apply")

                st.markdown("<hr style='margin:4px 0;border:none;border-top:1px solid #ddd'>",
                            unsafe_allow_html=True)

                for j in page_jobs:
                    chk_col, info_col = st.columns([0.5, 9.5])
                    chk_col.checkbox(
                        "Select", key=f"chk_{j['content_hash']}",
                        label_visibility="collapsed",
                    )
                    with info_col:
                        sc = score_badge(j.get("score"))
                        if j.get("url"):
                            title_md = f"[{j['title']}]({j['url']})"
                        else:
                            fallback = company_careers_lookup.get(j.get("company", ""), "")
                            if fallback:
                                title_md = f"{j['title']} [(careers ↗)]({fallback})"
                            else:
                                title_md = j["title"]
                        meta = " · ".join(filter(None, [
                            j.get("company"), j.get("location"), j.get("department"),
                            _fmt_local(j.get("first_seen", "")),
                        ]))
                        st.markdown(f"{sc}  **{title_md}**  \n<small>{meta}</small>",
                                    unsafe_allow_html=True)
                        if j.get("score_reason"):
                            st.caption(j["score_reason"])
                    st.markdown(
                        "<hr style='margin:3px 0;border:none;border-top:1px solid #f0f0f0'>",
                        unsafe_allow_html=True,
                    )

                # Action bar at BOTTOM too
                bb1, bb2, bb3, bb4, _ = st.columns([2, 2, 1.2, 1.2, 2.6])
                sub_notjob_bot = bb1.form_submit_button("🚫 Not a Job ")
                sub_save_bot   = bb2.form_submit_button("⭐ Save Selected ")
                sub_prev_bot   = bb3.form_submit_button("⬅ Prev ", disabled=(page_num <= 1))
                sub_next_bot   = bb4.form_submit_button("➡ Next ", disabled=(page_num >= total_pages))

            sub_notjob = sub_notjob_top or sub_notjob_bot
            sub_save   = sub_save_top   or sub_save_bot
            sub_prev   = sub_prev_top   or sub_prev_bot
            sub_next   = sub_next_top   or sub_next_bot

            # Process after form submit
            if sub_notjob or sub_save or sub_prev or sub_next:
                checked = [
                    j["content_hash"] for j in page_jobs
                    if st.session_state.get(f"chk_{j['content_hash']}", False)
                ]
                if (sub_notjob or sub_save) and not checked:
                    st.warning("No rows checked — tick at least one before submitting.")
                else:
                    # Apply action to any checked items
                    if checked:
                        new_st = "saved" if sub_save else "not_a_job"
                        for h in checked:
                            update_job_status(h, new_st)
                        get_recent_jobs.clear()
                    # Navigate
                    if sub_next:
                        st.session_state["_triage_pending_page"] = min(page_num + 1, total_pages)
                    elif sub_prev:
                        st.session_state["_triage_pending_page"] = max(page_num - 1, 1)
                    # Clear all checkbox states for the current page before rerun
                    for j in page_jobs:
                        st.session_state.pop(f"chk_{j['content_hash']}", None)
                    st.rerun()

    # ── Detail view ──────────────────────────────────────────────────────
    else:
        for j in page_jobs:
            sc = score_badge(j.get("score"))
            with st.expander(f"{sc}  **{j['title']}** — {j['company']}  ·  {j.get('location','')}"):
                col_a, col_b = st.columns([3, 1])
                with col_a:
                    if j.get("department"):
                        st.caption(f"Department: {j['department']}")
                    if j.get("score_reason"):
                        st.info(j["score_reason"])
                    kw = j.get("matched_keywords")
                    if kw:
                        if isinstance(kw, str):
                            try: kw = json.loads(kw)
                            except: kw = []
                        if kw:
                            st.write("**Keywords:** " + "  ·  ".join(kw))
                    if j.get("url"):
                        st.markdown(f"[🔗 View Job]({j['url']})")
                    else:
                        fallback = company_careers_lookup.get(j.get("company", ""), "")
                        if fallback:
                            st.markdown(f"[🔗 View Company Jobs ↗]({fallback})")
                    st.caption(f"First seen: {_fmt_local(j.get('first_seen',''))}")
                with col_b:
                    current_status = j.get("user_status", "new")
                    safe_status = current_status if current_status in ("new", "saved", "not_interested") else "new"
                    new_status = st.selectbox(
                        "Status",
                        ["new", "saved", "not_interested"],
                        index=["new", "saved", "not_interested"].index(safe_status),
                        key=f"status_{j['content_hash']}",
                    )
                    if new_status != j.get("user_status", "new"):
                        update_job_status(j["content_hash"], new_status)
                        get_recent_jobs.clear()
                        st.rerun()
                    if st.button("🚫 Not a Job", key=f"notjob_{j['content_hash']}",
                                 help="Hide permanently — stays in DB so it won't reappear."):
                        update_job_status(j["content_hash"], "not_a_job")
                        get_recent_jobs.clear()
                        st.rerun()


# ════════════════════════════════════════════════════════════════════════
# TAB 3: Companies
# ════════════════════════════════════════════════════════════════════════
with tabs[2]:
    st.subheader("Company Watchlist")
    companies = load_companies()

    # ── Sort controls ────────────────────────────────────────────────────
    sc1, sc2, _ = st.columns([2, 1, 2])
    sort_by  = sc1.selectbox("Sort by", ["Name", "Platform", "Method", "Active", "Interval"])
    sort_asc = sc2.radio("Order", ["A→Z", "Z→A"], horizontal=True) == "A→Z"

    _sort_keys = {
        "Name":     lambda c: c.get("name", "").lower(),
        "Platform": lambda c: c.get("platform", "").lower(),
        "Method":   lambda c: c.get("scraping_method", "").lower(),
        "Active":   lambda c: (0 if c.get("active", True) else 1),
        "Interval": lambda c: float(c.get("check_interval_hours", 0)),
    }
    sorted_indexed = sorted(
        enumerate(companies), key=lambda x: _sort_keys[sort_by](x[1]), reverse=not sort_asc
    )

    with st.expander("➕ Add a Company"):
        new_name  = st.text_input("Company name")
        new_url   = st.text_input("Career page URL")
        new_notes = st.text_input("Notes (optional)")
        if st.button("Add Company"):
            if new_name and new_url:
                platform, slug = detect_platform(new_url)
                interval_min = int(env.get("FAST_CHECK_INTERVAL_MIN", str(DEFAULT_FAST_MIN))) if platform in API_PLATFORMS else int(env.get("SLOW_CHECK_INTERVAL_MIN", str(DEFAULT_SLOW_MIN)))
                method = "api" if platform in API_PLATFORMS and slug else "playwright"
                companies.append({
                    "name": new_name, "platform": platform, "ats_slug": slug,
                    "careers_url": new_url, "scraping_method": method,
                    "check_interval_hours": round(interval_min / 60, 2),
                    "active": True, "notes": new_notes,
                })
                save_companies(companies)
                st.success(f"Added {new_name} ({platform}, {method})")
                st.rerun()
            else:
                st.warning("Name and URL are required.")

    # Legend
    st.caption(
        "🟢 Scraped OK · 🟡 Scraped but 0 jobs returned · 🔴 Last scrape errored · "
        "⚪ Never scraped · ⏸️ Paused"
    )
    st.divider()

    co_states   = get_company_states()
    job_counts  = get_job_counts_by_company()

    for i, c in sorted_indexed:
        state       = co_states.get(c["name"], {})
        last_status = state.get("last_status")
        last_error  = state.get("last_error")
        real_jobs   = job_counts.get(c["name"], 0)

        if not c.get("active", True):
            status_icon = "⏸️"
        elif last_status == "error":
            status_icon = "🔴"
        elif last_status == "empty":
            status_icon = "🟡"
        elif last_status == "success":
            status_icon = "🟢"
        else:
            status_icon = "⚪"

        job_badge = f" · **{real_jobs}** real jobs" if real_jobs else ""
        label = (
            f"{status_icon} **{c['name']}** — {c.get('platform','?')} · "
            f"{c.get('scraping_method','?')} · every {c.get('check_interval_hours','?')}h"
            f"{job_badge}"
        )
        with st.expander(label, key=f"co_{i}"):
            col_x, col_y, col_z, col_w = st.columns([2, 1, 1, 1])
            with col_x:
                st.markdown(f"**URL:** [{c.get('careers_url','')}]({c.get('careers_url','')})")
                if c.get("ats_slug"):
                    st.caption(f"Slug: `{c['ats_slug']}`")
                if c.get("notes"):
                    st.caption(c["notes"])
                if last_error:
                    st.error(f"Last error: {last_error}")
                if state.get("last_checked"):
                    st.caption(f"Last checked: {_fmt_local(state['last_checked'])} · "
                               f"{state.get('last_job_count','?')} jobs returned")
            with col_y:
                if st.button("Pause" if c.get("active", True) else "Resume", key=f"tog_{i}"):
                    companies[i]["active"] = not c.get("active", True)
                    save_companies(companies)
                    st.rerun()
            with col_z:
                if st.button("✏️ Edit", key=f"edit_btn_{i}"):
                    cur = st.session_state.get(f"editing_co_{i}", False)
                    st.session_state[f"editing_co_{i}"] = not cur
                    st.rerun()
            with col_w:
                if st.button("🗑️ Remove", key=f"rm_{i}"):
                    companies.pop(i)
                    save_companies(companies)
                    st.rerun()

            if st.session_state.get(f"editing_co_{i}", False):
                st.markdown("---")
                with st.form(key=f"edit_form_{i}"):
                    e1, e2 = st.columns(2)
                    new_co_name = e1.text_input("Company name", value=c.get("name", ""))
                    new_co_url  = e2.text_input("Career page URL", value=c.get("careers_url", ""))

                    e3, e4, e5 = st.columns(3)
                    new_platform = e3.text_input("Platform", value=c.get("platform") or "")
                    new_slug     = e4.text_input("ATS slug", value=c.get("ats_slug") or "")
                    new_method   = e5.selectbox(
                        "Scraping method",
                        ["playwright", "api"],
                        index=0 if c.get("scraping_method", "playwright") == "playwright" else 1,
                    )

                    e6, e7 = st.columns(2)
                    new_interval = e6.number_input(
                        "Check interval (hours)", min_value=0.25, max_value=24.0,
                        value=float(c.get("check_interval_hours", 2)), step=0.25,
                    )
                    new_notes = e7.text_input("Notes", value=c.get("notes") or "")

                    sv, cancel = st.columns([1, 4])
                    if sv.form_submit_button("💾 Save", type="primary"):
                        if new_co_name:
                            companies[i].update({
                                "name":                 new_co_name,
                                "careers_url":          new_co_url,
                                "platform":             new_platform or None,
                                "ats_slug":             new_slug or None,
                                "scraping_method":      new_method,
                                "check_interval_hours": round(new_interval, 2),
                                "notes":                new_notes,
                            })
                            save_companies(companies)
                            st.session_state[f"editing_co_{i}"] = False
                            st.rerun()
                        else:
                            st.warning("Company name is required.")
                    if cancel.form_submit_button("Cancel"):
                        st.session_state[f"editing_co_{i}"] = False
                        st.rerun()

    if not companies:
        st.info("No companies yet. Run `python tools/seed_companies.py` or add one above.")


# ════════════════════════════════════════════════════════════════════════
# TAB 4: Profile
# ════════════════════════════════════════════════════════════════════════
with tabs[3]:
    PROFILE_DIR.mkdir(exist_ok=True)
    resume_path = PROFILE_DIR / "resume.md"
    prefs_path  = PROFILE_DIR / "preferences.md"

    prof_t1, prof_t2 = st.tabs(["📄 Resume", "🎯 Preferences"])
    with prof_t1:
        st.subheader("Your Resume")
        st.caption("Paste your full resume here. Claude uses this to score job matches.")
        current_resume = resume_path.read_text(encoding="utf-8") if resume_path.exists() else ""
        new_resume = st.text_area("Resume", value=current_resume, height=450, label_visibility="collapsed")
        if st.button("💾 Save Resume", type="primary"):
            resume_path.write_text(new_resume, encoding="utf-8")
            st.success("Resume saved.")

    with prof_t2:
        st.subheader("Job Preferences")
        with st.expander("📖 How to fill this out", expanded=not prefs_path.exists()):
            st.markdown("""
**Format rules:**
- Each section has a `##` heading — keep the headings exactly as they are
- Add one item per line, starting with `- ` (a dash and a space)
- To leave a section blank, just keep the heading and add nothing below it
- Claude reads this verbatim when scoring jobs, so be specific

**Desired Roles** — job titles you're targeting (used to match title and seniority)
```
## Desired Roles
- Senior Product Manager
- Director of Product
- Head of Product
```

**Keywords** — terms that should appear in the description (technologies, industries, themes)
```
## Keywords
- B2B SaaS, enterprise software, analytics, experimentation
```

**Location Preference** — where you're willing to work
```
## Location Preference
- Remote preferred; open to hybrid in Boston or NYC
```

**Must-Haves** — non-negotiable requirements (Claude will penalize jobs missing these)
```
## Must-Haves
- Senior IC or management role
- Product-led company
```

**Deal-Breakers** — automatic disqualifiers
```
## Deal-Breakers
- Requires relocation
- Series A or earlier
- Agency or consulting role
```
""")
        default_prefs = (
            "## Desired Roles\n"
            "- \n\n"
            "## Keywords\n"
            "- \n\n"
            "## Location Preference\n"
            "- Remote preferred\n\n"
            "## Must-Haves\n"
            "- \n\n"
            "## Deal-Breakers\n"
            "- \n"
        )
        current_prefs = prefs_path.read_text(encoding="utf-8") if prefs_path.exists() else default_prefs
        new_prefs = st.text_area("Preferences", value=current_prefs, height=350, label_visibility="collapsed")
        if st.button("💾 Save Preferences", type="primary"):
            prefs_path.write_text(new_prefs, encoding="utf-8")
            st.success("Preferences saved.")


# ════════════════════════════════════════════════════════════════════════
# TAB 5: Settings
# ════════════════════════════════════════════════════════════════════════
with tabs[4]:
    st.subheader("Settings")

    # ── Check Intervals ─────────────────────────────────────────────────
    st.markdown("### Check Intervals")
    st.caption("How often each company's career page is scraped. Lower = more current, higher = lighter resource use.")

    ci_col1, ci_col2, ci_col3 = st.columns(3)

    with ci_col1:
        st.markdown("**Task Scheduler (run.py fires every...)**")
        task_min = st.slider(
            "minutes", 1, 60,
            int(env.get("TASK_INTERVAL_MINUTES", str(DEFAULT_TASK_MIN))),
            key="task_min",
            help="How often Windows runs run.py. Should be ≤ your fastest company interval.",
        )
        if task_min < 5:
            st.warning("⚠️ Under 5 min may cause high background CPU usage.")
        elif task_min <= 10:
            st.info("ℹ️ Frequent — fine for a dedicated machine.")
        else:
            st.success(f"✓ Fires every {task_min} min")

    with ci_col2:
        st.markdown("**API companies** (Greenhouse, Lever, Ashby…)")
        fast_min = st.slider(
            "minutes", 5, 240,
            int(env.get("FAST_CHECK_INTERVAL_MIN", str(DEFAULT_FAST_MIN))),
            step=5,
            key="fast_min",
            help="Free REST API calls — can safely be checked frequently.",
        )
        if fast_min < 10:
            st.warning("⚠️ Under 10 min risks rate-limiting on some ATS APIs.")
        elif fast_min < 20:
            st.info("ℹ️ Aggressive but generally safe.")
        else:
            st.success(f"✓ Every {fast_min} min")

    with ci_col3:
        st.markdown("**Playwright companies** (Workday, iCIMS…)")
        slow_min = st.slider(
            "minutes", 15, 480,
            int(env.get("SLOW_CHECK_INTERVAL_MIN", str(DEFAULT_SLOW_MIN))),
            step=15,
            key="slow_min",
            help="Launches a headless browser — more CPU/memory intensive.",
        )
        if slow_min < 30:
            st.warning("⚠️ Under 30 min is aggressive for Playwright — will increase CPU/RAM usage.")
        elif slow_min < 60:
            st.info("ℹ️ Fairly frequent — monitor memory if running overnight.")
        else:
            st.success(f"✓ Every {slow_min} min")

    update_task_col, _ = st.columns([1, 2])
    with update_task_col:
        if st.button("🔄 Apply & Update Task Scheduler"):
            ok, err = update_task_scheduler(task_min)
            if ok:
                st.success(f"Task Scheduler updated — run.py fires every {task_min} min.")
            else:
                st.error(f"Failed to update scheduler: {err}\nTry running as Administrator.")

    st.divider()

    # ── Email / Digest Times ────────────────────────────────────────────
    st.markdown("### Email & Digest Schedule")
    email_enabled_s = st.toggle(
        "Email alerts enabled",
        value=env.get("EMAIL_ENABLED", "true").lower() != "false",
        help="When off, no emails are sent regardless of score thresholds. Useful when testing or cleaning up.",
    )

    mail_col1, mail_col2 = st.columns(2)

    with mail_col1:
        alert_email = st.text_input("Alert email address", value=env.get("ALERT_EMAIL", ""))
        st.caption("Preset digest times (check any you want)")

        preset_times = ["06:00","07:00","08:00","09:00","10:00","11:00",
                        "12:00","13:00","14:00","15:00","16:00","17:00",
                        "18:00","19:00","20:00","21:00"]
        current_digest_raw = env.get("DIGEST_TIMES", env.get("DIGEST_TIME", "09:00"))
        current_digest_list = [t.strip() for t in current_digest_raw.split(",") if t.strip()]

        selected_times = st.multiselect(
            "Digest times (one email per selected time)",
            options=preset_times,
            default=[t for t in current_digest_list if t in preset_times],
        )
        custom_times = st.text_input(
            "Custom times (comma-separated, e.g. 06:30, 21:30)",
            value=", ".join(t for t in current_digest_list if t not in preset_times),
        )

    with mail_col2:
        anthropic_key  = st.text_input("Anthropic API key", value=env.get("ANTHROPIC_API_KEY", ""), type="password")
        alert_threshold_s = st.slider(
            "Alert threshold — immediate email (score ≥)",
            1, 10, int(env.get("IMMEDIATE_ALERT_THRESHOLD", "9")),
            help="Jobs at or above this score trigger an immediate email.",
        )
        digest_threshold_s = st.slider(
            "Digest threshold — include in scheduled email (score ≥)",
            1, 10, int(env.get("SCORE_THRESHOLD", "6")),
        )

    # ── Email test ──────────────────────────────────────────────────────
    test_col, _ = st.columns([1, 2])
    with test_col:
        if st.button("📧 Send Test Email"):
            from tools.send_email import send_test_email
            ok, msg = send_test_email()
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    st.divider()

    # ── LLM Backend ─────────────────────────────────────────────────────
    st.markdown("### LLM Backend (Job Scoring)")
    _current_ollama_url = env.get("OLLAMA_BASE_URL", "")
    _current_ollama_model = env.get("OLLAMA_MODEL", "qwen2.5:7b")

    if _current_ollama_url:
        st.info(f"**Active backend: Ollama** — `{_current_ollama_url}` using model `{_current_ollama_model}`")
    else:
        st.info("**Active backend: Anthropic Claude** (OLLAMA_BASE_URL not set)")

    llm_col1, llm_col2 = st.columns(2)
    with llm_col1:
        ollama_url_s = st.text_input(
            "Ollama base URL",
            value=_current_ollama_url,
            placeholder="http://localhost:11434",
            help="Set this to use a local Ollama instance instead of the Anthropic API. Leave blank to keep using Claude.",
        )
        ollama_model_s = st.text_input(
            "Ollama model name",
            value=_current_ollama_model,
            placeholder="qwen2.5:7b",
            help="The model to use. Must be pulled on the Ollama server first (e.g. ollama pull qwen2.5:7b).",
        )
    with llm_col2:
        st.markdown("**Recommended models**")
        st.markdown("- `qwen2.5:7b` — best JSON accuracy, ~5 GB RAM")
        st.markdown("- `llama3.2:3b` — faster, ~2 GB RAM, slightly lower accuracy")
        st.markdown("- `mistral:7b` — good alternative to qwen2.5")
        st.caption("Pull a model: `docker exec -it <ollama-container> ollama pull qwen2.5:7b`")

    st.divider()

    # ── Active Hours ────────────────────────────────────────────────────
    st.markdown("### Active Hours")
    ah_col1, ah_col2 = st.columns(2)
    active_start = ah_col1.slider("Start hour (24h)", 0, 23, int(env.get("ACTIVE_HOURS_START", "7")))
    active_end   = ah_col2.slider("End hour (24h)",   0, 23, int(env.get("ACTIVE_HOURS_END",   "20")))
    st.caption(f"Automation only runs between {active_start}:00 and {active_end}:00")

    st.divider()

    # ── Alert Style ─────────────────────────────────────────────────────
    st.markdown("### In-App Alert Style")
    alert_sound_s  = st.radio(
        "Alert sound",
        ["Ascending Chime", "Gentle Bell", "Two-Tone Ping", "Soft Pulse", "Off"],
        horizontal=True,
        index=["Ascending Chime","Gentle Bell","Two-Tone Ping","Soft Pulse","Off"].index(
            env.get("ALERT_SOUND", "Ascending Chime")
        ),
        help=(
            "Ascending Chime = C-E-G major arpeggio, celebratory | "
            "Gentle Bell = single soft fade | "
            "Two-Tone Ping = two ascending notes | "
            "Soft Pulse = quick low beep"
        ),
    )
    if st.button("🔊 Preview Sound"):
        inject_alert(
            [{"title": "Test Alert", "company": "Preview", "score": 9, "content_hash": "preview"}],
            alert_sound_s, "Banner Only",
        )
        st.caption("If you heard nothing, click the button again — browsers require a user gesture first.")

    alert_visual_s = st.radio(
        "Alert visual style",
        ["Both", "Browser Notification", "Banner Only"],
        horizontal=True,
        index=["Both","Browser Notification","Banner Only"].index(
            env.get("ALERT_VISUAL", "Both")
        ),
        help=(
            "Browser Notification = OS-level popup, visible even when app is minimized | "
            "Banner = colored block at top of Dashboard | "
            "Both = recommended"
        ),
    )
    poll_secs_s = st.slider(
        "Live monitoring refresh (seconds)",
        15, 300, int(env.get("ALERT_POLL_SECONDS", "60")),
        step=15,
        help="How often the dashboard checks for new hot jobs when Live Monitoring is ON.",
    )

    st.divider()

    # ── Save all settings ────────────────────────────────────────────────
    if st.button("💾 Save All Settings", type="primary"):
        all_times = selected_times + [t.strip() for t in custom_times.split(",") if t.strip()]
        digest_times_str = ",".join(sorted(set(all_times))) or "09:00"

        # Update check_interval_hours in companies.json for companies still on defaults
        companies = load_companies()
        old_fast_min = int(env.get("FAST_CHECK_INTERVAL_MIN", str(DEFAULT_FAST_MIN)))
        old_slow_min = int(env.get("SLOW_CHECK_INTERVAL_MIN", str(DEFAULT_SLOW_MIN)))
        changed = 0
        for c in companies:
            cur_h = c.get("check_interval_hours", 0)
            is_api = c.get("scraping_method") == "api"
            old_default_h = round(old_fast_min / 60, 2) if is_api else round(old_slow_min / 60, 2)
            new_default_h = round(fast_min / 60, 2) if is_api else round(slow_min / 60, 2)
            if abs(cur_h - old_default_h) < 0.05:
                c["check_interval_hours"] = new_default_h
                changed += 1
        if changed:
            save_companies(companies)

        save_env({
            "TASK_INTERVAL_MINUTES":     str(task_min),
            "FAST_CHECK_INTERVAL_MIN":   str(fast_min),
            "SLOW_CHECK_INTERVAL_MIN":   str(slow_min),
            "DIGEST_TIMES":              digest_times_str,
            "ALERT_EMAIL":               alert_email,
            "ANTHROPIC_API_KEY":         anthropic_key,
            "OLLAMA_BASE_URL":           ollama_url_s.strip(),
            "OLLAMA_MODEL":              ollama_model_s.strip() or "qwen2.5:7b",
            "IMMEDIATE_ALERT_THRESHOLD": str(alert_threshold_s),
            "SCORE_THRESHOLD":           str(digest_threshold_s),
            "ACTIVE_HOURS_START":        str(active_start),
            "ACTIVE_HOURS_END":          str(active_end),
            "ALERT_SOUND":               alert_sound_s,
            "ALERT_VISUAL":              alert_visual_s,
            "ALERT_POLL_SECONDS":        str(poll_secs_s),
            "EMAIL_ENABLED":             "true" if email_enabled_s else "false",
        })
        st.success(
            f"Settings saved. Updated check intervals for {changed} companies. "
            f"Digest times: {digest_times_str}"
        )

    st.divider()
    st.markdown("### System Controls")

    sys_col1, sys_col2 = st.columns(2)

    if sys.platform == "win32":
        # Windows: manage via Task Scheduler
        task_result = subprocess.run(
            ["schtasks", "/query", "/tn", "JobBoardWatcher", "/fo", "LIST"],
            capture_output=True, text=True,
        )
        task_exists   = task_result.returncode == 0
        task_disabled = task_exists and "Disabled" in task_result.stdout

        with sys_col1:
            st.markdown("**Automation (Task Scheduler)**")
            if task_exists:
                if task_disabled:
                    st.warning("⏸ Automation is paused")
                    if st.button("▶ Resume Automation"):
                        subprocess.run(["schtasks", "/change", "/tn", "JobBoardWatcher", "/enable"],
                                       capture_output=True)
                        st.success("Automation resumed.")
                        st.rerun()
                else:
                    st.success("✅ Automation is running")
                    if st.button("⏸ Pause Automation"):
                        subprocess.run(["schtasks", "/change", "/tn", "JobBoardWatcher", "/disable"],
                                       capture_output=True)
                        st.success("Automation paused — no scraping until resumed.")
                        st.rerun()
            else:
                st.info("Task not registered")
                st.caption("Click 'Apply & Update Task Scheduler' above to register.")

        with sys_col2:
            st.markdown("**Task Scheduler Status**")
            if task_exists:
                with st.expander("View task details"):
                    st.code(task_result.stdout)
            else:
                st.caption("No task found.")
    else:
        # Docker / Linux: scraper loop is managed by entrypoint.sh
        with sys_col1:
            st.markdown("**Automation (Docker)**")
            st.success("✅ Scraper loop running via Docker entrypoint")
            st.caption(
                f"Runs every {env.get('TASK_INTERVAL_MINUTES', '5')} minutes. "
                "To pause, stop the container from Unraid."
            )

    st.divider()
    st.markdown("**Dashboard**")
    st.caption("Stops the Streamlit web server. In Docker, the container will restart it automatically.")
    if st.button("🛑 Stop Dashboard"):
        st.warning("Shutting down...")
        import threading, time as _time
        def _shutdown():
            _time.sleep(1)
            os._exit(0)
        threading.Thread(target=_shutdown, daemon=True).start()
