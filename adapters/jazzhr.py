import logging
from urllib.parse import urljoin, urlsplit, urlunsplit
import requests
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from utils.extract_text import extract_text

logger = logging.getLogger(__name__)

PLATFORM = "jazzhr"

JOB_LIST_SELECTOR = ".list-group-item-heading a"
JOB_TITLE_SELECTOR = ".job-header h2"
JOB_LOCATION_SELECTOR = "div[title='Location']"
JOB_TYPE_SELECTOR = "#resumator-job-employment"
JOB_DESCRIPTION_SELECTOR = "#job-description"

# Unknown types map to None, never to a guessed default.
# Adjust values to match your board DB enum.
JOB_TYPE_MAP = {
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
    )
})


class ParseError(Exception):
    """Page loaded but did not look like what we expect (layout change, block page)."""


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
def _get(url: str) -> requests.Response | None:
    """None on 404. Raises on any other failure."""
    resp = session.get(url, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp


def _clean_text(text: str | None) -> str | None:
    if not text:
        return None
    return " ".join(text.split()) or None


def _normalize_url(url: str, base: str | None = None) -> str:
    if base:
        url = urljoin(base, url.strip())
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def list_jobs(company_url: str) -> list[str] | None:
    """Phase 1. Job URLs currently on the board.

    None  = board does not exist (404, or redirected off the board's host).
    []    = board loaded and is positively empty.
    Raises on transient failure or an unrecognized page, so the caller
    skips mark-inactive and counts a failure instead.
    """
    resp = _get(company_url)
    if resp is None:
        return None

    if urlsplit(resp.url).netloc != urlsplit(company_url).netloc:
        return None  # redirected away, board is gone

    soup = BeautifulSoup(resp.text, "html.parser")
    links = soup.select(JOB_LIST_SELECTOR)

    if not links:
        return []

    urls = set()
    for link in links:
        href = link.get("href")
        if isinstance(href, str) and href.strip():
            urls.add(_normalize_url(href, base=company_url))
    return sorted(urls)


def fetch_detail(job_url: str) -> dict | None:
    """Phase 2. Job-level fields only (no company fields).

    None  = job is gone (404 or redirected to another page).
    Raises on transient failure or an unrecognized page, so the caller
    increments detail_attempts instead of marking the job inactive.
    """
    resp = _get(job_url)
    if resp is None:
        return None

    # Closed jobs often redirect to the board listing.
    if _normalize_url(resp.url) != _normalize_url(job_url):
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    title_el = soup.select_one(JOB_TITLE_SELECTOR)
    title = _clean_text(title_el.get_text()) if title_el else None
    if not title:
        raise ParseError(f"No title found at {job_url}")

    location_el = soup.select_one(JOB_LOCATION_SELECTOR)
    location = _clean_text(location_el.get_text()) if location_el else None

    type_el = soup.select_one(JOB_TYPE_SELECTOR)
    raw_type = (_clean_text(type_el.get_text()) or "").lower() if type_el else ""
    job_type = JOB_TYPE_MAP.get(raw_type)

    description_el = soup.select_one(JOB_DESCRIPTION_SELECTOR)
    description = extract_text(str(description_el)) if description_el else ""

    logger.debug("jazzhr job_url=%s title=%r raw_type=%r", job_url, title, raw_type)

    return {
        "job_url": _normalize_url(job_url),
        "locations": [location] if location else [],
        "job_type": job_type,
        "salary_range": None,
        "salary_min": None,
        "salary_max": None,
        "job_name": title,
        "description": description,
        "platform": PLATFORM,
    }