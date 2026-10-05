"""
Syncs a capped, balanced subset of jobs into the all_jobs table of the job board DB
(small DB, so storage is the constraint).

The scraper DB no longer needs an all_jobs table. Candidates are chosen from jobs_state
(small rows: url, company, platform, status, payload file, timestamp), and the full job
content is read from the GCS payload files only for the jobs that are actually written.

Strategy:
  1. Delete board jobs that are no longer active in jobs_state (hard delete frees space).
  2. Keep everything else (sticky, so board listings do not churn between runs).
  3. Compute a fair per-platform quota by water-filling: small platforms keep all they have,
     unused quota is redistributed to big platforms.
  4. Add new jobs up to each platform's quota, newest first, with a per-company cap so one
     huge employer cannot flood the board. If the board is full, evict the oldest jobs
     from platforms that are over quota.
  5. Refresh kept jobs whose state changed (reopened, or platform label differs).
  6. Cap the number of adds so the projected DB size, including embeddings that
     are still to be generated, stays under --max-db-mb.

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
from storage import read_jsonl_gz

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


DEFAULT_EMBEDDING_BYTES = 12_300  # 3072-dim vector; used only if no row has an embedding yet


def board_size_stats(conn) -> dict:
    """Database size plus what is needed to project the cost of a new row, embedding included."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            select pg_database_size(current_database()) as db_bytes,
                   pg_total_relation_size('public.all_jobs') as table_bytes,
                   count(*) as n_rows,
                   count(embedding_new) as n_embedded,
                   coalesce(avg(pg_column_size(embedding_new)), 0) as emb_bytes
            from public.all_jobs
            """
        )
        return {k: float(v) for k, v in cur.fetchone().items()}


def max_adds_for_size(st: dict, ceiling_mb: float) -> tuple[int, float]:
    """How many new rows fit under the size ceiling.

    Counts the embeddings that existing rows are still waiting for, and gives every new row the
    cost of its text and indexes plus its own future embedding. Dead space from earlier deletes
    inflates the per-row figure, so the estimate errs on the cautious side.
    """
    emb = st["emb_bytes"] or DEFAULT_EMBEDDING_BYTES
    n_rows = max(st["n_rows"], 1)
    base_per_row = max((st["table_bytes"] - st["n_embedded"] * emb) / n_rows, 0)
    pending_embeddings = (st["n_rows"] - st["n_embedded"]) * emb
    headroom = ceiling_mb * 1024 * 1024 - st["db_bytes"] - pending_embeddings
    per_new_row = base_per_row + emb
    return max(0, int(headroom // per_new_row)), per_new_row


def source_live(conn, urls) -> dict:
    """url -> {updated_at, platform, payload_key} for the given urls still active in jobs_state."""
    live = {}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for part in chunks(urls, 2000):
            cur.execute(
                """
                select job_url, updated_at, platform, payload_key
                from jobs_state where status = 'active' and job_url = any(%s)
                """,
                (part,),
            )
            for r in cur.fetchall():
                live[r["job_url"]] = r
    return live


def source_platforms(conn) -> list[str]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "select distinct platform from jobs_state where status = 'active' and payload_key is not null"
        )
        return [r["platform"] for r in cur.fetchall()]


def source_candidates(conn, platform: str, per_company_cap: int, limit: int) -> list[dict]:
    """Newest first, at most per_company_cap per company. Only jobs whose content is in a payload file."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            select job_url, updated_at, payload_key from (
                select job_url, updated_at, payload_key,
                       row_number() over (partition by company_url order by updated_at desc) as rn
                from jobs_state
                where status = 'active' and platform = %s and payload_key is not null
            ) t
            where rn <= %s
            order by updated_at desc
            limit %s
            """,
            (platform, per_company_cap, limit),
        )
        return cur.fetchall()


def load_rows(key: str, wanted: dict) -> list[dict]:
    """Read one GCS payload file and return board rows for the wanted urls (url -> updated_at)."""
    rows = []
    for rec in read_jsonl_gz(key):
        url = rec.get("job_url")
        if url in wanted:
            row = {f: rec.get(f) for f in FIELDS}
            row["locations"] = row["locations"] or []
            row["updated_at"] = wanted[url]
            rows.append(row)
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
        stats = board_size_stats(board)
    size_mb = stats["db_bytes"] / 1024 / 1024
    log.info("board: %d rows (%d embedded), db size %.1f MB",
             len(board_rows), int(stats["n_embedded"]), size_mb)

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
        if live[u]["payload_key"]
        and (newer(live[u]["updated_at"], board_rows[u]["updated_at"])
             or live[u]["platform"] != board_rows[u]["platform"])
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
    # Size cap: never plan more rows than fit under the DB size ceiling, embeddings included
    max_adds, per_new_row = max_adds_for_size(stats, max_db_mb)
    effective_limit = min(limit, len(keep) + max_adds)
    log.info("size: ~%.1f KB per new row with its embedding, ceiling %.0f MB -> room for %d new rows",
             per_new_row / 1024, max_db_mb, max_adds)
    if effective_limit < limit:
        log.warning("size ceiling caps the board at %d rows (limit was %d)", effective_limit, limit)
    quota = fair_quota(available, effective_limit)

    adds: list[dict] = []
    for p in platforms:
        want = max(0, quota[p] - len(kept_by_platform.get(p, [])))
        adds += new_by_platform.get(p, [])[:want]

    # Evict oldest jobs from over-quota platforms, only as many as the adds need
    free = effective_limit - len(keep)
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

    # Group everything to write by payload file, so each file is downloaded once.
    want_by_key: dict[str, dict] = {}
    for c in adds:
        want_by_key.setdefault(c["payload_key"], {})[c["job_url"]] = c["updated_at"]
    for u in refresh:
        want_by_key.setdefault(live[u]["payload_key"], {})[u] = live[u]["updated_at"]
    total_writes = sum(len(w) for w in want_by_key.values())

    written = 0
    # Files with the most wanted jobs first, so a budget cut still lands as many jobs as possible.
    for key, wanted in sorted(want_by_key.items(), key=lambda kv: -len(kv[1])):
        if deadline.expired():
            log.info("time budget reached with %d of %d writes left, resuming next run",
                     total_writes - written, total_writes)
            break
        try:
            rows = load_rows(key, wanted)  # GCS download and parsing, no DB connection held
        except Exception as e:
            log.warning("%s: could not read payload (%s), those jobs stay queued", key, e)
            continue
        if len(rows) < len(wanted):
            log.warning("%s: %d expected jobs not found in payload", key, len(wanted) - len(rows))
        for r in rows:
            r["job_type"] = map_job_type(r["job_type"])  # NOT NULL enum on the board
        for part in chunks(rows, WRITE_CHUNK):
            with common.db_conn(board_url) as board:
                with board.cursor() as cur:
                    # Tell the insert trigger not to fire one HTTP request per row; a rate-limited
                    # background worker (embed_pending_jobs) embeds new rows instead.
                    cur.execute("set local app.skip_embedding = 'on'")
                    execute_values(cur, UPSERT_SQL, part, template=TEMPLATE)
        written += len(wanted)

    # Safety net: the plan should make this impossible, so fail loudly if it is ever wrong.
    with common.db_conn(board_url) as board:
        total = count_scraped(board)
    if total > limit:
        raise RuntimeError(f"board holds {total} scraped jobs, over the limit of {limit}")

    log.info("done: board holds %d scraped jobs (%d/%d writes done)", total, written, total_writes)
    if unmapped:
        log.warning("job_type values not in the enum, defaulted: %s", unmapped.most_common(10))

    # Phase 5: fill normalized_locations for the new rows (runs only if some are missing)
    with common.db_conn(board_url) as board:
        with board.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("select exists (select 1 from all_jobs where normalized_locations is null) as pending")
            if cur.fetchone()["pending"]:
                cur.execute("set local statement_timeout = '15min'")
                cur.execute(f"select {NORMALIZE_FN}()")
                log.info("normalized job locations (%s)", NORMALIZE_FN)
            else:
                log.info("no jobs need location normalization")


if __name__ == "__main__":
    common.setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20000, help="max listings on the job board")
    parser.add_argument("--per-company-cap", type=int, default=50, help="max listings per company")
    parser.add_argument("--max-db-mb", type=float, default=430, help="skip adds above this DB size")
    parser.add_argument("--budget-minutes", type=float, default=45, help="stop writing after this long")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.limit, args.per_company_cap, args.max_db_mb, args.dry_run, args.budget_minutes)

    # test: python sync_to_board.py --limit 20000 --dry-run