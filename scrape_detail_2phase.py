"""
Two-phase platforms, step 2: drain the needs_detail backlog.

Per job:
  fetch detail -> buffer payload -> one R2 write per flush -> needs_detail=false.
  Detail returns None (job gone)  -> mark inactive.
  Detail raises                   -> detail_attempts + 1, retried on a later run
                                     until MAX_DETAIL_ATTEMPTS (see db.py), then parked.

Usage:
    python scrape_detail_2phase.py --platform jazzhr --budget-minutes 15
"""
import argparse
import logging
import time

import common
import db
import registry
from storage import PayloadWriter

log = logging.getLogger("scrape_detail")


def run(platforms: list[str], budget_minutes: float, batch_size: int, delay: float):
    deadline = common.Deadline(budget_minutes)
    conn = db.get_connection()
    try:
        for platform in platforms:
            module = registry.TWO_PHASE[platform]
            backlog = db.get_detail_backlog(conn, platform, batch_size)
            log.info("%s: %d jobs in detail backlog (batch)", platform, len(backlog))
            writer = PayloadWriter(conn, platform, mode="detail")
            ok = gone = failed = 0
            try:
                for row in backlog:
                    if deadline.expired():
                        log.info("%s: time budget reached", platform)
                        break
                    job_url = row["job_url"]
                    try:
                        detail = module.fetch_detail(job_url)
                    except Exception as e:
                        conn.rollback()
                        db.mark_detail_failed(conn, job_url, str(e))
                        conn.commit()
                        failed += 1
                        log.warning("[%s] detail failed %s: %s", platform, job_url, e)
                        time.sleep(delay)
                        continue

                    if detail is None:
                        db.mark_inactive(conn, [job_url])
                        conn.commit()
                        gone += 1
                    else:
                        payload = dict(detail)
                        payload["job_url"] = job_url  # keep the exact key stored in jobs_state
                        payload["company_url"] = row["company_url"]
                        payload["platform"] = platform
                        payload["company_name"] = row.get("company_name") or row.get("slug")
                        writer.add(payload)
                        ok += 1
                        if writer.should_flush():
                            writer.flush()
                    time.sleep(delay)
            finally:
                writer.flush()
            log.info("%s: %d ok, %d gone, %d failed", platform, ok, gone, failed)
    finally:
        conn.close()


if __name__ == "__main__":
    common.setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=list(registry.TWO_PHASE), default=None)
    parser.add_argument("--budget-minutes", type=float, default=15)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between detail requests")
    args = parser.parse_args()
    run(
        [args.platform] if args.platform else list(registry.TWO_PHASE),
        args.budget_minutes,
        args.batch_size,
        args.delay,
    )