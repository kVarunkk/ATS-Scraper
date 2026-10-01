# """
# One-off scrape run: for every company in the `companies` table, calls the matching
# platform adapter, upserts returned jobs into `all_jobs`, and marks any previously-active
# job for that company that no longer appears as inactive.

# Usage:
#     python scrape.py                     # all platforms
#     python scrape.py --platform ashby    # just one
# """
# import argparse
# import time

# from db import get_connection, get_companies, upsert_job, mark_missing_jobs_inactive
# from adapters import ashby, greenhouse, lever, rippling, workable, recruitee, personio, jobvite, jazzhr, bamboohr, workday

# ADAPTERS = {
#     "ashby": ashby.fetch_jobs,
#     "greenhouse": greenhouse.fetch_jobs,
#     "lever": lever.fetch_jobs,
#     "rippling": rippling.fetch_jobs,
#     "workable": workable.fetch_jobs,
#     "recruitee": recruitee.fetch_jobs,
#     "personio": personio.fetch_jobs,
#     "jobvite": jobvite.fetch_jobs,
#     "jazzhr": jazzhr.fetch_jobs,
#     "bamboohr": bamboohr.fetch_jobs,
#     "workday": workday.fetch_jobs
# }


# def run(platforms: list[str]):
#     conn = get_connection()
#     total_jobs = 0
#     total_companies = 0

#     try:
#         for platform in platforms:
#             fetch_jobs = ADAPTERS[platform]
#             companies = get_companies(conn, platform=platform)
#             print(f"\n{platform}: {len(companies)} companies to scrape")

#             for company in companies:
#                 slug = company["slug"]
#                 company_url = company["company_url"]

#                 try:
#                     jobs = fetch_jobs(slug, company_url)
#                 except Exception as e:
#                     print(f"  [{platform}/{slug}] failed: {e}")
#                     continue

#                 for job in jobs:
#                     if not job.get("job_url"):
#                         continue  # can't upsert without the unique key
#                     upsert_job(conn, job)

#                 seen_urls = [j["job_url"] for j in jobs if j.get("job_url")]
#                 mark_missing_jobs_inactive(conn, company_url, seen_urls)
#                 conn.commit()

#                 total_companies += 1
#                 total_jobs += len(jobs)
#                 print(f"  [{platform}/{slug}] {len(jobs)} jobs")

#                 time.sleep(1)  # light rate limiting per request
#     finally:
#         conn.close()

#     print(f"\nDone. {total_companies} companies scraped, {total_jobs} jobs seen.")


# if __name__ == "__main__":
#     parser = argparse.ArgumentParser()
#     parser.add_argument("--platform", choices=list(ADAPTERS.keys()), default=None)
#     args = parser.parse_args()

#     target_platforms = [args.platform] if args.platform else list(ADAPTERS.keys())
#     run(target_platforms)
