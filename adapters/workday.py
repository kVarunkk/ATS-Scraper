from urllib.parse import urljoin
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from tenacity import retry, stop_after_attempt, wait_exponential
from utils.extract_text import extract_text

JOB_LIST_SELECTOR = 'a[data-automation-id="jobTitle"]'
JOB_TITLE_SELECTOR = '[data-automation-id="jobPostingHeader"]'
JOB_LOCATION_SELECTOR = 'div[data-automation-id="locations"] dd'
JOB_TIME_TYPE_SELECTOR = 'div[data-automation-id="time"] dd'
JOB_REQUISITION_SELECTOR = 'div[data-automation-id="requisitionId"] dd'
JOB_DESCRIPTION_SELECTOR = 'div[data-automation-id="jobPostingDescription"]'
NEXT_PAGE_SELECTOR = 'button[data-automation-id="next"], button[aria-label="next"]'

WORKDAY_JOB_TYPE_MAP = {
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
    urls = []
    seen = set()
    base_url = company_url.rstrip("/")

    while True:
        try:
            page.wait_for_selector(JOB_LIST_SELECTOR, timeout=15000)
        except PlaywrightTimeoutError:
            break

        elements = page.query_selector_all(JOB_LIST_SELECTOR)
        for el in elements:
            href = el.get_attribute("href")
            if href and "/job/" in href:
                # Isolate everything starting from /job/ onwards
                job_subpath = href[href.index("/job/"):]
                
                # Combine company_url with the /job/... subpath
                full_url = f"{base_url}{job_subpath}"

                if full_url not in seen:
                    seen.add(full_url)
                    urls.append(full_url)

        # Pagination handling
        next_button = page.query_selector(NEXT_PAGE_SELECTOR)
        if not next_button:
            break

        is_disabled = next_button.get_attribute("disabled")
        aria_disabled = next_button.get_attribute("aria-disabled")
        if is_disabled is not None or aria_disabled == "true":
            break

        try:
            next_button.click()
            page.wait_for_timeout(2000)
        except Exception:
            break

    return urls


def _clean_field(el) -> str | None:
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

    location_els = page.query_selector_all('div[data-automation-id="locations"] dd')
    locations = []
    for el in location_els:
        cleaned_loc = _clean_field(el)
        if cleaned_loc:
            locations.append(cleaned_loc)

    time_type_el = page.query_selector(JOB_TIME_TYPE_SELECTOR)
    raw_type = (_clean_field(time_type_el) or "").lower().replace("-", " ")
    job_type = WORKDAY_JOB_TYPE_MAP.get(raw_type, "Fulltime")

    description_el = page.query_selector(JOB_DESCRIPTION_SELECTOR)
    description_html = description_el.inner_html() if description_el else ""
    description_text = extract_text(description_html)

    print(f"DEBUG workday job_url={job_url} title={title!r} locations={locations} raw_type={raw_type!r}")

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
    """Returns normalized job dicts ready for db.upsert_job using a single browser tab.
    """
    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            # 1. Harvest all job URLs across pagination in the single tab
            job_urls = _extract_job_urls(page, company_url)
            
            # 2. Visit each job detail page sequentially in the same tab
            for job_url in job_urls:
                detail = _extract_job_detail(page, job_url)
                if not detail:
                    continue
                detail["company_url"] = company_url
                detail["company_name"] = slug
                detail["platform"] = "workday"
                jobs.append(detail)
        finally:
            browser.close()
    return jobs