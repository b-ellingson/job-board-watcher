"""
Score new job postings against the user's resume and preferences.
Backend selection (checked at call time):
  - OLLAMA_BASE_URL is set → use Ollama (local, free)
  - Otherwise             → use Anthropic Claude (API, paid)
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).parent.parent
PROFILE_DIR = ROOT / "profile"

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore

try:
    import requests as _requests
except ImportError:
    _requests = None  # type: ignore

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 256  # score + reason + keywords only — keep it short


def _load_profile() -> str:
    resume = ""
    prefs = ""
    resume_path = PROFILE_DIR / "resume.md"
    prefs_path  = PROFILE_DIR / "preferences.md"
    if resume_path.exists():
        resume = resume_path.read_text(encoding="utf-8").strip()
    if prefs_path.exists():
        prefs = prefs_path.read_text(encoding="utf-8").strip()
    return f"## RESUME\n{resume}\n\n## JOB PREFERENCES\n{prefs}"


def _build_system_prompt(profile_text: str) -> str:
    return f"""You are a job-fit evaluator. You will be given a job posting and must rate how well it matches the candidate's profile below.

{profile_text}

For each job, respond with ONLY a JSON object (no markdown, no extra text):
{{
  "score": <integer 1-10>,
  "score_reason": "<one concise sentence explaining the match or mismatch>",
  "matched_keywords": ["<keyword1>", "<keyword2>"]
}}

Scoring guide:
1-3: Poor fit (wrong industry, wrong level, deal-breaker present)
4-5: Weak fit (some overlap but missing key requirements)
6-7: Good fit (aligns with background and preferences)
8-9: Strong fit (excellent match on role, level, and criteria)
10: Perfect fit (everything aligns)"""


def _score_with_ollama(system_prompt: str, user_content: str) -> dict:
    """Call Ollama's native /api/chat endpoint. Returns parsed score dict."""
    if _requests is None:
        raise RuntimeError("requests not installed")
    base  = os.getenv("OLLAMA_BASE_URL", "").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
    resp  = _requests.post(
        f"{base}/api/chat",
        json={
            "model":    model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
            "stream":  False,
            "options": {"temperature": 0.1, "num_predict": 512},
        },
        timeout=120,
    )
    if resp.status_code == 404:
        raise RuntimeError(
            f"Ollama model '{model}' not found — "
            f"pull it first: docker exec -it <ollama-container> ollama pull {model}"
        )
    resp.raise_for_status()
    raw = resp.json()["message"]["content"].strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


def _score_with_anthropic(client, system_prompt: str, user_content: str) -> dict:
    """Call Anthropic Claude with prompt caching. Returns parsed score dict."""
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
    )
    raw = response.content[0].text.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


def score_jobs(jobs: list[dict], dry_run: bool = False) -> list[dict]:
    """
    Score a list of job dicts. Returns the same dicts with score/score_reason/matched_keywords added.
    Already-scored jobs (score is not None) are passed through unchanged.
    Uses Ollama if OLLAMA_BASE_URL is set, otherwise falls back to Anthropic.
    """
    if not jobs:
        return []

    to_score = [j for j in jobs if j.get("score") is None]
    pre_scored = [j for j in jobs if j.get("score") is not None]

    if not to_score:
        return jobs

    if dry_run:
        for j in to_score:
            j["score"] = 5
            j["score_reason"] = "[dry-run] Scoring skipped"
            j["matched_keywords"] = []
        return pre_scored + to_score

    profile_text = _load_profile()
    if not profile_text.strip():
        print("  [score] No profile found in profile/ — skipping scoring")
        return jobs

    system_prompt = _build_system_prompt(profile_text)

    use_ollama = bool(os.getenv("OLLAMA_BASE_URL", "").strip())
    anthropic_client = None

    if use_ollama:
        model_label = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
        print(f"  Scoring {len(to_score)} jobs with Ollama ({model_label}) ...")
    else:
        if anthropic is None:
            print("  [score] anthropic not installed — skipping scoring")
            return jobs
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            print("  [score] ANTHROPIC_API_KEY not set — skipping scoring")
            return jobs
        anthropic_client = anthropic.Anthropic(api_key=api_key)
        print(f"  Scoring {len(to_score)} jobs with Claude ({ANTHROPIC_MODEL}) ...")

    for i, job in enumerate(to_score):
        user_content = (
            f"Company: {job.get('company','')}\n"
            f"Title: {job.get('title','')}\n"
            f"Department: {job.get('department','')}\n"
            f"Location: {job.get('location','')}\n\n"
            f"Description:\n{(job.get('description','') or '')[:2000]}"
        )
        try:
            if use_ollama:
                result = _score_with_ollama(system_prompt, user_content)
            else:
                result = _score_with_anthropic(anthropic_client, system_prompt, user_content)
            job["score"] = int(result.get("score", 0))
            job["score_reason"] = str(result.get("score_reason", ""))
            job["matched_keywords"] = result.get("matched_keywords", [])
        except Exception as e:
            print(f"    [score] Error on '{job.get('title')}': {e}")
            job["score"] = 0
            job["score_reason"] = f"Scoring error: {e}"
            job["matched_keywords"] = []

        if (i + 1) % 10 == 0:
            print(f"    Scored {i+1}/{len(to_score)} ...")

    return pre_scored + to_score


if __name__ == "__main__":
    # Quick test with a fake job
    fake = [{
        "company": "Acme Corp",
        "title": "Senior Product Manager",
        "department": "Product",
        "location": "Remote, US",
        "url": "https://example.com/jobs/1",
        "description": "We are looking for a Senior PM to lead our B2B SaaS platform growth.",
        "content_hash": "abc123",
        "score": None,
    }]
    results = score_jobs(fake, dry_run=True)
    print(json.dumps(results, indent=2))
