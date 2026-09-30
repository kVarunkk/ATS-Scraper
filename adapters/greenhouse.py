# import requests
# from tenacity import retry, stop_after_attempt, wait_exponential

# JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
# BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}"


# @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
# def _get(url: str, params: dict | None = None) -> dict | None:
#     resp = requests.get(url, params=params, timeout=20)
#     if resp.status_code == 404:
#         return None
#     resp.raise_for_status()
#     return resp.json()


# def fetch_jobs(slug: str, company_url: str) -> list[dict]:
#     """Returns normalized job dicts ready for db.upsert_job. Empty list if board doesn't exist / has no jobs."""
#     data = _get(JOBS_URL.format(slug=slug), params={"content": "true"})
#     if not data:
#         return []

#     board_meta = _get(BOARD_URL.format(slug=slug))
#     company_name = (board_meta or {}).get("name") or slug

#     jobs = []
#     for posting in data.get("jobs", []):
#         location_name = (posting.get("location") or {}).get("name")

#         jobs.append({
#             "job_url": posting.get("absolute_url"),
#             "locations": [location_name] if location_name else [],
#             # Greenhouse doesn't expose a consistent employment-type field across boards;
#             # left at the column default (Fulltime) unless you add per-board metadata parsing.
#             "job_type": "Fulltime",
#             "salary_range": None,
#             "salary_min": None,
#             "salary_max": None,
#             "job_name": posting.get("title"),
#             "description": posting.get("content"),  # HTML
#             "company_url": company_url,
#             "company_name": company_name,
#             "platform": "greenhouse",
#         })
#     return jobs


import html
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}"

session = requests.Session()
session.headers["User-Agent"] = "your-jobboard-bot/1.0 (contact@example.com)"


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _get(url: str, params: dict | None = None) -> dict | None:
    resp = session.get(url, params=params, timeout=45)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _clean_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.split("?")[0].rstrip("/")


def fetch_jobs(slug: str, company_url: str, company_name: str | None = None) -> list[dict] | None:
    """Returns None if the board does not exist (404), [] if it exists with no jobs.
    Raises on transient failures so the caller can skip mark-inactive."""
    data = _get(JOBS_URL.format(slug=slug), params={"content": "true"})
    if data is None:
        return None

    if not company_name:
        try:
            company_name = (_get(BOARD_URL.format(slug=slug)) or {}).get("name") or slug
        except Exception:
            company_name = slug

    jobs = []
    for posting in data.get("jobs", []):
        job_url = _clean_url(posting.get("absolute_url"))
        if not job_url:
            continue
        location_name = (posting.get("location") or {}).get("name")
        jobs.append({
            "job_url": job_url,
            "locations": [location_name] if location_name else [],
            "job_type": None,
            "salary_range": None,
            "salary_min": None,
            "salary_max": None,
            "job_name": posting.get("title"),
            "description": html.unescape(posting.get("content") or ""),
            "company_url": company_url,
            "company_name": company_name,
            "platform": "greenhouse",
        })
    return jobs