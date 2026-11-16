"""Ingest validation, idempotency, snapshot immutability and version retirement."""
from __future__ import annotations

import unittest
from pathlib import Path

from promolens import ingest, reports, versions
from promolens.db import connect

from .fixtures import drain, enqueue, get_env


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.env = get_env()
        self.db = self.env.db_path
        self.data = self.env.data_dir

    def test_rejects_are_quarantined(self):
        reasons = []
        conn = connect(self.db)
        try:
            for row in conn.execute("SELECT table_name, reason FROM ingest_rejects"):
                reasons.append(row["reason"])
        finally:
            conn.close()
        joined = " ".join(reasons)
        self.assertIn("net_cents", joined)
        self.assertIn("carrier-pigeon", joined)
        self.assertIn("exposed_at", joined)
        self.assertGreaterEqual(len(reasons), 3)

    def test_reingest_is_idempotent(self):
        first = ingest.ingest_csv(self.db, "refunds", self.data / "refunds_initial.csv")
        second = ingest.ingest_csv(self.db, "refunds", self.data / "refunds_initial.csv")
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["unchanged"], first["rows_read"] - first["rejected"])
        snapshot = ingest.create_snapshot(self.db)
        self.assertTrue(snapshot["reused"])

    def test_bad_header_fails_loudly(self):
        tmp = Path("/tmp/promolens-bad.csv")
        tmp.write_text("store_id,store_name\nS999,nope\n", encoding="utf-8")
        with self.assertRaises(ingest.IngestError):
            ingest.ingest_csv(self.db, "stores", tmp)
        failed = [b for b in ingest.list_batches(self.db) if b["status"] == "failed"]
        self.assertTrue(failed)

    def test_report_binds_snapshot_that_later_ingest_cannot_change(self):
        before = ingest.list_snapshots(self.db, limit=1)[0]
        report = enqueue(method_algorithm="raw", threshold=15)
        self.assertEqual(report["status"], "final")
        stored_hash = report["snapshot_hash"]
        self.assertEqual(stored_hash, before["snapshot_hash"])

        extra = Path("/tmp/promolens-late-refund.csv")
        extra.write_text(
            "refund_id,order_id,customer_id,store_id,refunded_at,amount_cents\n"
            "RB-TEST-1,O00000001,CS001-00001,S001,2026-09-20T10:00:00Z,1500\n",
            encoding="utf-8",
        )
        ingest.ingest_csv(self.db, "refunds", extra)
        after = ingest.create_snapshot(self.db)
        self.assertNotEqual(before["snapshot_hash"], after["snapshot_hash"])
        self.assertEqual(after["row_counts"]["refunds"], before["row_counts"]["refunds"] + 1)

        again = reports.get_report(self.db, report["report_id"])
        self.assertEqual(again["snapshot_hash"], stored_hash)
        self.assertEqual(
            again["payload"]["inputs"]["snapshot_row_counts"]["refunds"],
            before["row_counts"]["refunds"],
        )
        old = ingest.get_snapshot(self.db, stored_hash)
        self.assertEqual(old["row_counts"]["refunds"], before["row_counts"]["refunds"])
        self.assertNotEqual(old["max_ingest_seq"], after["max_ingest_seq"])
        # the payload digest must not have moved either
        self.assertEqual(again["payload_digest"], report["payload_digest"])


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.env = get_env()
        self.db = self.env.db_path

    def test_new_rule_version_retires_old_without_mutation(self):
        old = versions.active_rule(self.db)
        created = versions.create_rule_version(
            self.db,
            {
                "label": "测试规则",
                "lookback_hours": 12,
                "order_statuses": ["completed"],
                "channel_scope": ["app"],
                "exposure_kinds": ["campaign"],
                "require_same_store": True,
                "post_exposure_only": True,
            },
        )
        self.assertNotEqual(created["rule_version"], old["rule_version"])
        self.assertEqual(created["status"], "active")
        stale = versions.get_rule(self.db, old["rule_version"])
        self.assertEqual(stale["status"], "retired")
        self.assertEqual(stale["lookback_hours"], old["lookback_hours"])
        self.assertEqual(stale["channel_scope"], old["channel_scope"])
        self.assertEqual(stale["order_statuses"], old["order_statuses"])

    def test_invalid_rule_rejected(self):
        with self.assertRaises(versions.VersionError):
            versions.create_rule_version(self.db, {"lookback_hours": 0})
        with self.assertRaises(versions.VersionError):
            versions.create_rule_version(
                self.db, {"label": "x", "order_statuses": ["teleported"]}
            )
        with self.assertRaises(versions.VersionError):
            versions.create_rule_version(
                self.db, {"label": "x", "channel_scope": ["pigeon"]}
            )

    def test_cohort_validation(self):
        base = {
            "label": "坏窗口",
            "campaign_start": "2026-06-29",
            "campaign_end": "2026-08-23",
            "pre_start": "2026-04-06",
            "pre_end": "2026-06-28",
            "refund_cutoff": "2026-09-06T23:59:59Z",
            "refund_mode": "as_of_cutoff",
        }
        with self.assertRaises(versions.VersionError):
            versions.create_cohort_version(self.db, {**base, "pre_end": "2026-07-01"})
        with self.assertRaises(versions.VersionError):
            versions.create_cohort_version(self.db, {**base, "refund_mode": "whatever"})
        with self.assertRaises(versions.VersionError):
            versions.create_cohort_version(
                self.db, {**base, "refund_cutoff": "2026-08-01T00:00:00Z"}
            )
        created = versions.create_cohort_version(
            self.db, {**base, "label": "合法窗口", "refund_mode": "final_observed"}
        )
        self.assertEqual(created["refund_mode"], "final_observed")
        self.assertEqual(created["refund_cutoff"], "2026-09-06T23:59:59Z")

    def test_method_versions_are_stable(self):
        methods = versions.list_methods(get_env().db_path)
        algorithms = {m["algorithm"] for m in methods}
        self.assertEqual(algorithms, {"raw", "matched", "stratified"})
        for method in methods:
            self.assertEqual(method["method_version"], f"{method['algorithm']}-v1")
            self.assertIsInstance(method["default_params"], dict)


if __name__ == "__main__":
    unittest.main()
