# from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
# from tenacity import retry, stop_after_attempt, wait_exponential
# from utils.extract_text import extract_text
# import re

# JOB_LIST_SELECTOR = "td.jv-job-list-name a"
# JOB_TITLE_SELECTOR = "h2.jv-header"
# JOB_DESCRIPTION_SELECTOR = ".jv-job-detail-description"
# JOB_META_SELECTOR = "p.jv-job-detail-meta"


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
#     return [link for link in links if link]

# SALARY_PATTERN = re.compile(
#     r"Salary:\s*([A-Za-z]{3})?\s*([\d,.]+)\s*-\s*([\d,.]+)",
#     re.IGNORECASE
# )


# def _extract_meta(meta_el) -> dict:
#     if not meta_el:
#         return {"locations": [], "salary_range": None, "salary_min": None, "salary_max": None}

#     segments = meta_el.evaluate("""
#         el => {
#             const parts = [];
#             let current = "";
#             for (const node of el.childNodes) {
#                 const isSeparator = node.nodeType === 1 && (
#                     node.classList.contains("jv-inline-separator") || node.tagName === "BR"
#                 );
#                 if (isSeparator) {
#                     parts.push(current);
#                     current = "";
#                 } else {
#                     current += node.textContent;
#                 }
#             }
#             parts.push(current);
#             return parts;
#         }
#     """)

#     # collapse internal newlines/whitespace from the source HTML into single spaces
#     segments = [" ".join(s.split()) for s in segments]
#     segments = [s for s in segments if s]
#     print(f"DEBUG jobvite meta segments: {segments}")

#     locations = []
#     salary_range = salary_min = salary_max = None

#     for i, seg in enumerate(segments):
#         if seg.lower().startswith("salary"):
#             match = SALARY_PATTERN.search(seg)
#             if match:
#                 currency, lo, hi = match.groups()
#                 try:
#                     salary_min = float(lo.replace(",", ""))
#                     salary_max = float(hi.replace(",", ""))
#                     salary_range = f"{currency or ''} {salary_min:,.2f} - {salary_max:,.2f}".strip()
#                 except ValueError:
#                     pass
#             continue
#         if i == 0:
#             continue  # first segment is department/category, not a location
#         locations.append(seg)

#     print(f"DEBUG jobvite parsed locations={locations} salary_range={salary_range}")
#     return {
#         "locations": locations,
#         "salary_range": salary_range,
#         "salary_min": salary_min,
#         "salary_max": salary_max,
#     }

# def _extract_job_detail(page, job_url: str) -> dict | None:
#     try:
#         _goto(page, job_url)
#         page.wait_for_selector(JOB_TITLE_SELECTOR, timeout=15000)
#     except PlaywrightTimeoutError:
#         return None

#     title_el = page.query_selector(JOB_TITLE_SELECTOR)
#     title = title_el.inner_text().strip() if title_el else None

#     description_el = page.query_selector(JOB_DESCRIPTION_SELECTOR)
#     description_html = description_el.inner_html() if description_el else ""
#     description_text = extract_text(description_html)

#     meta_el = page.query_selector(JOB_META_SELECTOR)
#     # locations = _parse_locations(meta_el.inner_text()) if meta_el else []
#     # locations = _extract_locations(page, meta_el)
#     meta = _extract_meta(meta_el)
#     # print(f"DEBUG jobvite job_url={job_url} title={title!r} locations={locations}")

#     return {
#         "job_url": job_url,
#         "locations": meta["locations"],
#         "job_type": "Fulltime",
#         "salary_range": meta["salary_range"],
#         "salary_min": meta["salary_min"],
#         "salary_max": meta["salary_max"],
#         "job_name": title,
#         "description": description_text,
#     }


# def fetch_jobs(slug: str, company_url: str) -> list[dict]:
#     """Returns normalized job dicts ready for db.upsert_job. Empty list if the
#     board is unreachable or has no postings.

#     Jobvite has no public API, so this scrapes the listing page for job urls,
#     then visits each job page for title, location and description. Unlike
#     Workable, Jobvite doesn't expose job_type or salary anywhere in the DOM,
#     so those fields come back as None. company_name also has to be passed in
#     from the company table since there's no API response to pull it from.
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
#                 detail["platform"] = "jobvite"
#                 jobs.append(detail)
#         finally:
#             browser.close()
#     return jobs



import logging
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup
from bs4.element import NavigableString
import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from utils.extract_text import extract_text

logger = logging.getLogger(__name__)

PLATFORM = "jobvite"

JOB_LIST_SELECTOR = "td.jv-job-list-name a"
JOB_TITLE_SELECTOR = "h2.jv-header"
JOB_DESCRIPTION_SELECTOR = ".jv-job-detail-description"
JOB_META_SELECTOR = "p.jv-job-detail-meta"

SALARY_PATTERN = re.compile(
    r"Salary:\s*([A-Za-z]{3})?\s*([\d,.]+)\s*-\s*([\d,.]+)",
    re.IGNORECASE
)

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


def _extract_meta(meta_el) -> dict:
    if not meta_el:
        return {"locations": [], "salary_range": None, "salary_min": None, "salary_max": None}

    # Replicate the child-node separation logic using BeautifulSoup
    parts = []
    current = ""
    for node in meta_el.children:
        is_separator = False
        if getattr(node, "name", None) == "br":
            is_separator = True
        elif hasattr(node, "get") and node.get("class") and any(c.startswith("jv-inline-separator") for c in node.get("class")):
            is_separator = True

        if is_separator:
            parts.append(current)
            current = ""
        else:
            if isinstance(node, NavigableString):
                current += str(node)
            elif hasattr(node, "get_text"):
                current += node.get_text()
    parts.append(current)

    # collapse internal newlines/whitespace from the source HTML into single spaces
    segments = [" ".join(s.split()) for s in parts]
    segments = [s for s in segments if s]
    logger.debug("jobvite meta segments: %s", segments)

    locations = []
    salary_range = salary_min = salary_max = None

    for i, seg in enumerate(segments):
        if seg.lower().startswith("salary"):
            match = SALARY_PATTERN.search(seg)
            if match:
                currency, lo, hi = match.groups()
                try:
                    salary_min = float(lo.replace(",", ""))
                    salary_max = float(hi.replace(",", ""))
                    currency_str = f"{currency} " if currency else ""
                    salary_range = f"{currency_str}{salary_min:,.2f} - {salary_max:,.2f}"
                except ValueError:
                    pass
            continue
        if i == 0:
            continue  # first segment is department/category, not a location
        locations.append(seg)

    logger.debug("jobvite parsed locations=%s salary_range=%s", locations, salary_range)
    return {
        "locations": locations,
        "salary_range": salary_range,
        "salary_min": salary_min,
        "salary_max": salary_max,
    }


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

    description_el = soup.select_one(JOB_DESCRIPTION_SELECTOR)
    description_html = str(description_el) if description_el else ""
    description_text = extract_text(description_html) if description_html else ""

    meta_el = soup.select_one(JOB_META_SELECTOR)
    meta = _extract_meta(meta_el)

    return {
        "job_url": _normalize_url(job_url),
        "locations": meta["locations"],
        "job_type": "Fulltime",
        "salary_range": meta["salary_range"],
        "salary_min": meta["salary_min"],
        "salary_max": meta["salary_max"],
        "job_name": title,
        "description": description_text,
        "platform": PLATFORM,
    }