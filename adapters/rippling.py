import requests
from tenacity import retry, stop_after_attempt, wait_exponential
from bs4 import BeautifulSoup

JOBS_URL = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"

JOB_TYPE_MAP = {
    "SALARIED_FT": "Fulltime",
    "SALARIED_PT": "Intern",
    "HOURLY_FT": "Fulltime",
    "HOURLY_PT": "Intern",
    "CONTRACT": "Contract",
    "INTERN": "Intern",
    "TEMPORARY": "Contract",
}

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _fetch(slug: str):
    resp = requests.get(JOBS_URL.format(slug=slug), timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()

def extract_text(html: str) -> str:
    html_content = html 
    soup = BeautifulSoup(html_content, "html.parser")
    clean_text = soup.get_text(separator="\n", strip=True)
    return clean_text

    
def _extract_list(data) -> list:
    # NOTE: response shape for the list endpoint hasn't been directly verified here -
    # handles the common cases (bare list, or wrapped under "jobs"/"results"). Check
    # one real response for your slug and adjust this if it comes back differently.
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("jobs") or data.get("results") or []
    return []


def _format_salary(pay_range_details) -> tuple[str | None, float | None, float | None]:
    if not pay_range_details:
        return None, None, None
    try:
        first = pay_range_details[0]
        lo = first.get("minAmount") or first.get("min")
        hi = first.get("maxAmount") or first.get("max")
        currency = first.get("currency", "")
        if lo and hi:
            return f"{currency} {lo:,.0f} - {hi:,.0f}", lo, hi
    except (KeyError, IndexError, TypeError):
        pass
    return None, None, None


def fetch_jobs(slug: str, company_url: str) -> list[dict]:
    """Returns normalized job dicts ready for db.upsert_job. Empty list if board doesn't exist / has no jobs."""
    data = _fetch(slug)
    if not data:
        return []

    postings = _extract_list(data)
    jobs = []
    for posting in postings:
        uuid = posting.get("uuid")
        job_url = posting.get("url") or (f"https://ats.rippling.com/{slug}/jobs/{uuid}" if uuid else None)

        description = posting.get("description") or {}
        description_text = None
        if isinstance(description, dict):
            description_text = (description.get("role") or "") + (description.get("company") or "")
        elif isinstance(description, str):
            description_text = description

        description_text = extract_text(description_text)    

        employment_type = posting.get("employmentType") or {}
        salary_range, salary_min, salary_max = _format_salary(posting.get("payRangeDetails"))
        company_name = posting.get("companyName") or (posting.get("board") or {}).get("companyName") or slug

        jobs.append({
            "job_url": job_url,
            "locations": posting.get("workLocations") or [],
            "job_type": JOB_TYPE_MAP.get(employment_type.get("label") or "", "Fulltime"),
            "salary_range": salary_range,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "job_name": posting.get("name"),
            "description": description_text,
            "company_url": company_url,
            "company_name": company_name,
            "platform": "rippling",
        })
    return jobs