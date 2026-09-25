import requests
from tenacity import retry, stop_after_attempt, wait_exponential
from utils.extract_text import extract_text

RECRUITEE_JOBS_URL = "https://{slug}.recruitee.com/api/offers"

RECRUITEE_JOB_TYPE_MAP = {
    "fulltime_fixed_term": "Fulltime",
    "fulltime_permanent": "Fulltime",
    "parttime_fixed_term": "Intern",
    "parttime_permanent": "Intern",
    "contract": "Contract",
    "temporary": "Contract",
    "internship": "Intern",
    "other": "Fulltime",
    "freelance": "Contract"
}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _fetch_recruitee(slug: str):
    resp = requests.get(RECRUITEE_JOBS_URL.format(slug=slug), timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _extract_list_recruitee(data) -> list:
    # Recruitee's public offers endpoint wraps postings under "offers" on a single
    # response, no pagination observed on this endpoint. Adjust if a given board
    # comes back shaped differently.
    if isinstance(data, dict):
        return data.get("offers") or []
    if isinstance(data, list):
        return data
    return []


def _format_salary_recruitee(salary) -> tuple[str | None, float | None, float | None]:
    if not salary or not isinstance(salary, dict):
        return None, None, None
    try:
        lo = salary.get("min")
        hi = salary.get("max")
        currency = salary.get("currency", "")
        if lo and hi:
            return f"{currency} {lo:,.0f} - {hi:,.0f}", lo, hi
    except (KeyError, TypeError):
        pass
    return None, None, None


def fetch_jobs(slug: str, company_url: str) -> list[dict]:
    """Returns normalized job dicts ready for db.upsert_job. Empty list if board doesn't exist / has no jobs.

    Like Workable, Recruitee's offers endpoint returns full descriptions for every
    posting in the single call, so no per-job detail fetch is needed.
    """
    data = _fetch_recruitee(slug)
    if not data:
        return []

    postings = _extract_list_recruitee(data)
    company_name = postings[0].get("company_name") if postings else slug

    jobs = []
    for posting in postings:
        job_url = posting.get("careers_url") or posting.get("careers_apply_url")

        description_parts = [
            posting.get("description") or "",
            posting.get("requirements") or "",
        ]
        description_text = extract_text("".join(description_parts))

        locations = posting.get("locations") or []
        location_names = [
            ", ".join(filter(None, [loc.get("city"), loc.get("state"), loc.get("country")]))
            for loc in locations
            if isinstance(loc, dict)
        ]
        if not location_names:
            fallback = posting.get("location")
            if fallback:
                location_names = [fallback]
        if posting.get("remote"):
            location_names = location_names or ["Remote"]

        employment_type = (posting.get("employment_type_code") or "").strip().lower()
        salary_range, salary_min, salary_max = _format_salary_recruitee(posting.get("salary"))

        jobs.append({
            "job_url": job_url,
            "locations": location_names,
            "job_type": RECRUITEE_JOB_TYPE_MAP.get(employment_type, "Fulltime"),
            "salary_range": salary_range,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "job_name": posting.get("title"),
            "description": description_text,
            "company_url": company_url,
            "company_name": posting.get("company_name") or company_name,
            "platform": "recruitee",
        })
    return jobs