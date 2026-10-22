"""Follow-on integration: synthetic cache → review gate → ledger (NAV parity).

Historically this executed Stage3 notebook cells via ``load_helpers``. Runtime no
longer loads notebooks; coverage is preserved against ``pipeline`` with the
same synthetic fixtures and ending NAV (1020).
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (str(SRC), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import cache_io, pipeline
from corporate_actions import CA_CANONICAL_COLUMNS, reconcile_corporate_actions
from tests import test_research_pipeline as replay_tests


class FollowonNotebookTests(unittest.TestCase):
    def test_review_gate_then_complete_cached_quarter(self):
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
            bloomberg.write_text("synthetic input fingerprint\n")
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
                # Before approvals the gate is not ready (pending decisions).
                self.assertTrue(decisions.decision.eq("PENDING").all())
                decisions["decision"] = "APPROVE"
                decisions["rationale"] = "Synthetic fixture reviewed for integration test"
                decisions.to_csv(review / "event_decisions.csv", index=False)
                scope = pd.read_csv(review / "scope_decisions.csv")
                if not scope.empty:
                    scope["decision"] = "EXCLUDE"
                    scope["rationale"] = "synthetic quarantine"
                    scope.to_csv(review / "scope_decisions.csv", index=False)
                result = pipeline.run_ledger(cfg)
                self.assertEqual(result["ending_nav"], 1020.)
                self.assertTrue((out / "run_provenance.json").exists())
                self.assertTrue((out / "daily_nav.csv").exists())
                # coverage fingerprint still matches Bloomberg bytes used above
                coverage = cache_io.load_coverage(cache)
                self.assertEqual(
                    coverage["bloomberg_sha256"],
                    hashlib.sha256(bloomberg.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
