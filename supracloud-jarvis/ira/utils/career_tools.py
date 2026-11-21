"""
IRA Career Automation Tools — Phase 4.

  analyze_my_codebase()             → GitHub: repos, READMEs, language stats
  scrape_job_posting(url)           → Apify: job description from LinkedIn/Indeed
  generate_tailored_resume(jd_text) → LLM: rewrite base_resume.md bullet points
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger("ira.career_tools")


def _github_token() -> str:
    from config import get_settings
    return get_settings().github_token or os.getenv("GITHUB_TOKEN", "")


def _apify_token() -> str:
    from config import get_settings
    return get_settings().apify_api_token or os.getenv("APIFY_API_TOKEN", "")

BASE_RESUME_PATH = Path(__file__).parent.parent / "base_resume.md"
# Fix #37: tailored resumes are now written to unique per-session files rather
# than a single shared path, so concurrent calls never overwrite each other.
_RESUME_DIR = Path(__file__).parent.parent / "tailored_resumes"


# ── GitHub Analysis ───────────────────────────────────────────────────────────

#: P16-R3. This used `PyGithub`, which is **LGPL**, for four plain REST calls.
#: The licence position for a commercial hosted product is a question this
#: project has no business asking on the owner's behalf, and the cheapest
#: honest answer to an LGPL dependency you can do without is to not have it —
#: so the four calls are made directly with `httpx`, which is already a
#: dependency and is BSD-3-Clause. No feature is lost and no legal conclusion
#: is asserted.
GITHUB_API = "https://api.github.com"
_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


async def analyze_my_codebase() -> dict:
    """
    Authenticate with GitHub, fetch the 3 most recently-pushed repos,
    read READMEs and language stats, and return a structured tech-stack summary.
    """
    GITHUB_TOKEN = _github_token()
    if not GITHUB_TOKEN:
        return {"error": "GITHUB_TOKEN not set in .env"}

    import httpx

    headers = {**_GITHUB_HEADERS, "Authorization": f"Bearer {GITHUB_TOKEN}"}
    try:
        async with httpx.AsyncClient(base_url=GITHUB_API, headers=headers,
                                     timeout=30.0) as client:
            me = await client.get("/user")
            me.raise_for_status()
            login = me.json()["login"]

            listing = await client.get(
                "/user/repos",
                params={"type": "owner", "sort": "pushed", "direction": "desc",
                        "per_page": 100})
            listing.raise_for_status()
            repos = [r for r in listing.json() if not r.get("fork")][:3]

            summaries = []
            for repo in repos:
                full_name = repo["full_name"]

                languages = await client.get(f"/repos/{full_name}/languages")
                languages.raise_for_status()

                # 404 means the repository genuinely has no README. Any other
                # status is a failure and must not be reported as "no README" —
                # an error rendered as an ordinary empty result is Phase 13's
                # whole subject.
                readme_resp = await client.get(
                    f"/repos/{full_name}/readme",
                    headers={"Accept": "application/vnd.github.raw"})
                if readme_resp.status_code == 404:
                    readme = "(no README)"
                else:
                    readme_resp.raise_for_status()
                    readme = readme_resp.text[:600]

                summaries.append({
                    "name": repo["name"],
                    "description": repo.get("description") or "",
                    "languages": languages.json(),
                    "stars": repo.get("stargazers_count", 0),
                    "last_pushed": repo.get("pushed_at") or "",
                    "readme_excerpt": readme,
                })

        # Aggregate language bytes across all repos
        all_langs: dict[str, int] = {}
        for r in summaries:
            for lang, byte_count in r["languages"].items():
                all_langs[lang] = all_langs.get(lang, 0) + byte_count
        top_langs = sorted(all_langs.items(), key=lambda x: x[1], reverse=True)[:6]

        result = {
            "github_user": login,
            "top_languages": [{"language": l, "bytes": b} for l, b in top_langs],
            "repositories": summaries,
        }
        logger.info(f"GitHub analysis done: {login}, {len(summaries)} repos")
        return result
    except Exception as e:
        logger.error(f"GitHub analysis failed: {e}")
        return {"error": str(e)}


# ── Job Board Scraping ────────────────────────────────────────────────────────

async def scrape_job_posting(url: str) -> dict:
    """
    Scrape a LinkedIn or Indeed job posting via Apify and return structured
    job details: title, company, location, description, requirements.

    Fix #38: logs a warning and returns a clear error when Apify returns no
    results instead of silently returning an empty dict.
    Fix #47: Apify actor IDs are read from config (env vars) so they can be
    updated without touching code.
    """
    APIFY_API_TOKEN = _apify_token()
    if not APIFY_API_TOKEN:
        return {"error": "APIFY_API_TOKEN not set in .env — get a free key at apify.com"}

    try:
        from apify_client import ApifyClient
    except ImportError:
        return {"error": "apify-client not installed — run: pip install apify-client"}

    from config import get_settings
    cfg = get_settings()

    def _run() -> dict:
        client = ApifyClient(APIFY_API_TOKEN)

        # Fix #47: read actor IDs from config so they survive Apify deprecations
        if "linkedin.com" in url:
            actor_id = cfg.apify_linkedin_actor
            run_input = {"startUrls": [{"url": url}], "maxItems": 1}
        elif "indeed.com" in url:
            actor_id = cfg.apify_indeed_actor
            run_input = {"startUrls": [{"url": url}], "maxResults": 1}
        else:
            actor_id = cfg.apify_fallback_actor
            run_input = {"startUrls": [{"url": url}], "maxPagesPerCrawl": 1}

        run = client.actor(actor_id).call(run_input=run_input)
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

        # Fix #38: surface empty results as an explicit error instead of
        # silently returning {} and letting callers get "Unknown" everywhere.
        if not items:
            raise ValueError(
                f"Apify actor '{actor_id}' returned no results for URL: {url}. "
                "The page may require login, be geo-blocked, or the URL format may "
                "have changed. Check APIFY_LINKEDIN_ACTOR / APIFY_INDEED_ACTOR in .env."
            )
        return items[0]

    try:
        raw = await asyncio.get_running_loop().run_in_executor(None, _run)
        job = {
            "url": url,
            "title": raw.get("title") or raw.get("positionName") or raw.get("jobTitle", "Unknown"),
            "company": raw.get("company") or raw.get("companyName", "Unknown"),
            "location": raw.get("location") or raw.get("jobLocation", ""),
            "description": (raw.get("description") or raw.get("jobDescription", ""))[:3000],
            "requirements": raw.get("requirements") or "",
        }
        logger.info(f"Job scraped: {job['title']} at {job['company']}")
        return job
    except Exception as e:
        logger.error(f"Apify scrape failed: {e}")
        return {"error": str(e)}


# ── Resume Tailoring ──────────────────────────────────────────────────────────

async def generate_tailored_resume(
    job_description: str,
    session_id: str | None = None,
) -> dict:
    """
    Load base_resume.md, cross-reference with job_description via deep LLM,
    rewrite bullet points to highlight matching skills, and save the result.

    Fix #37: output is written to a unique per-session file inside the
    tailored_resumes/ directory (e.g. tailored_resume_<session_id>.md) so
    concurrent resume generations never overwrite each other.
    """
    if not BASE_RESUME_PATH.exists():
        return {
            "error": f"base_resume.md not found. Create it at: {BASE_RESUME_PATH}",
            "hint": "Add your resume in Markdown format to that path, then retry.",
        }

    # Fix #37: unique per-session output path
    _RESUME_DIR.mkdir(parents=True, exist_ok=True)
    if session_id:
        # Sanitise session_id to prevent path traversal
        safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64]
    else:
        import uuid
        safe_id = str(uuid.uuid4())
    output_path = _RESUME_DIR / f"tailored_resume_{safe_id}.md"

    base = BASE_RESUME_PATH.read_text(encoding="utf-8")

    prompt = (
        "You are an expert resume writer.\n\n"
        "TASK: Rewrite the resume below so it perfectly targets the job description.\n"
        "Rules: Do NOT change any facts. Only rephrase and reorder bullet points to "
        "mirror the job's keywords. Use strong action verbs. Output ONLY the rewritten "
        "Markdown resume — no commentary, no explanation.\n\n"
        f"TARGET JOB:\n{job_description[:2000]}\n\n"
        f"BASE RESUME:\n{base[:4000]}"
    )

    try:
        from utils.llm import chat_complete
        tailored = await chat_complete(
            [{"role": "user", "content": prompt}],
            use_deep=True,
            temperature=0.3,
            max_tokens=4096,
        )
        output_path.write_text(tailored, encoding="utf-8")
        logger.info(f"Tailored resume saved to {output_path}")
        return {
            "status": "done",
            "saved_to": str(output_path),
            "preview": tailored[:400] + "…" if len(tailored) > 400 else tailored,
        }
    except Exception as e:
        logger.error(f"Resume generation failed: {e}")
        return {"error": str(e)}
