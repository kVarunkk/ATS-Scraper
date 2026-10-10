"""
Discovery run: queries Common Crawl's CDX index for each target ATS platform,
extracts company slugs, and upserts them into the Postgres `companies` table.
Meant to run monthly. Already-known companies keep their state; deactivated
companies are NOT reactivated here.

Usage:
    python discover.py                    # all platforms
    python discover.py --platform ashby   # just one
"""
import argparse
import time
import requests
import json
from urllib.parse import urlparse
import common
from db import upsert_companies

COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"

# Each platform can have multiple historical domains
PLATFORM_DOMAINS = {
    "ashby": ["jobs.ashbyhq.com"],
    "greenhouse": ["job-boards.greenhouse.io", "boards.greenhouse.io"],
    # todo: companies cant be discovered but job api exists
    "lever": ["jobs.lever.co"],
    "rippling": ["ats.rippling.com"],
    "workable": ["apply.workable.com"],
    "jobvite": ["jobs.jobvite.com"]
}

SUBDOMAIN_PLATFORM_DOMAINS = {
    "jazzhr": ["applytojob.com"],
    "recruitee": ["recruitee.com"],
    "personio": ["jobs.personio.de"],
    "bamboohr": ["bamboohr.com"],
    "workday": ["wd1.myworkdayjobs.com", "wd3.myworkdayjobs.com", "wd5.myworkdayjobs.com"],
    "keka": ["keka.com"],
    # "eightfoldai": ["eightfold.ai"]
}

EXCLUDE_SLUGS = {"embed", "api", "static", "assets", "favicon.ico"}
ATTEMPTS=10


def get_latest_crawl_id() -> str:
    resp = requests.get(COLLINFO_URL, timeout=20)
    resp.raise_for_status()
    collections = resp.json()
    return collections[0]["id"]  # most recent crawl is first in the list


def query_cdx(domain: str, crawl_id: str) -> set[str]:
    """Returns the set of company slugs found under this domain in this crawl."""
    base_url = f"https://index.commoncrawl.org/{crawl_id}-index"
    slugs = set()

    resp = requests.get(base_url, params={
        "url": f"{domain}*", "output": "json", "showNumPages": "true",
    }, timeout=30)
    if resp.status_code != 200:
        print(f"  [{domain}] index query failed ({resp.status_code}), skipping")
        return slugs
    num_pages = resp.json().get("pages", 1)

    for page in range(num_pages):
        for attempt in range(ATTEMPTS):
            try:
                resp = requests.get(base_url, params={
                    "url": f"{domain}*", "output": "json", "fl": "url", "page": page,
                }, timeout=60)
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                print(f"  [{domain}] page {page} attempt {attempt + 1} failed: {e}")
                time.sleep(2 ** attempt)
        else:
            continue  

        for line in resp.text.strip().splitlines():
            try:
                url = json.loads(line)["url"]
                path = url.split(f"{domain}/")[1].split("?")[0]
                slug = path.split("/")[0].strip().lower()
                if slug and slug not in EXCLUDE_SLUGS and "." not in slug:
                    slugs.add(slug)
            except (IndexError, KeyError, ValueError):
                continue

        time.sleep(0.5)  # be polite to the CDX API

    return slugs


def query_cdx_subdomain(domain: str, crawl_id: str) -> set[str]:
    """Returns the set of company slugs found as subdomains of this domain in this crawl."""
    base_url = f"https://index.commoncrawl.org/{crawl_id}-index"
    slugs = set()

    resp = requests.get(base_url, params={
        "url": domain, "matchType": "domain", "output": "json", "showNumPages": "true",
    }, timeout=30)
    if resp.status_code != 200:
        print(f"  [{domain}] index query failed ({resp.status_code}), skipping")
        return slugs
    num_pages = resp.json().get("pages", 1)

    for page in range(num_pages):
        for attempt in range(ATTEMPTS):
            try:
                resp = requests.get(base_url, params={
                    "url": domain, "matchType": "domain", "output": "json", "fl": "url", "page": page,
                }, timeout=60)
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                print(f"  [{domain}] page {page} attempt {attempt + 1} failed: {e}")
                time.sleep(2 ** attempt)
        else:
            continue

        for line in resp.text.strip().splitlines():
            try:
                url = json.loads(line)["url"]
                host = urlparse(url).netloc.lower()
                if not host.endswith(f".{domain}"):
                    continue
                slug = host[: -(len(domain) + 1)]
                if slug and slug not in EXCLUDE_SLUGS and "." not in slug and slug != "www":
                    slugs.add(slug)
            except (IndexError, KeyError, ValueError):
                continue

        time.sleep(0.5)

    return slugs

def query_workday_urls(domain: str, crawl_id: str) -> set[str]:
    """Returns the set of full workday career base URLs found before '/job'."""
    base_url = f"https://index.commoncrawl.org/{crawl_id}-index"
    career_urls = set()

    resp = requests.get(base_url, params={
        "url": domain, "matchType": "domain", "output": "json", "showNumPages": "true",
    }, timeout=30)
    if resp.status_code != 200:
        print(f"  [{domain}] index query failed ({resp.status_code}), skipping")
        return career_urls
    num_pages = resp.json().get("pages", 1)

    for page in range(num_pages):
        for attempt in range(ATTEMPTS):
            try:
                resp = requests.get(base_url, params={
                    "url": domain, "matchType": "domain", "output": "json", "fl": "url", "page": page,
                }, timeout=60)
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                print(f"  [{domain}] page {page} attempt {attempt + 1} failed: {e}")
                time.sleep(2 ** attempt)
        else:
            continue

        for line in resp.text.strip().splitlines():
            try:
                url = json.loads(line)["url"]
                if "/job/" in url:
                    base_career_url = url.split("/job/")[0]
                    career_urls.add(base_career_url)
            except (IndexError, KeyError, ValueError):
                continue

        time.sleep(0.5)

    return career_urls


def save(label: str, rows: list[tuple[str, str, str]]):
    """Bulk upsert one batch in its own short connection, so earlier work survives a later crash."""
    with common.db_conn() as conn:   # commits on success, rolls back on error
        upsert_companies(conn, rows)
    print(f"  [{label}] saved {len(rows)} companies")


def run(platforms: list[str]):
    crawl_id = get_latest_crawl_id()
    print(f"Using crawl: {crawl_id}\n")

    for platform in platforms:
        if platform in PLATFORM_DOMAINS:
            for domain in PLATFORM_DOMAINS[platform]:
                print(f"Querying {domain} ...")
                slugs = query_cdx(domain, crawl_id)          # slow, no connection held
                print(f"  found {len(slugs)} slugs")
                rows = [(slug, platform, f"https://{domain}/{slug}") for slug in slugs]
                save(domain, rows)

        elif platform == "workday":
            for domain in SUBDOMAIN_PLATFORM_DOMAINS["workday"]:
                print(f"Querying Workday *.{domain} ...")
                career_urls = query_workday_urls(domain, crawl_id)
                print(f"  found {len(career_urls)} career base URLs")
                rows = [
                    (urlparse(company_url).netloc.split(".")[0], platform, company_url)
                    for company_url in career_urls
                ]
                save(domain, rows)

        elif platform in SUBDOMAIN_PLATFORM_DOMAINS:
            for domain in SUBDOMAIN_PLATFORM_DOMAINS[platform]:
                print(f"Querying *.{domain} ...")
                slugs = query_cdx_subdomain(domain, crawl_id)
                print(f"  found {len(slugs)} slugs")
                suffix = "/careers" if platform == "bamboohr" or platform == "keka" or platform == "eightfoldai" else ""
                rows = [(slug, platform, f"https://{slug}.{domain}{suffix}") for slug in slugs]
                save(domain, rows)


if __name__ == "__main__":
    all_platforms = list(PLATFORM_DOMAINS.keys()) + list(SUBDOMAIN_PLATFORM_DOMAINS.keys())

    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=all_platforms, default=None)
    args = parser.parse_args()

    target_platforms = [args.platform] if args.platform else all_platforms
    run(target_platforms)

    # test: python discover.py --platform greenhouse