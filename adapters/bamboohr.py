from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from tenacity import retry, stop_after_attempt, wait_exponential
from utils.extract_text import extract_text

# BambooHR's careers app is built with emotion/MUI, so most class names
# (fabric-xxxxxx, css-xxxxxx) are content hashes that can change on any
# BambooHR frontend deploy. The data-fabric-component attributes are far
# more stable since they describe the component's role rather than its
# generated style, so selectors lean on those wherever possible. Anything
# still keyed off a raw class name should be re-verified periodically.

JOB_LIST_SELECTOR = "a.fab-LinkUnstyled"
JOB_TITLE_SELECTOR = "[data-fabric-component='Headline']"
JOB_ROW_SELECTOR = "div[data-fabric-component='Flex']"
JOB_LABELBOX_SELECTOR = "div[data-fabric-component='LayoutBox']"
JOB_DESCRIPTION_SELECTOR = "section[data-fabric-component='Section']"

BAMBOOHR_JOB_TYPE_MAP = {
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
    # Dedupe while preserving order, since the listing markup can repeat
    # the same anchor inside nested wrapper elements.
    seen = set()
    urls = []
    for link in links:
        if link and link not in seen:
            seen.add(link)
            urls.append(link)
    return urls


def _clean_field(el) -> str | None:
    if not el:
        return None
    text = " ".join(el.inner_text().split())
    return text or None


def _extract_labeled_fields(page) -> dict:
    """The BambooHR job detail page lists Location, Department, Employment
    Type, Minimum Experience and Compensation as a series of Flex rows,
    each holding two LayoutBox children: a label and a value. There is no
    stable per-field selector, so this walks every row and builds a
    lowercased label to value map instead.
    """
    fields = {}
    rows = page.query_selector_all(JOB_ROW_SELECTOR)
    for row in rows:
        boxes = row.query_selector_all(JOB_LABELBOX_SELECTOR)
        if len(boxes) < 2:
            continue
        label = _clean_field(boxes[0])
        value = _clean_field(boxes[1])
        if label:
            fields[label.strip().lower()] = value
    return fields


def _extract_job_detail(page, job_url: str) -> dict | None:
    try:
        _goto(page, job_url)
        page.wait_for_selector(JOB_TITLE_SELECTOR, timeout=15000)
    except PlaywrightTimeoutError:
        return None

    title_el = page.query_selector(JOB_TITLE_SELECTOR)
    title = _clean_field(title_el)

    fields = _extract_labeled_fields(page)

    location = fields.get("location")
    locations = [location] if location else []

    raw_type = (fields.get("employment type") or "").lower().replace("-", " ")
    job_type = BAMBOOHR_JOB_TYPE_MAP.get(raw_type, "Fulltime")

    # Compensation is free text on BambooHR ("Competitive", a range, a
    # single figure, etc), so it is kept as-is rather than parsed into
    # salary_min/salary_max.
    salary_range = fields.get("compensation")

    description_el = page.query_selector(JOB_DESCRIPTION_SELECTOR)
    description_html = description_el.inner_html() if description_el else ""
    description_text = extract_text(description_html)

    print(f"DEBUG bamboohr job_url={job_url} title={title!r} locations={locations} raw_type={raw_type!r}")

    return {
        "job_url": job_url,
        "locations": locations,
        "job_type": job_type,
        "salary_range": salary_range,
        "salary_min": None,
        "salary_max": None,
        "job_name": title,
        "description": description_text,
    }


def fetch_jobs(slug: str, company_url: str) -> list[dict]:
    """Returns normalized job dicts ready for db.upsert_job. Empty list if the
    board is unreachable or has no postings.

    BambooHR has no public API for careers pages either, so this scrapes the
    listing page for job urls, then visits each job page for title,
    location, department, employment type and compensation. Compensation is
    free text on BambooHR (often just "Competitive"), so salary_min and
    salary_max always come back as None; the raw text is kept in
    salary_range for whatever downstream parsing wants to attempt.
    company_name comes from the company table since there's no API response
    to pull it from.
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
                detail["platform"] = "bamboohr"
                jobs.append(detail)
        finally:
            browser.close()
    return jobs