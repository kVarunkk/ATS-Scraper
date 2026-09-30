# from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
# from tenacity import retry, stop_after_attempt, wait_exponential
# from utils.extract_text import extract_text

# # all jobs page: GET https://ctwo.bamboohr.com/careers/list
# # singular job page: GET https://ctwo.bamboohr.com/careers/57/detail

# JOB_LIST_SELECTOR = "a.fab-LinkUnstyled"
# JOB_TITLE_SELECTOR = "[data-fabric-component='Headline']"
# JOB_ROW_SELECTOR = "div[data-fabric-component='Flex']"
# JOB_LABELBOX_SELECTOR = "div[data-fabric-component='LayoutBox']"
# JOB_DESCRIPTION_SELECTOR = "section[data-fabric-component='Section']"

# BAMBOOHR_JOB_TYPE_MAP = {
#     "full time": "Fulltime",
#     "part time": "Intern",
#     "contract": "Contract",
#     "temporary": "Contract",
#     "intern": "Intern",
#     "internship": "Intern",
#     "seasonal": "Contract",
# }


# @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
# def _goto(page, url):
#     page.goto(url, timeout=30000, wait_until="domcontentloaded")
#     return page


# def _extract_job_urls(page, company_url: str) -> list[str]:
#     _goto(page, company_url)
#     try:
#         page.wait_for_selector(JOB_LIST_SELECTOR, timeout=15000)
#     except PlaywrightTimeoutError:
#         return []
#     links = page.eval_on_selector_all(JOB_LIST_SELECTOR, "els => els.map(el => el.href)")
#     # Dedupe while preserving order, since the listing markup can repeat
#     # the same anchor inside nested wrapper elements.
#     seen = set()
#     urls = []
#     for link in links:
#         if link and link not in seen:
#             seen.add(link)
#             urls.append(link)
#     return urls


# def _clean_field(el) -> str | None:
#     if not el:
#         return None
#     text = " ".join(el.inner_text().split())
#     return text or None


# def _extract_labeled_fields(page) -> dict:
#     """The BambooHR job detail page lists Location, Department, Employment
#     Type, Minimum Experience and Compensation as a series of Flex rows,
#     each holding two LayoutBox children: a label and a value. There is no
#     stable per-field selector, so this walks every row and builds a
#     lowercased label to value map instead.
#     """
#     fields = {}
#     rows = page.query_selector_all(JOB_ROW_SELECTOR)
#     for row in rows:
#         boxes = row.query_selector_all(JOB_LABELBOX_SELECTOR)
#         if len(boxes) < 2:
#             continue
#         label = _clean_field(boxes[0])
#         value = _clean_field(boxes[1])
#         if label:
#             fields[label.strip().lower()] = value
#     return fields


# def _extract_job_detail(page, job_url: str) -> dict | None:
#     try:
#         _goto(page, job_url)
#         page.wait_for_selector(JOB_TITLE_SELECTOR, timeout=15000)
#     except PlaywrightTimeoutError:
#         return None

#     title_el = page.query_selector(JOB_TITLE_SELECTOR)
#     title = _clean_field(title_el)

#     fields = _extract_labeled_fields(page)

#     location = fields.get("location")
#     locations = [location] if location else []

#     raw_type = (fields.get("employment type") or "").lower().replace("-", " ")
#     job_type = BAMBOOHR_JOB_TYPE_MAP.get(raw_type, "Fulltime")

#     # Compensation is free text on BambooHR ("Competitive", a range, a
#     # single figure, etc), so it is kept as-is rather than parsed into
#     # salary_min/salary_max.
#     salary_range = fields.get("compensation")

#     description_el = page.query_selector(JOB_DESCRIPTION_SELECTOR)
#     description_html = description_el.inner_html() if description_el else ""
#     description_text = extract_text(description_html)

#     print(f"DEBUG bamboohr job_url={job_url} title={title!r} locations={locations} raw_type={raw_type!r}")

#     return {
#         "job_url": job_url,
#         "locations": locations,
#         "job_type": job_type,
#         "salary_range": salary_range,
#         "salary_min": None,
#         "salary_max": None,
#         "job_name": title,
#         "description": description_text,
#     }


# def fetch_jobs(slug: str, company_url: str) -> list[dict]:
#     """Returns normalized job dicts ready for db.upsert_job. Empty list if the
#     board is unreachable or has no postings.

#     BambooHR has no public API for careers pages either, so this scrapes the
#     listing page for job urls, then visits each job page for title,
#     location, department, employment type and compensation. Compensation is
#     free text on BambooHR (often just "Competitive"), so salary_min and
#     salary_max always come back as None; the raw text is kept in
#     salary_range for whatever downstream parsing wants to attempt.
#     company_name comes from the company table since there's no API response
#     to pull it from.
#     """
#     jobs = []
#     with sync_playwright() as p:
#         browser = p.chromium.launch()
#         page = browser.new_page()
#         try:
#             job_urls = _extract_job_urls(page, company_url)
#             for job_url in job_urls:
#                 detail = _extract_job_detail(page, job_url)
#                 if not detail:
#                     continue
#                 detail["company_url"] = company_url
#                 detail["company_name"] = slug
#                 detail["platform"] = "bamboohr"
#                 jobs.append(detail)
#         finally:
#             browser.close()
#     return jobs


import logging
from urllib.parse import urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup
import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from utils.extract_text import extract_text

logger = logging.getLogger(__name__)

PLATFORM = "bamboohr"

JOB_LIST_SELECTOR = "a.fab-LinkUnstyled"
JOB_TITLE_SELECTOR = "[data-fabric-component='Headline']"
JOB_ROW_SELECTOR = "div[data-fabric-component='Flex']"
JOB_LABELBOX_SELECTOR = "div[data-fabric-component='LayoutBox']"
JOB_DESCRIPTION_SELECTOR = "section[data-fabric-component='Section']"

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


def _extract_labeled_fields(soup) -> dict:
    """The BambooHR job detail page lists Location, Department, Employment
    Type, Minimum Experience and Compensation as a series of Flex rows,
    each holding two LayoutBox children: a label and a value.
    """
    fields = {}
    rows = soup.select(JOB_ROW_SELECTOR)
    for row in rows:
        boxes = row.select(JOB_LABELBOX_SELECTOR)
        if len(boxes) < 2:
            continue
        label = _clean_text(boxes[0].get_text())
        value = _clean_text(boxes[1].get_text())
        if label:
            fields[label.strip().lower()] = value
    return fields


def fetch_detail(job_url: str) -> dict | None:
    """Phase 2. Job-level fields only (no company fields).

    None  = job is gone (404 or redirected to another page).
    Raises on transient failure or an unrecognized page, so the caller
    increments detail_attempts instead of marking the job inactive.
    """
    resp = _get(job_url)
    if resp is None:
        return None

    # Closed jobs often redirect away from the job detail URL.
    if _normalize_url(resp.url) != _normalize_url(job_url):
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    title_el = soup.select_one(JOB_TITLE_SELECTOR)
    title = _clean_text(title_el.get_text()) if title_el else None
    if not title:
        raise ParseError(f"No title found at {job_url}")

    fields = _extract_labeled_fields(soup)

    location = fields.get("location")
    locations = [location] if location else []

    raw_type = (fields.get("employment type") or "").lower().replace("-", " ")
    job_type = JOB_TYPE_MAP.get(raw_type)

    # Compensation is free text on BambooHR, so it is kept as-is.
    salary_range = fields.get("compensation")

    description_el = soup.select_one(JOB_DESCRIPTION_SELECTOR)
    description_html = str(description_el) if description_el else ""
    description = extract_text(description_html) if description_html else ""

    logger.debug("bamboohr job_url=%s title=%r locations=%s raw_type=%r", job_url, title, locations, raw_type)

    return {
        "job_url": _normalize_url(job_url),
        "locations": locations,
        "job_type": job_type,
        "salary_range": salary_range,
        "salary_min": None,
        "salary_max": None,
        "job_name": title,
        "description": description,
        "platform": PLATFORM,
    }