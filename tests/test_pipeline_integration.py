"""Non-notebook integration: synthetic cache -> approve -> run_ledger."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (str(SRC), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import cache_io, pipeline
from corporate_actions import CA_CANONICAL_COLUMNS, reconcile_corporate_actions
from tests import test_research_pipeline as replay_tests


class PipelineIntegrationTests(unittest.TestCase):
    def test_synthetic_cache_review_run(self):
        w, prices, calendar, events = replay_tests.DailyReplayTests().fixtures()
        w = w.copy()
        w["nominal_date"] = w.date
        w["reference_date"] = w.date
        wrds = events.reindex(columns=CA_CANONICAL_COLUMNS)
        wrds["review_reason"] = ""
        wrds["source"] = "WRDS_CRSP"
        bbg = wrds.copy()
        bbg["source"] = "BLOOMBERG"
        bbg["source_event_id"] = "BBG:" + bbg.source_event_id.astype(str)
        unified = reconcile_corporate_actions(wrds, bbg)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "data" / "cache"
            review = root / "data" / "review"
            raw = root / "data" / "raw"
            out = root / "output" / "data"
            for p in (cache, review, raw, out):
                p.mkdir(parents=True)

            bloomberg = raw / "bloomberg_corporate_actions.csv"
            bloomberg.write_text("synthetic\n")
            namespace = dict(
                weights=w, wrds_corporate_actions=wrds,
                wrds_delist_events=wrds.iloc[:0],
                wrds_adj_factors=pd.DataFrame(columns=["permno", "dlycaldt", "dlycumfacshr"]),
                wrds_id_history=pd.DataFrame({"permno": [1]}),
                bloomberg_corporate_actions=bbg, unified_corporate_actions=unified,
                wrds_table_report=pd.DataFrame(columns=["table", "available"]),
                selected_permnos=[1], CA_START_DATE="2026-01-02", CA_END_DATE="2026-04-03",
            )
            cache_io.export_cache(namespace, cache)
            prices.to_csv(cache / "daily_prices.csv", index=False)
            pd.DataFrame({"date": calendar}).to_csv(cache / "calendar.csv", index=False)
            cache_io.write_coverage(
                cache, start="2026-01-02", end="2026-04-03", schema="crsp_q_stock",
                universe=[1], bloomberg_path=bloomberg)

            with mock.patch.object(pipeline, "CACHE_DIR", cache), \
                 mock.patch.object(pipeline, "REVIEW_DIR", review), \
                 mock.patch.object(pipeline, "OUTPUT_DIR", out), \
                 mock.patch.object(pipeline, "PROJECT_ROOT", root), \
                 mock.patch.object(cache_io, "CACHE_DIR", cache), \
                 mock.patch("pipeline.bloomberg_raw_path", return_value=bloomberg):
                cfg = pipeline.PipelineConfig(
                    start_date="2026-01-02", end_date="2026-04-03",
                    stock_schema="crsp_q_stock", weights_start="2025-01-01",
                    input_mode="cache", initial_cash=1000.0,
                    cash_buffer_bps=0.0, fee_bps=0.0, batch_size=150,
                    excluded_event_policy="keep_securities",
                    exclude_reverse_split_securities=False,
                )
                info = pipeline.review_init(cfg)
                self.assertFalse(info.get("preserved_existing_decisions"))
                decisions = pd.read_csv(review / "event_decisions.csv")
                decisions["decision"] = "APPROVE"
                decisions["rationale"] = "Synthetic fixture reviewed for integration test"
                decisions.to_csv(review / "event_decisions.csv", index=False)
                # empty quarantine -> empty scope_decisions with EXCLUDE not required
                scope = pd.read_csv(review / "scope_decisions.csv")
                if not scope.empty:
                    scope["decision"] = "EXCLUDE"
                    scope["rationale"] = "synthetic quarantine"
                    scope.to_csv(review / "scope_decisions.csv", index=False)
                result = pipeline.run_ledger(cfg)
                self.assertEqual(result["ending_nav"], 1020.)
                self.assertTrue((out / "run_provenance.json").exists())
                self.assertTrue((out / "daily_nav.csv").exists())

                # Reset clears APPROVE/EXCLUDE back to PENDING.
                reset_info = pipeline.review_init(cfg, reset_to_pending=True)
                self.assertTrue(reset_info.get("reset_to_pending"))
                self.assertFalse(reset_info.get("preserved_existing_decisions"))
                reset_decisions = pd.read_csv(review / "event_decisions.csv")
                self.assertTrue(reset_decisions.decision.eq("PENDING").all())
                preserved = pipeline.review_init(cfg, reset_to_pending=False)
                self.assertFalse(preserved.get("preserved_existing_decisions"))

    def test_source_term_change_invalidates_approvals_via_pipeline(self):
        """Fresh candidates must win over on-disk artifacts; old APPROVE cannot stick."""
        w, prices, calendar, events = replay_tests.DailyReplayTests().fixtures()
        w = w.copy()
        w["nominal_date"] = w.date
        w["reference_date"] = w.date
        wrds = events.reindex(columns=CA_CANONICAL_COLUMNS)
        wrds["review_reason"] = ""
        wrds["source"] = "WRDS_CRSP"
        bbg = wrds.copy()
        bbg["source"] = "BLOOMBERG"
        bbg["source_event_id"] = "BBG:" + bbg.source_event_id.astype(str)
        unified = reconcile_corporate_actions(wrds, bbg)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "data" / "cache"
            review = root / "data" / "review"
            raw = root / "data" / "raw"
            out = root / "output" / "data"
            for p in (cache, review, raw, out):
                p.mkdir(parents=True)

            bloomberg = raw / "bloomberg_corporate_actions.csv"
            bloomberg.write_text("synthetic\n")
            namespace = dict(
                weights=w, wrds_corporate_actions=wrds,
                wrds_delist_events=wrds.iloc[:0],
                wrds_adj_factors=pd.DataFrame(columns=["permno", "dlycaldt", "dlycumfacshr"]),
                wrds_id_history=pd.DataFrame({"permno": [1]}),
                bloomberg_corporate_actions=bbg, unified_corporate_actions=unified,
                wrds_table_report=pd.DataFrame(columns=["table", "available"]),
                selected_permnos=[1], CA_START_DATE="2026-01-02", CA_END_DATE="2026-04-03",
            )
            cache_io.export_cache(namespace, cache)
            prices.to_csv(cache / "daily_prices.csv", index=False)
            pd.DataFrame({"date": calendar}).to_csv(cache / "calendar.csv", index=False)
            cache_io.write_coverage(
                cache, start="2026-01-02", end="2026-04-03", schema="crsp_q_stock",
                universe=[1], bloomberg_path=bloomberg)

            with mock.patch.object(pipeline, "CACHE_DIR", cache), \
                 mock.patch.object(pipeline, "REVIEW_DIR", review), \
                 mock.patch.object(pipeline, "OUTPUT_DIR", out), \
                 mock.patch.object(pipeline, "PROJECT_ROOT", root), \
                 mock.patch.object(cache_io, "CACHE_DIR", cache), \
                 mock.patch("pipeline.bloomberg_raw_path", return_value=bloomberg):
                cfg = pipeline.PipelineConfig(
                    start_date="2026-01-02", end_date="2026-04-03",
                    stock_schema="crsp_q_stock", weights_start="2025-01-01",
                    input_mode="cache", initial_cash=1000.0,
                    cash_buffer_bps=0.0, fee_bps=0.0, batch_size=150,
                    excluded_event_policy="keep_securities",
                    exclude_reverse_split_securities=False,
                )
                pipeline.review_init(cfg)
                decisions = pd.read_csv(review / "event_decisions.csv")
                decisions["decision"] = "APPROVE"
                decisions["rationale"] = "Synthetic fixture reviewed for integration test"
                decisions.to_csv(review / "event_decisions.csv", index=False)
                scope = pd.read_csv(review / "scope_decisions.csv")
                if not scope.empty:
                    scope["decision"] = "EXCLUDE"
                    scope["rationale"] = "synthetic quarantine"
                    scope.to_csv(review / "scope_decisions.csv", index=False)

                # Keep approved decisions + on-disk candidates; change source terms
                # in cache (and refresh that table's manifest hash) so a correct
                # pipeline recomputes new review_keys and rejects the old APPROVE.
                cand_path = review / "artifacts" / "event_candidates.csv"
                self.assertTrue(cand_path.exists())
                old_keys = set(pd.read_csv(cand_path)["review_key"])
                wrds_path = cache / "wrds_corporate_actions.csv"
                mutated = pd.read_csv(wrds_path)
                self.assertIn("cash_per_share", mutated.columns)
                mask = mutated["cash_per_share"].notna()
                mutated.loc[mask, "cash_per_share"] = (
                    mutated.loc[mask, "cash_per_share"].astype(float) + 1.0)
                mutated.to_csv(wrds_path, index=False)
                manifest = json.loads((cache / "manifest.json").read_text())
                manifest["tables"]["wrds_corporate_actions"]["sha256"] = hashlib.sha256(
                    wrds_path.read_bytes()).hexdigest()
                (cache / "manifest.json").write_text(json.dumps(manifest, indent=2))

                with self.assertRaisesRegex(ValueError, "stale"):
                    pipeline.run_ledger(cfg)
                # Stale artifact keys must not be what a fresh fingerprint would produce.
                _, inputs = cache_io.load_cache(cache)
                from review import review_candidates, scope_events
                wrds_all = inputs["wrds_corporate_actions"]
                bbg_all = inputs["bloomberg_corporate_actions"]
                wrds_s, _ = scope_events(wrds_all, [1], "2026-01-02", "2026-04-03")
                bbg_s, _ = scope_events(bbg_all, [1], "2026-01-02", "2026-04-03")
                fresh = review_candidates(
                    reconcile_corporate_actions(wrds_s, bbg_s),
                    pd.concat([wrds_s, bbg_s], ignore_index=True))
                self.assertNotEqual(old_keys, set(fresh["review_key"]))

    def test_run_provenance_matches_sim_shape(self):
        w, prices, calendar, events = replay_tests.DailyReplayTests().fixtures()
        w = w.copy()
        w["nominal_date"] = w.date
        w["reference_date"] = w.date
        wrds = events.reindex(columns=CA_CANONICAL_COLUMNS)
        wrds["review_reason"] = ""
        wrds["source"] = "WRDS_CRSP"
        bbg = wrds.copy()
        bbg["source"] = "BLOOMBERG"
        bbg["source_event_id"] = "BBG:" + bbg.source_event_id.astype(str)
        unified = reconcile_corporate_actions(wrds, bbg)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "data" / "cache"
            review = root / "data" / "review"
            raw = root / "data" / "raw"
            out = root / "output" / "data"
            for p in (cache, review, raw, out):
                p.mkdir(parents=True)

            bloomberg = raw / "bloomberg_corporate_actions.csv"
            bloomberg.write_text("synthetic\n")
            namespace = dict(
                weights=w, wrds_corporate_actions=wrds,
                wrds_delist_events=wrds.iloc[:0],
                wrds_adj_factors=pd.DataFrame(columns=["permno", "dlycaldt", "dlycumfacshr"]),
                wrds_id_history=pd.DataFrame({"permno": [1]}),
                bloomberg_corporate_actions=bbg, unified_corporate_actions=unified,
                wrds_table_report=pd.DataFrame(columns=["table", "available"]),
                selected_permnos=[1], CA_START_DATE="2026-01-02", CA_END_DATE="2026-04-03",
            )
            cache_io.export_cache(namespace, cache)
            prices.to_csv(cache / "daily_prices.csv", index=False)
            pd.DataFrame({"date": calendar}).to_csv(cache / "calendar.csv", index=False)
            cache_io.write_coverage(
                cache, start="2026-01-02", end="2026-04-03", schema="crsp_q_stock",
                universe=[1], bloomberg_path=bloomberg)

            with mock.patch.object(pipeline, "CACHE_DIR", cache), \
                 mock.patch.object(pipeline, "REVIEW_DIR", review), \
                 mock.patch.object(pipeline, "OUTPUT_DIR", out), \
                 mock.patch.object(pipeline, "PROJECT_ROOT", root), \
                 mock.patch.object(cache_io, "CACHE_DIR", cache), \
                 mock.patch("pipeline.bloomberg_raw_path", return_value=bloomberg):
                cfg = pipeline.PipelineConfig(
                    start_date="2026-01-02", end_date="2026-04-03",
                    stock_schema="crsp_q_stock", weights_start="2025-01-01",
                    input_mode="cache", initial_cash=1000.0,
                    cash_buffer_bps=0.0, fee_bps=0.0, batch_size=150,
                    excluded_event_policy="keep_securities",
                    exclude_reverse_split_securities=False,
                )
                pipeline.review_init(cfg)
                decisions = pd.read_csv(review / "event_decisions.csv")
                decisions["decision"] = "APPROVE"
                decisions["rationale"] = "Synthetic fixture reviewed for integration test"
                decisions.to_csv(review / "event_decisions.csv", index=False)
                scope = pd.read_csv(review / "scope_decisions.csv")
                if not scope.empty:
                    scope["decision"] = "EXCLUDE"
                    scope["rationale"] = "synthetic quarantine"
                    scope.to_csv(review / "scope_decisions.csv", index=False)
                pipeline.run_ledger(cfg)
                prov = json.loads((out / "run_provenance.json").read_text())
                self.assertEqual(
                    set(prov),
                    {"start", "end", "aum", "cash_buffer_bps", "fee_bps", "files"})
                self.assertEqual(prov["start"], "2026-01-02")
                self.assertEqual(prov["aum"], 1000.0)
                files = prov["files"]
                self.assertTrue(any(k.endswith("manifest.json") for k in files))
                self.assertTrue(any(k.endswith("coverage.json") for k in files))
                self.assertTrue(any(k.endswith("event_decisions.csv") for k in files))
                self.assertTrue(any(k.endswith("scope_decisions.csv") for k in files))
                self.assertIn("research_pipeline.py", files)
                self.assertIn("data_loader.py", files)
                self.assertNotIn("config", prov)
                self.assertNotIn("code", prov)
                self.assertNotIn("inputs", prov)


class SchemaSmokeTests(unittest.TestCase):

    def test_reference_cache_headers_if_present(self):
        ref = ROOT / "_Simulated-Investment-Manager" / "data" / "stage2_inputs"
        if not ref.exists():
            self.skipTest("reference _SIM data not present")
        required = {
            "weights.csv": {"date", "permno", "cap_weight_proxy"},
            "daily_prices.csv": {"permno", "date", "close_price", "price_flag"},
            "calendar.csv": {"date"},
        }
        for name, cols in required.items():
            header = set(pd.read_csv(ref / name, nrows=0).columns)
            self.assertTrue(cols <= header, msg=f"{name} missing {cols - header}")


if __name__ == "__main__":
    unittest.main()
