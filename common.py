import inspect
import logging
import time
from contextlib import contextmanager
import db

MAX_CLOSE_HOLDS = 2


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class Deadline:
    def __init__(self, minutes: float):
        self.end = time.monotonic() + minutes * 60

    def expired(self) -> bool:
        return time.monotonic() >= self.end


def close_is_suspicious(active_count: int, missing_count: int) -> bool:
    """Closing more than half of a 10+ job company in one go looks like a bad fetch."""
    return active_count >= 10 and missing_count > active_count * 0.5


def decide_close(holds: int, active_count: int, missing_count: int) -> tuple[bool, int]:
    """Returns (should_close, new_holds).

    A suspicious mass close is held for MAX_CLOSE_HOLDS consecutive runs. If the same
    jobs are still missing after that, it is treated as real (layoffs, a batch close)
    so a company is never stuck in limbo forever."""
    if missing_count == 0:
        return False, 0
    if close_is_suspicious(active_count, missing_count) and holds < MAX_CLOSE_HOLDS:
        return False, holds + 1
    return True, 0


def call_fetch_jobs(module, company: dict):
    """Passes company_name to adapters that accept it, so they can skip the extra request."""
    fn = module.fetch_jobs
    if "company_name" in inspect.signature(fn).parameters:
        return fn(company["slug"], company["company_url"], company_name=company.get("company_name"))
    return fn(company["slug"], company["company_url"])


@contextmanager
def db_conn(url: str | None = None):
    conn = db.get_connection(url) if url else db.get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()        
