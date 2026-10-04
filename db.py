import os

import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/jobs")

MAX_DETAIL_ATTEMPTS = 5
GONE_DEACTIVATE_AFTER = 3      # consecutive 404s before a company is deactivated and its jobs closed
ERROR_DEACTIVATE_AFTER = 10    # consecutive errors before a company is deactivated (jobs untouched)


def get_connection(url: str | None = None):
    return psycopg2.connect(
        url or DATABASE_URL,
        connect_timeout=15,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )


# ---------------------------------------------------------------- companies

def upsert_companies(conn, rows: list[tuple[str, str, str]]):
    """rows: (slug, platform, company_url). Bulk upsert. Does NOT commit and
    does NOT reactivate deactivated companies."""
    unique = {(slug, platform): company_url for slug, platform, company_url in rows}
    data = [(slug, platform, url) for (slug, platform), url in unique.items()]
    if not data:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            insert into companies (slug, platform, company_url)
            values %s
            on conflict (slug, platform) do update set company_url = excluded.company_url
            """,
            data,
            page_size=1000,
        )


def get_companies_due(conn, platform: str) -> list[dict]:
    """Active companies, least recently scraped first. Never-scraped come first."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select id, slug, platform, company_url, company_name, close_holds
            from companies
            where platform = %s and is_active
            order by last_scraped_at asc nulls first, id
            """,
            (platform,),
        )
        return cur.fetchall()


def set_company_name(conn, company_id: int, name: str):
    with conn.cursor() as cur:
        cur.execute(
            "update companies set company_name = %s where id = %s and company_name is null",
            (name, company_id),
        )


def record_success(conn, company_id: int, close_holds: int = 0):
    with conn.cursor() as cur:
        cur.execute(
            """
            update companies
            set consecutive_failures = 0, close_holds = %s, last_scraped_at = now()
            where id = %s
            """,
            (close_holds, company_id),
        )


def record_failure(conn, company_id: int, gone: bool, error: str | None = None):
    """gone=True means the board returned 404. gone=False means an error.
    last_scraped_at is bumped either way so failing companies do not hog the front of the queue."""
    with conn.cursor() as cur:
        cur.execute(
            """
            update companies
            set last_error = %s, consecutive_failures = coalesce(consecutive_failures, 0) + 1, last_scraped_at = now()
            where id = %s
            returning consecutive_failures, company_url, platform
            """,
            ((error or "")[:1000] or None, company_id),
        )
        failures, company_url, platform = cur.fetchone()
        limit = GONE_DEACTIVATE_AFTER if gone else ERROR_DEACTIVATE_AFTER
        if failures >= limit:
            cur.execute("update companies set is_active = false where id = %s", (company_id,))
            if gone:
                cur.execute(
                    """
                    update jobs_state
                    set status = 'inactive', needs_detail = false, updated_at = now()
                    where company_url = %s and platform = %s and status = 'active'
                    """,
                    (company_url, platform),
                )
    conn.commit()


# --------------------------------------------------------------- jobs_state

def get_state(conn, company_url: str) -> dict[str, str]:
    """job_url -> status for every job we have ever seen for this company."""
    with conn.cursor() as cur:
        cur.execute("select job_url, status from jobs_state where company_url = %s", (company_url,))
        return dict(cur.fetchall())


def mark_inactive(conn, job_urls) -> int:
    urls = list(job_urls)
    if not urls:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            update jobs_state
            set status = 'inactive', needs_detail = false, updated_at = now()
            where job_url = any(%s) and status = 'active'
            """,
            (urls,),
        )
        return cur.rowcount


def upsert_state_with_payload(conn, rows: list[tuple[str, str, str]], payload_key: str):
    """Single-call platforms. rows: (job_url, company_url, platform).
    Covers both new jobs and reopened ones."""
    if not rows:
        return
    data = [(u, c, p, "active", payload_key, False, 0) for u, c, p in rows]
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            insert into jobs_state
                (job_url, company_url, platform, status, payload_key, needs_detail, detail_attempts, updated_at)
            values %s
            on conflict (job_url) do update set
                status = 'active',
                payload_key = excluded.payload_key,
                needs_detail = false,
                detail_attempts = 0,
                last_error = null,
                updated_at = now()
            """,
            data,
            template="(%s, %s, %s, %s, %s, %s, %s, now())",
            page_size=1000,
        )


def insert_pending(conn, rows: list[tuple[str, str, str]]):
    """Two-phase platforms, list step. rows: (job_url, company_url, platform).
    New URLs get needs_detail=true. Inactive rows that reappear are reopened and
    queued for a fresh detail fetch. Already-active rows are left untouched."""
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            insert into jobs_state
                (job_url, company_url, platform, status, payload_key, needs_detail, detail_attempts, updated_at)
            values %s
            on conflict (job_url) do update set
                status = 'active',
                needs_detail = true,
                detail_attempts = 0,
                last_error = null,
                updated_at = now()
            where jobs_state.status = 'inactive'
            """,
            rows,
            template="(%s, %s, %s, 'active', null, true, 0, now())",
            page_size=1000,
        )


def get_detail_backlog(conn, platform: str, limit: int) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select s.job_url, s.company_url, s.platform, c.slug, c.company_name
            from jobs_state s
            left join companies c on c.company_url = s.company_url and c.platform = s.platform
            where s.platform = %s and s.needs_detail and s.status = 'active'
              and s.detail_attempts < %s
            order by s.first_seen_at
            limit %s
            """,
            (platform, MAX_DETAIL_ATTEMPTS, limit),
        )
        return cur.fetchall()


def complete_details(conn, job_urls: list[str], payload_key: str):
    if not job_urls:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            update jobs_state
            set payload_key = %s, needs_detail = false, last_error = null, updated_at = now()
            where job_url = any(%s)
            """,
            (payload_key, job_urls),
        )


def mark_detail_failed(conn, job_url: str, error: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            update jobs_state
            set detail_attempts = detail_attempts + 1, last_error = %s
            where job_url = %s
            """,
            (error[:500], job_url),
        )


# ------------------------------------------------------------------- ingest

def get_watermark(conn, name: str):
    with conn.cursor() as cur:
        cur.execute(
            "select coalesce((select watermark from ingest_state where name = %s), 'epoch'::timestamptz)",
            (name,),
        )
        return cur.fetchone()[0]


def set_watermark(conn, name: str, value):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into ingest_state (name, watermark) values (%s, %s)
            on conflict (name) do update set watermark = excluded.watermark
            """,
            (name, value),
        )


def get_changed_state(conn, watermark) -> list[dict]:
    """Rows to push to the board: anything closed, or anything with a finished payload.
    A 5 minute overlap covers transactions that committed late. Upserts are idempotent."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select job_url, status, payload_key, updated_at
            from jobs_state
            where updated_at > %s - interval '5 minutes'
              and (status = 'inactive'
                   or (status = 'active' and needs_detail = false and payload_key is not null))
            order by updated_at
            """,
            (watermark,),
        )
        return cur.fetchall()