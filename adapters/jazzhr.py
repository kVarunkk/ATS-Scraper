from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from tenacity import retry, stop_after_attempt, wait_exponential
from utils.extract_text import extract_text

JOB_LIST_SELECTOR = ".list-group-item-heading a"
JOB_TITLE_SELECTOR = ".job-header h2"
JOB_LOCATION_SELECTOR = "div[title='Location']"
JOB_TYPE_SELECTOR = "#resumator-job-employment"
JOB_DESCRIPTION_SELECTOR = "#job-description"

JAZZHR_JOB_TYPE_MAP = {
    "full time": "Fulltime",
    "part time": "Intern",
    "contract": "Contract",
    "temporary": "Contract",
    "intern": "Intern",
    "internship": "Intern",
    "seasonal": "Contract",
}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _goto(page, url):
    page.goto(url, timeout=30000, wait_until="domcontentloaded")
    return page


def _extract_job_urls(page, company_url: str) -> list[str]:
    _goto(page, company_url)
    try:
        page.wait_for_selector(JOB_LIST_SELECTOR, timeout=15000)
    except PlaywrightTimeoutError:
        return []
    links = page.eval_on_selector_all(JOB_LIST_SELECTOR, "els => els.map(el => el.href)")
    return [link for link in links if link]


def _clean_field(el) -> str | None:
    """job-attributes-container divs prefix their text with an <i> icon that
    has no text content, but inner_text() can still carry stray whitespace
    from the source markup, so collapse it down to a single-spaced string.
    """
    if not el:
        return None
    text = " ".join(el.inner_text().split())
    return text or None


def _extract_job_detail(page, job_url: str) -> dict | None:
    try:
        _goto(page, job_url)
        page.wait_for_selector(JOB_TITLE_SELECTOR, timeout=15000)
    except PlaywrightTimeoutError:
        return None

    title_el = page.query_selector(JOB_TITLE_SELECTOR)
    title = _clean_field(title_el)

    location_el = page.query_selector(JOB_LOCATION_SELECTOR)
    location = _clean_field(location_el)
    locations = [location] if location else []

    type_el = page.query_selector(JOB_TYPE_SELECTOR)
    raw_type = (_clean_field(type_el) or "").lower()
    job_type = JAZZHR_JOB_TYPE_MAP.get(raw_type, "Fulltime")

    description_el = page.query_selector(JOB_DESCRIPTION_SELECTOR)
    description_html = description_el.inner_html() if description_el else ""
    description_text = extract_text(description_html)

    print(f"DEBUG jazzhr job_url={job_url} title={title!r} locations={locations} raw_type={raw_type!r}")

    return {
        "job_url": job_url,
        "locations": locations,
        "job_type": job_type,
        "salary_range": None,
        "salary_min": None,
        "salary_max": None,
        "job_name": title,
        "description": description_text,
    }


def fetch_jobs(slug: str, company_url: str) -> list[dict]:
    """Returns normalized job dicts ready for db.upsert_job. Empty list if the
    board is unreachable or has no postings.

    JazzHR has no public API either, so this scrapes the listing page for job
    urls, then visits each job page for title, location, type and
    description. No salary info is exposed on JazzHR job pages, so those
    fields come back as None. company_name comes from the company table since
    there's no API response to pull it from.
    """
    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            job_urls = _extract_job_urls(page, company_url)
            for job_url in job_urls:
                detail = _extract_job_detail(page, job_url)
                if not detail:
                    continue
                detail["company_url"] = company_url
                detail["company_name"] = slug
                detail["platform"] = "jazzhr"
                jobs.append(detail)
        finally:
            browser.close()
    return jobs