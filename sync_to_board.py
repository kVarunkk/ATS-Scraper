"""
Syncs a capped, balanced subset of all_jobs from the scraping DB into the
all_jobs table of the job board DB (small DB, so storage is the constraint).

Strategy:
  1. Delete board jobs that are no longer active in the scraping DB (hard delete frees space).
  2. Keep everything else (sticky, so board listings do not churn between runs).
  3. Compute a fair per-platform quota by water-filling: small platforms keep all they have,
     unused quota is redistributed to big platforms.
  4. Add new jobs up to each platform's quota, newest first, with a per-company cap so one
     huge employer cannot flood the board. If the board is full, evict the oldest jobs
     from platforms that are over quota.
  5. Refresh kept jobs whose content changed in the scraping DB.
  6. Refuse to add anything if the board DB is already above a size ceiling.

Only rows with is_platform_job = false are ever read, counted, updated or deleted, so the
board's own platform jobs are never touched. The cap is enforced by the plan (adds only fill
free slots, a hard trim handles a lowered limit) plus a final count check that fails loudly.
Writes are committed in chunks and stop at --budget-minutes; the next run resumes from the
board's current state.

Required env var:
    JOB_BOARD_DATABASE_URL   connection string of the job board DB

Usage:
    python sync_to_board.py --limit 20000
    python sync_to_board.py --dry-run
"""
import argparse
import logging
import os
import re
from collections import Counter

from psycopg2.extras import RealDictCursor, execute_values

import common

log = logging.getLogger("sync_board")

FIELDS = [
    "job_url", "locations", "job_type", "salary_range", "salary_min", "salary_max",
    "job_name", "description", "company_url", "company_name", "platform",
]
COLS = ", ".join(FIELDS)

UPSERT_SQL = f"""
insert into all_jobs ({COLS}, status, updated_at) values %s
on conflict (job_url) do update set
    locations = excluded.locations,
    job_type = excluded.job_type,
    salary_range = excluded.salary_range,
    salary_min = excluded.salary_min,
    salary_max = excluded.salary_max,
    job_name = excluded.job_name,
    description = excluded.description,
    company_name = excluded.company_name,
    company_url = excluded.company_url,
    platform = excluded.platform,
    status = 'active',
    updated_at = excluded.updated_at
where all_jobs.is_platform_job = false
"""
TEMPLATE = "(" + ", ".join(f"%({f})s" for f in FIELDS) + ", 'active', %(updated_at)s)"


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_job_types(conn) -> list[str]:
    """The labels the board's job_type_enum actually accepts."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("select unnest(enum_range(null::public.job_type_enum))::text as v")
        return [r["v"] for r in cur.fetchall()]


# raw-text keyword groups, checked in order, each mapped to the first enum label containing one of its words
JOB_TYPE_GROUPS = [
    ("intern",),
    ("part",),
    ("contract", "freelance", "temp", "seasonal"),
    ("full",),
]


def make_job_type_mapper(labels: list[str], default: str = "Fulltime"):
    """Returns (mapper, unmapped). The mapper turns any scraped job_type string into a valid enum label."""
    by_norm = {_norm(label): label for label in labels}
    fallback = default if default in labels else labels[0]
    unmapped: Counter = Counter()

    def label_containing(word):
        return next((lab for n, lab in by_norm.items() if word in n), None)

    def mapper(raw):
        n = _norm(raw)
        if not n:
            return fallback
        if n in by_norm:
            return by_norm[n]
        for group in JOB_TYPE_GROUPS:
            if any(word in n for word in group):
                for word in group:
                    hit = label_containing(word)
                    if hit:
                        return hit
        unmapped[raw] += 1
        return fallback

    return mapper, unmapped


WRITE_CHUNK = 500  # rows per upsert; each chunk is committed on its own
NORMALIZE_FN = "normalize_all_job_locations_test"  # SQL function that fills normalized_locations


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def read_board(conn) -> dict:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "select job_url, platform, updated_at, status from all_jobs "
            "where is_platform_job = false and job_url is not null"
        )
        return {r["job_url"]: r for r in cur.fetchall()}


def board_size_mb(conn) -> float:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("select pg_database_size(current_database()) / 1024.0 / 1024.0 as mb")
        return float(cur.fetchone()["mb"])


def source_live(conn, urls) -> dict:
    """url -> {updated_at, platform} for the given urls that are still active in the scraping DB."""
    live = {}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for part in chunks(urls, 2000):
            cur.execute(
                "select job_url, updated_at, platform from all_jobs where status = 'active' and job_url = any(%s)",
                (part,),
            )
            for r in cur.fetchall():
                live[r["job_url"]] = r
    return live


def source_platforms(conn) -> list[str]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("select distinct platform from all_jobs where status = 'active'")
        return [r["platform"] for r in cur.fetchall()]


def source_candidates(conn, platform: str, per_company_cap: int, limit: int) -> list[dict]:
    """Lightweight rows (no description), newest first, at most per_company_cap per company."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            select job_url, updated_at from (
                select job_url, updated_at,
                       row_number() over (partition by company_url order by updated_at desc) as rn
                from all_jobs
                where status = 'active' and platform = %s
            ) t
            where rn <= %s
            order by updated_at desc
            limit %s
            """,
            (platform, per_company_cap, limit),
        )
        return cur.fetchall()


def fetch_full(conn, urls, map_job_type) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"select {COLS}, updated_at from all_jobs where job_url = any(%s)",
            (list(urls),),
        )
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["locations"] = r["locations"] or []
        r["job_type"] = map_job_type(r["job_type"])  # NOT NULL enum on the board
    return rows


def fair_quota(available: dict[str, int], limit: int) -> dict[str, int]:
    """Water-filling: split `limit` evenly across platforms, redistributing unused share."""
    quota = {}
    remaining = limit
    left = len(available)
    for platform, avail in sorted(available.items(), key=lambda kv: kv[1]):
        share = remaining // left if left else 0
        q = min(avail, share)
        quota[platform] = q
        remaining -= q
        left -= 1
    return quota


def newer(src_ts, board_ts) -> bool:
    return src_ts is not None and (board_ts is None or src_ts > board_ts)


def count_scraped(conn) -> int:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("select count(*) as n from all_jobs where is_platform_job = false")
        return cur.fetchone()["n"]


def run(limit: int, per_company_cap: int, max_db_mb: float, dry_run: bool, budget_minutes: float):
    deadline = common.Deadline(budget_minutes)
    board_url = os.environ.get("JOB_BOARD_DATABASE_URL")
    if not board_url:
        raise SystemExit("JOB_BOARD_DATABASE_URL is not set")

    # Connections are opened only for the work that needs them and closed straight after.
    # All planning below is plain Python with no connection held.

    # Phase 1: read the board
    with common.db_conn(board_url) as board:
        board_rows = read_board(board)
        map_job_type, unmapped = make_job_type_mapper(load_job_types(board))
        size_mb = board_size_mb(board)
    log.info("board: %d rows, db size %.1f MB", len(board_rows), size_mb)

    # Phase 2: read the scraping DB (one short connection for every read)
    with common.db_conn() as src:
        live = source_live(src, list(board_rows))
        candidates = {
            p: source_candidates(src, p, per_company_cap, limit)
            for p in source_platforms(src)
        }

    # Phase 3: plan (no connections)
    keep = {u for u, r in board_rows.items() if u in live and r["status"] == "active"}
    dead = [u for u in board_rows if u not in keep]

    kept_by_platform: dict[str, list] = {}
    for u in keep:
        # group by the scraping DB's platform, so stale labels on old board rows do not skew quotas
        kept_by_platform.setdefault(live[u]["platform"], []).append((board_rows[u]["updated_at"], u))
    for p in kept_by_platform:
        kept_by_platform[p].sort(key=lambda t: (t[0] is not None, t[0]))  # oldest first

    refresh = [
        u for u in keep
        if newer(live[u]["updated_at"], board_rows[u]["updated_at"])
        or live[u]["platform"] != board_rows[u]["platform"]
    ]

    new_by_platform = {
        p: [c for c in cands if c["job_url"] not in keep]
        for p, cands in candidates.items()
    }

    platforms = set(new_by_platform) | set(kept_by_platform)
    available = {
        p: len(kept_by_platform.get(p, [])) + len(new_by_platform.get(p, []))
        for p in platforms
    }
    quota = fair_quota(available, limit)

    adds: list[str] = []
    for p in platforms:
        want = max(0, quota[p] - len(kept_by_platform.get(p, [])))
        adds += [c["job_url"] for c in new_by_platform.get(p, [])[:want]]

    # Size guard: never grow a DB that is already near its ceiling
    if size_mb > max_db_mb and adds:
        log.warning("board DB is %.1f MB (ceiling %.1f MB), skipping %d adds", size_mb, max_db_mb, len(adds))
        adds = []

    # Evict oldest jobs from over-quota platforms, only as many as the adds need
    free = limit - len(keep)
    need_evict = max(0, len(adds) - free)
    evict: list[str] = []
    if need_evict:
        over = []
        for p, items in kept_by_platform.items():
            excess = len(items) - quota.get(p, 0)
            if excess > 0:
                over += items[:excess]  # items are oldest first
        over.sort(key=lambda t: (t[0] is not None, t[0]))
        evict = [u for _, u in over[:need_evict]]

    # Hard cap: if still over the limit (for example the limit was lowered), trim the oldest
    projected = len(keep) - len(evict) + len(adds)
    if projected > limit:
        already = set(evict)
        oldest = sorted(
            (t for items in kept_by_platform.values() for t in items if t[1] not in already),
            key=lambda t: (t[0] is not None, t[0]),
        )
        evict += [u for _, u in oldest[: projected - limit]]

    log.info(
        "plan: delete %d dead, evict %d, add %d, refresh %d, keep %d | quota %s",
        len(dead), len(evict), len(adds), len(refresh), len(keep) - len(evict), dict(sorted(quota.items())),
    )
    if dry_run:
        log.info("dry run, no changes written")
        return

    # Phase 4: apply. Deletes are committed first, then each upsert chunk gets its own short
    # pair of connections. Adds were planned to fit the free slots, so the board never exceeds
    # the cap at any point, and a run stopped by the budget or a crash resumes on the next run.
    with common.db_conn(board_url) as board:
        with board.cursor() as cur:
            for part in chunks(dead + evict, 1000):
                cur.execute(
                    "delete from all_jobs where job_url = any(%s) and is_platform_job = false",
                    (part,),
                )
    # committed here, connection closed

    to_write = adds + refresh
    written = 0
    for part in chunks(to_write, WRITE_CHUNK):
        if deadline.expired():
            log.info("time budget reached with %d of %d writes left, resuming next run",
                     len(to_write) - written, len(to_write))
            break
        with common.db_conn() as src:
            rows = fetch_full(src, part, map_job_type)
        with common.db_conn(board_url) as board:
            with board.cursor() as cur:
                # Tell the insert trigger not to fire one HTTP request per row; a rate-limited
                # background worker (embed_pending_jobs) embeds new rows instead.
                cur.execute("set local app.skip_embedding = 'on'")
                execute_values(cur, UPSERT_SQL, rows, template=TEMPLATE)
        written += len(part)

    # Safety net: the plan should make this impossible, so fail loudly if it is ever wrong.
    with common.db_conn(board_url) as board:
        total = count_scraped(board)
    if total > limit:
        raise RuntimeError(f"board holds {total} scraped jobs, over the limit of {limit}")

    log.info("done: board holds %d scraped jobs (%d/%d writes done)", total, written, len(to_write))
    if unmapped:
        log.warning("job_type values not in the enum, defaulted: %s", unmapped.most_common(10))

    # Phase 5: fill normalized_locations for the new rows (runs only if some are missing)
    with common.db_conn(board_url) as board:
        with board.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("select exists (select 1 from all_jobs where normalized_locations is null) as pending")
            if cur.fetchone()["pending"]:
                cur.execute("set local statement_timeout = '15min'")
                cur.execute(f"select {NORMALIZE_FN}()")
            else:
                log.info("no jobs need location normalization")


if __name__ == "__main__":
    common.setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20000, help="max listings on the job board")
    parser.add_argument("--per-company-cap", type=int, default=50, help="max listings per company")
    parser.add_argument("--max-db-mb", type=float, default=430, help="skip adds above this DB size")
    parser.add_argument("--budget-minutes", type=float, default=20, help="stop writing after this long")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.limit, args.per_company_cap, args.max_db_mb, args.dry_run, args.budget_minutes)

    # test: python sync_to_board.py --limit 20000 --dry-run