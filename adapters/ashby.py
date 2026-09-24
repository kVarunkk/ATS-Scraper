import requests
from tenacity import retry, stop_after_attempt, wait_exponential

API_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _fetch(slug: str) -> dict | None:
    resp = requests.get(API_URL.format(slug=slug), params={"includeCompensation": "true"}, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _format_salary(comp: dict | None) -> tuple[str | None, float | None, float | None]:
    if not comp or not comp.get("summaryComponents"):
        return None, None, None
    for comp_item in comp["summaryComponents"]:
        if comp_item.get("compensationType") == "Salary":
            lo = comp_item.get("minValue")
            hi = comp_item.get("maxValue")
            currency = comp_item.get("currencyCode", "")
            label = f"{currency} {lo:,.0f} - {hi:,.0f}" if lo and hi else None
            return label, lo, hi
    return None, None, None


def fetch_jobs(slug: str, company_url: str) -> list[dict]:
    """Returns normalized job dicts ready for db.upsert_job. Empty list if company has no board / no jobs."""
    data = _fetch(slug)
    if not data:
        return []

    company_name = data.get("organizationName") or slug
    jobs = []
    for posting in data.get("jobs", []):
        salary_range, salary_min, salary_max = _format_salary(posting.get("compensation"))
        location = posting.get("location")
        secondary_locations = [loc.get("location") for loc in posting.get("secondaryLocations", []) if loc.get("location")]
        locations = [l for l in ([location] + secondary_locations) if l]

        jobs.append({
            "job_url": posting.get("jobUrl"),
            "locations": locations,
            "job_type": _map_job_type(posting.get("employmentType")),
            "salary_range": salary_range,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "job_name": posting.get("title"),
            "description": posting.get("descriptionPlain") or posting.get("descriptionHtml"),
            "company_url": company_url,
            "company_name": company_name,
            "platform": "ashby",
        })
    return jobs


def _map_job_type(raw: str | None) -> str:
    mapping = {
        "FullTime": "Fulltime",
        "PartTime": "Intern",
        "Contract": "Contract",
        "Intern": "Intern",
        "Temporary": "Contract",
    }
    return mapping.get(raw or "", "Fulltime")
