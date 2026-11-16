"""PromoLens HTTP server: stdlib only.

JSON API + static single-page app.  Analysis runs in a background worker thread;
ingest and enqueue are independent so they can genuinely overlap, which is what
the snapshot/locking design exists for.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import analysis, ingest, reports, versions
from .db import canonical_json, connect, init_db
from .ingest import IngestError, create_snapshot, ingest_csv, list_batches, list_snapshots
from .versions import VersionError

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
REPORT_PATH = re.compile(r"^/api/reports/([A-Za-z0-9_]+)(?:/([A-Za-z0-9_.-]+))?$")
COMBO_PATH = re.compile(r"^/api/combos/([A-Za-z0-9_]+)(?:/([a-z-]+))?$")
STORE_PATH = re.compile(r"^/api/stores/([A-Za-z0-9_-]+)$")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class State:
    def __init__(self, db_path: str, data_dir: Path):
        self.db_path = db_path
        self.data_dir = data_dir
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.ingest_lock = threading.Lock()

    def start_worker(self) -> None:
        self.worker = threading.Thread(
            target=reports.worker_loop, args=(self.db_path, self.stop), daemon=True
        )
        self.worker.start()

    def shutdown(self) -> None:
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=5)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    if length > 2_000_000:
        raise ApiError("请求体过大", 413)
    raw = handler.rfile.read(length)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("请求体不是有效 JSON") from None
    if not isinstance(value, dict):
        raise ApiError("请求体必须是 JSON 对象")
    return value


def _safe_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def make_handler(state: State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "PromoLens/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
            if os.environ.get("PROMOLENS_HTTP_LOG"):
                super().log_message(fmt, *args)

        # -- helpers ---------------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8", extra)

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": message})

        def _static(self, path: Path) -> None:
            if not path.exists() or not path.is_file():
                self._error(404, "资源不存在")
                return
            resolved = path.resolve()
            if WEB_ROOT.resolve() not in resolved.parents and resolved != WEB_ROOT.resolve():
                self._error(403, "禁止访问")
                return
            content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in (
                "application/javascript",
                "application/json",
            ):
                content_type += "; charset=utf-8"
            self._send(200, resolved.read_bytes(), content_type)

        # -- routing ---------------------------------------------------------
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if path.startswith("/api/"):
                    self._api_get(path, query)
                    return
                if path in ("/", "/index.html"):
                    self._static(WEB_ROOT / "index.html")
                    return
                self._static(WEB_ROOT / path.lstrip("/"))
            except ApiError as exc:
                self._error(exc.status, str(exc))
            except (VersionError, IngestError, reports.ReportError) as exc:
                self._error(400, str(exc))
            except Exception as exc:  # pragma: no cover
                traceback.print_exc()
                self._error(500, f"服务器错误：{exc}")

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            try:
                body = _read_json(self)
                self._api_post(path, body)
            except ApiError as exc:
                self._error(exc.status, str(exc))
            except (VersionError, IngestError, reports.ReportError) as exc:
                self._error(400, str(exc))
            except Exception as exc:  # pragma: no cover
                traceback.print_exc()
                self._error(500, f"服务器错误：{exc}")

        # -- GET endpoints ---------------------------------------------------
        def _api_get(self, path: str, query: dict[str, list[str]]) -> None:
            db = state.db_path
            if path == "/api/health":
                self._json(200, {"ok": True, "db": os.path.basename(db)})
                return
            if path == "/api/bootstrap":
                snapshots = list_snapshots(db, limit=10)
                self._json(
                    200,
                    {
                        "methods": versions.list_methods(db),
                        "rules": versions.list_rules(db),
                        "cohorts": versions.list_cohorts(db),
                        "active_rule": versions.active_rule(db),
                        "active_cohort": versions.active_cohort(db),
                        "snapshots": snapshots,
                        "latest_snapshot": snapshots[0] if snapshots else None,
                        "combos": reports.list_combos(db),
                        "engine_version": analysis.ENGINE_VERSION,
                        "limits": {
                            "min_orders_per_window": analysis.MIN_ORDERS_PER_WINDOW,
                            "outcome_horizon_days": analysis.OUTCOME_HORIZON_DAYS,
                        },
                        "store_regions": _distinct(db, "region"),
                        "store_formats": _distinct(db, "format"),
                    },
                )
                return
            if path == "/api/state":
                self._json(200, reports.system_state(db))
                return
            if path == "/api/combos":
                self._json(200, {"combos": reports.list_combos(db)})
                return
            match = COMBO_PATH.match(path)
            if match:
                combo_id, sub = match.groups()
                if sub == "versions":
                    self._json(
                        200,
                        {
                            "combo_id": combo_id,
                            "versions": reports.report_versions(db, combo_id),
                        },
                    )
                    return
            match = REPORT_PATH.match(path)
            if match:
                report_id, sub = match.groups()
                if sub is None:
                    report = reports.get_report(db, report_id)
                    report["staleness"] = reports.staleness(db, report)
                    self._json(200, report)
                    return
                if sub == "evidence":
                    self._evidence(report_id)
                    return
                if sub == "stores.csv":
                    self._stores_csv(report_id)
                    return
                if sub == "methodology.md":
                    self._methodology(report_id)
                    return
            match = STORE_PATH.match(path)
            if match:
                store_id = match.group(1)
                report_id = (query.get("report_id") or [""])[0]
                if not report_id:
                    raise ApiError("需要 report_id 参数")
                report = reports.get_report(db, report_id)
                spec = report["spec"]
                detail = analysis.store_detail(
                    db,
                    snapshot_hash=report["snapshot_hash"],
                    store_id=store_id,
                    rule=versions.get_rule(db, report["rule_version"]),
                    cohort=versions.get_cohort(db, report["cohort_version"]),
                )
                # attach this store's row from the report so the drawer and the
                # table agree exactly
                row = next(
                    (s for s in (report["payload"] or {}).get("stores", []) if s["store_id"] == store_id),
                    None,
                )
                detail["report_row"] = row
                detail["report"] = {
                    "report_id": report["report_id"],
                    "version_number": report["version_number"],
                    "snapshot_hash": report["snapshot_hash"],
                    "rule_version": report["rule_version"],
                }
                self._json(200, detail)
                return
            if path == "/api/ingest/batches":
                self._json(200, {"batches": list_batches(db)})
                return
            raise ApiError("接口不存在", 404)

        def _evidence(self, report_id: str) -> None:
            report = reports.get_report(state.db_path, report_id)
            if report["status"] != "final":
                raise ApiError("报告尚未完成，暂不能导出依据", 409)
            bundle = {
                "export_format": "promolens-evidence/1",
                "exported_at": versions.utcnow(),
                "report": {
                    key: report[key]
                    for key in (
                        "report_id",
                        "combo_id",
                        "combo_label",
                        "version_number",
                        "status",
                        "created_at",
                        "finalized_at",
                        "snapshot_hash",
                        "rule_version",
                        "cohort_version",
                        "method_version",
                        "engine_version",
                        "inputs_digest",
                        "payload_digest",
                        "method_params",
                        "spec",
                        "events",
                        "runs",
                    )
                },
                "result": report["payload"],
                "reproduce": {
                    "endpoint": "POST /api/reports",
                    "body": {
                        "method": report["spec"]["method_algorithm"],
                        "method_params": report["spec"]["method_params"],
                        "threshold": report["spec"]["threshold"],
                        "filters": report["spec"]["filters"],
                        "rule_version": report["rule_version"],
                        "cohort_version": report["cohort_version"],
                        "snapshot_hash": report["snapshot_hash"],
                        "label": f"复现 {report['report_id']}",
                    },
                },
            }
            body = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
            self._send(
                200,
                body,
                "application/json; charset=utf-8",
                {
                    "Content-Disposition": f'attachment; filename="promolens-{report_id}-evidence.json"'
                },
            )

        def _stores_csv(self, report_id: str) -> None:
            report = reports.get_report(state.db_path, report_id)
            if report["status"] != "final":
                raise ApiError("报告尚未完成，暂不能导出依据", 409)
            import csv
            import io

            payload = report["payload"]
            columns = [
                "store_id", "store_name", "region", "format", "group", "members",
                "size_sqm", "traffic_index", "urban_score", "member_share",
                "pre_orders", "campaign_orders", "attributed_pre_orders",
                "attributed_campaign_orders", "pre_rate_asof", "campaign_rate_asof",
                "outcome_asof", "outcome_final", "weight", "matched_to", "eligible",
                "exclusion_reason", "attributed_campaign_share", "refund_rate_campaign",
                "late_refund_cents_in_snapshot",
            ]
            out = io.StringIO()
            writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in payload["stores"]:
                writer.writerow(row)
            body = ("﻿" + out.getvalue()).encode("utf-8")
            self._send(
                200,
                body,
                "text/csv; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="promolens-{report_id}-stores.csv"'},
            )

        def _methodology(self, report_id: str) -> None:
            report = reports.get_report(state.db_path, report_id)
            if report["status"] != "final":
                raise ApiError("报告尚未完成，暂不能导出依据", 409)
            payload = report["payload"]
            spec = report["spec"]
            lines = [
                f"# PromoLens 报告 {report['report_id']}（{report['combo_label']} v{report['version_number']}）",
                "",
                f"- 生成时间：{payload['generated_at']}",
                f"- 快照：`{report['snapshot_hash']}`",
                f"- 归因规则：`{report['rule_version']}`（回看 {spec['rule']['lookback_hours']} 小时；"
                f"状态 {', '.join(spec['rule']['order_statuses'])}；渠道 {', '.join(spec['rule']['channel_scope'])}）",
                f"- 观察窗：`{report['cohort_version']}`（活动 {spec['cohort']['campaign_start']} ~ "
                f"{spec['cohort']['campaign_end']}；期前 {spec['cohort']['pre_start']} ~ {spec['cohort']['pre_end']}；"
                f"退款截止 {spec['cohort']['refund_cutoff']}，口径 {spec['cohort']['refund_mode']}）",
                f"- 方法：{spec['method']['label']}（`{report['method_version']}`，参数 {canonical_json(spec['method_params'])}）",
                f"- 输入摘要：`{report['inputs_digest']}`",
                f"- 结果摘要：`{payload['payload_digest']}`",
                f"- 引擎版本：{report['engine_version']}",
                "",
                "## 估计量",
                "",
                payload["estimand"],
                "",
                payload["caveat"],
                "",
                "## 结果",
                "",
                f"- 原始差异：{payload['headline']['raw_difference']} 元/会员/28天",
                f"- 调整后估计：{payload['headline']['adjusted_estimate']} 元/会员/28天",
                f"- 区间：{payload['headline']['ci']['ci_low']} ~ {payload['headline']['ci']['ci_high']}"
                f"（{payload['headline']['ci']['note']}）",
                f"- 使用门店：促销 {payload['headline']['n_stores_promo']} 家 / 对照 "
                f"{payload['headline']['n_stores_control']} 家；会员 {payload['headline']['n_members_promo']} / "
                f"{payload['headline']['n_members_control']}",
                "",
                "## 假设与边界",
                "",
                "- 分组不是随机分配；匹配/分层只能平衡已观测特征。",
                "- 匹配假设：已观测门店特征足以解释分组与结果的共同原因（条件可忽略性），且重叠区间内存在可比对照。",
                "- 分层假设：层内分组近似随机，且层之间处理效应可按会员数加权平均。",
                "- 退款：较早口径只计退款截止日前到账的退款；截止日后到账的退款需要回补并重算才会进入结果。",
                "- 归因：曝光归属规则是近似口径，不区分自然流量与促销增量。",
                "",
                "## 被排除样本",
                "",
                f"- 门店：合格 {payload['exclusions']['counts']['stores_eligible']} / 共 "
                f"{payload['exclusions']['counts']['stores_total']}；方法额外排除 "
                f"{len(payload['exclusions']['method_level'])} 家。",
            ]
            for item in payload["exclusions"]["method_level"]:
                lines.append(
                    f"  - {item['store_id']} {item.get('store_name', '')}：{item['reason']}"
                )
            row_level = payload["exclusions"]["row_level"]
            lines.extend(
                [
                    "",
                    "订单行排除：",
                    f"- 状态不符 {row_level['orders_excluded_status']} 行；渠道不符 "
                    f"{row_level['orders_excluded_channel']} 行；金额为 0 {row_level['orders_excluded_gross']} 行；"
                    f"主数据缺失 {row_level['orders_unknown_store']} 行。",
                    "",
                    "## 共同支持",
                    "",
                    canonical_json(payload["support"]),
                    "",
                    "## 敏感性",
                    "",
                ]
            )
            for item in payload["sensitivity"]:
                lines.append(
                    f"- {item['name']}：{item['estimate']}（相对主估计 {item['delta_vs_primary']:+}）；{item['detail']}"
                )
            lines.extend(
                [
                    "",
                    "## 曝光归因",
                    "",
                    f"- 活动期订单（按当前规则计入）：{payload['attribution']['campaign_eligible_orders']} 笔；"
                    f"其中可归因 {payload['attribution']['campaign_attributed_orders']} 笔。",
                    f"- {payload['attribution']['note']}",
                    "- 归因覆盖率是口径覆盖，不是增量，也不能当作提升幅度。",
                    "",
                    "## 复现",
                    "",
                    "```json",
                    json.dumps(
                        {
                            "method": spec["method_algorithm"],
                            "method_params": spec["method_params"],
                            "threshold": spec["threshold"],
                            "filters": spec["filters"],
                            "rule_version": report["rule_version"],
                            "cohort_version": report["cohort_version"],
                            "snapshot_hash": report["snapshot_hash"],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "```",
                    "",
                ]
            )
            body = "\n".join(lines).encode("utf-8")
            self._send(
                200,
                body,
                "text/markdown; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="promolens-{report_id}-methodology.md"'},
            )

        # -- POST endpoints --------------------------------------------------
        def _api_post(self, path: str, body: dict[str, Any]) -> None:
            db = state.db_path
            if path == "/api/reports":
                method = body.get("method", "raw")
                if method not in ("raw", "matched", "stratified"):
                    raise ApiError("method 必须是 raw / matched / stratified")
                report = reports.enqueue_report(
                    db,
                    label=body.get("label") or None,
                    method_algorithm=method,
                    method_params=body.get("method_params") or None,
                    threshold=(
                        body["threshold"]
                        if body.get("threshold") is not None
                        else analysis.MIN_ORDERS_PER_WINDOW
                    ),
                    filters=body.get("filters") or None,
                    rule_version=body.get("rule_version") or None,
                    cohort_version=body.get("cohort_version") or None,
                    rule_overrides=body.get("rule_overrides") or None,
                    cohort_overrides=body.get("cohort_overrides") or None,
                    snapshot_hash=body.get("snapshot_hash") or None,
                    recomputes_report_id=body.get("recomputes_report_id") or None,
                )
                self._json(202, report)
                return
            if path == "/api/compare":
                left = body.get("left")
                right = body.get("right")
                if not left or not right:
                    raise ApiError("需要 left 与 right 两个报告 id")
                self._json(200, reports.compare_reports(db, left, right))
                return
            if path == "/api/rule-versions":
                payload = versions.create_rule_version(db, body)
                self._json(201, payload)
                return
            if path == "/api/cohort-versions":
                payload = versions.create_cohort_version(db, body)
                self._json(201, payload)
                return
            if path == "/api/ingest/backfill":
                self._backfill(body)
                return
            if path == "/api/ingest/file":
                table = body.get("table")
                filename = body.get("filename")
                if table not in ingest.TABLES or not filename:
                    raise ApiError("需要合法的 table 与 filename")
                candidate = (state.data_dir / filename).resolve()
                if state.data_dir.resolve() not in candidate.parents:
                    raise ApiError("只允许读取演示数据目录内的文件", 403)
                if not candidate.exists():
                    raise ApiError(f"文件不存在：{filename}", 404)
                with state.ingest_lock:
                    batch = ingest_csv(db, table, candidate)
                    snapshot = create_snapshot(db)
                self._json(
                    200,
                    {"batch": batch, "snapshot": _snapshot_summary(snapshot)},
                )
                return
            raise ApiError("接口不存在", 404)

        def _backfill(self, body: dict[str, Any]) -> None:
            db = state.db_path
            filename = body.get("filename") or "refunds_backfill.csv"
            candidate = (state.data_dir / filename).resolve()
            if state.data_dir.resolve() not in candidate.parents:
                raise ApiError("只允许读取演示数据目录内的文件", 403)
            if not candidate.exists():
                raise ApiError(f"未找到回补文件：{filename}", 404)
            with state.ingest_lock:
                before = (list_snapshots(db, limit=1) or [None])[0]
                batch = ingest_csv(db, "refunds", candidate)
                snapshot = create_snapshot(db)
            conn = connect(db)
            try:
                stale = conn.execute(
                    "SELECT COUNT(*) FROM reports WHERE status='final'"
                ).fetchone()[0]
            finally:
                conn.close()
            self._json(
                200,
                {
                    "batch": batch,
                    "snapshot": _snapshot_summary(snapshot),
                    "previous_snapshot": before["snapshot_hash"] if before else None,
                    "reports_now_stale": stale,
                    "note": "回补已写入落地表并生成新快照。已有报告保持原版本与结果，不会被改写；需要重算才能纳入新退款。",
                },
            )

    return Handler


def _distinct(db_path: str, column: str) -> list[str]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT {column} AS v FROM stores ORDER BY {column}"
        ).fetchall()
    finally:
        conn.close()
    return [r["v"] for r in rows]


def _snapshot_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_hash": snapshot["snapshot_hash"],
        "row_counts": snapshot["row_counts"],
        "max_ingest_seq": snapshot["max_ingest_seq"],
        "reused": snapshot.get("reused", False),
        "table_digests": snapshot.get("table_digests"),
        "batch_ids": snapshot.get("batch_ids"),
    }


def serve(db_path: str, data_dir: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    init_db(db_path)
    versions.seed_versions(db_path)
    state = State(db_path, Path(data_dir))
    state.start_worker()
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.daemon_threads = True
    print(f"PromoLens 正在 http://{host}:{port} 运行（db={db_path}）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…", flush=True)
    finally:
        state.shutdown()
        server.server_close()
