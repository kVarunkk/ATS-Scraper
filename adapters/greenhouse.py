import requests
from tenacity import retry, stop_after_attempt, wait_exponential

JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _get(url: str, params: dict | None = None) -> dict | None:
    resp = requests.get(url, params=params, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def fetch_jobs(slug: str, company_url: str) -> list[dict]:
    """Returns normalized job dicts ready for db.upsert_job. Empty list if board doesn't exist / has no jobs."""
    data = _get(JOBS_URL.format(slug=slug), params={"content": "true"})
    if not data:
        return []

    board_meta = _get(BOARD_URL.format(slug=slug))
    company_name = (board_meta or {}).get("name") or slug

    jobs = []
    for posting in data.get("jobs", []):
        location_name = (posting.get("location") or {}).get("name")

        jobs.append({
            "job_url": posting.get("absolute_url"),
            "locations": [location_name] if location_name else [],
            # Greenhouse doesn't expose a consistent employment-type field across boards;
            # left at the column default (Fulltime) unless you add per-board metadata parsing.
            "job_type": "Fulltime",
            "salary_range": None,
            "salary_min": None,
            "salary_max": None,
            "job_name": posting.get("title"),
            "description": posting.get("content"),  # HTML
            "company_url": company_url,
            "company_name": company_name,
            "platform": "greenhouse",
        })
    return jobs
