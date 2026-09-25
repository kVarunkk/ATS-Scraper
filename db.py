import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/jobs")


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def upsert_company(conn, slug: str, platform: str, company_url: str):
    """Insert a newly discovered company, or update it if it already exists."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into companies (slug, platform, company_url, last_checked_at)
            values (%s, %s, %s, now())
            on conflict (slug, platform)
            do update set
                company_url = excluded.company_url,
                last_checked_at = now(),
                is_active = true
            """,
            (slug, platform, company_url),
        )

def get_companies(conn, platform: str | None = None):
    """Return companies to scrape, optionally filtered to one platform."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if platform:
            cur.execute(
                "select id, slug, platform, company_url from companies where platform = %s and is_active = true",
                (platform,),
            )
        else:
            cur.execute("select id, slug, platform, company_url from companies where is_active = true")
        return cur.fetchall()


def upsert_job(conn, job: dict):
    """Insert a job, or update it in place if job_url already exists. Always marks it active."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into all_jobs (
                job_url, locations, job_type, salary_range, salary_min, salary_max,
                job_name, description, company_url, company_name, platform, status, updated_at
            ) values (
                %(job_url)s, %(locations)s, %(job_type)s, %(salary_range)s, %(salary_min)s, %(salary_max)s,
                %(job_name)s, %(description)s, %(company_url)s, %(company_name)s, %(platform)s, 'active', now()
            )
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
            """,
            job,
        )


def mark_missing_jobs_inactive(conn, company_url: str, seen_job_urls: list[str]):
    """Any job previously stored for this company that wasn't seen in the latest pull gets marked inactive."""
    with conn.cursor() as cur:
        if seen_job_urls:
            cur.execute(
                """
                update all_jobs
                set status = 'inactive', updated_at = now()
                where company_url = %s and status = 'active' and job_url <> all(%s)
                """,
                (company_url, seen_job_urls),
            )
        else:
            # No jobs returned at all this run -> everything for this company goes inactive.
            cur.execute(
                """
                update all_jobs
                set status = 'inactive', updated_at = now()
                where company_url = %s and status = 'active'
                """,
                (company_url,),
            )
