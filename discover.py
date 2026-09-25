"""
One-off discovery run: queries Common Crawl's CDX index for each target ATS platform,
extracts company slugs, and upserts them into the local `companies` table.

Usage:
    python discover.py                     # all platforms
    python discover.py --platform ashby    # just one
"""
import argparse
import time
import requests
import json
from urllib.parse import urlparse

from db import get_connection, upsert_company

COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"

# Each platform can have multiple historical domains (e.g. Greenhouse migrated hosts at one point).
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
    # todo: playwright scraping
    "bamboohr": ["bamboohr.com"]
}

EXCLUDE_SLUGS = {"embed", "api", "static", "assets", "favicon.ico"}


def get_latest_crawl_id() -> str:
    resp = requests.get(COLLINFO_URL, timeout=20)
    resp.raise_for_status()
    collections = resp.json()
    return collections[0]["id"]  # most recent crawl is first in the list


def query_cdx(domain: str, crawl_id: str) -> set[str]:
    """Returns the set of company slugs found under this domain in this crawl."""
    base_url = f"https://index.commoncrawl.org/{crawl_id}-index"
    slugs = set()

    # Find out how many pages of results exist first.
    resp = requests.get(base_url, params={
        "url": f"{domain}*", "output": "json", "showNumPages": "true",
    }, timeout=30)
    if resp.status_code != 200:
        print(f"  [{domain}] index query failed ({resp.status_code}), skipping")
        return slugs
    num_pages = resp.json().get("pages", 1)

    for page in range(num_pages):
        for attempt in range(5):
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
            continue  # gave up on this page after 5 attempts

        for line in resp.text.strip().splitlines():
            try:
                url = __import__("json").loads(line)["url"]
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
        for attempt in range(5):
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

        time.sleep(0.5)  # be polite to the CDX API

    return slugs

def run(platforms: list[str]):
    crawl_id = get_latest_crawl_id()
    print(f"Using crawl: {crawl_id}\n")

    conn = get_connection()
    try:
        for platform in platforms:
            if platform in PLATFORM_DOMAINS:
                for domain in PLATFORM_DOMAINS[platform]:
                    print(f"Querying {domain} ...")
                    slugs = query_cdx(domain, crawl_id)
                    print(f"  found {len(slugs)} slugs")

                    for slug in slugs:
                        company_url = f"https://{domain}/{slug}"
                        upsert_company(conn, slug, platform, company_url)
                    conn.commit()

            elif platform in SUBDOMAIN_PLATFORM_DOMAINS:
                for domain in SUBDOMAIN_PLATFORM_DOMAINS[platform]:
                    print(f"Querying *.{domain} ...")
                    slugs = query_cdx_subdomain(domain, crawl_id)
                    print(f"  found {len(slugs)} slugs")

                    for slug in slugs:
                        suffix = "/careers" if platform == "bamboohr" else ""
                        company_url = f"https://{slug}.{domain}{suffix}"
                        upsert_company(conn, slug, platform, company_url)
                    conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    all_platforms = list(PLATFORM_DOMAINS.keys()) + list(SUBDOMAIN_PLATFORM_DOMAINS.keys())

    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=all_platforms, default=None)
    args = parser.parse_args()

    target_platforms = [args.platform] if args.platform else all_platforms
    run(target_platforms)
