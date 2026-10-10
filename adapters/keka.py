import re

import requests
from tenacity import retry, stop_after_attempt, wait_exponential
from utils.extract_text import extract_text

PORTAL_URL = "https://{slug}.keka.com/careers/api/organization/default/careerportalinfo"
JOBS_URL = "https://{slug}.keka.com/careers/api/embedjobs/default/active/{org_id}"
JOB_URL = "https://{slug}.keka.com/careers/jobdetails/{job_id}"

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _get(url: str) -> dict | list | None:
    resp = requests.get(url, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _extract_org_id(portal: dict) -> str | None:
    """The jobs endpoint needs the org UUID. The portal info response has no explicit
    id field, but the asset paths embed it: /ats/documents/<org_id>/careerportal/..."""
    for key in ("careersBackgroundPath", "logoPath"):
        match = _UUID_RE.search(portal.get(key) or "")
        if match:
            return match.group(0)
    return None


def _format_salary(posting: dict) -> tuple[str | None, float | None, float | None]:
    sr = posting.get("salaryRange") or {}
    lo = sr.get("min") if sr.get("min") is not None else sr.get("minimum")
    hi = sr.get("max") if sr.get("max") is not None else sr.get("maximum")
    currency = sr.get("currency") or ""
    label = posting.get("salaryRangeFormat") or None
    if not label and lo and hi:
        label = f"{currency} {lo:,.0f} - {hi:,.0f}".strip()
    return label, lo, hi


def _map_job_type(title: str | None, raw: int | str | None = 2) -> str:
    # Keka's jobType enum is undocumented. Values below are best guesses, verify
    # against a few real postings. Unknown values fall back to Fulltime.
    if title and "intern" in title.lower():
        return "Intern"
    mapping = {
        "1": "Intern",
        "2": "Fulltime",
        "3": "Contract",
        "4": "Intern",
    }
    key = str(raw) if raw is not None else "2"
    return mapping.get(key, "Fulltime")


def fetch_jobs(slug: str, company_url: str) -> list[dict]:
    """Returns normalized job dicts ready for db.upsert_job. Empty list if company has no board / no jobs.

    slug is the Keka subdomain (e.g. "thenudge" for thenudge.keka.com).
    org_id can be passed to skip the portal lookup.
    """
    portal_data = _get(PORTAL_URL.format(slug=slug))
    portal = portal_data if isinstance(portal_data, dict) else {}
    org_id = _extract_org_id(portal)
    if not org_id:
        return []

    data = _get(JOBS_URL.format(slug=slug, org_id=org_id))
    if not data:
        return []

    # Response is an object keyed by index ("0", "1", ...), but handle a plain list too.
    postings = list(data.values()) if isinstance(data, dict) else data

    company_name = portal.get("name") or slug
    jobs = []
    for posting in postings:
        job_id = posting.get("id")
        if job_id is None:
            continue

        salary_range, salary_min, salary_max = _format_salary(posting)
        locations = [loc.get("name") for loc in posting.get("jobLocations") or [] if loc.get("name")]
        title = posting.get("title")

        jobs.append({
            "job_url": JOB_URL.format(slug=slug, job_id=job_id),
            "locations": locations,
            "job_type": _map_job_type(title, posting.get("jobType") or 2),
            "salary_range": salary_range,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "job_name": title,
            "description": extract_text(posting.get("description") or posting.get("excerpt")),
            "company_url": company_url,
            "company_name": company_name,
            "platform": "keka",
        })
    return jobs