import logging
from urllib.parse import urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup
import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from utils.extract_text import extract_text

logger = logging.getLogger(__name__)

PLATFORM = "workday"

WORKDAY_JOB_TYPE_MAP = {
    "full time": "Fulltime",
    "part time": "Intern",
    "contract": "Contract",
    "temporary": "Contract",
    "intern": "Intern",
    "internship": "Intern",
    "seasonal": "Contract",
}

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
})


class ParseError(Exception):
    """Page or API response loaded but did not look like what we expect."""


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
def _get(url: str, params: dict | None = None) -> requests.Response | None:
    """None on 404. Raises on any other failure."""
    resp = session.get(url, params=params, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _post(url: str, json_data: dict) -> requests.Response | None:
    """None on 404. Raises on any other failure."""
    resp = session.post(url, json=json_data, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp


def _normalize_url(url: str, base: str | None = None) -> str:
    if base:
        url = urljoin(base, url.strip())
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _derive_api_and_job_base(company_url: str) -> tuple[str, str] | tuple[None, None]:
    """Derives the Workday CXS API search endpoint and the base job detail URL pattern
    from a company career page URL (e.g., https://academy.wd1.myworkdayjobs.com/en-US/Careers).
    """
    parsed = urlsplit(company_url)
    netloc = parsed.netloc  # e.g., academy.wd1.myworkdayjobs.com
    path_segments = [seg for seg in parsed.path.split("/") if seg]

    if not path_segments:
        return None, None

    subdomain = netloc.split(".")[0]
    site_name = path_segments[-1]  # e.g. 'Careers' or 'Search'

    api_url = f"{parsed.scheme}://{netloc}/wday/cxs/{subdomain}/{site_name}/jobs"
    job_detail_base = f"{parsed.scheme}://{netloc}/{path_segments[0]}/{site_name}"

    return api_url, job_detail_base


def list_jobs(company_url: str) -> list[str] | None:
    """Phase 1. Harvest job URLs using Workday's pagination API via POST requests."""
    api_url, _ = _derive_api_and_job_base(company_url)
    if not api_url:
        return None

    limit = 20
    offset = 0
    job_urls = set()
    total_hits = None

    while True:
        payload = {
            "appliedFacets": {},
            "limit": limit,
            "offset": offset,
            "searchText": ""
        }

        try:
            resp = _post(api_url, payload)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

        if resp is None:
            return None

        try:
            data = resp.json()
        except ValueError:
            raise ParseError(f"Invalid JSON response from Workday API at {api_url}")

        postings = data.get("jobPostings", [])
        if not postings:
            break

        if total_hits is None:
            total_hits = data.get("total", 0)

        for posting in postings:
            external_path = posting.get("externalPath")
            if external_path:
                # full_url = _normalize_url(external_path, base=company_url)
                full_url = _normalize_url(company_url.rstrip("/") + "/" + external_path.lstrip("/"))
                job_urls.add(full_url)

        # total_hits = data.get("total", 0)
        offset += limit
        if offset >= total_hits or len(postings) < limit:
            break

    return sorted(job_urls)


def fetch_detail(job_url: str) -> dict | None:
    """Phase 2. Fetch individual job details using Workday's CXS job detail endpoint API,
    with fallback to BeautifulSoup HTML scraping if necessary.
    """
    parsed = urlsplit(job_url)
    path_parts = [p for p in parsed.path.split("/") if p]

    if len(path_parts) >= 4 and "job" in path_parts:
        subdomain = parsed.netloc.split(".")[0]
        job_idx = path_parts.index("job")
        site_name = path_parts[job_idx - 1]

        api_detail_path = "/".join(path_parts[job_idx:])
        api_detail_url = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{subdomain}/{site_name}/{api_detail_path}"

        resp = _get(api_detail_url)
        if resp is not None and resp.status_code == 200:
            try:
                data = resp.json()
                job_info = data.get("jobPostingInfo", {})

                title = job_info.get("title")
                if not title:
                    raise ParseError(f"No title found in Workday API response for {job_url}")

                # Extract location from descriptor or fallback string
                location_text = (
                    job_info.get("location") or
                    job_info.get("jobRequisitionLocation", {}).get("descriptor")
                )
                locations = [location_text] if location_text else []

                raw_type = (job_info.get("timeType") or "").lower().replace("-", " ")
                job_type = WORKDAY_JOB_TYPE_MAP.get(raw_type)

                description_html = job_info.get("jobDescription", "")
                description_text = extract_text(description_html) if description_html else ""

                logger.debug("workday api job_url=%s title=%r locations=%s", job_url, title, locations)

                return {
                    "job_url": _normalize_url(job_url),
                    "locations": locations,
                    "job_type": job_type,
                    "salary_range": None,
                    "salary_min": None,
                    "salary_max": None,
                    "job_name": title,
                    "description": description_text,
                    "platform": PLATFORM,
                }
            except (ValueError, KeyError):
                pass  # Fall back to HTML scraping if API JSON parsing fails

    # Fallback: HTML parsing via BeautifulSoup
    resp = _get(job_url)
    if resp is None:
        return None

    if _normalize_url(resp.url) != _normalize_url(job_url):
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    title_el = soup.select_one('[data-automation-id="jobPostingHeader"]')
    title = " ".join(title_el.get_text().split()) if title_el else None
    if not title:
        raise ParseError(f"No title found at {job_url}")

    location_els = soup.select('div[data-automation-id="locations"] dd')
    locations = [" ".join(el.get_text().split()) for el in location_els if el.get_text().strip()]

    time_type_el = soup.select_one('div[data-automation-id="time"] dd')
    raw_type = (" ".join(time_type_el.get_text().split()) if time_type_el else "").lower().replace("-", " ")
    job_type = WORKDAY_JOB_TYPE_MAP.get(raw_type)

    description_el = soup.select_one('div[data-automation-id="jobPostingDescription"]')
    description_html = str(description_el) if description_el else ""
    description_text = extract_text(description_html) if description_html else ""

    return {
        "job_url": _normalize_url(job_url),
        "locations": locations,
        "job_type": job_type,
        "salary_range": None,
        "salary_min": None,
        "salary_max": None,
        "job_name": title,
        "description": description_text,
        "platform": PLATFORM,
    }