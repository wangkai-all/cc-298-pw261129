"""Report versioning, staleness rules, compare and analysis determinism."""
from __future__ import annotations

import json
import unittest

from promolens import analysis, reports, versions
from promolens.db import connect

from .fixtures import enqueue, get_env


class ReportVersionTests(unittest.TestCase):
    def setUp(self):
        self.env = get_env()
        self.db = self.env.db_path

    def test_versions_increase_and_old_results_stay_frozen(self):
        first = enqueue(method_algorithm="raw", label="版本测试", threshold=15)
        second = enqueue(
            method_algorithm="raw",
            label="版本测试",
            threshold=15,
            recomputes_report_id=first["report_id"],
        )
        self.assertEqual(first["combo_id"], second["combo_id"])
        self.assertEqual(second["version_number"], first["version_number"] + 1)
        self.assertEqual(second["recomputes_report_id"], first["report_id"])
        stored_first = reports.get_report(self.db, first["report_id"])
        self.assertEqual(stored_first["status"], "final")
        self.assertEqual(stored_first["payload_digest"], first["payload_digest"])
        versions_list = reports.report_versions(self.db, first["combo_id"])
        self.assertGreaterEqual(len(versions_list), 2)
        self.assertTrue(all(v["status"] in ("final", "superseded") for v in versions_list))

    def test_stale_report_is_discarded_not_overwritten(self):
        report = reports.enqueue_report(
            self.db, method_algorithm="raw", label="并发覆盖测试", threshold=15
        )
        claimed = reports._claim_next(self.db)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["report_id"], report["report_id"])
        conn = connect(self.db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE reports SET status='superseded', superseded_by='rpt_newer' "
                "WHERE report_id=?",
                (report["report_id"],),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
        outcome = reports._finalize(
            self.db,
            report["report_id"],
            claimed["run_id"],
            {"payload_digest": "x" * 64, "headline": {}},
        )
        self.assertEqual(outcome, "discarded")
        conn = connect(self.db)
        try:
            row = conn.execute(
                "SELECT status, payload FROM reports WHERE report_id=?", (report["report_id"],)
            ).fetchone()
            run_row = conn.execute(
                "SELECT status FROM analysis_runs WHERE run_id=?", (claimed["run_id"],)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "superseded")
        self.assertIsNone(row["payload"])
        self.assertEqual(run_row["status"], "discarded")

    def test_new_enqueue_supersedes_pending_same_combo(self):
        first = reports.enqueue_report(
            self.db, method_algorithm="raw", label="覆盖队列", threshold=15
        )
        claimed = reports._claim_next(self.db)
        self.assertEqual(claimed["report_id"], first["report_id"])
        second = reports.enqueue_report(
            self.db, method_algorithm="raw", label="覆盖队列", threshold=15
        )
        first_pending = reports.get_report(self.db, first["report_id"])
        self.assertEqual(first_pending["status"], "superseded")
        self.assertEqual(first_pending["superseded_by"], second["report_id"])
        # the superseded run can still finish, but its output is dropped
        outcome = reports.execute_run(self.db, claimed)
        self.assertEqual(outcome, "discarded")
        reports.drain(self.db, timeout=180)
        first_done = reports.get_report(self.db, first["report_id"])
        second_done = reports.get_report(self.db, second["report_id"])
        self.assertEqual(first_done["status"], "superseded")
        self.assertIsNone(first_done["payload"])
        self.assertEqual(second_done["status"], "final")
        self.assertIsNotNone(second_done["payload"])

    def test_compare_lists_input_differences(self):
        raw = enqueue(method_algorithm="raw", label="对比-A", threshold=15)
        matched = enqueue(method_algorithm="matched", label="对比-B", threshold=15)
        result = reports.compare_reports(self.db, raw["report_id"], matched["report_id"])
        self.assertIn("method_version", result["changed_inputs"])
        self.assertIn("adjusted_estimate", result["metrics"])
        self.assertEqual(
            result["metrics"]["raw_difference"]["left"],
            raw["payload"]["headline"]["raw_difference"],
        )
        # same inputs, different snapshot: snapshot digest has to show up
        self.assertNotEqual(result["inputs_left"]["inputs_digest"], result["inputs_right"]["inputs_digest"])

    def test_staleness_flags_newer_rule_and_snapshot(self):
        report = enqueue(method_algorithm="raw", label="陈旧测试", threshold=15)
        state = reports.staleness(self.db, report)
        self.assertFalse(state["rule_changed"] and state["cohort_changed"])
        versions.create_rule_version(
            self.db,
            {
                "label": "使报告陈旧",
                "lookback_hours": 36,
                "order_statuses": ["completed", "shipped"],
                "channel_scope": ["*"],
                "exposure_kinds": ["campaign", "regular"],
            },
        )
        state = reports.staleness(
            self.db, reports.get_report(self.db, report["report_id"])
        )
        self.assertTrue(state["rule_changed"])
        self.assertNotEqual(state["active_rule_version"], report["rule_version"])


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.env = get_env()
        self.db = self.env.db_path

    def _run(self, algorithm, **overrides):
        return enqueue(
            method_algorithm=algorithm,
            label=f"分析-{algorithm}-{overrides.get('threshold', 15)}",
            threshold=overrides.pop("threshold", 15),
            **overrides,
        )

    def test_methods_trade_samples_for_balance(self):
        raw = self._run("raw")
        matched = self._run("matched")
        stratified = self._run("stratified")
        for report in (raw, matched, stratified):
            self.assertEqual(report["status"], "final")
            payload = report["payload"]
            self.assertIn("headline", payload)
            self.assertNotEqual(
                payload["headline"]["adjusted_estimate"],
                payload["caveat"],
            )
        self.assertGreater(
            raw["payload"]["headline"]["n_stores_promo"]
            + raw["payload"]["headline"]["n_stores_control"],
            matched["payload"]["headline"]["n_stores_promo"]
            + matched["payload"]["headline"]["n_stores_control"],
        )
        self.assertLessEqual(
            matched["payload"]["balance_summary"]["max_abs_smd_adjusted"],
            matched["payload"]["balance_summary"]["max_abs_smd_unadjusted"],
        )
        self.assertIsNotNone(matched["payload"]["support"]["overlap_range"])
        self.assertGreater(
            stratified["payload"]["support"]["strata_dropped"], 0
        )
        # raw keeps every eligible store; matches none
        self.assertEqual(raw["payload"]["exclusions"]["method_level"], [])
        self.assertGreater(len(matched["payload"]["exclusions"]["method_level"]), 0)

    def test_headline_is_not_presented_as_causal_lift(self):
        raw = self._run("raw")
        payload = raw["payload"]
        self.assertIn("关联差异", payload["caveat"])
        self.assertIn("并非随机分配", payload["caveat"])
        for word in ("提升", "因果"):
            self.assertNotIn(word, payload["headline"]["interpretation"])
            self.assertNotIn(word, payload["estimand"])
        self.assertEqual(
            payload["headline"]["raw_difference"],
            round(
                payload["arms"]["promo"]["outcome_mean"]
                - payload["arms"]["control"]["outcome_mean"],
                3,
            ),
        )

    def test_deterministic_for_same_inputs(self):
        first = self._run("matched")
        second = self._run("matched")
        self.assertEqual(first["inputs_digest"], second["inputs_digest"])
        self.assertEqual(first["payload_digest"], second["payload_digest"])
        self.assertEqual(
            first["payload"]["headline"]["ci"], second["payload"]["headline"]["ci"]
        )

    def test_refund_mode_changes_outcome_and_reports_late_refunds(self):
        cutoff = self._run("raw", cohort_version="cohort-v1")
        final = self._run(
            "raw",
            cohort_overrides={
                "label": "含回补退款",
                "campaign_start": "2026-06-29",
                "campaign_end": "2026-08-23",
                "pre_start": "2026-04-06",
                "pre_end": "2026-06-28",
                "refund_cutoff": "2026-09-06T23:59:59Z",
                "refund_mode": "final_observed",
            },
        )
        self.assertEqual(cutoff["spec"]["cohort"]["refund_mode"], "as_of_cutoff")
        self.assertEqual(final["spec"]["cohort"]["refund_mode"], "final_observed")
        self.assertNotEqual(cutoff["inputs_digest"], final["inputs_digest"])
        late_final = final["payload"]["refund_quality"]["refunds_after_cutoff_in_snapshot"]
        late_cutoff = cutoff["payload"]["refund_quality"]["refunds_after_cutoff_in_snapshot"]
        self.assertGreaterEqual(late_final, late_cutoff)
        self.assertTrue(
            final["payload"]["refund_quality"]["late_refunds_in_snapshot_affect_primary"]
        )
        self.assertFalse(
            cutoff["payload"]["refund_quality"]["late_refunds_in_snapshot_affect_primary"]
        )
        self.assertGreaterEqual(
            final["payload"]["refund_quality"]["refunds_in_snapshot"],
            cutoff["payload"]["refund_quality"]["refunds_in_snapshot"],
        )
        other = cutoff["payload"]["refund_quality"]["sensitivity_estimate_other_mode"]
        self.assertIsNotNone(other)

    def test_filters_exclude_stores_with_reasons(self):
        unfiltered = self._run("raw")
        filtered = self._run(
            "raw", filters={"regions": ["华东"], "formats": [], "exclude_store_ids": []}
        )
        self.assertLess(
            filtered["payload"]["exclusions"]["counts"]["stores_eligible"],
            unfiltered["payload"]["exclusions"]["counts"]["stores_eligible"],
        )
        reasons = " ".join(
            row.get("reason") or row.get("exclusion_reason") or ""
            for row in filtered["payload"]["exclusions"]["store_level"] or []
        )
        self.assertIn("页面筛选", reasons)
        store_id = unfiltered["payload"]["stores"][0]["store_id"]
        excluded = self._run(
            "raw",
            filters={
                "regions": [],
                "formats": [],
                "exclude_store_ids": [store_id],
            },
        )
        matched_row = next(
            s for s in excluded["payload"]["stores"] if s["store_id"] == store_id
        )
        self.assertFalse(matched_row["eligible"])
        self.assertIn("手动排除", matched_row["exclusion_reason"])

    def test_store_detail_matches_report_row(self):
        report = self._run("matched")
        store = next(
            s for s in report["payload"]["stores"] if s["eligible"] and s["weight"] > 0
        )
        detail = analysis.store_detail(
            self.db,
            snapshot_hash=report["snapshot_hash"],
            store_id=store["store_id"],
            rule=versions.get_rule(self.db, report["rule_version"]),
            cohort=versions.get_cohort(self.db, report["cohort_version"]),
        )
        self.assertTrue(detail["series"])
        self.assertEqual(detail["store"]["store_id"], store["store_id"])
        self.assertEqual(
            sum(w["orders"] for w in detail["series"]),
            store["pre_orders"] + store["campaign_orders"],
        )

    def test_attribution_counts_match_store_rows_and_rule(self):
        report = self._run("raw")
        attribution = report["payload"]["attribution"]
        self.assertEqual(attribution["rule_version"], report["rule_version"])
        self.assertEqual(
            attribution["campaign_eligible_orders"],
            sum(s["campaign_orders"] for s in report["payload"]["stores"]),
        )
        self.assertEqual(
            attribution["campaign_attributed_orders"],
            sum(s["attributed_campaign_orders"] for s in report["payload"]["stores"]),
        )
        self.assertLessEqual(
            attribution["campaign_attributed_orders"],
            attribution["campaign_eligible_orders"],
        )
        rows = attribution["sample_rows"]
        self.assertTrue(rows)
        hits = [r for r in rows if r["attributed"]]
        misses = [r for r in rows if not r["attributed"]]
        self.assertTrue(hits)
        self.assertTrue(misses)
        for row in hits:
            self.assertLessEqual(row["exposure_at"], row["ordered_at"])
            self.assertGreaterEqual(row["candidate_count"], 1)
        for row in misses:
            self.assertIsNone(row["exposure_id"])
            self.assertEqual(row["candidate_count"], 0)

    def test_evidence_export_contains_reproduction_request(self):
        report = self._run("stratified")
        reproduce = {
            "method": report["spec"]["method_algorithm"],
            "method_params": report["spec"]["method_params"],
            "threshold": report["spec"]["threshold"],
            "filters": report["spec"]["filters"],
            "rule_version": report["rule_version"],
            "cohort_version": report["cohort_version"],
            "snapshot_hash": report["snapshot_hash"],
        }
        rerun = enqueue(
            method_algorithm=reproduce["method"],
            method_params=reproduce["method_params"],
            threshold=reproduce["threshold"],
            filters=reproduce["filters"],
            rule_version=reproduce["rule_version"],
            cohort_version=reproduce["cohort_version"],
            snapshot_hash=reproduce["snapshot_hash"],
            label="复现测试",
        )
        self.assertEqual(rerun["inputs_digest"], report["inputs_digest"])
        self.assertEqual(rerun["payload_digest"], report["payload_digest"])


if __name__ == "__main__":
    unittest.main()
