# ATS Scraper

A Python job-scraping pipeline that discovers companies via Common Crawl and scrapes public job listings from multiple Applicant Tracking Systems (ATSs). The project stores results in Postgres with `pgvector` support and is designed for incremental, low-noise updates instead of re-scraping everything on every run.

## Why this project exists

This codebase is built to:

- discover company slugs and ATS URLs using Common Crawl indexes
- scrape job boards for several ATS providers
- deduplicate and diff job updates against prior runs
- mark vanished jobs as inactive instead of silently losing history
- store normalized job records in a schema compatible with downstream syncs

It is useful for building a local or production-ready job data feed for hiring research, recruiting intelligence, or ATS coverage analysis.

## Supported ATS platforms

The project currently supports both single-call and two-phase ATS integrations.

### Single-call platforms

These adapters return a full job list in one request:

- Ashby
- Greenhouse
- Lever
- Rippling
- Workable
- Recruitee
- Personio

### Two-phase platforms

These require a listing step followed by a detail fetch per job URL:

- JazzHR
- BambooHR
- Jobvite
- Workday

## Project layout

```text
.
├── adapters/                 # ATS-specific scraping adapters
├── utils/                   # utility helpers
├── discover.py              # Common Crawl company discovery
├── scrape_fast.py           # single-call ATS scraper
├── scrape_list_2phase.py    # two-phase ATS listing scraper
├── scrape_detail_2phase.py # two-phase detail scraper
├── db.py                    # database queries and state tracking
├── registry.py              # platform-to-adapter registry
├── common.py                # shared DB/HTTP helpers and retry logic
├── init.sql                 # Postgres schema and enum setup
├── docker-compose.yml       # Postgres + pgvector container setup
├── requirements.txt         # Python dependencies
├── .env.example             # example environment configuration
├── storage.py               # payload write batching logic
├── sync_to_board.py         # optional sync/export helper
├── prune_scraper_db.py      # DB maintenance helper
└── README.md
```

## Features

- Common Crawl-based company discovery for ATS domains
- Platform-specific adapter architecture for quick expansion
- Incremental job sync using state tracking in Postgres
- Automatic marking of removed jobs as inactive
- Support for `pgvector` and schema-compatible `all_jobs` collection
- Batch payload writes for efficient ingestion
- Per-platform scheduling and budget-based scraping runs

## Prerequisites

- Python 3.10+
- Docker Desktop or Docker Engine
- Postgres 16 via the included `pgvector` image

## Quick start

### 1) Start the database

```bash
docker compose up -d
```

The first start runs `init.sql` automatically. If you have an existing local database and need to reinitialize it manually, run:

```bash
docker exec -i job_scraper_pg psql -U postgres -d jobs < init.sql
```

### 2) Create a virtual environment and install dependencies

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### 3) Configure environment variables

Example `.env`:

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/jobs
```

The Docker Compose service exposes Postgres on port `5433` to avoid collisions with local default Postgres installs.

## Discovery workflow

Discovery finds company slugs and ATS URLs by querying Common Crawl's CDX index. It is meant to be run periodically (for example, monthly) to identify newly onboarded companies.

```bash
python discover.py
python discover.py --platform ashby
python discover.py --platform greenhouse
```

## Scraping workflow

### Single-call platforms

```bash
python scrape_fast.py --platform greenhouse --budget-minutes 60
python scrape_fast.py --platform lever --budget-minutes 60
python scrape_fast.py --budget-minutes 60
```

This script fetches jobs, compares them to the existing state, writes new or reopened records, and marks missing jobs as inactive when appropriate.

### Two-phase platforms

Step 1: queue job URLs for later detail fetches.

```bash
python scrape_list_2phase.py --platform jazzhr --budget-minutes 50
```

Step 2: fetch the detail payloads for queued jobs.

```bash
python scrape_detail_2phase.py --platform jazzhr --budget-minutes 15
```

## Database schema

The database is initialized from `init.sql`. The important tables are:

- `companies`
  - stores discovered companies and platform metadata
  - keyed by `(slug, platform)`
  - tracks `first_discovered_at`, `last_checked_at`, and `is_active`

- `all_jobs`
  - canonical job records for all scraped positions
  - includes job metadata such as `job_url`, `job_name`, `company_name`, `locations`, `salary_range`, `experience`, `status`, and `embedding_new`
  - uses Postgres enums for `job_type` and `job_posting_status`

The schema also enables `pgvector` for future embedding-based enrichment or semantic search.

## Recommended scheduling

Use a two-part cadence:

- `discover.py`: run occasionally to detect newly onboarded companies
- `scrape_*`: run more frequently to keep listings fresh and updated

Example cron entries:

```cron
0 3 1 * *  cd /path/to/job-scraper && venv/bin/python discover.py >> discover.log 2>&1
0 4 * * *  cd /path/to/job-scraper && venv/bin/python scrape_fast.py --platform greenhouse --budget-minutes 60 >> scrape.log 2>&1
```

## Known limitations

This is a version 1 pipeline and there are several intentional gaps:

- Greenhouse boards do not consistently expose reliable employment type or compensation metadata, so some fields default to common v1 values.
- Lever may not return a company display name in all cases; some records fall back to the slug or a best-effort name.
- Common Crawl discovery is a sample of indexed pages rather than a guaranteed complete company list.
- No enrichment step is included yet for `ai_summary`, embedding generation, salary normalization, equity parsing, or visa requirements.
- Some platform enum defaults in `init.sql` may need to be adapted to match your production schema exactly.

## Extending the project

To add a new ATS:

1. Create a new adapter under `adapters/`
2. Register it in `registry.py`
3. Add any schema or normalization logic needed in the data model
4. Validate against a small set of real companies before production use

## License

This project is currently distributed without a formal license notice. If you plan to deploy or redistribute it, confirm ownership and add an explicit license file before publishing externally.

## Notes

This repository was intentionally built as a practical scraper pipeline rather than a polished SaaS app. It is best treated as a data-collection foundation that can be extended for downstream analytics, enrichment, and syncing.
