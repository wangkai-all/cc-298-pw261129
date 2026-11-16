"""Concurrency: ingest + backfill + version change + enqueue happening together."""
from __future__ import annotations

import json
import threading
import time
import unittest

from promolens import ingest, reports, versions
from promolens.db import connect
from promolens.ingest import create_snapshot

from .fixtures import get_env


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.env = get_env()
        self.db = self.env.db_path
        self.data = self.env.data_dir

    def test_parallel_enqueue_gets_distinct_versions(self):
        reports.drain(self.db, timeout=180)
        results = []
        errors = []

        def worker(i: int) -> None:
            try:
                report = reports.enqueue_report(
                    self.db,
                    method_algorithm="raw",
                    label=f"并发入队-{i}",
                    threshold=120 + i,
                )
                results.append(report)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        combos = {r["combo_id"] for r in results}
        self.assertEqual(len(combos), 4, "阈值不同 -> 不同组合")
        for report in results:
            self.assertGreaterEqual(report["version_number"], 1)

        # same combo from parallel requests must still get unique version numbers
        same = []
        errors.clear()

        def same_worker() -> None:
            try:
                same.append(
                    reports.enqueue_report(
                        self.db,
                        method_algorithm="raw",
                        label="并发同组合",
                        threshold=140,
                    )
                )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=same_worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        version_numbers = sorted(r["version_number"] for r in same)
        self.assertEqual(version_numbers, sorted(set(version_numbers)))
        reports.drain(self.db, timeout=180)
        finals = 0
        for report in same:
            stored = reports.get_report(self.db, report["report_id"])
            if stored["status"] == "final":
                finals += 1
            else:
                self.assertEqual(stored["status"], "superseded")
                self.assertIsNone(stored["payload"])
        # at most one version per combo can be "current", but versions that had
        # already finished before a newer one was enqueued keep their result
        self.assertGreaterEqual(finals, 1)
        latest = reports.report_versions(self.db, same[0]["combo_id"])[0]
        self.assertEqual(latest["status"], "final")
        self.assertEqual(latest["version_number"], max(version_numbers))
        for report in same:
            stored = reports.get_report(self.db, report["report_id"])
            if stored["status"] == "final":
                self.assertIsNotNone(stored["payload"])
                self.assertEqual(stored["snapshot_hash"], latest["snapshot_hash"])
            else:
                self.assertEqual(stored["status"], "superseded")

    def test_ingest_during_run_binds_old_snapshot(self):
        reports.drain(self.db, timeout=180)
        before = ingest.list_snapshots(self.db, limit=1)[0]
        report = reports.enqueue_report(
            self.db, method_algorithm="raw", label="接入口径并发", threshold=15
        )
        # land more refunds and freeze a new snapshot while the report waits
        ingest.ingest_csv(self.db, "refunds", self.data / "refunds_backfill.csv")
        create_snapshot(self.db)
        reports.drain(self.db, timeout=180)
        done = reports.get_report(self.db, report["report_id"])
        self.assertEqual(done["status"], "final")
        self.assertEqual(done["snapshot_hash"], before["snapshot_hash"])
        self.assertEqual(
            done["payload"]["inputs"]["snapshot_row_counts"]["refunds"],
            before["row_counts"]["refunds"],
        )

    def test_rule_change_during_run_does_not_alter_resolved_rule(self):
        reports.drain(self.db, timeout=180)
        report = reports.enqueue_report(
            self.db, method_algorithm="raw", label="规则并发", threshold=15
        )
        original_rule = report["rule_version"]
        versions.create_rule_version(
            self.db,
            {
                "label": "并发新规则",
                "lookback_hours": 6,
                "order_statuses": ["completed"],
                "channel_scope": ["app"],
                "exposure_kinds": ["campaign"],
            },
        )
        reports.drain(self.db, timeout=180)
        done = reports.get_report(self.db, report["report_id"])
        self.assertEqual(done["rule_version"], original_rule)
        self.assertEqual(
            done["spec"]["rule"]["lookback_hours"],
            versions.get_rule(self.db, original_rule)["lookback_hours"],
        )
        self.assertNotEqual(done["spec"]["rule"]["lookback_hours"], 6)
        state = reports.staleness(self.db, done)
        self.assertTrue(state["rule_changed"])

    def test_no_partial_report_rows_after_concurrent_activity(self):
        conn = connect(self.db)
        try:
            broken_final = conn.execute(
                "SELECT COUNT(*) FROM reports WHERE status='final' AND payload IS NULL"
            ).fetchone()[0]
            broken_computing = conn.execute(
                "SELECT COUNT(*) FROM reports WHERE status='computing'"
            ).fetchone()[0]
            orphan_runs = conn.execute(
                "SELECT COUNT(*) FROM analysis_runs a LEFT JOIN reports r "
                "ON r.report_id=a.report_id WHERE r.report_id IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(broken_final, 0)
        self.assertEqual(broken_computing, 0)
        self.assertEqual(orphan_runs, 0)


if __name__ == "__main__":
    unittest.main()
