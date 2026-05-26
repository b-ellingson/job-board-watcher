"""
Hybrid scraper: routes each company to the right strategy based on platform.
  - greenhouse / lever / ashby / workable / smartrecruiters → free REST API
  - everything else → Playwright headless Chromium
Returns a normalized list of job dicts per company.
"""
import hashlib
import json
import re
import time
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobBoardWatcher/1.0)"
}
REQUEST_TIMEOUT = 15

# Matches markdown-style chars and common nav text that bleed into scraped titles
_TITLE_JUNK = re.compile(
    r'(\*+|\[|\]|\bRead More\b|\bRead Less\b|\bLoad More\b|\bSee More\b)',
    re.IGNORECASE,
)


def _clean_title(title: str) -> str:
    if not title:
        return title
    title = _TITLE_JUNK.sub("", title)
    title = re.sub(r'\s+', ' ', title).strip()
    return title


# ── Normalization helpers ───────────────────────────────────────────────────

def _hash(company: str, title: str, url: str) -> str:
    key = f"{company.lower().strip()}|{title.lower().strip()}|{(url or '').strip()}"
    return hashlib.sha256(key.encode()).hexdigest()


def _norm(company: str, raw: dict, title: str, dept: str, loc: str, url: str, desc: str) -> dict:
    clean = _clean_title((title or "").strip())
    return {
        "company":      company,
        "title":        clean,
        "department":   (dept or "").strip(),
        "location":     (loc or "").strip(),
        "url":          (url or "").strip(),
        "description":  (desc or "")[:4000],
        "content_hash": _hash(company, clean, url),
    }


# ── ATS REST API scrapers ───────────────────────────────────────────────────

def _get(url: str) -> Any:
    if requests is None:
        raise RuntimeError("requests not installed")
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def scrape_greenhouse(company: dict) -> list[dict]:
    slug = company.get("ats_slug")
    if not slug:
        return []
    try:
        data = _get(f"https://api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
        jobs = []
        for j in data.get("jobs", []):
            dept = j.get("departments", [{}])[0].get("name", "") if j.get("departments") else ""
            loc  = j.get("location", {}).get("name", "")
            desc = re.sub(r"<[^>]+>", " ", j.get("content", ""))
            jobs.append(_norm(company["name"], j, j.get("title",""), dept, loc, j.get("absolute_url",""), desc))
        return jobs
    except Exception as e:
        print(f"  [greenhouse] {company['name']}: {e}")
        return []


def scrape_lever(company: dict) -> list[dict]:
    slug = company.get("ats_slug")
    if not slug:
        return []
    try:
        data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        jobs = []
        for j in data:
            dept = j.get("categories", {}).get("team", "")
            loc  = j.get("categories", {}).get("location", "") or j.get("workplaceType", "")
            desc = re.sub(r"<[^>]+>", " ", j.get("descriptionPlain", "") or j.get("description", ""))
            jobs.append(_norm(company["name"], j, j.get("text",""), dept, loc, j.get("hostedUrl",""), desc))
        return jobs
    except Exception as e:
        print(f"  [lever] {company['name']}: {e}")
        return []


def scrape_ashby(company: dict) -> list[dict]:
    slug = company.get("ats_slug")
    if not slug:
        return []
    try:
        data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        jobs = []
        for j in (data.get("jobPostings") or data.get("jobs") or []):
            dept = j.get("departmentName", "")
            loc  = j.get("locationName", "") or j.get("isRemote") and "Remote" or ""
            desc = re.sub(r"<[^>]+>", " ", j.get("descriptionHtml", "") or j.get("description", ""))
            url  = j.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{slug}/{j.get('id','')}"
            jobs.append(_norm(company["name"], j, j.get("title",""), dept, loc, url, desc))
        return jobs
    except Exception as e:
        print(f"  [ashby] {company['name']}: {e}")
        return []


def scrape_workable(company: dict) -> list[dict]:
    slug = company.get("ats_slug")
    if not slug:
        return []
    try:
        data = _get(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs?details=true")
        jobs = []
        for j in data.get("results", []):
            dept = j.get("department", "")
            loc  = j.get("location", {}).get("city", "") if isinstance(j.get("location"), dict) else str(j.get("location",""))
            desc = re.sub(r"<[^>]+>", " ", j.get("description", ""))
            url  = f"https://apply.workable.com/{slug}/j/{j.get('shortcode','')}"
            jobs.append(_norm(company["name"], j, j.get("title",""), dept, loc, url, desc))
        return jobs
    except Exception as e:
        print(f"  [workable] {company['name']}: {e}")
        return []


def scrape_smartrecruiters(company: dict) -> list[dict]:
    slug = company.get("ats_slug")
    if not slug:
        return []
    try:
        data = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
        jobs = []
        for j in data.get("content", []):
            dept = j.get("department", {}).get("label", "") if isinstance(j.get("department"), dict) else ""
            loc  = j.get("location", {}).get("city", "") if isinstance(j.get("location"), dict) else ""
            url  = f"https://jobs.smartrecruiters.com/{slug}/{j.get('id','')}"
            jobs.append(_norm(company["name"], j, j.get("name",""), dept, loc, url, ""))
        return jobs
    except Exception as e:
        print(f"  [smartrecruiters] {company['name']}: {e}")
        return []


# ── Playwright scraper (fallback for JS-heavy sites) ───────────────────────

def scrape_playwright(company: dict) -> list[dict]:
    url = company.get("careers_url", "")
    if not url:
        return []
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print(f"  [playwright] Not installed. Run: playwright install chromium")
        return []

    jobs = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers({"User-Agent": HEADERS["User-Agent"]})

            captured_jobs: list[dict] = []

            # Intercept API responses that look like job listing payloads
            def handle_response(response):
                try:
                    if response.status != 200:
                        return
                    ct = response.headers.get("content-type", "")
                    if "json" not in ct:
                        return
                    body = response.json()
                    _extract_jobs_from_json(body, url, company["name"], captured_jobs)
                except Exception:
                    pass

            page.on("response", handle_response)
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            time.sleep(3)

            if not captured_jobs:
                # Fallback: parse visible page text for job titles
                captured_jobs = _parse_page_html(page, company["name"], url)

            browser.close()
            jobs = captured_jobs
    except Exception as e:
        print(f"  [playwright] {company['name']}: {e}")

    return jobs


def _extract_jobs_from_json(body: Any, page_url: str, company: str, out: list):
    """Try to extract job listings from intercepted JSON API responses."""
    candidates = []

    # Workday pattern
    if isinstance(body, dict):
        for key in ("jobPostings", "jobs", "positions", "postings", "results", "data"):
            val = body.get(key)
            if isinstance(val, list) and val:
                candidates = val
                break

    if not candidates and isinstance(body, list):
        candidates = body

    for item in candidates[:200]:
        if not isinstance(item, dict):
            continue
        title = (
            item.get("title") or item.get("jobTitle") or item.get("name") or
            item.get("job_title") or item.get("Title") or ""
        )
        if not title or len(str(title)) < 3:
            continue
        dept = (
            item.get("department") or item.get("team") or item.get("function") or
            item.get("category") or item.get("jobFamily") or ""
        )
        if isinstance(dept, dict):
            dept = dept.get("name") or dept.get("label") or ""
        loc = (
            item.get("location") or item.get("city") or item.get("locationName") or
            item.get("primaryLocation") or ""
        )
        if isinstance(loc, dict):
            loc = loc.get("city") or loc.get("name") or loc.get("label") or ""
        link = (
            item.get("url") or item.get("link") or item.get("externalPath") or
            item.get("jobUrl") or item.get("applyUrl") or item.get("jobHref") or
            item.get("detailUrl") or item.get("postingPath") or item.get("path") or
            item.get("href") or ""
        )
        if link and not link.startswith("http"):
            from urllib.parse import urljoin
            # Ensure base URL has trailing slash so relative paths don't drop the ATS subdir
            base = page_url if page_url.endswith("/") else page_url + "/"
            link = urljoin(base, link)
        desc = item.get("description") or item.get("summary") or item.get("shortDescription") or ""
        if isinstance(desc, dict):
            desc = desc.get("text") or desc.get("value") or ""
        desc = re.sub(r"<[^>]+>", " ", str(desc))

        out.append(_norm(company, item, str(title), str(dept), str(loc), str(link), desc))


_NAV_TERMS = {
    "privacy policy", "security", "terms", "cookie", "powered by",
    "accessibility", "sitemap", "contact us", "about us", "home",
    "login", "sign in", "sign up", "careers", "talent community",
    "vulnerability disclosure", "legal", "copyright",
}


def _parse_page_html(page: Any, company: str, url: str) -> list[dict]:
    """Last-resort: scan rendered HTML for anchor elements that look like job postings."""
    try:
        links = page.query_selector_all("a[href]")
        seen: set[str] = set()
        jobs = []
        for link in links:
            text = (link.inner_text() or "").strip()
            href = link.get_attribute("href") or ""
            if not text or len(text) < 8 or len(text) > 150:
                continue
            if text.lower() in _NAV_TERMS or any(t in text.lower() for t in _NAV_TERMS):
                continue
            if href in seen:
                continue
            seen.add(href)
            if not href.startswith("http"):
                from urllib.parse import urljoin
                href = urljoin(url, href)
            jobs.append(_norm(company, {}, text, "", "", href, ""))
        return jobs[:150]
    except Exception:
        return []


# ── Main router ─────────────────────────────────────────────────────────────

API_SCRAPERS = {
    "greenhouse":     scrape_greenhouse,
    "lever":          scrape_lever,
    "ashby":          scrape_ashby,
    "workable":       scrape_workable,
    "smartrecruiters": scrape_smartrecruiters,
}


def scrape_company(company: dict) -> list[dict]:
    method = company.get("scraping_method", "playwright")
    platform = company.get("platform", "")

    if method == "api" and platform in API_SCRAPERS:
        jobs = API_SCRAPERS[platform](company)
        # If API returned nothing (slug wrong/changed), fall back to Playwright
        if not jobs and company.get("careers_url"):
            print(f"  [api-fallback] {company['name']}: API empty, trying Playwright")
            jobs = scrape_playwright(company)
        return jobs

    return scrape_playwright(company)


if __name__ == "__main__":
    # Quick smoke test
    test = {
        "name": "HackerOne",
        "platform": "ashby",
        "ats_slug": "hackerone",
        "careers_url": "https://jobs.ashbyhq.com/hackerone",
        "scraping_method": "api",
    }
    jobs = scrape_company(test)
    print(f"Found {len(jobs)} jobs for {test['name']}")
    for j in jobs[:3]:
        print(f"  {j['title']} | {j['department']} | {j['location']}")
