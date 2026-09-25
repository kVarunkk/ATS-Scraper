import requests
from tenacity import retry, stop_after_attempt, wait_exponential
from utils.extract_text import extract_text

WORKABLE_JOBS_URL = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"

WORKABLE_JOB_TYPE_MAP = {
    "full": "Fulltime",
    "full-time": "Fulltime",
    "part": "Intern",
    "part-time": "Intern",
    "contract": "Contract",
    "temporary": "Contract",
    "intern": "Intern",
    "internship": "Intern",
    "other": "Fulltime",
}

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _fetch_workable(slug: str):
    resp = requests.get(WORKABLE_JOBS_URL.format(slug=slug), timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _extract_list_workable(data) -> list:
    # Workable's widget endpoint wraps postings under "jobs" on a single response,
    # no pagination on this endpoint as far as observed. Adjust if a given board
    # comes back shaped differently.
    if isinstance(data, dict):
        return data.get("jobs") or []
    if isinstance(data, list):
        return data
    return []


def _format_salary_workable(salary) -> tuple[str | None, float | None, float | None]:
    if not salary or not isinstance(salary, dict):
        return None, None, None
    try:
        lo = salary.get("rate_from") or salary.get("min")
        hi = salary.get("rate_to") or salary.get("max")
        currency = salary.get("currency", "")
        if lo and hi:
            return f"{currency} {lo:,.0f} - {hi:,.0f}", lo, hi
    except (KeyError, TypeError):
        pass
    return None, None, None


def fetch_jobs(slug: str, company_url: str) -> list[dict]:
    """Returns normalized job dicts ready for db.upsert_job. Empty list if board doesn't exist / has no jobs.

    Unlike Rippling, Workable's widget endpoint returns full descriptions for every
    posting in the single ?details=true call, so no per-job detail fetch is needed.
    """
    data = _fetch_workable(slug)
    if not data:
        return []

    postings = _extract_list_workable(data)
    company_name = data.get("name") if isinstance(data, dict) else slug

    jobs = []
    for posting in postings:
        job_url = posting.get("url") or posting.get("shortlink")

        description_parts = [
            posting.get("description") or "",
            posting.get("requirements") or "",
            posting.get("benefits") or "",
        ]
        description_text = extract_text("".join(description_parts))

        location = posting.get("location") or {}
        locations = posting.get("locations") or ([location] if location else [])
        location_names = [
            ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
            for loc in locations
            if isinstance(loc, dict)
        ]
        if not location_names and location.get("telecommuting"):
            location_names = ["Remote"]

        employment_type = (posting.get("employment_type") or "").strip().lower()
        salary_range, salary_min, salary_max = _format_salary_workable(posting.get("salary"))

        jobs.append({
            "job_url": job_url,
            "locations": location_names,
            "job_type": WORKABLE_JOB_TYPE_MAP.get(employment_type, "Fulltime"),
            "salary_range": salary_range,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "job_name": posting.get("title"),
            "description": description_text,
            "company_url": company_url,
            "company_name": company_name,
            "platform": "workable",
        })
    return jobs