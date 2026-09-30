"""
Two-phase platforms, step 1: listing only.

Per company:
  list job URLs -> diff against jobs_state -> insert NEW urls with needs_detail=true
  (reopened urls are re-queued for a fresh detail fetch) -> mark vanished urls inactive
  (guarded). Already-active rows are never touched.

Usage:
    python scrape_list_2phase.py --platform jazzhr --budget-minutes 50
"""
import argparse
import logging
import time

import common
import db
import registry

log = logging.getLogger("scrape_list")


def process_company(conn, company: dict, module) -> str:
    company_url = company["company_url"]
    platform = company["platform"]

    urls = module.list_jobs(company_url)  # raises on failure
    if urls is None:
        db.record_failure(conn, company["id"], gone=True)
        return "gone"

    fetched = set(urls)
    state = db.get_state(conn, company_url)
    active = {u for u, s in state.items() if s == "active"}

    to_queue = [u for u in fetched if state.get(u) != "active"]  # new + reopened
    missing = active - fetched

    db.insert_pending(conn, [(u, company_url, platform) for u in to_queue])

    should_close, holds = common.decide_close(company.get("close_holds", 0), len(active), len(missing))
    if should_close:
        db.mark_inactive(conn, missing)
    elif missing:
        log.warning("[%s/%s] holding close of %d/%d active jobs (hold %d)",
                    platform, company["slug"], len(missing), len(active), holds)

    db.record_success(conn, company["id"], close_holds=holds)
    conn.commit()
    return f"{len(fetched)} listed, {len(to_queue)} queued, {len(missing)} missing"


def run(platforms: list[str], budget_minutes: float, delay: float):
    deadline = common.Deadline(budget_minutes)
    conn = db.get_connection()
    try:
        for platform in platforms:
            module = registry.TWO_PHASE[platform]
            companies = db.get_companies_due(conn, platform)
            log.info("%s: %d companies due", platform, len(companies))
            done = 0
            for company in companies:
                if deadline.expired():
                    log.info("%s: time budget reached after %d companies", platform, done)
                    break
                try:
                    result = process_company(conn, company, module)
                    log.info("[%s/%s] %s", platform, company["slug"], result)
                except Exception as e:
                    conn.rollback()
                    log.warning("[%s/%s] failed: %s", platform, company["slug"], e)
                    db.record_failure(conn, company["id"], gone=False)
                done += 1
                time.sleep(delay)
    finally:
        conn.close()


if __name__ == "__main__":
    common.setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=list(registry.TWO_PHASE), default=None)
    parser.add_argument("--budget-minutes", type=float, default=50)
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between companies")
    args = parser.parse_args()
    run([args.platform] if args.platform else list(registry.TWO_PHASE), args.budget_minutes, args.delay)