"""Store-level promotion comparison engine.

The unit of analysis is the store.  For each store we compute a
pre-period-to-campaign change in net sales per member, then compare the
promotion arm with the control arm using one of three explicit methods.

Nothing here claims an experimental effect: the groups were not randomised, so
the output labels the raw gap, the adjusted estimate, the assumptions, the
stores each method drops, the overlap region it can speak to, and sensitivity
to the choices that were made.
"""
from __future__ import annotations

import bisect
import calendar
import hashlib
import json
import math
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from .db import canonical_json, connect
from .stats import (
    bootstrap_interval,
    fit_logistic,
    logit,
    mean,
    predict_logistic,
    sd,
    standardized_columns,
    standardized_mean_difference,
    weighted_mean,
)

ENGINE_VERSION = "1.0.0"
BOOTSTRAP_REPS = 600
MIN_ORDERS_PER_WINDOW = 30
OUTCOME_HORIZON_DAYS = 28
NUMERIC_COVARIATES = [
    "log_size",
    "traffic_index",
    "urban_score",
    "member_share",
    "log_members",
]
FORMAT_DUMMIES = {
    "format_大卖场": "大卖场",
    "format_标准超市": "标准超市",
    "format_社区店": "社区店",
    "format_便利店": "便利店",
}
REGION_DUMMIES = {f"region_{r}": r for r in ["华东", "华南", "华北", "西南", "华中", "东北"]}
MODEL_COVARIATES = NUMERIC_COVARIATES + list(FORMAT_DUMMIES) + list(REGION_DUMMIES)

CAVEAT = (
    "两组门店并非随机分配。下列估计是在给定方法、假设和重叠样本范围内的关联差异；"
    "只有方法假设成立时才可作效果解读，本页不把它写成可验证的因果提升。"
)


def _epoch(ts: str) -> int:
    """Fast parse of the normalized UTC timestamps stored in the DB."""
    return calendar.timegm(
        (
            int(ts[0:4]),
            int(ts[5:7]),
            int(ts[8:10]),
            int(ts[11:13]),
            int(ts[14:16]),
            int(ts[17:19]),
            0,
            0,
            0,
        )
    )


def _date_start(value: str) -> int:
    return _epoch(f"{value}T00:00:00Z")


def _date_end(value: str) -> int:
    return _epoch(f"{value}T23:59:59Z")


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _rng_for(digest: str, label: str) -> random.Random:
    seed = int(hashlib.sha256(f"{digest}:{label}".encode()).hexdigest()[:16], 16)
    return random.Random(seed)


# ---------------------------------------------------------------------------
# input extraction
# ---------------------------------------------------------------------------
def _load_rows(conn: sqlite3.Connection, snapshot_hash: str) -> dict[str, list[sqlite3.Row]]:
    return {
        "stores": conn.execute(
            "SELECT * FROM snapshot_stores WHERE snapshot_hash=? ORDER BY store_id",
            (snapshot_hash,),
        ).fetchall(),
        "orders": conn.execute(
            "SELECT * FROM snapshot_orders WHERE snapshot_hash=? ORDER BY order_id",
            (snapshot_hash,),
        ).fetchall(),
        "exposure": conn.execute(
            "SELECT * FROM snapshot_exposure WHERE snapshot_hash=? ORDER BY exposure_id",
            (snapshot_hash,),
        ).fetchall(),
        "refunds": conn.execute(
            "SELECT * FROM snapshot_refunds WHERE snapshot_hash=? ORDER BY refund_id",
            (snapshot_hash,),
        ).fetchall(),
    }


def _attribution_index(
    exposures: Iterable[sqlite3.Row], rule: dict[str, Any]
) -> dict[tuple[str, str], tuple[list[int], list[dict[str, Any]]]]:
    kinds = set(rule["exposure_kinds"])
    channels = set(rule["channel_scope"])
    allow_all = "*" in channels
    by_key: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    for row in exposures:
        if row["exposure_kind"] not in kinds:
            continue
        if not allow_all and row["channel"] not in channels:
            continue
        key = (row["customer_id"], row["store_id"] if rule["require_same_store"] else "*")
        by_key.setdefault(key, []).append(
            (
                _epoch(row["exposed_at"]),
                {
                    "exposure_id": row["exposure_id"],
                    "kind": row["exposure_kind"],
                    "channel": row["channel"],
                    "at": row["exposed_at"],
                    "amount_cents": row["amount_cents"],
                },
            )
        )
    index: dict[tuple[str, str], tuple[list[int], list[dict[str, Any]]]] = {}
    for key, rows in by_key.items():
        rows.sort(key=lambda item: (item[0], item[1]["exposure_id"]))
        index[key] = ([r[0] for r in rows], [r[1] for r in rows])
    return index


def _pick_exposure(
    index: dict[tuple[str, str], tuple[list[int], list[dict[str, Any]]]],
    customer_id: str,
    store_id: str,
    order_epoch: int,
    lookback_seconds: int,
    post_only: bool,
) -> tuple[dict[str, Any] | None, int]:
    """Return the nearest eligible exposure and how many were eligible."""
    candidates: list[tuple[int, dict[str, Any]]] = []
    keys = [(customer_id, store_id), (customer_id, "*")]
    for key in keys:
        entry = index.get(key)
        if not entry:
            continue
        times, rows = entry
        if post_only:
            # exposures at or before the order, within lookback
            hi = bisect.bisect_right(times, order_epoch)
            lo = bisect.bisect_left(times, order_epoch - lookback_seconds)
            candidates.extend(
                (order_epoch - times[i], rows[i]) for i in range(lo, hi)
            )
        else:
            lo = bisect.bisect_left(times, order_epoch - lookback_seconds)
            hi = bisect.bisect_right(times, order_epoch + lookback_seconds)
            candidates.extend(
                (abs(order_epoch - times[i]), rows[i]) for i in range(lo, hi)
            )
    if not candidates:
        return None, 0
    candidates.sort(key=lambda item: (item[0], item[1]["exposure_id"]))
    return candidates[0][1], len(candidates)


def _compute_store_tables(
    rows: dict[str, list[sqlite3.Row]],
    rule: dict[str, Any],
    cohort: dict[str, Any],
    cutoff_epoch: int,
    window: dict[str, int],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    statuses = set(rule["order_statuses"])
    channels = set(rule["channel_scope"])
    allow_all_channels = "*" in channels
    lookback_seconds = int(rule["lookback_hours"]) * 3600
    post_only = bool(rule["post_exposure_only"])
    require_gross = bool(rule["require_gross_cents"])

    stores = {
        row["store_id"]: {
            "store_id": row["store_id"],
            "store_name": row["store_name"],
            "region": row["region"],
            "format": row["format"],
            "cluster": row["cluster"],
            "size_sqm": row["size_sqm"],
            "traffic_index": row["traffic_index"],
            "urban_score": row["urban_score"],
            "customer_count": row["customer_count"],
            "member_share": row["member_share"],
            "group": row["store_group"],
            "log_size": math.log(max(1.0, row["size_sqm"])),
            "log_members": math.log(max(1, row["customer_count"])),
            **{
                key: 1.0 if row["format"] == fmt else 0.0
                for key, fmt in FORMAT_DUMMIES.items()
            },
            **{
                key: 1.0 if row["region"] == region else 0.0
                for key, region in REGION_DUMMIES.items()
            },
        }
        for row in rows["stores"]
    }

    refunds_by_order: dict[str, list[tuple[int, int]]] = {}
    row_exclusions = {
        "refunds_total": 0,
        "refunds_orphan_order": 0,
        "refunds_after_cutoff": 0,
        "refunds_after_cutoff_cents": 0,
        "refunds_capped": 0,
        "orders_total": 0,
        "orders_before_window": 0,
        "orders_after_window": 0,
        "orders_excluded_status": 0,
        "orders_excluded_gross": 0,
        "orders_excluded_channel": 0,
        "orders_unknown_store": 0,
        "exposure_total": 0,
        "exposure_excluded_kind": 0,
        "exposure_excluded_channel": 0,
        "exposure_unknown_store": 0,
    }
    order_store_map: dict[str, sqlite3.Row] = {}
    for row in rows["orders"]:
        order_store_map[row["order_id"]] = row
    for row in rows["refunds"]:
        row_exclusions["refunds_total"] += 1
        if row["order_id"] not in order_store_map:
            row_exclusions["refunds_orphan_order"] += 1
            continue
        epoch = _epoch(row["refunded_at"])
        if epoch > cutoff_epoch:
            row_exclusions["refunds_after_cutoff"] += 1
            row_exclusions["refunds_after_cutoff_cents"] += row["amount_cents"]
        refunds_by_order.setdefault(row["order_id"], []).append((epoch, row["amount_cents"]))

    for row in rows["exposure"]:
        row_exclusions["exposure_total"] += 1
        if row["exposure_kind"] not in set(rule["exposure_kinds"]):
            row_exclusions["exposure_excluded_kind"] += 1
        if not allow_all_channels and row["channel"] not in channels:
            row_exclusions["exposure_excluded_channel"] += 1
        if row["store_id"] not in stores:
            row_exclusions["exposure_unknown_store"] += 1

    index = _attribution_index(rows["exposure"], rule)

    acc: dict[str, dict[str, Any]] = {
        sid: {
            "pre_net_asof": 0.0,
            "pre_net_final": 0.0,
            "camp_net_asof": 0.0,
            "camp_net_final": 0.0,
            "pre_attr_asof": 0.0,
            "pre_attr_final": 0.0,
            "camp_attr_asof": 0.0,
            "camp_attr_final": 0.0,
            "pre_orders": 0,
            "camp_orders": 0,
            "pre_attr_orders": 0,
            "camp_attr_orders": 0,
            "pre_refund_asof": 0.0,
            "pre_refund_final": 0.0,
            "camp_refund_asof": 0.0,
            "camp_refund_final": 0.0,
            "camp_refund_after_cutoff": 0.0,
            "camp_refund_after_cutoff_count": 0,
            "pre_campaign_kind_orders": 0,
            "attributed_sample": None,
        }
        for sid in stores
    }

    pre_start, pre_end = window["pre_start"], window["pre_end"]
    camp_start, camp_end = window["campaign_start"], window["campaign_end"]
    placeholder_samples: list[dict[str, Any]] = []
    missed_samples: list[dict[str, Any]] = []

    for row in rows["orders"]:
        row_exclusions["orders_total"] += 1
        store = acc.get(row["store_id"])
        if store is None:
            row_exclusions["orders_unknown_store"] += 1
            continue
        epoch = _epoch(row["ordered_at"])
        if epoch < pre_start:
            row_exclusions["orders_before_window"] += 1
            continue
        if epoch > camp_end:
            row_exclusions["orders_after_window"] += 1
            continue
        in_pre = pre_start <= epoch <= pre_end
        in_camp = camp_start <= epoch <= camp_end
        if not (in_pre or in_camp):
            continue
        if row["status"] not in statuses:
            row_exclusions["orders_excluded_status"] += 1
            continue
        if require_gross and row["gross_cents"] <= 0:
            row_exclusions["orders_excluded_gross"] += 1
            continue
        if not allow_all_channels and row["channel"] not in channels:
            row_exclusions["orders_excluded_channel"] += 1
            continue

        net = float(row["net_cents"])
        refunds = refunds_by_order.get(row["order_id"], [])
        refund_asof = 0.0
        refund_final = 0.0
        capped = False
        for refund_epoch, amount in refunds:
            refund_final += amount
            if refund_epoch <= cutoff_epoch:
                refund_asof += amount
        if refund_final > net:
            refund_final = net
            capped = True
        if refund_asof > net:
            refund_asof = net
            capped = True
        if capped:
            row_exclusions["refunds_capped"] += 1
        refund_after_cutoff = refund_final - refund_asof

        exposure, candidates = _pick_exposure(
            index, row["customer_id"], row["store_id"], epoch, lookback_seconds, post_only
        )
        attributed = exposure is not None
        prefix = "pre" if in_pre else "camp"
        store[f"{prefix}_orders"] += 1
        store[f"{prefix}_net_asof"] += net - refund_asof
        store[f"{prefix}_net_final"] += net - refund_final
        store[f"{prefix}_refund_asof"] += refund_asof
        store[f"{prefix}_refund_final"] += refund_final
        if prefix == "camp":
            store["camp_refund_after_cutoff"] += refund_after_cutoff
            if refund_after_cutoff > 0:
                store["camp_refund_after_cutoff_count"] += 1
        if attributed:
            store[f"{prefix}_attr_orders"] += 1
            store[f"{prefix}_attr_asof"] += net - refund_asof
            store[f"{prefix}_attr_final"] += net - refund_final
            if exposure["kind"] == "campaign":
                store["pre_campaign_kind_orders"] += 1 if in_pre else 0
            if len(placeholder_samples) < 60:
                placeholder_samples.append(
                    {
                        "order_id": row["order_id"],
                        "store_id": row["store_id"],
                        "customer_id": row["customer_id"],
                        "ordered_at": row["ordered_at"],
                        "net_cents": row["net_cents"],
                        "refund_asof_cents": int(round(refund_asof)),
                        "refund_final_cents": int(round(refund_final)),
                        "attributed": True,
                        "exposure_id": exposure["exposure_id"],
                        "exposure_kind": exposure["kind"],
                        "exposure_channel": exposure["channel"],
                        "exposure_at": exposure["at"],
                        "candidate_count": candidates,
                        "window": prefix,
                    }
                )
        elif len(missed_samples) < 12:
            missed_samples.append(
                {
                    "order_id": row["order_id"],
                    "store_id": row["store_id"],
                    "customer_id": row["customer_id"],
                    "ordered_at": row["ordered_at"],
                    "net_cents": row["net_cents"],
                    "refund_asof_cents": int(round(refund_asof)),
                    "refund_final_cents": int(round(refund_final)),
                    "attributed": False,
                    "exposure_id": None,
                    "exposure_kind": None,
                    "exposure_channel": None,
                    "exposure_at": None,
                    "candidate_count": 0,
                    "window": prefix,
                }
            )
    return stores, acc, row_exclusions, placeholder_samples + missed_samples


def _store_metrics(
    stores: dict[str, dict[str, Any]],
    acc: dict[str, dict[str, Any]],
    cohort: dict[str, Any],
    window: dict[str, int],
    threshold: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pre_days = window["pre_days"]
    camp_days = window["campaign_days"]
    scale = OUTCOME_HORIZON_DAYS
    metrics: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for store_id, store in stores.items():
        agg = acc.get(store_id, {})
        members = int(store["customer_count"])
        pre_orders = int(agg.get("pre_orders", 0))
        camp_orders = int(agg.get("camp_orders", 0))
        reasons = []
        if members < 50:
            reasons.append("会员数低于 50，无法稳定计算每会员口径")
        if pre_orders < threshold:
            reasons.append(f"活动前窗口有效订单 {pre_orders} 笔，少于 {threshold} 笔")
        if camp_orders < threshold:
            reasons.append(f"活动窗口有效订单 {camp_orders} 笔，少于 {threshold} 笔")

        def rate(total_cents: float, days: int) -> float:
            if days <= 0 or members <= 0:
                return 0.0
            return (total_cents / 100.0) / members / days * scale

        record = {
            "store_id": store_id,
            "store_name": store["store_name"],
            "region": store["region"],
            "format": store["format"],
            "group": store["group"],
            "members": members,
            "size_sqm": store["size_sqm"],
            "traffic_index": store["traffic_index"],
            "urban_score": store["urban_score"],
            "member_share": store["member_share"],
            "pre_orders": pre_orders,
            "campaign_orders": camp_orders,
            "attributed_pre_orders": int(agg.get("pre_attr_orders", 0)),
            "attributed_campaign_orders": int(agg.get("camp_attr_orders", 0)),
            "pre_rate_asof": _round(rate(agg.get("pre_net_asof", 0.0), pre_days), 3),
            "campaign_rate_asof": _round(rate(agg.get("camp_net_asof", 0.0), camp_days), 3),
            "pre_rate_final": _round(rate(agg.get("pre_net_final", 0.0), pre_days), 3),
            "campaign_rate_final": _round(rate(agg.get("camp_net_final", 0.0), camp_days), 3),
            "outcome_asof": _round(
                rate(agg.get("camp_net_asof", 0.0), camp_days)
                - rate(agg.get("pre_net_asof", 0.0), pre_days),
                3,
            ),
            "outcome_final": _round(
                rate(agg.get("camp_net_final", 0.0), camp_days)
                - rate(agg.get("pre_net_final", 0.0), pre_days),
                3,
            ),
            "attributed_pre_share": _round(
                (agg.get("pre_attr_asof", 0.0) / agg.get("pre_net_asof", 0.0))
                if agg.get("pre_net_asof")
                else 0.0,
                4,
            ),
            "attributed_campaign_share": _round(
                (agg.get("camp_attr_asof", 0.0) / agg.get("camp_net_asof", 0.0))
                if agg.get("camp_net_asof")
                else 0.0,
                4,
            ),
            "attributed_campaign_rate": _round(
                rate(agg.get("camp_attr_asof", 0.0), camp_days), 3
            ),
            "refund_rate_campaign": _round(
                (agg.get("camp_refund_asof", 0.0) / agg.get("camp_net_asof", 0.0))
                if agg.get("camp_net_asof")
                else 0.0,
                4,
            ),
            "late_refund_cents_in_snapshot": int(round(agg.get("camp_refund_after_cutoff", 0.0))),
            "late_refund_orders_in_snapshot": int(agg.get("camp_refund_after_cutoff_count", 0)),
            "eligible": not reasons,
            "exclusion_reason": "；".join(reasons) if reasons else None,
            "covariates": {key: store[key] for key in MODEL_COVARIATES},
        }
        metrics.append(record)
        if reasons:
            excluded.append(
                {
                    "store_id": store_id,
                    "store_name": store["store_name"],
                    "group": store["group"],
                    "region": store["region"],
                    "format": store["format"],
                    "members": members,
                    "pre_orders": pre_orders,
                    "campaign_orders": camp_orders,
                    "reason": "；".join(reasons),
                }
            )
    return metrics, excluded


# ---------------------------------------------------------------------------
# methods
# ---------------------------------------------------------------------------
def _weights_raw(stores: Sequence[dict[str, Any]]) -> list[float]:
    # Unit of analysis is the store, so the naive comparison is an unweighted
    # difference of arm means.  Member weighting is offered as a sensitivity,
    # not hidden inside the headline.
    return [1.0 for _ in stores]


def _estimate_weighted(
    stores: Sequence[dict[str, Any]],
    weights: Sequence[float],
    field: str,
) -> float:
    treated = [(float(s[field]), w) for s, w in zip(stores, weights) if s["group"] == "promo" and w > 0]
    control = [(float(s[field]), w) for s, w in zip(stores, weights) if s["group"] == "control" and w > 0]
    if not treated or not control:
        return 0.0
    return weighted_mean([v for v, _ in treated], [w for _, w in treated]) - weighted_mean(
        [v for v, _ in control], [w for _, w in control]
    )


def _fit_propensity(stores: Sequence[dict[str, Any]]) -> tuple[dict[str, float], dict[str, float], float]:
    columns = MODEL_COVARIATES
    stats = {}
    for column in columns:
        values = [float(s["covariates"][column]) for s in stores]
        m = mean(values)
        s_dev = sd(values)
        stats[column] = (m, s_dev if s_dev > 1e-12 else 1.0)
    rows = []
    for store in stores:
        rows.append(
            [1.0]
            + [
                (float(store["covariates"][c]) - stats[c][0]) / stats[c][1]
                for c in columns
            ]
        )
    target = [1 if s["group"] == "promo" else 0 for s in stores]
    beta = fit_logistic(rows, target, ridge=2.0)
    propensities = predict_logistic(rows, beta)
    scores = {s["store_id"]: p for s, p in zip(stores, propensities)}
    linear = {sid: logit(p) for sid, p in scores.items()}
    spread = sd(list(linear.values())) or 1.0
    return scores, linear, spread


def _method_raw(
    stores: list[dict[str, Any]], params: dict[str, Any], outcome_field: str
) -> dict[str, Any]:
    weights = _weights_raw(stores)
    per_store = [
        {"store_id": s["store_id"], "weight": w, "outcome": s[outcome_field]}
        for s, w in zip(stores, weights)
    ]
    estimate = _estimate_weighted(stores, weights, outcome_field)
    return {
        "algorithm": "raw",
        "estimate": estimate,
        "weights": weights,
        "per_store": per_store,
        "pairs": [],
        "strata": [],
        "method_exclusions": [],
        "support": {
            "note": "未做任何可比性调整，全部合格门店进入比较。",
            "common_support": None,
        },
        "counts": {
            "stores_used": len(stores),
            "promo_stores": sum(1 for s in stores if s["group"] == "promo"),
            "control_stores": sum(1 for s in stores if s["group"] == "control"),
            "promo_members": sum(s["members"] for s in stores if s["group"] == "promo"),
            "control_members": sum(s["members"] for s in stores if s["group"] == "control"),
        },
    }


def _method_matched(
    stores: list[dict[str, Any]], params: dict[str, Any], outcome_field: str
) -> dict[str, Any]:
    caliper = float(params.get("caliper_sd", 0.2))
    replacement = bool(params.get("with_replacement", False))
    scores, linear, spread = _fit_propensity(stores)
    treated = sorted(
        [s for s in stores if s["group"] == "promo"],
        key=lambda s: (-linear[s["store_id"]], s["store_id"]),
    )
    control = [s for s in stores if s["group"] == "control"]
    used: dict[str, int] = {}
    pairs: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for t in treated:
        best = None
        best_distance = None
        for c in control:
            distance = abs(linear[t["store_id"]] - linear[c["store_id"]]) / spread
            if distance > caliper:
                continue
            if not replacement and used.get(c["store_id"], 0) > 0:
                continue
            if best_distance is None or distance < best_distance or (
                abs(distance - best_distance) < 1e-12 and c["store_id"] < best["store_id"]
            ):
                best, best_distance = c, distance
        if best is None:
            unmatched.append(
                {
                    "store_id": t["store_id"],
                    "store_name": t["store_name"],
                    "group": "promo",
                    "reason": f"卡钳 {caliper:.2f} SD 内没有可用对照门店",
                    "propensity": _round(scores[t["store_id"]], 6),
                }
            )
            continue
        used[best["store_id"]] = used.get(best["store_id"], 0) + 1
        pairs.append(
            {
                "promo_store_id": t["store_id"],
                "promo_store_name": t["store_name"],
                "control_store_id": best["store_id"],
                "control_store_name": best["store_name"],
                "promo_outcome": t[outcome_field],
                "control_outcome": best[outcome_field],
                "distance_sd": _round(best_distance, 4),
                "promo_propensity": _round(scores[t["store_id"]], 6),
                "control_propensity": _round(scores[best["store_id"]], 6),
                "region": t["region"],
                "format": t["format"],
            }
        )
    if pairs:
        treated_mean = mean([p["promo_outcome"] for p in pairs])
        control_mean = mean([p["control_outcome"] for p in pairs])
        estimate = treated_mean - control_mean
    else:
        estimate = 0.0
    weights = []
    pair_ids = {p["promo_store_id"] for p in pairs}
    for s in stores:
        if s["store_id"] in pair_ids:
            weights.append(1.0)
        elif s["group"] == "control" and used.get(s["store_id"], 0) > 0:
            weights.append(float(used[s["store_id"]]))
        else:
            weights.append(0.0)
    controls_used = len(used)
    treated_ps = [scores[s["store_id"]] for s in stores if s["group"] == "promo"]
    control_ps = [scores[s["store_id"]] for s in stores if s["group"] == "control"]
    overlap_low = max(min(treated_ps), min(control_ps)) if treated_ps and control_ps else None
    overlap_high = min(max(treated_ps), max(control_ps)) if treated_ps and control_ps else None
    outside = [
        s["store_id"]
        for s in stores
        if scores[s["store_id"]] < (overlap_low if overlap_low is not None else 0)
        or scores[s["store_id"]] > (overlap_high if overlap_high is not None else 1)
    ]
    method_exclusions = unmatched + [
        {
            "store_id": s["store_id"],
            "store_name": s["store_name"],
            "group": "control",
            "reason": "未被任何促销门店匹配，权重为 0",
            "propensity": _round(scores[s["store_id"]], 6),
        }
        for s in control
        if used.get(s["store_id"], 0) == 0
    ]
    return {
        "algorithm": "matched",
        "estimate": estimate,
        "weights": weights,
        "per_store": [
            {
                "store_id": s["store_id"],
                "weight": w,
                "outcome": s[outcome_field],
                "propensity": _round(scores[s["store_id"]], 6),
            }
            for s, w in zip(stores, weights)
        ],
        "pairs": pairs,
        "strata": [],
        "method_exclusions": method_exclusions,
        "support": {
            "note": "共同支持指倾向得分重叠区间；区间外观测不进入匹配，卡钳外的促销门店被排除。",
            "caliper_sd": caliper,
            "propensity_sd": _round(spread, 6),
            "promo_range": [_round(min(treated_ps), 6), _round(max(treated_ps), 6)]
            if treated_ps
            else None,
            "control_range": [_round(min(control_ps), 6), _round(max(control_ps), 6)]
            if control_ps
            else None,
            "overlap_range": [_round(overlap_low, 6), _round(overlap_high, 6)]
            if overlap_low is not None
            else None,
            "outside_overlap_count": len(outside),
            "outside_overlap_stores": outside,
            "unmatched_treated": len(unmatched),
            "controls_used": controls_used,
            "controls_unused": len(control) - controls_used,
        },
        "counts": {
            "stores_used": sum(1 for w in weights if w > 0),
            "promo_stores": len(pairs),
            "control_stores": controls_used,
            "promo_members": sum(
                s["members"] for s in stores if s["store_id"] in pair_ids
            ),
            "control_members": sum(
                s["members"] for s in stores if used.get(s["store_id"], 0) > 0
            ),
        },
    }


def _method_stratified(
    stores: list[dict[str, Any]], params: dict[str, Any], outcome_field: str
) -> dict[str, Any]:
    dims = params.get("strata_dims") or ["region", "format"]
    if not dims or any(d not in ("region", "format") for d in dims):
        dims = ["region", "format"]
    dims = list(dict.fromkeys(dims))
    buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for store in stores:
        key = tuple(str(store[d]) for d in dims)
        buckets.setdefault(key, []).append(store)
    strata = []
    weights = [0.0] * len(stores)
    index = {s["store_id"]: i for i, s in enumerate(stores)}
    dropped: list[dict[str, Any]] = []
    kept_members = 0
    for key, bucket in sorted(buckets.items()):
        promo = [s for s in bucket if s["group"] == "promo"]
        control = [s for s in bucket if s["group"] == "control"]
        label = " / ".join(key)
        if not promo or not control:
            reason = "该层没有促销门店" if not promo else "该层没有对照门店，无法比较"
            for store in bucket:
                dropped.append(
                    {
                        "store_id": store["store_id"],
                        "store_name": store["store_name"],
                        "group": store["group"],
                        "stratum": label,
                        "reason": reason,
                    }
                )
            continue
        total_members = sum(s["members"] for s in bucket)
        kept_members += total_members
        strata.append(
            {
                "stratum": label,
                "dims": {d: v for d, v in zip(dims, key)},
                "promo_stores": len(promo),
                "control_stores": len(control),
                "members": total_members,
                "promo_mean": _round(mean([s[outcome_field] for s in promo]), 3),
                "control_mean": _round(mean([s[outcome_field] for s in control]), 3),
                "difference": _round(
                    mean([s[outcome_field] for s in promo])
                    - mean([s[outcome_field] for s in control]),
                    3,
                ),
                "treated": False,
            }
        )
    total_kept = sum(s["members"] for s in strata) or 1
    for stratum in strata:
        stratum["weight"] = _round(stratum["members"] / total_kept, 4)
    # Second pass for store weights (need stratum totals first).
    stratum_map = {}
    for key, bucket in buckets.items():
        label = " / ".join(key)
        found = next((s for s in strata if s["stratum"] == label), None)
        if found is None:
            continue
        n_promo = sum(1 for s in bucket if s["group"] == "promo")
        n_control = sum(1 for s in bucket if s["group"] == "control")
        stratum_map[label] = (
            found["members"] / total_kept,
            n_promo,
            n_control,
        )
    for key, bucket in buckets.items():
        label = " / ".join(key)
        if label not in stratum_map:
            continue
        share, n_promo, n_control = stratum_map[label]
        for store in bucket:
            w = share / (n_promo if store["group"] == "promo" else n_control)
            weights[index[store["store_id"]]] = w
    estimate = sum(
        s["weight"] * (s["promo_mean"] - s["control_mean"]) for s in strata
    )
    return {
        "algorithm": "stratified",
        "estimate": estimate,
        "weights": weights,
        "per_store": [
            {"store_id": s["store_id"], "weight": w, "outcome": s[outcome_field]}
            for s, w in zip(stores, weights)
        ],
        "pairs": [],
        "strata": strata,
        "method_exclusions": dropped,
        "support": {
            "note": "只比较两层都有门店的层；单臂层整体被排除。层权重按会员数分配。",
            "strata_dims": dims,
            "strata_total": len(buckets),
            "strata_used": len(strata),
            "strata_dropped": len(buckets) - len(strata),
            "members_used": kept_members,
            "members_total": sum(s["members"] for s in stores),
        },
        "counts": {
            "stores_used": sum(1 for w in weights if w > 0),
            "promo_stores": sum(
                1 for s, w in zip(stores, weights) if w > 0 and s["group"] == "promo"
            ),
            "control_stores": sum(
                1 for s, w in zip(stores, weights) if w > 0 and s["group"] == "control"
            ),
            "promo_members": sum(
                s["members"] for s, w in zip(stores, weights) if w > 0 and s["group"] == "promo"
            ),
            "control_members": sum(
                s["members"] for s, w in zip(stores, weights) if w > 0 and s["group"] == "control"
            ),
        },
    }


def _run_method(
    algorithm: str,
    stores: list[dict[str, Any]],
    params: dict[str, Any],
    outcome_field: str,
) -> dict[str, Any]:
    if algorithm == "matched":
        return _method_matched(stores, params, outcome_field)
    if algorithm == "stratified":
        return _method_stratified(stores, params, outcome_field)
    return _method_raw(stores, params, outcome_field)


def _bootstrap(
    algorithm: str,
    stores: list[dict[str, Any]],
    method: dict[str, Any],
    outcome_field: str,
    digest: str,
    reps: int = BOOTSTRAP_REPS,
) -> dict[str, Any]:
    rng = _rng_for(digest, f"bootstrap:{algorithm}:{outcome_field}")
    by_id = {s["store_id"]: s for s in stores}
    estimates: list[float] = []
    if algorithm == "matched":
        pairs = method["pairs"]
        if not pairs:
            return {"reps": 0, "ci_low": None, "ci_high": None, "boot_sd": None}
        for _ in range(reps):
            sample = [pairs[rng.randrange(len(pairs))] for _ in range(len(pairs))]
            estimates.append(
                mean([p["promo_outcome"] for p in sample])
                - mean([p["control_outcome"] for p in sample])
            )
    elif algorithm == "stratified":
        strata = method["strata"]
        if not strata:
            return {"reps": 0, "ci_low": None, "ci_high": None, "boot_sd": None}
        for _ in range(reps):
            sample = [strata[rng.randrange(len(strata))] for _ in range(len(strata))]
            total = sum(s["members"] for s in sample) or 1
            estimates.append(
                sum(
                    (s["members"] / total) * (s["promo_mean"] - s["control_mean"])
                    for s in sample
                )
            )
    else:
        promo = [s for s in stores if s["group"] == "promo"]
        control = [s for s in stores if s["group"] == "control"]
        if not promo or not control:
            return {"reps": 0, "ci_low": None, "ci_high": None, "boot_sd": None}
        for _ in range(reps):
            tp = [promo[rng.randrange(len(promo))] for _ in range(len(promo))]
            cp = [control[rng.randrange(len(control))] for _ in range(len(control))]
            estimates.append(
                mean([float(s[outcome_field]) for s in tp])
                - mean([float(s[outcome_field]) for s in cp])
            )
    low, high = bootstrap_interval(estimates)
    return {
        "reps": reps,
        "ci_low": _round(low, 3),
        "ci_high": _round(high, 3),
        "boot_sd": _round(sd(estimates), 3),
        "note": "门店层面重抽样的百分位置信区间，不构成随机化推断。",
    }


# ---------------------------------------------------------------------------
# balance / sensitivity
# ---------------------------------------------------------------------------
def _balance_table(
    stores: list[dict[str, Any]],
    weights: Sequence[float],
    method_algorithm: str,
) -> list[dict[str, Any]]:
    rows = []
    for column in MODEL_COVARIATES + list(FORMAT_DUMMIES) + list(REGION_DUMMIES):
        treated = [float(s["covariates"][column]) for s in stores if s["group"] == "promo"]
        control = [float(s["covariates"][column]) for s in stores if s["group"] == "control"]
        treated_w = [w for s, w in zip(stores, weights) if s["group"] == "promo" and w > 0]
        control_w = [w for s, w in zip(stores, weights) if s["group"] == "control" and w > 0]
        treated_v = [float(s["covariates"][column]) for s, w in zip(stores, weights) if s["group"] == "promo" and w > 0]
        control_v = [float(s["covariates"][column]) for s, w in zip(stores, weights) if s["group"] == "control" and w > 0]
        unadjusted = standardized_mean_difference(
            treated, control, [float(s["members"]) for s in stores if s["group"] == "promo"],
            [float(s["members"]) for s in stores if s["group"] == "control"],
        )
        adjusted = (
            standardized_mean_difference(treated_v, control_v, treated_w, control_w)
            if treated_v and control_v
            else 0.0
        )
        rows.append(
            {
                "covariate": column,
                "label": _covariate_label(column),
                "promo_mean": _round(weighted_mean(treated, [1.0] * len(treated)), 4),
                "control_mean": _round(weighted_mean(control, [1.0] * len(control)), 4),
                "smd_unadjusted": _round(unadjusted, 3),
                "smd_adjusted": _round(adjusted, 3),
            }
        )
    return rows


def _covariate_label(column: str) -> str:
    return {
        "log_size": "门店面积（对数）",
        "traffic_index": "客流指数",
        "urban_score": "城市化得分",
        "member_share": "会员销售占比",
        "log_members": "会员数（对数）",
        "format_大卖场": "业态：大卖场",
        "format_标准超市": "业态：标准超市",
        "format_社区店": "业态：社区店",
        "format_便利店": "业态：便利店",
    }.get(column, column.replace("region_", "区域："))


def _trim_outliers(stores: list[dict[str, Any]], field: str) -> tuple[list[dict[str, Any]], list[str]]:
    values = sorted(float(s[field]) for s in stores)
    if len(values) < 8:
        return stores, []
    low = values[int(0.25 * (len(values) - 1))]
    high = values[int(0.75 * (len(values) - 1))]
    iqr = high - low
    if iqr <= 0:
        return stores, []
    lo = low - 1.5 * iqr
    hi = high + 1.5 * iqr
    keep, dropped = [], []
    for store in stores:
        value = float(store[field])
        if value < lo or value > hi:
            dropped.append(store["store_id"])
        else:
            keep.append(store)
    return keep, dropped


def _sensitivity(
    algorithm: str,
    params: dict[str, Any],
    stores: list[dict[str, Any]],
    primary: dict[str, Any],
    cohort: dict[str, Any],
    primary_field: str,
    swap_field: str,
    digest: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    swapped = _run_method(algorithm, stores, params, swap_field)
    rows.append(
        {
            "name": "退款处理方式改为另一种",
            "detail": (
                "用同一快照内的退款重算："
                + ("把截止日后已到账的退款纳入" if primary_field.endswith("asof") else "只计退款截止日前到账的退款")
            ),
            "estimate": _round(swapped["estimate"], 3),
            "delta_vs_primary": _round(swapped["estimate"] - primary["estimate"], 3),
            "n_promo": swapped["counts"]["promo_stores"],
            "n_control": swapped["counts"]["control_stores"],
        }
    )
    trimmed, dropped = _trim_outliers(stores, primary_field)
    if dropped and len(trimmed) >= 4:
        trimmed_method = _run_method(algorithm, trimmed, params, primary_field)
        rows.append(
            {
                "name": "剔除结果离群门店（1.5×IQR）",
                "detail": f"剔除 {len(dropped)} 家结果落在四分位距之外的门店",
                "estimate": _round(trimmed_method["estimate"], 3),
                "delta_vs_primary": _round(trimmed_method["estimate"] - primary["estimate"], 3),
                "n_promo": trimmed_method["counts"]["promo_stores"],
                "n_control": trimmed_method["counts"]["control_stores"],
                "excluded_store_ids": dropped,
            }
        )
    if algorithm == "matched":
        for caliper in (0.1, 0.2, 0.3):
            variant = _run_method("matched", stores, {**params, "caliper_sd": caliper}, primary_field)
            rows.append(
                {
                    "name": f"卡钳宽度 {caliper:.1f} SD",
                    "detail": "收紧卡钳提高可比性但会丢样本；放宽则保留更多但更依赖模型外推",
                    "estimate": _round(variant["estimate"], 3),
                    "delta_vs_primary": _round(variant["estimate"] - primary["estimate"], 3),
                    "n_promo": variant["counts"]["promo_stores"],
                    "n_control": variant["counts"]["control_stores"],
                    "is_primary": abs(caliper - float(params.get("caliper_sd", 0.2))) < 1e-9,
                }
            )
    elif algorithm == "stratified":
        two_plus = []
        for store in stores:
            two_plus.append(store)
        # Re-run only with strata that have at least two stores per arm.
        dims = params.get("strata_dims") or ["region", "format"]
        buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for store in stores:
            buckets.setdefault(tuple(str(store[d]) for d in dims), []).append(store)
        keep_ids = set()
        for bucket in buckets.values():
            if sum(1 for s in bucket if s["group"] == "promo") >= 2 and sum(
                1 for s in bucket if s["group"] == "control"
            ) >= 2:
                keep_ids.update(s["store_id"] for s in bucket)
        if keep_ids:
            subset = [s for s in stores if s["store_id"] in keep_ids]
            variant = _run_method("stratified", params, subset, primary_field)
            rows.append(
                {
                    "name": "每层两组至少 2 家门店",
                    "detail": f"剔除单薄层后保留 {len(subset)} 家门店",
                    "estimate": _round(variant["estimate"], 3),
                    "delta_vs_primary": _round(variant["estimate"] - primary["estimate"], 3),
                    "n_promo": variant["counts"]["promo_stores"],
                    "n_control": variant["counts"]["control_stores"],
                }
            )
    else:
        member_estimate = _estimate_weighted(
            stores, [float(s["members"]) for s in stores], primary_field
        )
        rows.append(
            {
                "name": "按会员数加权",
                "detail": "检验大店是否主导原始差异；估计量随之改变，不是误差范围",
                "estimate": _round(member_estimate, 3),
                "delta_vs_primary": _round(member_estimate - primary["estimate"], 3),
                "n_promo": sum(1 for s in stores if s["group"] == "promo"),
                "n_control": sum(1 for s in stores if s["group"] == "control"),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def analyze(
    db_path: str,
    *,
    snapshot_hash: str,
    rule: dict[str, Any],
    cohort: dict[str, Any],
    method: dict[str, Any],
    method_params: dict[str, Any],
    inputs_digest: str,
    threshold: int = MIN_ORDERS_PER_WINDOW,
    bootstrap_reps: int = BOOTSTRAP_REPS,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    conn = connect(db_path)
    try:
        snapshot = conn.execute(
            "SELECT * FROM input_snapshots WHERE snapshot_hash=?", (snapshot_hash,)
        ).fetchone()
        if snapshot is None:
            raise ValueError(f"快照不存在：{snapshot_hash}")
        rows = _load_rows(conn, snapshot_hash)
        refund_quality = {
            "refunds_in_snapshot": conn.execute(
                "SELECT COUNT(*) FROM snapshot_refunds WHERE snapshot_hash=?",
                (snapshot_hash,),
            ).fetchone()[0],
            "refunds_after_cutoff_in_snapshot": conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_cents),0) FROM snapshot_refunds "
                "WHERE snapshot_hash=? AND refunded_at > ?",
                (snapshot_hash, cohort["refund_cutoff"]),
            ).fetchone(),
        }
        batches = conn.execute(
            "SELECT batch_id, table_name, source_file, source_sha256, inserted, updated, "
            "unchanged, rejected, finished_at FROM ingest_batches WHERE status='succeeded' "
            "ORDER BY batch_id"
        ).fetchall()
    finally:
        conn.close()

    cutoff_epoch = _epoch(cohort["refund_cutoff"])
    window = {
        "pre_start": _date_start(cohort["pre_start"]),
        "pre_end": _date_end(cohort["pre_end"]),
        "campaign_start": _date_start(cohort["campaign_start"]),
        "campaign_end": _date_end(cohort["campaign_end"]),
        "pre_days": (
            datetime.fromisoformat(cohort["pre_end"]) - datetime.fromisoformat(cohort["pre_start"])
        ).days
        + 1,
        "campaign_days": (
            datetime.fromisoformat(cohort["campaign_end"])
            - datetime.fromisoformat(cohort["campaign_start"])
        ).days
        + 1,
    }

    store_lookup, acc, row_exclusions, samples = _compute_store_tables(
        rows, rule, cohort, cutoff_epoch, window
    )
    metrics, excluded = _store_metrics(store_lookup, acc, cohort, window, threshold)
    filters = filters or {}
    allowed_regions = set(filters.get("regions") or [])
    allowed_formats = set(filters.get("formats") or [])
    excluded_store_ids = set(filters.get("exclude_store_ids") or [])
    for metric in metrics:
        reasons = []
        if allowed_regions and metric["region"] not in allowed_regions:
            reasons.append("不符合页面筛选的区域条件")
        if allowed_formats and metric["format"] not in allowed_formats:
            reasons.append("不符合页面筛选的业态条件")
        if metric["store_id"] in excluded_store_ids:
            reasons.append("被页面手动排除")
        if metric["eligible"] and reasons:
            metric["eligible"] = False
            metric["exclusion_reason"] = "；".join(reasons)
            metric["excluded_by_filter"] = True
            excluded.append(
                {
                    "store_id": metric["store_id"],
                    "store_name": metric["store_name"],
                    "group": metric["group"],
                    "region": metric["region"],
                    "format": metric["format"],
                    "members": metric["members"],
                    "pre_orders": metric["pre_orders"],
                    "campaign_orders": metric["campaign_orders"],
                    "reason": "；".join(reasons),
                }
            )
    eligible = [m for m in metrics if m["eligible"]]

    order_stores = {row["store_id"] for row in rows["orders"]}
    missing_master = sorted(order_stores - set(store_lookup))
    if missing_master:
        for store_id in missing_master:
            excluded.append(
                {
                    "store_id": store_id,
                    "store_name": "（快照中缺少门店主数据）",
                    "group": "unknown",
                    "region": None,
                    "format": None,
                    "members": 0,
                    "pre_orders": 0,
                    "campaign_orders": 0,
                    "reason": "订单引用了门店主数据中不存在的 store_id，无法确定分组与特征",
                }
            )

    algorithm = method["algorithm"]
    primary_field = "outcome_final" if cohort["refund_mode"] == "final_observed" else "outcome_asof"
    swap_field = "outcome_asof" if primary_field == "outcome_final" else "outcome_final"
    result = _run_method(algorithm, eligible, method_params, primary_field)
    ci = _bootstrap(algorithm, eligible, result, primary_field, inputs_digest, reps=bootstrap_reps)
    balance = _balance_table(eligible, result["weights"], algorithm)

    store_rows = []
    weights_by_id = {s["store_id"]: w for s, w in zip(eligible, result["weights"])}
    pairs_by_promo = {p["promo_store_id"]: p for p in result["pairs"]}
    for m in metrics:
        entry = dict(m)
        entry["weight"] = _round(weights_by_id.get(m["store_id"], 0.0), 6)
        entry["matched_to"] = pairs_by_promo.get(m["store_id"], {}).get("control_store_id")
        store_rows.append(entry)

    def arm_summary(group: str) -> dict[str, Any]:
        arm = [m for m in eligible if m["group"] == group]
        used = [m for m in arm if weights_by_id.get(m["store_id"], 0) > 0]
        if not used:
            return {"stores": 0, "members": 0}
        return {
            "stores": len(used),
            "members": sum(m["members"] for m in used),
            "campaign_rate_mean": _round(mean([m["campaign_rate_asof"] for m in used]), 3),
            "pre_rate_mean": _round(mean([m["pre_rate_asof"] for m in used]), 3),
            "outcome_mean": _round(mean([m[primary_field] for m in used]), 3),
            "attributed_share_mean": _round(
                mean([m["attributed_campaign_share"] for m in used]), 4
            ),
            "refund_rate_mean": _round(mean([m["refund_rate_campaign"] for m in used]), 4),
        }

    promo_arm = arm_summary("promo")
    control_arm = arm_summary("control")
    raw_estimate = _estimate_weighted(eligible, _weights_raw(eligible), primary_field)
    relative = (
        result["estimate"] / control_arm["campaign_rate_mean"]
        if control_arm.get("campaign_rate_mean")
        else None
    )
    worst_before = max((abs(b["smd_unadjusted"]) for b in balance), default=0.0)
    worst_after = max((abs(b["smd_adjusted"]) for b in balance), default=0.0)

    sensitivity = _sensitivity(
        algorithm,
        method_params,
        eligible,
        result,
        cohort,
        primary_field,
        swap_field,
        inputs_digest,
    )

    refund_late = next(
        (row for row in sensitivity if row["name"] == "退款处理方式改为另一种"), None
    )
    sample_attr = []
    for sample in samples:
        store = store_lookup.get(sample["store_id"])
        sample_attr.append(
            {
                **sample,
                "store_name": store["store_name"] if store else sample["store_id"],
                "store_group": store["group"] if store else None,
            }
        )

    late_count, late_cents = refund_quality["refunds_after_cutoff_in_snapshot"]
    campaign_net = sum(
        (agg["camp_net_asof"] + agg["camp_refund_asof"]) for agg in acc.values()
    )
    finished = datetime.now(timezone.utc)
    payload = {
        "engine_version": ENGINE_VERSION,
        "generated_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "caveat": CAVEAT,
        "estimand": (
            f"促销门店相对对照门店的每会员净销售变化（元/会员/{OUTCOME_HORIZON_DAYS}天，"
            "活动窗口减去活动前窗口）"
        ),
        "inputs": {
            "snapshot_hash": snapshot_hash,
            "snapshot_created_at": snapshot["created_at"],
            "snapshot_row_counts": json.loads(snapshot["row_counts"]),
            "snapshot_table_digests": json.loads(snapshot["table_digests"]),
            "max_ingest_seq": snapshot["max_ingest_seq"],
            "rule": rule,
            "cohort": cohort,
            "method": method,
            "method_params": method_params,
            "inputs_digest": inputs_digest,
            "batches": [dict(b) for b in batches],
            "min_orders_per_window": threshold,
            "outcome_horizon_days": OUTCOME_HORIZON_DAYS,
            "filters": filters,
        },
        "windows": {
            "pre": [cohort["pre_start"], cohort["pre_end"]],
            "campaign": [cohort["campaign_start"], cohort["campaign_end"]],
            "pre_days": window["pre_days"],
            "campaign_days": window["campaign_days"],
            "refund_cutoff": cohort["refund_cutoff"],
            "refund_mode": cohort["refund_mode"],
        },
        "headline": {
            "raw_difference": _round(raw_estimate, 3),
            "adjusted_estimate": _round(result["estimate"], 3),
            "adjustment_delta": _round(result["estimate"] - raw_estimate, 3),
            "ci": ci,
            "relative_to_control_campaign": _round(relative, 4) if relative is not None else None,
            "method_label": method["label"],
            "method_algorithm": algorithm,
            "n_stores_promo": result["counts"]["promo_stores"],
            "n_stores_control": result["counts"]["control_stores"],
            "n_members_promo": result["counts"]["promo_members"],
            "n_members_control": result["counts"]["control_members"],
            "interpretation": (
                "这是方法假设下的调整后差异。若分组与结果受同一批未观测因素驱动，"
                "该数字仍可能包含选择偏差；请结合被排除样本、共同支持与敏感性一起阅读。"
            ),
        },
        "arms": {"promo": promo_arm, "control": control_arm},
        "balance": balance,
        "balance_summary": {
            "max_abs_smd_unadjusted": _round(worst_before, 3),
            "max_abs_smd_adjusted": _round(worst_after, 3),
            "improved": worst_after < worst_before,
            "threshold": 0.1,
        },
        "exclusions": {
            "store_level": store_rows and [m for m in store_rows if not m["eligible"]],
            "counts": {
                "stores_total": len(metrics),
                "stores_eligible": len(eligible),
                "stores_excluded": len(metrics) - len(eligible),
                "missing_store_master": len(missing_master),
            },
            "row_level": row_exclusions,
            "method_level": result["method_exclusions"],
        },
        "support": result["support"],
        "sensitivity": sensitivity,
        "pairs": result["pairs"],
        "strata": result["strata"],
        "stores": store_rows,
        "attribution": {
            "rule_version": rule["rule_version"],
            "campaign_eligible_orders": sum(m["campaign_orders"] for m in metrics),
            "campaign_attributed_orders": sum(
                m["attributed_campaign_orders"] for m in metrics
            ),
            "sample_rows": sample_attr,
            "note": "归属按订单发生时点向前回看窗口内最近的合格曝光；同刻多曝光按 exposure_id 决胜。",
        },
        "refund_quality": {
            "refunds_in_snapshot": refund_quality["refunds_in_snapshot"],
            "refunds_after_cutoff_in_snapshot": late_count,
            "refunds_after_cutoff_cents": late_cents,
            "refunds_after_cutoff_share_of_campaign_net": _round(
                late_cents / campaign_net, 5
            )
            if campaign_net
            else None,
            "refund_mode": cohort["refund_mode"],
            "refund_cutoff": cohort["refund_cutoff"],
            "sensitivity_estimate_other_mode": refund_late["estimate"] if refund_late else None,
            "late_refunds_in_snapshot_affect_primary": bool(
                refund_quality["refunds_after_cutoff_in_snapshot"]
            )
            and cohort["refund_mode"] == "final_observed",
            "note": (
                "快照只包含已到账的退款。截止日后到账的退款若尚未回补，不会出现在本报告的"
                "任何口径里；回补后重算才会体现。"
            ),
        },
        "timings": {
            "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seconds": round((finished - started).total_seconds(), 3),
        },
    }
    payload["payload_digest"] = _payload_digest(payload)
    return payload


def _payload_digest(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k not in {"generated_at", "timings", "payload_digest"}}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# drill-down and evidence export
# ---------------------------------------------------------------------------
def store_detail(
    db_path: str,
    *,
    snapshot_hash: str,
    store_id: str,
    rule: dict[str, Any],
    cohort: dict[str, Any],
) -> dict[str, Any]:
    """Weekly evidence for one store under the report's frozen rule/cohort."""
    conn = connect(db_path)
    try:
        store = conn.execute(
            "SELECT * FROM snapshot_stores WHERE snapshot_hash=? AND store_id=?",
            (snapshot_hash, store_id),
        ).fetchone()
        if store is None:
            raise ValueError(f"快照中没有门店 {store_id}")
        orders = conn.execute(
            "SELECT * FROM snapshot_orders WHERE snapshot_hash=? AND store_id=? ORDER BY ordered_at",
            (snapshot_hash, store_id),
        ).fetchall()
        refunds = conn.execute(
            "SELECT * FROM snapshot_refunds WHERE snapshot_hash=? AND store_id=? ORDER BY refunded_at",
            (snapshot_hash, store_id),
        ).fetchall()
        exposures = conn.execute(
            "SELECT * FROM snapshot_exposure WHERE snapshot_hash=? AND store_id=? ORDER BY exposed_at",
            (snapshot_hash, store_id),
        ).fetchall()
    finally:
        conn.close()
    cutoff = _epoch(cohort["refund_cutoff"])
    refunds_by_order: dict[str, list[tuple[int, int]]] = {}
    for row in refunds:
        refunds_by_order.setdefault(row["order_id"], []).append(
            (_epoch(row["refunded_at"]), row["amount_cents"])
        )
    rule_channels = set(rule["channel_scope"])
    allow_all = "*" in rule_channels
    statuses = set(rule["order_statuses"])
    buckets: dict[str, dict[str, Any]] = {}
    attributed_orders = 0
    eligible_orders = 0
    excluded_reasons = {
        "status": 0,
        "channel": 0,
        "outside_window": 0,
        "gross": 0,
    }
    pre_start = _date_start(cohort["pre_start"])
    camp_end = _date_end(cohort["campaign_end"])
    for row in orders:
        epoch = _epoch(row["ordered_at"])
        if epoch < pre_start or epoch > camp_end:
            excluded_reasons["outside_window"] += 1
            continue
        if row["status"] not in statuses:
            excluded_reasons["status"] += 1
            continue
        if not allow_all and row["channel"] not in rule_channels:
            excluded_reasons["channel"] += 1
            continue
        if rule["require_gross_cents"] and row["gross_cents"] <= 0:
            excluded_reasons["gross"] += 1
            continue
        eligible_orders += 1
        refund_asof = 0
        refund_final = 0
        for refund_epoch, amount in refunds_by_order.get(row["order_id"], []):
            refund_final += amount
            if refund_epoch <= cutoff:
                refund_asof += amount
        refund_asof = min(refund_asof, row["net_cents"])
        refund_final = min(refund_final, row["net_cents"])
        # week label: Monday of the order week
        day = datetime.strptime(row["ordered_at"][:10], "%Y-%m-%d")
        monday = (day - timedelta(days=day.weekday())).strftime("%Y-%m-%d")
        bucket = buckets.setdefault(
            monday,
            {"week": monday, "net_cents": 0, "late_refund_cents": 0, "orders": 0, "attributed_orders": 0, "attributed_net_cents": 0},
        )
        net_asof = row["net_cents"] - refund_asof
        bucket["net_cents"] += net_asof
        bucket["orders"] += 1
        bucket["late_refund_cents"] += refund_final - refund_asof
    index = _attribution_index(exposures, rule)
    for row in orders:
        epoch = _epoch(row["ordered_at"])
        if epoch < pre_start or epoch > camp_end:
            continue
        if row["status"] not in statuses:
            continue
        if not allow_all and row["channel"] not in rule_channels:
            continue
        exposure, _ = _pick_exposure(
            index,
            row["customer_id"],
            row["store_id"],
            epoch,
            int(rule["lookback_hours"]) * 3600,
            bool(rule["post_exposure_only"]),
        )
        if exposure is None:
            continue
        attributed_orders += 1
        day = datetime.strptime(row["ordered_at"][:10], "%Y-%m-%d")
        monday = (day - timedelta(days=day.weekday())).strftime("%Y-%m-%d")
        bucket = buckets.get(monday)
        if bucket is not None:
            bucket["attributed_orders"] += 1
            bucket["attributed_net_cents"] += row["net_cents"]
    series = [buckets[k] for k in sorted(buckets)]
    campaign_start = cohort["campaign_start"]
    for item in series:
        item["phase"] = (
            "campaign" if campaign_start <= item["week"] <= cohort["campaign_end"] else "pre"
        )
        item["net_yuan"] = round(item["net_cents"] / 100.0, 2)
        item["attributed_net_yuan"] = round(item["attributed_net_cents"] / 100.0, 2)
    return {
        "store": {
            "store_id": store["store_id"],
            "store_name": store["store_name"],
            "region": store["region"],
            "format": store["format"],
            "group": store["store_group"],
            "members": store["customer_count"],
            "size_sqm": store["size_sqm"],
            "traffic_index": store["traffic_index"],
            "urban_score": store["urban_score"],
            "member_share": store["member_share"],
        },
        "series": series,
        "totals": {
            "eligible_orders": eligible_orders,
            "attributed_orders": attributed_orders,
            "excluded_orders": excluded_reasons,
        },
        "windows": {
            "pre": [cohort["pre_start"], cohort["pre_end"]],
            "campaign": [cohort["campaign_start"], cohort["campaign_end"]],
        },
    }
