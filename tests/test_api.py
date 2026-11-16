"""HTTP API: bootstrap, report lifecycle, evidence exports, compare, ingest."""
from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.parse
import urllib.request

from .fixtures import ServerFixture, get_env, wait_until


def request(base: str, path: str, *, method: str = "GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            raw = response.read()
            return response.status, response.headers, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def json_request(base: str, path: str, *, method: str = "GET", body=None):
    status, headers, raw = request(base, path, method=method, body=body)
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    return status, headers, payload


class ApiTests(ServerFixture):
    def test_static_page_and_assets(self):
        for path, needle in (
            ("/", b"PromoLens"),
            ("/app.js", b"loadBootstrap"),
            ("/views.js", b"renderCompare"),
            ("/styles.css", b"--promo"),
        ):
            status, headers, raw = request(self.base, path)
            self.assertEqual(status, 200, path)
            self.assertIn(needle, raw)
        status, _, payload = json_request(
            self.base, "/..%2F..%2Fpromolens%2Fdb.py"
        )
        self.assertIn(status, (403, 404))

    def test_bootstrap_exposes_versions_and_filters(self):
        status, _, payload = json_request(self.base, "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(
            {m["algorithm"] for m in payload["methods"]},
            {"raw", "matched", "stratified"},
        )
        self.assertTrue(payload["rules"])
        self.assertTrue(payload["cohorts"])
        self.assertTrue(payload["snapshots"])
        self.assertIn("华东", payload["store_regions"])
        self.assertIn("大卖场", payload["store_formats"])
        self.assertEqual(payload["latest_snapshot"], payload["snapshots"][0])

    def _enqueue_and_wait(self, body: dict) -> dict:
        status, _, created = json_request(
            self.base, "/api/reports", method="POST", body=body
        )
        self.assertEqual(status, 202, created)
        report_id = created["report_id"]

        def done():
            code, _, report = json_request(self.base, f"/api/reports/{report_id}")
            return report if code == 200 and report["status"] in ("final", "failed") else None

        report = wait_until(done, timeout=180)
        self.assertEqual(report["status"], "final", report)
        return report

    def test_report_lifecycle_and_exports(self):
        report = self._enqueue_and_wait({"method": "matched", "threshold": 15})
        self.assertIn("payload", report)
        self.assertEqual(report["payload_digest"], report["payload"]["payload_digest"])
        self.assertTrue(report["staleness"] is not None)

        status, headers, raw = request(
            self.base, f"/api/reports/{report['report_id']}/evidence"
        )
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        bundle = json.loads(raw)
        self.assertEqual(bundle["report"]["report_id"], report["report_id"])
        self.assertEqual(
            bundle["result"]["payload_digest"], report["payload_digest"]
        )
        reproduce = bundle["reproduce"]["body"]
        self.assertEqual(reproduce["snapshot_hash"], report["snapshot_hash"])
        self.assertEqual(reproduce["rule_version"], report["rule_version"])

        status, headers, raw = request(
            self.base, f"/api/reports/{report['report_id']}/stores.csv"
        )
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type"))
        text = raw.decode("utf-8-sig")
        self.assertIn("store_id,store_name", text)
        self.assertEqual(len(text.strip().splitlines()), 73)  # header + 72 stores

        status, headers, raw = request(
            self.base, f"/api/reports/{report['report_id']}/methodology.md"
        )
        self.assertEqual(status, 200)
        markdown = raw.decode("utf-8")
        self.assertIn("# PromoLens 报告", markdown)
        self.assertIn("## 假设与边界", markdown)
        self.assertIn("## 曝光归因", markdown)
        self.assertIn("## 敏感性", markdown)
        self.assertIn(report["snapshot_hash"], markdown)

    def test_compare_endpoint_and_errors(self):
        left = self._enqueue_and_wait({"method": "raw", "threshold": 16})
        right = self._enqueue_and_wait({"method": "stratified", "threshold": 16})
        status, _, payload = json_request(
            self.base,
            "/api/compare",
            method="POST",
            body={"left": left["report_id"], "right": right["report_id"]},
        )
        self.assertEqual(status, 200)
        self.assertIn("method_version", payload["changed_inputs"])
        self.assertEqual(
            payload["metrics"]["adjusted_estimate"]["left"],
            left["payload"]["headline"]["adjusted_estimate"],
        )
        status, _, payload = json_request(
            self.base, "/api/compare", method="POST", body={"left": left["report_id"]}
        )
        self.assertEqual(status, 400)
        self.assertIn("left", payload["error"])
        status, _, payload = json_request(
            self.base,
            "/api/compare",
            method="POST",
            body={"left": "rpt_missing", "right": right["report_id"]},
        )
        self.assertEqual(status, 400)

    def test_store_drilldown_uses_report_binding(self):
        report = self._enqueue_and_wait({"method": "matched", "threshold": 15})
        store_id = report["payload"]["stores"][0]["store_id"]
        status, _, detail = json_request(
            self.base, f"/api/stores/{store_id}?report_id={report['report_id']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["store"]["store_id"], store_id)
        self.assertEqual(detail["report"]["report_id"], report["report_id"])
        self.assertTrue(detail["series"])
        self.assertIn("report_row", detail)
        status, _, payload = json_request(
            self.base, f"/api/stores/{store_id}"
        )
        self.assertEqual(status, 400)
        self.assertIn("report_id", payload["error"])

    def test_backfill_endpoint_creates_snapshot_but_not_rewrite_reports(self):
        report = self._enqueue_and_wait({"method": "raw", "threshold": 17})
        status, _, payload = json_request(
            self.base, "/api/ingest/backfill", method="POST", body={}
        )
        self.assertEqual(status, 200, payload)
        self.assertNotEqual(
            payload["snapshot"]["snapshot_hash"], report["snapshot_hash"]
        )
        self.assertGreaterEqual(payload["reports_now_stale"], 1)
        _, _, after = json_request(self.base, f"/api/reports/{report['report_id']}")
        self.assertEqual(after["snapshot_hash"], report["snapshot_hash"])
        self.assertEqual(after["payload_digest"], report["payload_digest"])
        # the newest snapshot is now the one offered to new reports
        _, _, boot = json_request(self.base, "/api/bootstrap")
        self.assertEqual(
            boot["latest_snapshot"]["snapshot_hash"],
            payload["snapshot"]["snapshot_hash"],
        )

    def test_ingest_file_route_rejects_traversal(self):
        status, _, payload = json_request(
            self.base,
            "/api/ingest/file",
            method="POST",
            body={"table": "exposure", "filename": "../../etc/passwd"},
        )
        self.assertEqual(status, 403)
        self.assertIn("演示数据目录", payload["error"])

    def test_invalid_requests(self):
        status, _, payload = json_request(
            self.base, "/api/reports", method="POST", body={"method": "magic"}
        )
        self.assertEqual(status, 400)
        status, _, payload = json_request(
            self.base, "/api/reports", method="POST", body={"threshold": 0}
        )
        self.assertEqual(status, 400)
        status, _, payload = json_request(self.base, "/api/nope")
        self.assertEqual(status, 404)
        status, _, payload = json_request(
            self.base, "/api/reports", method="POST", body={"filters": "regions"}
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
