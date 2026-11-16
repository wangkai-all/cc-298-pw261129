"""Immutable, explicitly-versioned rules, cohorts and analysis methods.

A report never reads the *current* definition at compute time: it stores the
version id and the resolved values in its frozen spec.  New versions retire the
previous active row but never mutate it, so old reports remain reproducible.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from .db import canonical_json, connect

RULE_DEFAULTS: dict[str, Any] = {
    "label": "默认归因规则",
    "lookback_hours": 48,
    "order_statuses": ["completed", "shipped"],
    "channel_scope": ["app", "store", "wechat", "sms"],
    "exposure_kinds": ["campaign", "regular"],
    "require_same_store": 1,
    "post_exposure_only": 1,
    "require_gross_cents": 1,
}

RULE_STATUSES = {"completed", "shipped", "cancelled", "returned", "pending"}
RULE_CHANNELS = {"app", "store", "wechat", "sms", "email"}
RULE_EXPOSURE_KINDS = {"campaign", "regular"}

COHORT_DEFAULTS: dict[str, Any] = {
    "label": "夏季促销观察窗",
    "campaign_start": "2026-06-29",
    "campaign_end": "2026-08-23",
    "pre_start": "2026-04-06",
    "pre_end": "2026-06-28",
    "refund_cutoff": "2026-09-06T23:59:59Z",
    "refund_mode": "as_of_cutoff",
}

METHOD_LABELS = {
    "raw": "原始两组对照",
    "matched": "倾向得分最近邻匹配",
    "stratified": "精确分层对照",
}

METHOD_DESCRIPTIONS = {
    "raw": "不做平衡处理，保留全部满足数据完整性要求的门店；差异同时包含分组选择偏差。",
    "matched": "对已观测门店特征拟合倾向得分，1:1 最近邻匹配并施加卡钳；用样本量与共同支持换可比性。",
    "stratified": "在区域×业态等精确层内比较，仅保留两组都存在的层；丢弃缺少对照的层。",
}

METHOD_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "raw": {},
    "matched": {"caliper_sd": 0.2, "with_replacement": False},
    "stratified": {"strata_dims": ["region", "format"]},
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class VersionError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VersionError(message)


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise VersionError(f"{field} 必须是 YYYY-MM-DD 日期") from None


def validate_rule(payload: dict[str, Any]) -> dict[str, Any]:
    body = {**RULE_DEFAULTS, **(payload or {})}
    label = str(body.get("label") or "").strip()
    _require(1 <= len(label) <= 80, "规则名称长度需在 1-80 字符")
    try:
        lookback = int(body["lookback_hours"])
    except (TypeError, ValueError):
        raise VersionError("lookback_hours 必须是整数") from None
    _require(1 <= lookback <= 720, "回看窗口需在 1-720 小时")
    statuses = body.get("order_statuses")
    _require(isinstance(statuses, list) and bool(statuses), "至少选择一个订单状态")
    for status in statuses:
        _require(isinstance(status, str) and status in RULE_STATUSES, f"未知订单状态：{status}")
    channels = body.get("channel_scope")
    if channels == "*":
        channels = ["*"]
    _require(isinstance(channels, list) and bool(channels), "至少选择一个渠道或使用通配")
    for channel in channels:
        _require(
            isinstance(channel, str) and (channel == "*" or channel in RULE_CHANNELS),
            f"未知渠道：{channel}",
        )
    kinds = body.get("exposure_kinds")
    _require(isinstance(kinds, list) and bool(kinds), "至少选择一种曝光类型")
    for kind in kinds:
        _require(isinstance(kind, str) and kind in RULE_EXPOSURE_KINDS, f"未知曝光类型：{kind}")
    if "*" in channels:
        channels = ["*"]
    return {
        "label": label,
        "lookback_hours": lookback,
        "order_statuses": sorted(set(statuses)),
        "channel_scope": sorted(set(channels)),
        "exposure_kinds": sorted(set(kinds)),
        "require_same_store": 1 if body.get("require_same_store", True) else 0,
        "post_exposure_only": 1 if body.get("post_exposure_only", True) else 0,
        "require_gross_cents": 1 if body.get("require_gross_cents", True) else 0,
    }


def validate_cohort(payload: dict[str, Any]) -> dict[str, Any]:
    body = {**COHORT_DEFAULTS, **(payload or {})}
    label = str(body.get("label") or "").strip()
    _require(1 <= len(label) <= 80, "观察窗名称长度需在 1-80 字符")
    campaign_start = _parse_date(body["campaign_start"], "campaign_start")
    campaign_end = _parse_date(body["campaign_end"], "campaign_end")
    pre_start = _parse_date(body["pre_start"], "pre_start")
    pre_end = _parse_date(body["pre_end"], "pre_end")
    _require(campaign_start <= campaign_end, "活动开始日期不能晚于结束日期")
    _require(pre_start <= pre_end, "期前开始日期不能晚于结束日期")
    _require(pre_end < campaign_start, "期前窗口必须早于活动窗口")
    refund_mode = body.get("refund_mode", "as_of_cutoff")
    _require(refund_mode in {"as_of_cutoff", "final_observed"}, "refund_mode 取值无效")
    cutoff = str(body.get("refund_cutoff") or "")
    try:
        parsed = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    except ValueError:
        raise VersionError("refund_cutoff 必须是 ISO 8601 时间") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    cutoff = parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _require(cutoff[:10] >= campaign_end.isoformat(), "退款截止时间不能早于活动结束日")
    return {
        "label": label,
        "campaign_start": campaign_start.isoformat(),
        "campaign_end": campaign_end.isoformat(),
        "pre_start": pre_start.isoformat(),
        "pre_end": pre_end.isoformat(),
        "refund_cutoff": cutoff,
        "refund_mode": refund_mode,
    }


def _next_version(conn: sqlite3.Connection, table: str, column: str, prefix: str) -> str:
    rows = [r[0] for r in conn.execute(f"SELECT {column} FROM {table}")]
    best = 0
    for value in rows:
        try:
            best = max(best, int(str(value).rsplit("-v", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"{prefix}-v{best + 1}"


def create_rule_version(db_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = validate_rule(payload)
    now = utcnow()
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = _next_version(conn, "attribution_rule_versions", "rule_version", "rule")
        conn.execute(
            "UPDATE attribution_rule_versions SET status='retired', superseded_at=? "
            "WHERE status='active'",
            (now,),
        )
        conn.execute(
            """INSERT INTO attribution_rule_versions
               (rule_version, label, lookback_hours, order_statuses, channel_scope,
                exposure_kinds, require_same_store, post_exposure_only,
                require_gross_cents, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,'active',?)""",
            (
                version,
                body["label"],
                body["lookback_hours"],
                canonical_json(body["order_statuses"]),
                canonical_json(body["channel_scope"]),
                canonical_json(body["exposure_kinds"]),
                body["require_same_store"],
                body["post_exposure_only"],
                body["require_gross_cents"],
                now,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get_rule(db_path, version)


def create_cohort_version(db_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = validate_cohort(payload)
    now = utcnow()
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = _next_version(conn, "cohort_versions", "cohort_version", "cohort")
        conn.execute(
            "UPDATE cohort_versions SET status='retired', superseded_at=? "
            "WHERE status='active'",
            (now,),
        )
        conn.execute(
            """INSERT INTO cohort_versions
               (cohort_version, label, campaign_start, campaign_end, pre_start,
                pre_end, refund_cutoff, refund_mode, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,'active',?)""",
            (
                version,
                body["label"],
                body["campaign_start"],
                body["campaign_end"],
                body["pre_start"],
                body["pre_end"],
                body["refund_cutoff"],
                body["refund_mode"],
                now,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get_cohort(db_path, version)


def _rule_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "rule_version": row["rule_version"],
        "label": row["label"],
        "lookback_hours": row["lookback_hours"],
        "order_statuses": json.loads(row["order_statuses"]),
        "channel_scope": json.loads(row["channel_scope"]),
        "exposure_kinds": json.loads(row["exposure_kinds"]),
        "require_same_store": bool(row["require_same_store"]),
        "post_exposure_only": bool(row["post_exposure_only"]),
        "require_gross_cents": bool(row["require_gross_cents"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "superseded_at": row["superseded_at"],
    }


def _cohort_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "cohort_version": row["cohort_version"],
        "label": row["label"],
        "campaign_start": row["campaign_start"],
        "campaign_end": row["campaign_end"],
        "pre_start": row["pre_start"],
        "pre_end": row["pre_end"],
        "refund_cutoff": row["refund_cutoff"],
        "refund_mode": row["refund_mode"],
        "status": row["status"],
        "created_at": row["created_at"],
        "superseded_at": row["superseded_at"],
    }


def get_rule(db_path: str, version: str) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM attribution_rule_versions WHERE rule_version=?", (version,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise VersionError(f"规则版本不存在：{version}")
    return _rule_row(row)


def get_cohort(db_path: str, version: str) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM cohort_versions WHERE cohort_version=?", (version,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise VersionError(f"观察窗版本不存在：{version}")
    return _cohort_row(row)


def active_rule(db_path: str) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM attribution_rule_versions WHERE status='active' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise VersionError("没有启用的归因规则版本")
    return _rule_row(row)


def active_cohort(db_path: str) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM cohort_versions WHERE status='active' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise VersionError("没有启用的观察窗版本")
    return _cohort_row(row)


def list_rules(db_path: str) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM attribution_rule_versions ORDER BY created_at DESC, rule_version DESC"
        ).fetchall()
    finally:
        conn.close()
    return [_rule_row(r) for r in rows]


def list_cohorts(db_path: str) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM cohort_versions ORDER BY created_at DESC, cohort_version DESC"
        ).fetchall()
    finally:
        conn.close()
    return [_cohort_row(r) for r in rows]


def list_methods(db_path: str) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM analysis_methods ORDER BY algorithm, created_at"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "method_version": r["method_version"],
            "label": r["label"],
            "algorithm": r["algorithm"],
            "description": r["description"],
            "default_params": json.loads(r["default_params"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def get_method(db_path: str, method_version: str) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM analysis_methods WHERE method_version=?", (method_version,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise VersionError(f"分析方法版本不存在：{method_version}")
    return {
        "method_version": row["method_version"],
        "label": row["label"],
        "algorithm": row["algorithm"],
        "description": row["description"],
        "default_params": json.loads(row["default_params"]),
        "created_at": row["created_at"],
    }


def method_for_algorithm(db_path: str, algorithm: str) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM analysis_methods WHERE algorithm=? ORDER BY created_at DESC LIMIT 1",
            (algorithm,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise VersionError(f"没有方法实现：{algorithm}")
    return {
        "method_version": row["method_version"],
        "label": row["label"],
        "algorithm": row["algorithm"],
        "description": row["description"],
        "default_params": json.loads(row["default_params"]),
        "created_at": row["created_at"],
    }


def seed_versions(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = utcnow()
        existing_rule = conn.execute(
            "SELECT 1 FROM attribution_rule_versions LIMIT 1"
        ).fetchone()
        if not existing_rule:
            body = validate_rule(RULE_DEFAULTS)
            conn.execute(
                """INSERT INTO attribution_rule_versions
                   (rule_version, label, lookback_hours, order_statuses, channel_scope,
                    exposure_kinds, require_same_store, post_exposure_only,
                    require_gross_cents, status, created_at)
                   VALUES ('rule-v1',?,?,?,?,?,?,?,?,'active',?)""",
                (
                    body["label"],
                    body["lookback_hours"],
                    canonical_json(body["order_statuses"]),
                    canonical_json(body["channel_scope"]),
                    canonical_json(body["exposure_kinds"]),
                    body["require_same_store"],
                    body["post_exposure_only"],
                    body["require_gross_cents"],
                    now,
                ),
            )
        existing_cohort = conn.execute(
            "SELECT 1 FROM cohort_versions LIMIT 1"
        ).fetchone()
        if not existing_cohort:
            body = validate_cohort(COHORT_DEFAULTS)
            conn.execute(
                """INSERT INTO cohort_versions
                   (cohort_version, label, campaign_start, campaign_end, pre_start,
                    pre_end, refund_cutoff, refund_mode, status, created_at)
                   VALUES ('cohort-v1',?,?,?,?,?,?,?,'active',?)""",
                (
                    body["label"],
                    body["campaign_start"],
                    body["campaign_end"],
                    body["pre_start"],
                    body["pre_end"],
                    body["refund_cutoff"],
                    body["refund_mode"],
                    now,
                ),
            )
        for algorithm in ("raw", "matched", "stratified"):
            version = f"{algorithm}-v1"
            conn.execute(
                """INSERT INTO analysis_methods
                   (method_version, label, algorithm, description, default_params, created_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(method_version) DO NOTHING""",
                (
                    version,
                    METHOD_LABELS[algorithm],
                    algorithm,
                    METHOD_DESCRIPTIONS[algorithm],
                    canonical_json(METHOD_DEFAULT_PARAMS[algorithm]),
                    now,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
