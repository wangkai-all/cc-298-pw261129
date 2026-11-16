"""Report lifecycle: combos, immutable versions, queue, staleness, compare.

Guarantees that matter for this app:

* a report freezes its entire input set at enqueue time (snapshot hash, exact
  rule/cohort/method version ids *and* their resolved values, method params,
  filters, threshold, engine version);
* a run can only finalize its *own* report row, and only while that row is
  still `computing` -- so a stale task can never overwrite a newer report;
* superseded reports keep their versions and payloads for audit, and can be
  re-derived later from the immutable snapshot.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from . import analysis, versions
from .db import canonical_json, connect, digest_parts
from .ingest import get_snapshot, list_snapshots
from .versions import VersionError, utcnow

TERMINAL = {"final", "failed", "superseded"}
ACTIVE_STATUSES = ("queued", "computing")


class ReportError(ValueError):
    pass


def _new_report_id() -> str:
    return "rpt_" + uuid.uuid4().hex[:16]


def _connect(db_path: str) -> sqlite3.Connection:
    return connect(db_path)


def _event(conn: sqlite3.Connection, report_id: str, kind: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO report_events (report_id, at, kind, detail) VALUES (?,?,?,?)",
        (report_id, utcnow(), kind, detail[:1000]),
    )


def _combo_id_for(spec: dict[str, Any]) -> str:
    stable = {
        "method_algorithm": spec["method_algorithm"],
        "method_params": spec["method_params"],
        "threshold": spec["threshold"],
        "filters": spec["filters"],
        "refund_mode": spec["cohort"]["refund_mode"],
        "rule": {
            "lookback_hours": spec["rule"]["lookback_hours"],
            "order_statuses": spec["rule"]["order_statuses"],
            "channel_scope": spec["rule"]["channel_scope"],
            "exposure_kinds": spec["rule"]["exposure_kinds"],
            "require_same_store": spec["rule"]["require_same_store"],
            "post_exposure_only": spec["rule"]["post_exposure_only"],
            "require_gross_cents": spec["rule"]["require_gross_cents"],
        },
        "cohort": {
            "campaign_start": spec["cohort"]["campaign_start"],
            "campaign_end": spec["cohort"]["campaign_end"],
            "pre_start": spec["cohort"]["pre_start"],
            "pre_end": spec["cohort"]["pre_end"],
        },
    }
    return "cmp_" + digest_parts("combo", stable)[:16]


def _inputs_digest(spec: dict[str, Any], snapshot_hash: str) -> str:
    return digest_parts(
        "report-inputs",
        analysis.ENGINE_VERSION,
        snapshot_hash,
        spec["rule"],
        spec["cohort"],
        spec["method"],
        spec["method_params"],
        spec["filters"],
        spec["threshold"],
    )


def build_spec(
    db_path: str,
    *,
    method_algorithm: str = "raw",
    method_params: dict[str, Any] | None = None,
    threshold: int = analysis.MIN_ORDERS_PER_WINDOW,
    filters: dict[str, Any] | None = None,
    rule_version: str | None = None,
    cohort_version: str | None = None,
    rule_overrides: dict[str, Any] | None = None,
    cohort_overrides: dict[str, Any] | None = None,
    snapshot_hash: str | None = None,
) -> dict[str, Any]:
    """Resolve all inputs now, creating a new rule/cohort version if the user
    changed a condition.  Nothing here trusts a value to stay current later."""
    if method_algorithm not in ("raw", "matched", "stratified"):
        raise ReportError(f"未知分析方法：{method_algorithm}")
    if rule_overrides:
        rule = versions.create_rule_version(db_path, rule_overrides)
    elif rule_version:
        rule = versions.get_rule(db_path, rule_version)
    else:
        rule = versions.active_rule(db_path)
    if cohort_overrides:
        cohort = versions.create_cohort_version(db_path, cohort_overrides)
    elif cohort_version:
        cohort = versions.get_cohort(db_path, cohort_version)
    else:
        cohort = versions.active_cohort(db_path)
    method = versions.method_for_algorithm(db_path, method_algorithm)
    params = dict(method["default_params"])
    if method_params:
        params.update(method_params)
    filters = _normalise_filters(filters or {})
    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        raise ReportError("订单门槛必须是整数") from None
    if not 1 <= threshold <= 100000:
        raise ReportError("订单门槛需在 1-100000 之间")
    if snapshot_hash:
        snapshot = get_snapshot(db_path, snapshot_hash)
    else:
        snapshots = list_snapshots(db_path, limit=1)
        if not snapshots:
            raise ReportError("尚无可分析快照，请先接入数据")
        snapshot = snapshots[0]
    spec = {
        "rule": rule,
        "cohort": cohort,
        "method": method,
        "method_algorithm": method_algorithm,
        "method_params": params,
        "threshold": threshold,
        "filters": filters,
        "snapshot_hash": snapshot["snapshot_hash"],
    }
    spec["inputs_digest"] = _inputs_digest(spec, snapshot["snapshot_hash"])
    return spec


def _normalise_filters(filters: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(filters, dict):
        raise ReportError("筛选条件必须是对象")

    def _list(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ReportError("筛选条件必须是字符串列表")
        out = []
        for item in value:
            if not isinstance(item, str) or len(item) > 60:
                raise ReportError("筛选条件包含非法值")
            out.append(item)
        return sorted(set(out))

    return {
        "regions": _list(filters.get("regions")),
        "formats": _list(filters.get("formats")),
        "exclude_store_ids": _list(filters.get("exclude_store_ids")),
    }


def enqueue_report(
    db_path: str,
    *,
    label: str | None = None,
    combo_id: str | None = None,
    spec: dict[str, Any] | None = None,
    method_algorithm: str = "raw",
    method_params: dict[str, Any] | None = None,
    threshold: int = analysis.MIN_ORDERS_PER_WINDOW,
    filters: dict[str, Any] | None = None,
    rule_version: str | None = None,
    cohort_version: str | None = None,
    rule_overrides: dict[str, Any] | None = None,
    cohort_overrides: dict[str, Any] | None = None,
    snapshot_hash: str | None = None,
    recomputes_report_id: str | None = None,
) -> dict[str, Any]:
    if spec is None:
        spec = build_spec(
            db_path,
            method_algorithm=method_algorithm,
            method_params=method_params,
            threshold=threshold,
            filters=filters,
            rule_version=rule_version,
            cohort_version=cohort_version,
            rule_overrides=rule_overrides,
            cohort_overrides=cohort_overrides,
            snapshot_hash=snapshot_hash,
        )
    else:
        spec = json.loads(canonical_json(spec))
        spec["inputs_digest"] = _inputs_digest(spec, spec["snapshot_hash"])
    combo = combo_id or _combo_id_for(spec)
    report_id = _new_report_id()
    now = utcnow()
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM combos WHERE combo_id=?", (combo,)).fetchone()
        if existing is None:
            combo_label = label or _default_label(spec)
            conn.execute(
                "INSERT INTO combos (combo_id, label, spec, created_at, last_enqueued_at) "
                "VALUES (?,?,?,?,?)",
                (combo, combo_label, canonical_json(_combo_spec(spec)), now, now),
            )
        else:
            if label:
                conn.execute("UPDATE combos SET label=? WHERE combo_id=?", (label, combo))
            conn.execute(
                "UPDATE combos SET last_enqueued_at=? WHERE combo_id=?", (now, combo)
            )
        max_version = conn.execute(
            "SELECT COALESCE(MAX(version_number),0) FROM reports WHERE combo_id=?", (combo,)
        ).fetchone()[0]
        version_number = int(max_version) + 1
        conn.execute(
            """INSERT INTO reports
               (report_id, combo_id, version_number, spec, snapshot_hash, rule_version,
                cohort_version, method_version, method_params, inputs_digest,
                engine_version, recomputes_report_id, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'queued', ?)""",
            (
                report_id,
                combo,
                version_number,
                canonical_json(spec),
                spec["snapshot_hash"],
                spec["rule"]["rule_version"],
                spec["cohort"]["cohort_version"],
                spec["method"]["method_version"],
                canonical_json(spec["method_params"]),
                spec["inputs_digest"],
                analysis.ENGINE_VERSION,
                recomputes_report_id,
                now,
            ),
        )
        stale = conn.execute(
            "SELECT report_id FROM reports WHERE combo_id=? AND report_id != ? "
            "AND status IN ('queued','computing')",
            (combo, report_id),
        ).fetchall()
        for row in stale:
            conn.execute(
                "UPDATE reports SET status='superseded', superseded_by=?, finalized_at=? "
                "WHERE report_id=? AND status IN ('queued','computing')",
                (report_id, now, row["report_id"]),
            )
            _event(
                conn,
                row["report_id"],
                "superseded",
                f"同组合出现新版本 v{version_number}（{report_id}）",
            )
        conn.execute(
            "INSERT INTO analysis_runs (run_id, report_id, attempt, status, queued_at) "
            "VALUES (?,?,1,'queued',?)",
            ("run_" + uuid.uuid4().hex[:16], report_id, now),
        )
        conn.execute(
            "UPDATE combos SET current_report_id=? WHERE combo_id=?", (report_id, combo)
        )
        _event(
            conn,
            report_id,
            "enqueued",
            f"组合 {combo} 第 {version_number} 版；输入摘要 {spec['inputs_digest'][:12]}",
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get_report(db_path, report_id)


def _combo_spec(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "method_algorithm": spec["method_algorithm"],
        "method_params": spec["method_params"],
        "threshold": spec["threshold"],
        "filters": spec["filters"],
        "refund_mode": spec["cohort"]["refund_mode"],
    }


def _default_label(spec: dict[str, Any]) -> str:
    return (
        f"{spec['method']['label']} · 退款{('截至截止日' if spec['cohort']['refund_mode'] == 'as_of_cutoff' else '含已回补')}"
    )


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
def _claim_next(db_path: str) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT r.report_id, r.spec, a.run_id FROM reports r "
            "JOIN analysis_runs a ON a.report_id = r.report_id AND a.attempt = ("
            "  SELECT MAX(attempt) FROM analysis_runs WHERE report_id = r.report_id) "
            "WHERE r.status='queued' AND a.status='queued' "
            "ORDER BY r.created_at, r.version_number LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        now = utcnow()
        changed = conn.execute(
            "UPDATE reports SET status='computing' WHERE report_id=? AND status='queued'",
            (row["report_id"],),
        ).rowcount
        if not changed:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE analysis_runs SET status='running', started_at=? WHERE run_id=?",
            (now, row["run_id"]),
        )
        _event(conn, row["report_id"], "started", f"运行 {row['run_id']} 开始")
        conn.execute("COMMIT")
        return {
            "report_id": row["report_id"],
            "run_id": row["run_id"],
            "spec": json.loads(row["spec"]),
        }
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _finalize(db_path: str, report_id: str, run_id: str, payload: dict[str, Any]) -> str:
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, superseded_by FROM reports WHERE report_id=?", (report_id,)
        ).fetchone()
        now = utcnow()
        if row is None:
            conn.execute("ROLLBACK")
            return "missing"
        if row["status"] != "computing":
            conn.execute(
                "UPDATE analysis_runs SET status='discarded', finished_at=?, error=? "
                "WHERE run_id=?",
                (now, f"报告状态为 {row['status']}，结果被丢弃", run_id),
            )
            conn.execute("COMMIT")
            return "discarded"
        digest = payload["payload_digest"]
        conn.execute(
            "UPDATE reports SET status='final', payload=?, payload_digest=?, finalized_at=? "
            "WHERE report_id=? AND status='computing'",
            (canonical_json(payload), digest, now, report_id),
        )
        conn.execute(
            "UPDATE analysis_runs SET status='succeeded', finished_at=? WHERE run_id=?",
            (now, run_id),
        )
        _event(conn, report_id, "finalized", f"结果摘要 {digest[:16]}")
        conn.execute("COMMIT")
        return "final"
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _fail(db_path: str, report_id: str, run_id: str, error: str) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM reports WHERE report_id=?", (report_id,)
        ).fetchone()
        now = utcnow()
        conn.execute(
            "UPDATE analysis_runs SET status='failed', finished_at=?, error=? WHERE run_id=?",
            (now, error[:1000], run_id),
        )
        if row and row["status"] == "computing":
            conn.execute(
                "UPDATE reports SET status='failed', finalized_at=? WHERE report_id=?",
                (now, report_id),
            )
            _event(conn, report_id, "failed", error[:500])
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def execute_run(db_path: str, claimed: dict[str, Any]) -> str:
    spec = claimed["spec"]
    try:
        rule = versions.get_rule(db_path, spec["rule"]["rule_version"])
        cohort = versions.get_cohort(db_path, spec["cohort"]["cohort_version"])
        method = versions.get_method(db_path, spec["method"]["method_version"])
        payload = analysis.analyze(
            db_path,
            snapshot_hash=spec["snapshot_hash"],
            rule=rule,
            cohort=cohort,
            method=method,
            method_params=spec["method_params"],
            inputs_digest=spec["inputs_digest"],
            threshold=spec["threshold"],
            filters=spec["filters"],
        )
        return _finalize(db_path, claimed["report_id"], claimed["run_id"], payload)
    except Exception as exc:  # pragma: no cover - surfaced through API
        _fail(db_path, claimed["report_id"], claimed["run_id"], f"{type(exc).__name__}: {exc}")
        return "failed"


def work_once(db_path: str) -> bool:
    claimed = _claim_next(db_path)
    if claimed is None:
        return False
    execute_run(db_path, claimed)
    return True


def worker_loop(db_path: str, stop: threading.Event, poll_seconds: float = 0.25) -> None:
    while not stop.is_set():
        try:
            worked = work_once(db_path)
        except Exception as exc:  # pragma: no cover
            print(f"[worker] error: {exc}", flush=True)
            worked = False
        if not worked:
            stop.wait(poll_seconds)


def drain(db_path: str, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = _connect(db_path)
        try:
            pending = conn.execute(
                "SELECT COUNT(*) FROM reports WHERE status IN ('queued','computing')"
            ).fetchone()[0]
        finally:
            conn.close()
        if pending == 0:
            return
        if not work_once(db_path):
            time.sleep(0.05)
    raise ReportError("等待分析任务完成超时")


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
def _report_row(row: sqlite3.Row) -> dict[str, Any]:
    spec = json.loads(row["spec"])
    return {
        "report_id": row["report_id"],
        "combo_id": row["combo_id"],
        "version_number": row["version_number"],
        "status": row["status"],
        "superseded_by": row["superseded_by"],
        "snapshot_hash": row["snapshot_hash"],
        "rule_version": row["rule_version"],
        "cohort_version": row["cohort_version"],
        "method_version": row["method_version"],
        "method_params": json.loads(row["method_params"]),
        "inputs_digest": row["inputs_digest"],
        "engine_version": row["engine_version"],
        "recomputes_report_id": row["recomputes_report_id"],
        "created_at": row["created_at"],
        "finalized_at": row["finalized_at"],
        "payload_digest": row["payload_digest"],
        "spec": spec,
    }


def get_report(db_path: str, report_id: str, include_payload: bool = True) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
        if row is None:
            raise ReportError(f"报告不存在：{report_id}")
        combo = conn.execute(
            "SELECT * FROM combos WHERE combo_id=?", (row["combo_id"],)
        ).fetchone()
        events = conn.execute(
            "SELECT at, kind, detail FROM report_events WHERE report_id=? ORDER BY event_id",
            (report_id,),
        ).fetchall()
        runs = conn.execute(
            "SELECT run_id, attempt, status, queued_at, started_at, finished_at, error "
            "FROM analysis_runs WHERE report_id=? ORDER BY attempt",
            (report_id,),
        ).fetchall()
    finally:
        conn.close()
    result = _report_row(row)
    result["combo_label"] = combo["label"] if combo else None
    result["events"] = [dict(e) for e in events]
    result["runs"] = [dict(r) for r in runs]
    result["payload"] = json.loads(row["payload"]) if include_payload and row["payload"] else None
    return result


def report_versions(db_path: str, combo_id: str) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM reports WHERE combo_id=? ORDER BY version_number DESC", (combo_id,)
        ).fetchall()
    finally:
        conn.close()
    return [_report_row(r) for r in rows]


def list_combos(db_path: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT c.*,
                      (SELECT COUNT(*) FROM reports r WHERE r.combo_id=c.combo_id) AS versions,
                      r.report_id AS latest_id, r.version_number AS latest_version,
                      r.status AS latest_status, r.payload AS latest_payload,
                      r.created_at AS latest_created_at
               FROM combos c LEFT JOIN reports r ON r.report_id = c.current_report_id
               ORDER BY c.last_enqueued_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        headline = None
        if row["latest_payload"]:
            try:
                payload = json.loads(row["latest_payload"])
                headline = payload.get("headline")
            except json.JSONDecodeError:
                headline = None
        out.append(
            {
                "combo_id": row["combo_id"],
                "label": row["label"],
                "created_at": row["created_at"],
                "last_enqueued_at": row["last_enqueued_at"],
                "versions": row["versions"],
                "latest_report_id": row["latest_id"],
                "latest_version": row["latest_version"],
                "latest_status": row["latest_status"],
                "latest_created_at": row["latest_created_at"],
                "headline": headline,
            }
        )
    return out


def system_state(db_path: str) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        counts = {
            row["status"]: row["n"]
            for row in conn.execute("SELECT status, COUNT(*) n FROM reports GROUP BY status")
        }
        latest_snapshot = conn.execute(
            "SELECT * FROM input_snapshots "
            "ORDER BY max_ingest_seq DESC, created_at DESC, snapshot_hash DESC LIMIT 1"
        ).fetchone()
        batches = conn.execute(
            "SELECT batch_id, table_name, source_file, source_sha256, finished_at, "
            "rows_read, inserted, updated, unchanged, rejected, status "
            "FROM ingest_batches ORDER BY started_at DESC LIMIT 12"
        ).fetchall()
        rejected = conn.execute(
            "SELECT batch_id, table_name, source_line, reason FROM ingest_rejects "
            "ORDER BY batch_id DESC LIMIT 20"
        ).fetchall()
    finally:
        conn.close()
    return {
        "reports_by_status": counts,
        "queued": counts.get("queued", 0),
        "running": counts.get("computing", 0),
        "active_rule": versions.active_rule(db_path),
        "active_cohort": versions.active_cohort(db_path),
        "latest_snapshot": {
            "snapshot_hash": latest_snapshot["snapshot_hash"],
            "created_at": latest_snapshot["created_at"],
            "row_counts": json.loads(latest_snapshot["row_counts"]),
            "max_ingest_seq": latest_snapshot["max_ingest_seq"],
        }
        if latest_snapshot
        else None,
        "batches": [dict(b) for b in batches],
        "rejects": [dict(r) for r in rejected],
        "versions": {
            "rules": versions.list_rules(db_path),
            "cohorts": versions.list_cohorts(db_path),
            "methods": versions.list_methods(db_path),
        },
        "engine_version": analysis.ENGINE_VERSION,
    }


def staleness(db_path: str, report: dict[str, Any]) -> dict[str, Any]:
    active_rule = versions.active_rule(db_path)
    active_cohort = versions.active_cohort(db_path)
    snapshots = list_snapshots(db_path, limit=1)
    latest_snapshot = snapshots[0]["snapshot_hash"] if snapshots else None
    return {
        "rule_changed": active_rule["rule_version"] != report["rule_version"],
        "cohort_changed": active_cohort["cohort_version"] != report["cohort_version"],
        "new_snapshot": latest_snapshot is not None
        and latest_snapshot != report["snapshot_hash"],
        "active_rule_version": active_rule["rule_version"],
        "active_cohort_version": active_cohort["cohort_version"],
        "latest_snapshot_hash": latest_snapshot,
    }


def compare_reports(db_path: str, left_id: str, right_id: str) -> dict[str, Any]:
    left = get_report(db_path, left_id)
    right = get_report(db_path, right_id)
    if left["status"] != "final" or right["status"] != "final":
        raise ReportError("只能对比已完成的报告")

    def _inputs(report: dict[str, Any]) -> dict[str, Any]:
        spec = report["spec"]
        return {
            "snapshot_hash": report["snapshot_hash"],
            "rule_version": report["rule_version"],
            "cohort_version": report["cohort_version"],
            "method_version": report["method_version"],
            "method_params": report["method_params"],
            "inputs_digest": report["inputs_digest"],
            "engine_version": report["engine_version"],
            "threshold": spec["threshold"],
            "filters": spec["filters"],
            "refund_mode": spec["cohort"]["refund_mode"],
            "rule": {
                k: spec["rule"][k]
                for k in (
                    "lookback_hours",
                    "order_statuses",
                    "channel_scope",
                    "exposure_kinds",
                    "require_same_store",
                    "post_exposure_only",
                    "require_gross_cents",
                )
            },
            "cohort": {
                k: spec["cohort"][k]
                for k in (
                    "campaign_start",
                    "campaign_end",
                    "pre_start",
                    "pre_end",
                    "refund_cutoff",
                    "refund_mode",
                )
            },
        }

    a, b = _inputs(left), _inputs(right)
    diffs = [key for key in a if a[key] != b[key]]
    hp = left["payload"]["headline"]
    hq = right["payload"]["headline"]
    metrics = {}
    for key in (
        "raw_difference",
        "adjusted_estimate",
        "adjustment_delta",
        "n_stores_promo",
        "n_stores_control",
    ):
        metrics[key] = {
            "left": hp.get(key),
            "right": hq.get(key),
            "delta": (hq.get(key) - hp.get(key))
            if isinstance(hq.get(key), (int, float)) and isinstance(hp.get(key), (int, float))
            else None,
        }
    counts_a = left["payload"]["exclusions"]["counts"]
    counts_b = right["payload"]["exclusions"]["counts"]
    return {
        "left": {k: left[k] for k in ("report_id", "combo_id", "version_number", "created_at", "payload_digest", "status")},
        "right": {k: right[k] for k in ("report_id", "combo_id", "version_number", "created_at", "payload_digest", "status")},
        "inputs_left": a,
        "inputs_right": b,
        "changed_inputs": diffs,
        "metrics": metrics,
        "coverage": {
            "left_eligible": counts_a["stores_eligible"],
            "right_eligible": counts_b["stores_eligible"],
            "left_excluded": counts_a["stores_excluded"],
            "right_excluded": counts_b["stores_excluded"],
        },
        "balance": {
            "left_max_smd_adjusted": left["payload"]["balance_summary"]["max_abs_smd_adjusted"],
            "right_max_smd_adjusted": right["payload"]["balance_summary"]["max_abs_smd_adjusted"],
        },
    }
