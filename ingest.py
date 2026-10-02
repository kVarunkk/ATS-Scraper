"""
Pushes changes from jobs_state + R2 payloads into the board table (all_jobs).

  1. read jobs_state rows changed since the watermark
  2. group active rows by payload_key, download each R2 file once, upsert matching jobs
  3. mark newly inactive jobs inactive on the board
  4. advance the watermark only after everything succeeded

Safe to re-run: every write is an idempotent upsert.

Usage:
    python ingest.py
"""
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


def _to_row(rec: dict) -> dict:
    row = {k: rec.get(k) for k in FIELDS}
    row["locations"] = row["locations"] or []
    return row

def run():
    with common.db_conn() as conn:
        watermark = db.get_watermark(conn, "board")
        changed = db.get_changed_state(conn, watermark)

    if not changed:
        log.info("nothing to ingest")
        return

    board_url = None if db.BOARD_DATABASE_URL == db.DATABASE_URL else db.BOARD_DATABASE_URL

    inactive = [r["job_url"] for r in changed if r["status"] == "inactive"]
    by_key: dict[str, set[str]] = defaultdict(set)
    for r in changed:
        if r["status"] == "active":
            by_key[r["payload_key"]].add(r["job_url"])

    upserted = 0
    for key, wanted in by_key.items():
        # R2 download and parsing: no DB connection held
        batch = [_to_row(rec) for rec in read_jsonl_gz(key) if rec.get("job_url") in wanted]
        found = {r["job_url"] for r in batch}
        if wanted - found:
            log.warning("%s: %d expected jobs not found in payload", key, len(wanted - found))

        with common.db_conn(board_url) as board_conn:
            with board_conn.cursor() as cur:
                for i in range(0, len(batch), CHUNK):
                    execute_values(cur, UPSERT_SQL, batch[i:i + CHUNK], template=TEMPLATE)
        upserted += len(batch)

    closed = 0
    with common.db_conn(board_url) as board_conn:
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

    with common.db_conn() as conn:
        db.set_watermark(conn, "board", max(r["updated_at"] for r in changed))

    log.info("ingested %d jobs, closed %d", upserted, closed)


if __name__ == "__main__":
    common.setup_logging()
    run()