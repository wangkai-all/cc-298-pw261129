"""CSV landing: validation, idempotent upsert, audit trail and snapshots.

Every landing file is treated as untrusted: rows are parsed field by field,
bad rows are written to `ingest_rejects` instead of silently poisoning the
warehouse, and a batch record records the file's SHA-256.  Snapshots are taken
under a write lock so a concurrent ingest can never produce a half-updated
input set.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .db import (
    SCHEMA_VERSION,
    canonical_json,
    connect,
    digest_parts,
    init_db,
    row_hash,
    sha256_text,
)
from .versions import utcnow


@dataclass(frozen=True)
class TableSpec:
    name: str
    pk: str
    business_fields: tuple[str, ...]
    snapshot_table: str


TABLES: dict[str, TableSpec] = {
    "stores": TableSpec(
        "stores",
        "store_id",
        (
            "store_id",
            "store_name",
            "region",
            "format",
            "cluster",
            "size_sqm",
            "traffic_index",
            "urban_score",
            "customer_count",
            "member_share",
            "store_group",
        ),
        "snapshot_stores",
    ),
    "exposure": TableSpec(
        "exposure",
        "exposure_id",
        (
            "exposure_id",
            "customer_id",
            "store_id",
            "channel",
            "exposure_kind",
            "exposed_at",
            "units",
            "amount_cents",
        ),
        "snapshot_exposure",
    ),
    "orders": TableSpec(
        "orders",
        "order_id",
        (
            "order_id",
            "customer_id",
            "store_id",
            "channel",
            "ordered_at",
            "status",
            "gross_cents",
            "discount_cents",
            "net_cents",
        ),
        "snapshot_orders",
    ),
    "refunds": TableSpec(
        "refunds",
        "refund_id",
        (
            "refund_id",
            "order_id",
            "customer_id",
            "store_id",
            "refunded_at",
            "amount_cents",
        ),
        "snapshot_refunds",
    ),
}

CHANNELS = {"app", "store", "wechat", "sms", "email"}
EXPOSURE_KINDS = {"campaign", "regular"}
ORDER_STATUSES = {"completed", "shipped", "cancelled", "returned", "pending"}
STORE_GROUPS = {"promo", "control"}


class IngestError(ValueError):
    pass


class RowRejected(ValueError):
    pass


def _norm_ts(raw: str, field_name: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise RowRejected(f"{field_name} 为空")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise RowRejected(f"{field_name} 不是 ISO 时间：{raw!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _req(raw: str, field_name: str, max_len: int = 120) -> str:
    text = (raw or "").strip()
    if not text:
        raise RowRejected(f"{field_name} 为空")
    if len(text) > max_len:
        raise RowRejected(f"{field_name} 过长")
    if any(ord(ch) < 32 for ch in text):
        raise RowRejected(f"{field_name} 含控制字符")
    return text


def _int(raw: str, field_name: str, minimum: int | None = None) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise RowRejected(f"{field_name} 不是整数：{raw!r}") from None
    if minimum is not None and value < minimum:
        raise RowRejected(f"{field_name} 小于 {minimum}")
    return value


def _float(raw: str, field_name: str, minimum: float | None = None) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        raise RowRejected(f"{field_name} 不是数字：{raw!r}") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise RowRejected(f"{field_name} 不是有限数字")
    if minimum is not None and value < minimum:
        raise RowRejected(f"{field_name} 小于 {minimum}")
    return round(value, 6)


def _store(row: dict[str, str]) -> dict[str, Any]:
    group = _req(row.get("store_group", ""), "store_group")
    if group not in STORE_GROUPS:
        raise RowRejected(f"store_group 未知：{group}")
    return {
        "store_id": _req(row.get("store_id", ""), "store_id", 40),
        "store_name": _req(row.get("store_name", ""), "store_name", 80),
        "region": _req(row.get("region", ""), "region", 40),
        "format": _req(row.get("format", ""), "format", 40),
        "cluster": _req(row.get("cluster", ""), "cluster", 60),
        "size_sqm": _float(row.get("size_sqm", ""), "size_sqm", 1),
        "traffic_index": _float(row.get("traffic_index", ""), "traffic_index", 0),
        "urban_score": _float(row.get("urban_score", ""), "urban_score", 0),
        "customer_count": _int(row.get("customer_count", ""), "customer_count", 1),
        "member_share": _float(row.get("member_share", ""), "member_share", 0),
        "store_group": group,
    }


def _exposure(row: dict[str, str]) -> dict[str, Any]:
    channel = _req(row.get("channel", ""), "channel", 20)
    if channel not in CHANNELS:
        raise RowRejected(f"channel 未知：{channel}")
    kind = _req(row.get("exposure_kind", ""), "exposure_kind", 20)
    if kind not in EXPOSURE_KINDS:
        raise RowRejected(f"exposure_kind 未知：{kind}")
    return {
        "exposure_id": _req(row.get("exposure_id", ""), "exposure_id", 60),
        "customer_id": _req(row.get("customer_id", ""), "customer_id", 60),
        "store_id": _req(row.get("store_id", ""), "store_id", 40),
        "channel": channel,
        "exposure_kind": kind,
        "exposed_at": _norm_ts(row.get("exposed_at", ""), "exposed_at"),
        "units": _int(row.get("units", ""), "units", 1),
        "amount_cents": _int(row.get("amount_cents", ""), "amount_cents", 0),
    }


def _orders(row: dict[str, str]) -> dict[str, Any]:
    channel = _req(row.get("channel", ""), "channel", 20)
    if channel not in CHANNELS:
        raise RowRejected(f"channel 未知：{channel}")
    status = _req(row.get("status", ""), "status", 20)
    if status not in ORDER_STATUSES:
        raise RowRejected(f"status 未知：{status}")
    gross = _int(row.get("gross_cents", ""), "gross_cents", 0)
    discount = _int(row.get("discount_cents", ""), "discount_cents", 0)
    net = _int(row.get("net_cents", ""), "net_cents", 0)
    if net != gross - discount:
        raise RowRejected("net_cents 不等于 gross_cents - discount_cents")
    return {
        "order_id": _req(row.get("order_id", ""), "order_id", 60),
        "customer_id": _req(row.get("customer_id", ""), "customer_id", 60),
        "store_id": _req(row.get("store_id", ""), "store_id", 40),
        "channel": channel,
        "ordered_at": _norm_ts(row.get("ordered_at", ""), "ordered_at"),
        "status": status,
        "gross_cents": gross,
        "discount_cents": discount,
        "net_cents": net,
    }


def _refunds(row: dict[str, str]) -> dict[str, Any]:
    return {
        "refund_id": _req(row.get("refund_id", ""), "refund_id", 60),
        "order_id": _req(row.get("order_id", ""), "order_id", 60),
        "customer_id": _req(row.get("customer_id", ""), "customer_id", 60),
        "store_id": _req(row.get("store_id", ""), "store_id", 40),
        "refunded_at": _norm_ts(row.get("refunded_at", ""), "refunded_at"),
        "amount_cents": _int(row.get("amount_cents", ""), "amount_cents", 1),
    }


PARSERS: dict[str, Callable[[dict[str, str]], dict[str, Any]]] = {
    "stores": _store,
    "exposure": _exposure,
    "orders": _orders,
    "refunds": _refunds,
}

REQUIRED_HEADERS: dict[str, set[str]] = {
    "stores": set(TABLES["stores"].business_fields),
    "exposure": set(TABLES["exposure"].business_fields),
    "orders": set(TABLES["orders"].business_fields),
    "refunds": set(TABLES["refunds"].business_fields),
}


def _next_ingest_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='ingest_seq'").fetchone()
    seq = int(row["value"]) + 1 if row else 1
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('ingest_seq', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(seq),),
    )
    return seq


def ingest_csv(db_path: str, table: str, path: str | Path) -> dict[str, Any]:
    init_db(db_path)
    if table not in TABLES:
        raise IngestError(f"未知表：{table}")
    spec = TABLES[table]
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise IngestError("文件不是 UTF-8 编码") from None

    batch_id = f"batch-{uuid.uuid4().hex[:12]}"
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO ingest_batches
               (batch_id, table_name, source_file, source_sha256, started_at, status)
               VALUES (?,?,?,?,?, 'running')""",
            (batch_id, table, path.name, digest, utcnow()),
        )
    finally:
        conn.close()

    inserted = updated = unchanged = rejected = read = 0
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        header = {h.strip() for h in (reader.fieldnames or []) if h}
        missing = REQUIRED_HEADERS[table] - header
        if missing:
            raise IngestError(f"缺少必需列：{', '.join(sorted(missing))}")
        conn = connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            seq = _next_ingest_seq(conn)
            existing = {
                r[spec.pk]: r
                for r in conn.execute(f"SELECT {spec.pk}, _row_hash FROM {table}")
            }
            placeholders = ",".join("?" for _ in range(len(spec.business_fields) + 4))
            columns = ",".join(spec.business_fields) + ", ingest_seq, source_file, source_line, _row_hash"
            updates = ",".join(
                f"{c}=excluded.{c}" for c in spec.business_fields[1:]
            )
            upsert = (
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT({spec.pk}) DO UPDATE SET {updates}, "
                f"ingest_seq=excluded.ingest_seq, source_file=excluded.source_file, "
                f"source_line=excluded.source_line, _row_hash=excluded._row_hash "
                f"WHERE {table}._row_hash != excluded._row_hash"
            )
            for line_no, raw_row in enumerate(reader, start=2):
                read += 1
                clean_row = {
                    str(k).strip(): (v if isinstance(v, str) else "")
                    for k, v in raw_row.items()
                    if k is not None
                }
                try:
                    parsed = PARSERS[table](clean_row)
                except RowRejected as exc:
                    rejected += 1
                    conn.execute(
                        """INSERT INTO ingest_rejects
                           (batch_id, table_name, source_line, reason, raw_row)
                           VALUES (?,?,?,?,?)""",
                        (
                            batch_id,
                            table,
                            line_no,
                            str(exc)[:300],
                            canonical_json(clean_row)[:2000],
                        ),
                    )
                    continue
                values = [parsed[f] for f in spec.business_fields]
                h = row_hash(values)
                previous = existing.get(parsed[spec.pk])
                if previous is None:
                    inserted += 1
                elif previous["_row_hash"] == h:
                    unchanged += 1
                else:
                    updated += 1
                conn.execute(
                    upsert,
                    (*values, seq, path.name, line_no, h),
                )
                existing[parsed[spec.pk]] = {spec.pk: parsed[spec.pk], "_row_hash": h}
            conn.execute(
                """UPDATE ingest_batches SET finished_at=?, rows_read=?, inserted=?,
                   updated=?, unchanged=?, rejected=?, status='succeeded'
                   WHERE batch_id=?""",
                (utcnow(), read, inserted, updated, unchanged, rejected, batch_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    except Exception as exc:
        conn = connect(db_path)
        try:
            conn.execute(
                """UPDATE ingest_batches SET finished_at=?, rows_read=?, inserted=?,
                   updated=?, unchanged=?, rejected=?, status='failed', error=?
                   WHERE batch_id=?""",
                (utcnow(), read, inserted, updated, unchanged, rejected, str(exc)[:500], batch_id),
            )
        finally:
            conn.close()
        raise

    return {
        "batch_id": batch_id,
        "table": table,
        "file": path.name,
        "sha256": digest,
        "ingest_seq": seq,
        "rows_read": read,
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "rejected": rejected,
        "status": "succeeded",
    }


def _digest_rows(rows: Iterable[sqlite3.Row]) -> str:
    hashes = sorted(row_hash([r["_row_hash"]]) for r in rows)
    return sha256_text("".join(hashes))


SNAPSHOT_COLUMNS: dict[str, tuple[str, ...]] = {
    "stores": (
        "store_id", "store_name", "region", "format", "cluster", "size_sqm",
        "traffic_index", "urban_score", "customer_count", "member_share",
        "store_group", "_row_hash",
    ),
    "exposure": (
        "exposure_id", "customer_id", "store_id", "channel", "exposure_kind",
        "exposed_at", "units", "amount_cents", "_row_hash",
    ),
    "orders": (
        "order_id", "customer_id", "store_id", "channel", "ordered_at", "status",
        "gross_cents", "discount_cents", "net_cents", "_row_hash",
    ),
    "refunds": (
        "refund_id", "order_id", "customer_id", "store_id", "refunded_at",
        "amount_cents", "_row_hash",
    ),
}


def create_snapshot(db_path: str) -> dict[str, Any]:
    """Freeze all currently landed rows; returns an existing hash if identical."""
    init_db(db_path)
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        max_seq_row = conn.execute("SELECT value FROM meta WHERE key='ingest_seq'").fetchone()
        max_seq = int(max_seq_row["value"]) if max_seq_row else 0
        counts: dict[str, int] = {}
        digests: dict[str, str] = {}
        data: dict[str, list[tuple[Any, ...]]] = {}
        for table, columns in SNAPSHOT_COLUMNS.items():
            rows = conn.execute(
                f"SELECT {', '.join(columns)} FROM {table} WHERE ingest_seq <= ? "
                f"ORDER BY {TABLES[table].pk}",
                (max_seq,),
            ).fetchall()
            data[table] = [tuple(r) for r in rows]
            counts[table] = len(rows)
            hashes = sorted(sha256_text(canonical_json(list(r[-1:]))) for r in data[table])
            digests[table] = sha256_text("".join(hashes))
        batch_rows = conn.execute(
            "SELECT batch_id, table_name, source_file, source_sha256, inserted, updated, "
            "unchanged, rejected, finished_at FROM ingest_batches WHERE status='succeeded' "
            "ORDER BY batch_id"
        ).fetchall()
        batch_ids = [r["batch_id"] for r in batch_rows]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "max_ingest_seq": max_seq,
            "counts": counts,
            "table_digests": digests,
            "batches": [dict(r) for r in batch_rows],
        }
        snapshot_hash = digest_parts(
            "snapshot", SCHEMA_VERSION, digests, counts
        )
        existing = conn.execute(
            "SELECT snapshot_hash FROM input_snapshots WHERE snapshot_hash=?",
            (snapshot_hash,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO input_snapshots
                   (snapshot_hash, created_at, row_counts, table_digests,
                    max_ingest_seq, batch_ids, manifest)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    snapshot_hash,
                    utcnow(),
                    canonical_json(counts),
                    canonical_json(digests),
                    max_seq,
                    canonical_json(batch_ids),
                    canonical_json(manifest),
                ),
            )
            for table, columns in SNAPSHOT_COLUMNS.items():
                if not data[table]:
                    continue
                placeholders = ",".join("?" for _ in range(len(columns) + 1))
                conn.executemany(
                    f"INSERT INTO snapshot_{table} (snapshot_hash, {', '.join(columns)}) "
                    f"VALUES ({placeholders})",
                    [(snapshot_hash, *row) for row in data[table]],
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {
        "snapshot_hash": snapshot_hash,
        "row_counts": counts,
        "table_digests": digests,
        "max_ingest_seq": max_seq,
        "batch_ids": batch_ids,
        "reused": existing is not None,
    }


def get_snapshot(db_path: str, snapshot_hash: str) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM input_snapshots WHERE snapshot_hash=?", (snapshot_hash,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise IngestError(f"快照不存在：{snapshot_hash}")
    return {
        "snapshot_hash": row["snapshot_hash"],
        "created_at": row["created_at"],
        "row_counts": json.loads(row["row_counts"]),
        "table_digests": json.loads(row["table_digests"]),
        "max_ingest_seq": row["max_ingest_seq"],
        "batch_ids": json.loads(row["batch_ids"]),
        "manifest": json.loads(row["manifest"]),
    }


def list_snapshots(db_path: str, limit: int = 20) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM input_snapshots "
            "ORDER BY max_ingest_seq DESC, created_at DESC, snapshot_hash DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "snapshot_hash": r["snapshot_hash"],
            "created_at": r["created_at"],
            "row_counts": json.loads(r["row_counts"]),
            "max_ingest_seq": r["max_ingest_seq"],
            "batch_ids": json.loads(r["batch_ids"]),
        }
        for r in rows
    ]


def list_batches(db_path: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM ingest_batches ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
