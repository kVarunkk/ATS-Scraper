"""Google Cloud Storage access plus a buffered writer that batches jobs into one jsonl.gz per flush.

Required env vars:
    GCS_BUCKET               bucket name
    GCS_CREDENTIALS_JSON     full service account key JSON as a string
                             (or leave unset and set GOOGLE_APPLICATION_CREDENTIALS to a key file path)
"""
import gzip
import io
import json
import os
import time
import uuid

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage as gcs
from google.oauth2 import service_account

import common
import db

_client = None


def get_client():
    global _client
    if _client is None:
        creds_json = os.environ.get("GCS_CREDENTIALS_JSON")
        if creds_json:
            creds = service_account.Credentials.from_service_account_info(json.loads(creds_json))
            _client = gcs.Client(credentials=creds, project=creds.project_id)
        else:
            _client = gcs.Client()
    return _client


def _bucket():
    return get_client().bucket(os.environ["GCS_BUCKET"])


def put_jsonl_gz(key: str, records: list[dict]):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for rec in records:
            gz.write((json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
    try:
        # Keys are unique per flush, so if_generation_match=0 makes the upload
        # safe to retry. A 412 means a retry landed after the first attempt
        # actually succeeded, so the object is already ours.
        _bucket().blob(key).upload_from_string(
            buf.getvalue(), content_type="application/gzip", if_generation_match=0
        )
    except PreconditionFailed:
        pass


def read_jsonl_gz(key: str):
    data = _bucket().blob(key).download_as_bytes()
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        for line in gz:
            line = line.strip()
            if line:
                yield json.loads(line)

class PayloadWriter:
    """Buffers full job payloads and writes them to R2 in batches.
 
    Order on flush: R2 upload first, then the jobs_state update, then commit.
    If the process dies before flush, the buffered jobs are simply rediscovered
    on the next run (single-call) or stay needs_detail=true (two-phase).
 
    mode="insert": single-call platforms, inserts or reopens jobs_state rows.
    mode="detail": two-phase detail step, completes existing pending rows.
    """
 
    def __init__(self, platform: str, mode: str, flush_jobs: int = 500, flush_seconds: int = 1200):
        if mode not in ("insert", "detail"):
            raise ValueError(mode)
        # self.conn = conn
        self.platform = platform
        self.mode = mode
        self.flush_jobs = flush_jobs
        self.flush_seconds = flush_seconds
        self.run_id = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
        self.part = 0
        self.records: dict[str, dict] = {}
        self.buffer_started: float | None = None
 
    def add(self, payload: dict):
        """payload must include job_url, company_url and platform."""
        if not self.records:
            self.buffer_started = time.monotonic()
        self.records[payload["job_url"]] = payload
 
    def should_flush(self) -> bool:
        if not self.records:
            return False
        if len(self.records) >= self.flush_jobs:
            return True
        return time.monotonic() - (self.buffer_started or time.monotonic()) >= self.flush_seconds
 
    def flush(self):
        if not self.records:
            return
        key = f"jobs/{self.platform}/{self.run_id}-{self.part:03d}.jsonl.gz"
        records = list(self.records.values())
 
        put_jsonl_gz(key, records)

        with common.db_conn() as conn:
            if self.mode == "insert":
                db.upsert_state_with_payload(
                    conn,
                    [(r["job_url"], r["company_url"], r["platform"]) for r in records],
                    key,
                )
            else:
                db.complete_details(conn, [r["job_url"] for r in records], key)
        # conn.commit()
 
        self.part += 1
        self.records = {}
        self.buffer_started = None
      