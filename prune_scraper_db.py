"""
Keeps the scraper DB small by deleting jobs_state rows for jobs that closed a while ago.

The scraper DB only needs to remember what it has seen (jobs_state, about 0.4 KB per row).
Full job content lives in the GCS payload files and on the job board, not here.

Inactive rows older than --retention-days (default 60) are removed. If a removed job ever
comes back, the scraper simply treats it as a new job and scrapes it again. The board sync
treats a job that is missing from jobs_state as gone, so nothing else depends on these rows.

Deletes run in small batches, each in its own short connection.

Usage:
    python prune_scraper_db.py --dry-run
    python prune_scraper_db.py
"""
import argparse
import logging
from datetime import datetime, timedelta, timezone

import common

log = logging.getLogger("prune")

BATCH = 5000

KEYS = """
    select job_url from jobs_state
    where status = 'inactive'
      and updated_at < %(cutoff)s
"""


def table_size() -> str:
    with common.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("select pg_size_pretty(pg_total_relation_size('jobs_state'))")
            return cur.fetchone()[0]


def count_rows(params: dict) -> int:
    with common.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"select count(*) from ({KEYS}) t", params)
            return cur.fetchone()[0]


def delete_rows(params: dict) -> int:
    total = 0
    while True:
        with common.db_conn() as conn:  # short connection per batch, commits on exit
            with conn.cursor() as cur:
                cur.execute(
                    f"delete from jobs_state where job_url in ({KEYS} limit %(batch)s)",
                    {**params, "batch": BATCH},
                )
                n = cur.rowcount
        total += n
        log.info("jobs_state: deleted %d so far", total)
        if n < BATCH:
            return total


def run(retention_days: int, dry_run: bool):
    params = {"cutoff": datetime.now(timezone.utc) - timedelta(days=retention_days)}
    log.info("jobs_state size before: %s", table_size())

    n = count_rows(params)
    log.info("jobs_state: %d inactive rows older than %d days", n, retention_days)

    if dry_run:
        log.info("dry run, nothing deleted")
        return
    if n:
        delete_rows(params)

    # Deleted rows free space for reuse. The file only shrinks after a VACUUM FULL.
    log.info("jobs_state size after: %s", table_size())


if __name__ == "__main__":
    common.setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--retention-days", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.retention_days, args.dry_run)

    # test: python prune_scraper_db.py --dry-run