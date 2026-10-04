import re
from urllib.parse import urlsplit
import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from utils.extract_text import extract_text

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
})


class ParseError(Exception):
    """Page loaded but did not look like what we expect (layout change, block page)."""

def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _get_json(url: str) -> dict | None:
    """None on 404. Raises on any other failure."""
    resp = session.get(url, headers={"Accept": "application/json"}, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        raise ParseError(f"Invalid JSON from {url}")


def _careers_base(company_url: str) -> str | None:
    """https://acme.bamboohr.com/careers (any path after the host is ignored)."""
    parts = urlsplit(company_url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}/careers"


# ---------- Phase 1 ----------

def list_jobs(company_url: str) -> list[str] | None:
    """One request returns every open job. Job URL = <careers base>/<id>."""
    base = _careers_base(company_url)
    if not base:
        return None

    data = _get_json(f"{base}/list")
    if data is None:
        return None

    results = data.get("result")
    if not isinstance(results, list):
        raise ParseError(f"Unexpected list payload at {base}/list")

    return sorted({f"{base}/{j['id']}" for j in results if j.get("id")})


# ---------- Phase 2 ----------

_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?")


def _parse_compensation(text: str | None) -> tuple[str | None, float | None, float | None]:
    """'$36 - 43 / hour' -> ('$36 - 43 / hour', 36.0, 43.0). Values are NOT annualized."""
    if not text or not text.strip():
        return None, None, None
    raw = text.strip()
    nums = []
    for m in _NUM.finditer(raw):
        v = float(m.group(1).replace(",", ""))
        if m.group(2):
            v *= 1000
        nums.append(v)
    if not nums:
        return raw, None, None
    pair = nums[:2]
    return raw, min(pair), max(pair)


def _build_location(opening: dict) -> list[str]:
    loc = opening.get("location") or {}
    ats = opening.get("atsLocation") or {}

    # Remote roles often leave `location` empty and fill `atsLocation` instead.
    parts = [loc.get("city"), loc.get("state"), loc.get("addressCountry")]
    if not any(parts):
        parts = [ats.get("city"), ats.get("state") or ats.get("province"), ats.get("country")]

    parts = [p.strip() for p in parts if p and p.strip()]
    return [", ".join(parts)] if parts else []


def fetch_detail(job_url: str) -> dict | None:
    """None when the job is gone (404 or no longer open)."""
    data = _get_json(f"{job_url.rstrip('/')}/detail")
    if data is None:
        return None

    opening = (data.get("result") or {}).get("jobOpening")
    if not opening:
        raise ParseError(f"No jobOpening in detail payload for {job_url}")

    if opening.get("jobOpeningStatus") not in (None, "Open"):
        return None

    salary_range, salary_min, salary_max = _parse_compensation(opening.get("compensation"))

    return {
        "job_name": opening.get("jobOpeningName"),
        "description": extract_text(opening.get("description", "") or ""), 
        "job_type": _map_job_type(opening.get("employmentType").lower() if opening.get("employmentType") else None),
        "locations": _build_location(opening),
        "salary_range": salary_range,
        "salary_min": salary_min,
        "salary_max": salary_max,
    }

def _map_job_type(raw: str | None) -> str:
    mapping = {
        "full-time": "Fulltime",
        "part-time": "Intern",
        "contractor": "Contract",
        "intern": "Intern",
        "temporary": "Contract",
    }
    return mapping.get(raw or "", "Fulltime")    