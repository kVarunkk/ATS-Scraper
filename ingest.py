"""
Pushes changes from jobs_state + R2 payloads into the board table (all_jobs).

  1. read jobs_state rows changed since the watermark, oldest first
  2. work through them in time-ordered windows; for each window:
       group active rows by payload_key, download each file once, upsert matching jobs,
       mark newly inactive jobs inactive, then advance the watermark to the end of the window
  3. stop between windows when the time budget runs out; the next run resumes from the watermark

Safe to re-run: every write is an idempotent upsert, and the watermark only moves past
windows that fully succeeded.

Usage:
    python ingest.py --budget-minutes 40
"""
import argparse
import logging
from collections import defaultdict

from psycopg2.extras import execute_values

import common
import db
from storage import read_jsonl_gz

log = logging.getLogger("ingest")

FIELDS = [
    "job_url", "locations", "job_type", "salary_range", "salary_min", "salary_max",
    "job_name", "description", "company_url", "company_name", "platform",
]

UPSERT_SQL = """
insert into all_jobs (
    job_url, locations, job_type, salary_range, salary_min, salary_max,
    job_name, description, company_url, company_name, platform, status, updated_at
) values %s
on conflict (job_url) do update set
    locations = excluded.locations,
    job_type = excluded.job_type,
    salary_range = excluded.salary_range,
    salary_min = excluded.salary_min,
    salary_max = excluded.salary_max,
    job_name = excluded.job_name,
    description = excluded.description,
    company_name = excluded.company_name,
    status = 'active',
    updated_at = now()
"""
TEMPLATE = (
    "(%(job_url)s, %(locations)s, %(job_type)s, %(salary_range)s, %(salary_min)s, %(salary_max)s, "
    "%(job_name)s, %(description)s, %(company_url)s, %(company_name)s, %(platform)s, 'active', now())"
)

CHUNK = 500
WINDOW_ROWS = 5000  # changed rows per window; the watermark advances after each one


def _to_row(rec: dict) -> dict:
    row = {k: rec.get(k) for k in FIELDS}
    row["locations"] = row["locations"] or []
    return row


def windows(changed: list[dict], size: int):
    """Split time-ordered rows into windows, never splitting rows that share an updated_at.

    Keeping equal timestamps together matters because the watermark is a single timestamp:
    ending a window in the middle of a tie could skip the rest of the tie on the next run.
    """
    i, n = 0, len(changed)
    while i < n:
        j = min(i + size, n)
        while j < n and changed[j]["updated_at"] == changed[j - 1]["updated_at"]:
            j += 1
        yield changed[i:j]
        i = j


def process_window(window: list[dict]) -> tuple[int, int]:
    inactive = [r["job_url"] for r in window if r["status"] == "inactive"]
    by_key: dict[str, set[str]] = defaultdict(set)
    for r in window:
        if r["status"] == "active":
            by_key[r["payload_key"]].add(r["job_url"])

    upserted = 0
    for key, wanted in by_key.items():
        # R2 download and parsing: no DB connection held
        batch = [_to_row(rec) for rec in read_jsonl_gz(key) if rec.get("job_url") in wanted]
        found = {r["job_url"] for r in batch}
        if wanted - found:
            log.warning("%s: %d expected jobs not found in payload", key, len(wanted - found))

        with common.db_conn() as board_conn:
            with board_conn.cursor() as cur:
                for i in range(0, len(batch), CHUNK):
                    execute_values(cur, UPSERT_SQL, batch[i:i + CHUNK], template=TEMPLATE)
        upserted += len(batch)

    closed = 0
    with common.db_conn() as board_conn:
        with board_conn.cursor() as cur:
            for i in range(0, len(inactive), 1000):
                cur.execute(
                    """
                    update all_jobs set status = 'inactive', updated_at = now()
                    where job_url = any(%s) and status <> 'inactive'
                    """,
                    (inactive[i:i + 1000],),
                )
                closed += cur.rowcount

    return upserted, closed


def run(budget_minutes: float):
    deadline = common.Deadline(budget_minutes)

    with common.db_conn() as conn:
        watermark = db.get_watermark(conn, "board")
        changed = db.get_changed_state(conn, watermark)

    if not changed:
        log.info("nothing to ingest")
        return

    changed = sorted(changed, key=lambda r: r["updated_at"])
    total_upserted = total_closed = 0
    done_rows = 0

    for window in windows(changed, WINDOW_ROWS):
        if deadline.expired():
            log.info("time budget reached with %d of %d changed rows left, resuming next run",
                     len(changed) - done_rows, len(changed))
            break

        upserted, closed = process_window(window)

        # Advance only past this window, and only after every write in it succeeded.
        with common.db_conn() as conn:
            db.set_watermark(conn, "board", window[-1]["updated_at"])

        total_upserted += upserted
        total_closed += closed
        done_rows += len(window)

    log.info("ingested %d jobs, closed %d (%d/%d changed rows processed)",
             total_upserted, total_closed, done_rows, len(changed))


if __name__ == "__main__":
    common.setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget-minutes", type=float, default=40)
    args = parser.parse_args()
    run(args.budget_minutes)