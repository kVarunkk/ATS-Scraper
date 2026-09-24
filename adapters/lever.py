import requests
from tenacity import retry, stop_after_attempt, wait_exponential

API_URL = "https://api.lever.co/v0/postings/{slug}"

JOB_TYPE_MAP = {
    "full-time": "Fulltime",
    "part-time": "Intern",
    "contract": "Contract",
    "intern": "Intern",
    "internship": "Intern",
    "temporary": "Contract",
}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _fetch(slug: str) -> list | None:
    resp = requests.get(API_URL.format(slug=slug), params={"mode": "json"}, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    # Lever returns [] for a valid but empty board, and occasionally an error dict for an invalid slug.
    return data if isinstance(data, list) else None


def _map_job_type(commitment: str | None) -> str:
    if not commitment:
        return "Fulltime"
    return JOB_TYPE_MAP.get(commitment.strip().lower(), "Fulltime")


def _format_salary(posting: dict) -> tuple[str | None, float | None, float | None]:
    salary_range = posting.get("salaryRange")
    if not salary_range:
        return None, None, None
    lo = salary_range.get("min")
    hi = salary_range.get("max")
    currency = salary_range.get("currency", "")
    label = f"{currency} {lo:,.0f} - {hi:,.0f}" if lo and hi else None
    return label, lo, hi


def fetch_jobs(slug: str, company_url: str) -> list[dict]:
    """Returns normalized job dicts ready for db.upsert_job. Empty list if board doesn't exist / has no jobs."""
    postings = _fetch(slug)
    if not postings:
        return []

    jobs = []
    for posting in postings:
        categories = posting.get("categories", {})
        salary_range, salary_min, salary_max = _format_salary(posting)
        locations = categories.get("allLocations") or ([categories["location"]] if categories.get("location") else [])

        jobs.append({
            "job_url": posting.get("hostedUrl"),
            "locations": locations,
            "job_type": _map_job_type(categories.get("commitment")),
            "salary_range": salary_range,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "job_name": posting.get("text"),
            "description": posting.get("descriptionPlain") or posting.get("description"),
            "company_url": company_url,
            # Lever's API doesn't return a company display name; falling back to the slug.
            "company_name": slug,
            "platform": "lever",
        })
    return jobs
