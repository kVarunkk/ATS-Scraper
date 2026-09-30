"""Which adapters belong to which script.

SINGLE_CALL modules expose:
    fetch_jobs(slug, company_url[, company_name]) -> list[dict] | None
TWO_PHASE modules expose:
    list_jobs(company_url) -> list[str] | None
    fetch_detail(job_url) -> dict | None
Contract for all: None means gone, [] means a real empty board, exceptions mean failure.
"""
from adapters import (
    ashby, greenhouse, lever, rippling, workable,
    recruitee, personio, jobvite, jazzhr, bamboohr, workday,
)

SINGLE_CALL = {
    "ashby": ashby,
    "greenhouse": greenhouse,
    "lever": lever,
    "rippling": rippling,
    "workable": workable,
    "recruitee": recruitee,
    "personio": personio,
}

TWO_PHASE = {
    "jazzhr": jazzhr,
    "bamboohr": bamboohr,
    "jobvite": jobvite,
    "workday": workday,
}