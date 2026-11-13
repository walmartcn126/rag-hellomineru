"""SQLite 表结构（启动时 CREATE TABLE IF NOT EXISTS）。"""

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    page_count INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    document_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    ext TEXT NOT NULL,
    engine TEXT NOT NULL,
    runner TEXT NOT NULL,
    options_json TEXT NOT NULL,
    resolved_options_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT,
    progress REAL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    worker_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    error TEXT,
    warnings_json TEXT,
    result_markdown_path TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_document ON jobs(document_id);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    hostname TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'online',
    last_heartbeat TEXT,
    current_job_id TEXT,
    version TEXT,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotations (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    annotator TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    total REAL NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, annotator)
);

CREATE INDEX IF NOT EXISTS idx_annotations_job ON annotations(job_id);

CREATE TABLE IF NOT EXISTS annotation_items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    annotation_id TEXT,
    page_idx INTEGER NOT NULL DEFAULT 0,
    block_ref TEXT NOT NULL,
    label TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_annotation_items_job ON annotation_items(job_id);
"""
