# Job Scraper v1 — Ashby, Greenhouse, Lever

## Setup

1. Start Postgres (pgvector included):
   ```
   docker compose up -d
   ```
   `init.sql` runs automatically the first time (empty data volume). If you already have a
   running container from a previous attempt, apply it manually instead:
   ```
   docker exec -i job_scraper_pg psql -U postgres -d jobs < init.sql
   ```

2. Install dependencies:
   ```
   python -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```

## Usage

Run discovery first (finds company slugs via Common Crawl):
```
python discover.py                  # all three platforms
python discover.py --platform ashby # just one
```

Then scrape jobs for every discovered company:
```
python scrape.py
python scrape.py --platform lever
```

Run `discover.py` occasionally (e.g. monthly) to catch newly onboarded companies.
Run `scrape.py` more often (e.g. daily) to keep job listings current — set these up as two
separate cron entries once you're ready to automate:

```
0 3 1 * *  cd /path/to/job-scraper && venv/bin/python discover.py >> discover.log 2>&1
0 4 * * *  cd /path/to/job-scraper && venv/bin/python scrape.py   >> scrape.log 2>&1
```

## Known limitations / v1 shortcuts worth knowing about

- **Greenhouse**: no per-board employment-type field, so `job_type` defaults to `Fulltime`
  for every Greenhouse job. Salary is also left null — Greenhouse's pay-transparency data
  isn't consistently available across boards without deeper per-posting parsing.
- **Lever**: no company display name in the API response, so `company_name` falls back to
  the slug. Salary is populated only for companies that have Lever's compensation
  transparency feature turned on.
- **Common Crawl discovery** only finds what Common Crawl actually indexed — it's a sample,
  not exhaustive. Run discovery across a few different crawl dates and merge if you want
  better coverage (see discover.py's `get_latest_crawl_id`, easy to extend to loop over
  several recent crawl IDs).
- No enrichment yet (`ai_summary`, `embedding_new`, `experience_min/max`, `equity_range`,
  `visa_requirement` are all left null/default) — that's a separate pass on top of this data.
- `job_type_enum` and `job_posting_status` values in `init.sql` are guesses at reasonable
  defaults. Swap them for your actual Supabase enum values before you build the sync step,
  so the two schemas stay identical.
