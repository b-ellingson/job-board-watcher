"""
Background job runner — non-blocking subprocess manager for the Streamlit UI.

State is persisted in .tmp/ so the dashboard can show status across reruns:
  runner_status.json  — current / most recent job
  runner.log          — stdout+stderr of the running job
  runner_history.json — completed run history (last 50)
"""
import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT     = Path(__file__).parent.parent
_STATUS  = ROOT / ".tmp" / "runner_status.json"
_LOG     = ROOT / ".tmp" / "runner.log"
_HISTORY = ROOT / ".tmp" / "runner_history.json"


def start(label: str, cmd: list[str]) -> None:
    """Start cmd as a detached background process. Returns immediately."""
    _STATUS.parent.mkdir(exist_ok=True)
    log_f = open(_LOG, "w", encoding="utf-8", buffering=1)

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        cmd,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
        **kwargs,
    )
    _STATUS.write_text(json.dumps({
        "label":      label,
        "pid":        proc.pid,
        "started_at": datetime.now().isoformat(),
        "state":      "running",
    }), encoding="utf-8")


def get_status() -> dict | None:
    """Return status dict, updating state to 'done' if the process has exited."""
    if not _STATUS.exists():
        return None
    try:
        s = json.loads(_STATUS.read_text(encoding="utf-8"))
    except Exception:
        return None

    if s.get("state") == "running" and not _pid_alive(s.get("pid")):
        s["state"] = "done"
        s["finished_at"] = datetime.now().isoformat()
        _STATUS.write_text(json.dumps(s), encoding="utf-8")
        _record_history(s)

    return s


def stop() -> bool:
    """Send SIGTERM / taskkill to the running process. Returns True if killed."""
    s = get_status()
    if not s or s.get("state") != "running":
        return False
    pid = s.get("pid")
    try:
        if sys.platform == "win32":
            subprocess.call(["taskkill", "/F", "/T", "/PID", str(pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.kill(int(pid), signal.SIGTERM)
        s["state"] = "stopped"
        s["finished_at"] = datetime.now().isoformat()
        _STATUS.write_text(json.dumps(s), encoding="utf-8")
        _record_history(s)
        return True
    except Exception:
        return False


def is_running() -> bool:
    s = get_status()
    return bool(s and s.get("state") == "running")


def get_log_tail(n: int = 40) -> str:
    if not _LOG.exists():
        return ""
    try:
        lines = _LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def get_history(limit: int = 50) -> list[dict]:
    if not _HISTORY.exists():
        return []
    try:
        return json.loads(_HISTORY.read_text(encoding="utf-8"))[-limit:]
    except Exception:
        return []


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True,
            )
            return str(pid) in r.stdout
        else:
            os.kill(int(pid), 0)
            return True
    except (ProcessLookupError, PermissionError, TypeError, ValueError):
        return False


def _record_history(s: dict) -> None:
    history = get_history()
    entry = dict(s)
    try:
        start_dt = datetime.fromisoformat(entry["started_at"])
        end_dt   = datetime.fromisoformat(entry.get("finished_at", datetime.now().isoformat()))
        entry["duration_s"] = int((end_dt - start_dt).total_seconds())
    except Exception:
        entry["duration_s"] = None
    history.append(entry)
    _HISTORY.write_text(json.dumps(history[-50:], indent=2), encoding="utf-8")
