"""SQLite schema, connection helpers and canonical hashing.

Everything an analysis reads is a content-addressed snapshot, so a report can
always be re-derived from exactly the rows and version values that went into
it.  Landed tables are mutable (ingest is an upsert); snapshots are immutable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Raw landed tables.  Rows are upserted by natural key; `_row_hash` covers
-- business fields only.  Repeated ingests of the same file are idempotent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stores (
    store_id               TEXT PRIMARY KEY,
    store_name             TEXT NOT NULL,
    region                 TEXT NOT NULL,
    format                 TEXT NOT NULL,
    cluster                TEXT NOT NULL,
    size_sqm               REAL NOT NULL,
    traffic_index          REAL NOT NULL,
    urban_score            REAL NOT NULL,
    customer_count         INTEGER NOT NULL,
    member_share           REAL NOT NULL,
    store_group            TEXT NOT NULL CHECK (store_group IN ('promo','control')),
    ingest_seq             INTEGER NOT NULL,
    source_file            TEXT NOT NULL,
    source_line            INTEGER NOT NULL,
    _row_hash              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exposure (
    exposure_id            TEXT PRIMARY KEY,
    customer_id            TEXT NOT NULL,
    store_id               TEXT NOT NULL,
    channel                TEXT NOT NULL,
    exposure_kind          TEXT NOT NULL,
    exposed_at             TEXT NOT NULL,
    units                  INTEGER NOT NULL,
    amount_cents           INTEGER NOT NULL,
    ingest_seq             INTEGER NOT NULL,
    source_file            TEXT NOT NULL,
    source_line            INTEGER NOT NULL,
    _row_hash              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id               TEXT PRIMARY KEY,
    customer_id            TEXT NOT NULL,
    store_id               TEXT NOT NULL,
    channel                TEXT NOT NULL,
    ordered_at             TEXT NOT NULL,
    status                 TEXT NOT NULL,
    gross_cents            INTEGER NOT NULL,
    discount_cents         INTEGER NOT NULL,
    net_cents              INTEGER NOT NULL,
    ingest_seq             INTEGER NOT NULL,
    source_file            TEXT NOT NULL,
    source_line            INTEGER NOT NULL,
    _row_hash              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id              TEXT PRIMARY KEY,
    order_id               TEXT NOT NULL,
    customer_id            TEXT NOT NULL,
    store_id               TEXT NOT NULL,
    refunded_at            TEXT NOT NULL,
    amount_cents           INTEGER NOT NULL,
    ingest_seq             INTEGER NOT NULL,
    source_file            TEXT NOT NULL,
    source_line            INTEGER NOT NULL,
    _row_hash              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exposure_customer ON exposure(customer_id);
CREATE INDEX IF NOT EXISTS idx_exposure_store ON exposure(store_id);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_store ON orders(store_id);
CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(order_id);
CREATE INDEX IF NOT EXISTS idx_refunds_store ON refunds(store_id);

-- ---------------------------------------------------------------------------
-- ingest audit
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_batches (
    batch_id               TEXT PRIMARY KEY,
    table_name             TEXT NOT NULL,
    source_file            TEXT NOT NULL,
    source_sha256          TEXT NOT NULL,
    started_at             TEXT NOT NULL,
    finished_at            TEXT,
    rows_read              INTEGER NOT NULL DEFAULT 0,
    inserted               INTEGER NOT NULL DEFAULT 0,
    updated                INTEGER NOT NULL DEFAULT 0,
    unchanged              INTEGER NOT NULL DEFAULT 0,
    rejected               INTEGER NOT NULL DEFAULT 0,
    status                 TEXT NOT NULL CHECK (status IN ('running','succeeded','failed')),
    error                  TEXT
);

CREATE TABLE IF NOT EXISTS ingest_rejects (
    batch_id               TEXT NOT NULL,
    table_name             TEXT NOT NULL,
    source_line            INTEGER NOT NULL,
    reason                 TEXT NOT NULL,
    raw_row                TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- versioned rule / cohort / method definitions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attribution_rule_versions (
    rule_version            TEXT PRIMARY KEY,
    label                   TEXT NOT NULL,
    lookback_hours          INTEGER NOT NULL,
    order_statuses          TEXT NOT NULL,       -- JSON list
    channel_scope           TEXT NOT NULL,       -- JSON list or ["*"]
    exposure_kinds          TEXT NOT NULL,       -- JSON list
    require_same_store      INTEGER NOT NULL,
    post_exposure_only      INTEGER NOT NULL,
    require_gross_cents     INTEGER NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('active','retired')),
    created_at              TEXT NOT NULL,
    superseded_at           TEXT
);

CREATE TABLE IF NOT EXISTS cohort_versions (
    cohort_version          TEXT PRIMARY KEY,
    label                   TEXT NOT NULL,
    campaign_start          TEXT NOT NULL,
    campaign_end            TEXT NOT NULL,
    pre_start               TEXT NOT NULL,
    pre_end                 TEXT NOT NULL,
    refund_cutoff           TEXT NOT NULL,
    refund_mode             TEXT NOT NULL CHECK (refund_mode IN ('as_of_cutoff','final_observed')),
    status                  TEXT NOT NULL CHECK (status IN ('active','retired')),
    created_at              TEXT NOT NULL,
    superseded_at           TEXT
);

CREATE TABLE IF NOT EXISTS analysis_methods (
    method_version          TEXT PRIMARY KEY,
    label                   TEXT NOT NULL,
    algorithm               TEXT NOT NULL,
    description             TEXT NOT NULL,
    default_params          TEXT NOT NULL,
    created_at              TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- frozen input snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS input_snapshots (
    snapshot_hash           TEXT PRIMARY KEY,
    created_at              TEXT NOT NULL,
    row_counts              TEXT NOT NULL,       -- JSON
    table_digests           TEXT NOT NULL,       -- JSON
    max_ingest_seq          INTEGER NOT NULL,
    batch_ids               TEXT NOT NULL,       -- JSON list
    manifest                TEXT NOT NULL        -- JSON (schema version etc.)
);

CREATE TABLE IF NOT EXISTS snapshot_exposure (
    snapshot_hash           TEXT NOT NULL REFERENCES input_snapshots(snapshot_hash),
    exposure_id             TEXT NOT NULL,
    customer_id             TEXT NOT NULL,
    store_id                TEXT NOT NULL,
    channel                 TEXT NOT NULL,
    exposure_kind           TEXT NOT NULL,
    exposed_at              TEXT NOT NULL,
    units                   INTEGER NOT NULL,
    amount_cents            INTEGER NOT NULL,
    _row_hash               TEXT NOT NULL,
    PRIMARY KEY (snapshot_hash, exposure_id)
);

CREATE TABLE IF NOT EXISTS snapshot_orders (
    snapshot_hash           TEXT NOT NULL REFERENCES input_snapshots(snapshot_hash),
    order_id                TEXT NOT NULL,
    customer_id             TEXT NOT NULL,
    store_id                TEXT NOT NULL,
    channel                 TEXT NOT NULL,
    ordered_at              TEXT NOT NULL,
    status                  TEXT NOT NULL,
    gross_cents             INTEGER NOT NULL,
    discount_cents          INTEGER NOT NULL,
    net_cents               INTEGER NOT NULL,
    _row_hash               TEXT NOT NULL,
    PRIMARY KEY (snapshot_hash, order_id)
);

CREATE TABLE IF NOT EXISTS snapshot_refunds (
    snapshot_hash           TEXT NOT NULL REFERENCES input_snapshots(snapshot_hash),
    refund_id               TEXT NOT NULL,
    order_id                TEXT NOT NULL,
    customer_id             TEXT NOT NULL,
    store_id                TEXT NOT NULL,
    refunded_at             TEXT NOT NULL,
    amount_cents            INTEGER NOT NULL,
    _row_hash               TEXT NOT NULL,
    PRIMARY KEY (snapshot_hash, refund_id)
);

CREATE TABLE IF NOT EXISTS snapshot_stores (
    snapshot_hash           TEXT NOT NULL REFERENCES input_snapshots(snapshot_hash),
    store_id                TEXT NOT NULL,
    store_name              TEXT NOT NULL,
    region                  TEXT NOT NULL,
    format                  TEXT NOT NULL,
    cluster                 TEXT NOT NULL,
    size_sqm                REAL NOT NULL,
    traffic_index           REAL NOT NULL,
    urban_score             REAL NOT NULL,
    customer_count          INTEGER NOT NULL,
    member_share            REAL NOT NULL,
    store_group             TEXT NOT NULL,
    _row_hash               TEXT NOT NULL,
    PRIMARY KEY (snapshot_hash, store_id)
);

CREATE INDEX IF NOT EXISTS idx_snap_orders_store ON snapshot_orders(snapshot_hash, store_id);
CREATE INDEX IF NOT EXISTS idx_snap_orders_customer ON snapshot_orders(snapshot_hash, customer_id);
CREATE INDEX IF NOT EXISTS idx_snap_exp_customer ON snapshot_exposure(snapshot_hash, customer_id);
CREATE INDEX IF NOT EXISTS idx_snap_exp_ts ON snapshot_exposure(snapshot_hash, exposed_at);
CREATE INDEX IF NOT EXISTS idx_snap_exp_cust_time ON snapshot_exposure(snapshot_hash, customer_id, exposed_at);
CREATE INDEX IF NOT EXISTS idx_snap_refunds_order ON snapshot_refunds(snapshot_hash, order_id);

-- ---------------------------------------------------------------------------
-- combos / reports / runs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS combos (
    combo_id                TEXT PRIMARY KEY,
    label                   TEXT NOT NULL,
    spec                    TEXT NOT NULL,       -- base spec JSON (no version)
    current_report_id       TEXT,
    created_at              TEXT NOT NULL,
    last_enqueued_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    report_id               TEXT PRIMARY KEY,
    combo_id                TEXT NOT NULL REFERENCES combos(combo_id),
    version_number          INTEGER NOT NULL,
    spec                    TEXT NOT NULL,       -- full spec JSON
    snapshot_hash           TEXT NOT NULL,
    rule_version            TEXT NOT NULL,
    cohort_version          TEXT NOT NULL,
    method_version          TEXT NOT NULL,
    method_params           TEXT NOT NULL,
    inputs_digest           TEXT NOT NULL,
    engine_version          TEXT NOT NULL,
    recomputes_report_id    TEXT,
    status                  TEXT NOT NULL CHECK (status IN ('queued','computing','final','failed','superseded')),
    superseded_by           TEXT,
    created_at              TEXT NOT NULL,
    finalized_at            TEXT,
    payload                 TEXT,
    payload_digest          TEXT,
    UNIQUE (combo_id, version_number)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id                  TEXT PRIMARY KEY,
    report_id               TEXT NOT NULL REFERENCES reports(report_id),
    attempt                 INTEGER NOT NULL DEFAULT 1,
    status                  TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','discarded')),
    queued_at               TEXT NOT NULL,
    started_at              TEXT,
    finished_at             TEXT,
    error                   TEXT,
    UNIQUE (report_id, attempt)
);

CREATE TABLE IF NOT EXISTS report_events (
    event_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id               TEXT NOT NULL,
    at                      TEXT NOT NULL,
    kind                    TEXT NOT NULL,
    detail                  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_report ON analysis_runs(report_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON analysis_runs(status, queued_at);
CREATE INDEX IF NOT EXISTS idx_events_report ON report_events(report_id, event_id);
CREATE INDEX IF NOT EXISTS idx_reports_combo ON reports(combo_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=90, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=90000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str | Path) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
    finally:
        conn.close()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_parts(*parts: Any) -> str:
    """Stable digest of structured parts (dicts are key-sorted)."""
    return sha256_text(canonical_json(list(parts)))


def row_hash(row: Iterable[Any]) -> str:
    return sha256_text(canonical_json(list(row)))
