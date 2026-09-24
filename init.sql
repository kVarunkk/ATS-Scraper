-- Runs automatically the first time the container starts with an empty data volume.
-- If you already have a running container, apply this manually instead:
--   docker exec -i job_scraper_pg psql -U postgres -d jobs < init.sql

create extension if not exists pgcrypto;
create extension if not exists vector;

do $$ begin
  create type job_type_enum as enum ('Fulltime', 'Intern', 'Contract');
exception
  when duplicate_object then null;
end $$;

do $$ begin
  create type job_posting_status as enum ('active', 'inactive', 'expired');
exception
  when duplicate_object then null;
end $$;

-- Companies discovered via Common Crawl (or supplied manually). One row per company+platform.
create table if not exists companies (
  id uuid not null default gen_random_uuid(),
  slug text not null,
  platform text not null,
  company_url text not null,
  first_discovered_at timestamp with time zone not null default now(),
  last_checked_at timestamp with time zone null,
  is_active boolean not null default true,
  constraint companies_pkey primary key (id),
  constraint companies_slug_platform_key unique (slug, platform)
);

-- Mirrors your Supabase all_jobs table so the schema is portable between local and prod.
create table if not exists all_jobs (
  id uuid not null default gen_random_uuid(),
  created_at timestamp with time zone not null default now(),
  updated_at timestamp with time zone null default now(),
  job_url text null,
  locations text[] not null default '{}'::text[],
  job_type job_type_enum not null default 'Fulltime'::job_type_enum,
  visa_requirement text null,
  salary_range text null,
  salary_min double precision null,
  salary_max double precision null,
  equity_range text null,
  equity_min double precision null,
  equity_max double precision null,
  job_name text null,
  experience text null,
  experience_min bigint not null default '0'::bigint,
  experience_max bigint null,
  company_url text null,
  description text null,
  company_name text null,
  platform text null,
  embedding_updated_at timestamp with time zone null,
  status job_posting_status not null default 'active'::job_posting_status,
  normalized_locations text[] null,
  ai_summary text null,
  embedding_new vector null,
  is_platform_job boolean not null default false,
  constraint all_jobs_pkey primary key (id),
  constraint all_jobs_job_url_key unique (job_url)
);

create index if not exists all_jobs_platform_idx on all_jobs (platform);
create index if not exists all_jobs_status_idx on all_jobs (status);
create index if not exists all_jobs_company_url_idx on all_jobs (company_url);
