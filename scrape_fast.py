"""
Single-call platforms: one request returns the full job.

Per company:
  fetch -> diff against jobs_state -> buffer only NEW or REOPENED jobs for R2
  -> mark jobs that vanished as inactive (guarded) -> update company bookkeeping.
Unchanged jobs cause no writes at all.

Usage:
    python scrape_fast.py --platform greenhouse --budget-minutes 50
"""
import argparse
import logging
import time

import common
import db
import registry
from storage import PayloadWriter

log = logging.getLogger("scrape_fast")

def process_company(company: dict, module, writer: PayloadWriter) -> str:
    company_url = company["company_url"]
    platform = company["platform"]

    jobs = common.call_fetch_jobs(module, company)  # network call, no DB connection held; raises on failure

    if jobs is None:
        with common.db_conn() as conn:
            db.record_failure(conn, company["id"], gone=True, error="board not found (404)")
        return "gone"

    # Pure Python work, done before touching the DB
    fetched = {j["job_url"]: j for j in jobs if j.get("job_url")}
    name = company.get("company_name") or next(
        (j["company_name"] for j in jobs if j.get("company_name")), None
    ) or company["slug"]

    with common.db_conn() as conn:
        state = db.get_state(conn, company_url)
        active = {u for u, s in state.items() if s == "active"}

        to_write = [u for u in fetched if state.get(u) != "active"]  # new + reopened
        missing = active - fetched.keys()

        should_close, holds = common.decide_close(
            company.get("close_holds", 0), len(active), len(missing)
        )
        if should_close:
            db.mark_inactive(conn, missing)
        elif missing:
            log.warning("[%s/%s] holding close of %d/%d active jobs (hold %d)",
                        platform, company["slug"], len(missing), len(active), holds)

        # Only store a real name, never the slug fallback, so a later run can still improve it.
        if not company.get("company_name") and name != company["slug"]:
            db.set_company_name(conn, company["id"], name)

        db.record_success(conn, company["id"], close_holds=holds)
        # commit happens when the with block exits

    # Buffer payloads only after the DB work has committed
    for url in to_write:
        payload = dict(fetched[url])
        payload["company_url"] = company_url
        payload["platform"] = platform
        payload["company_name"] = payload.get("company_name") or name
        writer.add(payload)

    return f"{len(fetched)} fetched, {len(to_write)} to write, {len(missing)} missing"


def run(platforms: list[str], budget_minutes: float, delay: float):
    deadline = common.Deadline(budget_minutes)

    for platform in platforms:
        module = registry.SINGLE_CALL[platform]

        with common.db_conn() as conn:
            companies = db.get_companies_due(conn, platform)
        log.info("%s: %d companies due", platform, len(companies))

        writer = PayloadWriter(platform, mode="insert")
        done = 0
        try:
            for company in companies:
                if deadline.expired():
                    log.info("%s: time budget reached after %d companies", platform, done)
                    break
                try:
                    result = process_company(company, module, writer)
                    log.info("[%s/%s] %s", platform, company["slug"], result)
                except Exception as e:
                    log.warning("[%s/%s] failed: %s", platform, company["slug"], e)
                    try:
                        with common.db_conn() as conn:
                            db.record_failure(conn, company["id"], gone=False, error=f"{type(e).__name__}: {e}")
                    except Exception:
                        log.exception("[%s/%s] could not record failure", platform, company["slug"])
                done += 1

                if writer.should_flush():
                    try:
                        writer.flush()
                    except Exception:
                        log.exception("%s: flush failed, records stay buffered", platform)
                time.sleep(delay)
        finally:
            try:
                writer.flush()
            except Exception:
                log.exception("%s: final flush failed", platform)


if __name__ == "__main__":
    common.setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=list(registry.SINGLE_CALL), default=None)
    parser.add_argument("--budget-minutes", type=float, default=50)
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between companies")
    args = parser.parse_args()
    run([args.platform] if args.platform else list(registry.SINGLE_CALL), args.budget_minutes, args.delay)


    # test: python scrape_fast.py --platform greenhouse --budget-minutes 5