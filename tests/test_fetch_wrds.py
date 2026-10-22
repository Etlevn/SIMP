"""Mocked WRDS fetch_wrds paths (no live credentials)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (str(SRC), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import cache_io, pipeline
from corporate_actions import CA_CANONICAL_COLUMNS, CA_TABLE_NAMES


def _weights_panel(start="2026-03-23", end="2026-06-30"):
    rows = []
    for d, permno, w in ((start, 1, 0.6), (start, 2, 0.4), (end, 1, 0.5), (end, 2, 0.5)):
        ts = pd.Timestamp(d)
        rows.append(dict(
            nominal_date=ts, reference_date=ts, date=ts, permno=permno, permco=permno,
            ticker=f"T{permno}", cusip="X", hdrcusip="X", siccd=1, nasdissuno=1,
            market_cap=1e9, cap_weight_proxy=w, cap_weight_proxy_pct=100 * w,
        ))
    return pd.DataFrame(rows)


def _empty_ca():
    return pd.DataFrame(columns=CA_CANONICAL_COLUMNS)


def _bundle_tables(universe):
    hist = pd.DataFrame({
        "permno": universe, "secinfostartdt": pd.Timestamp("2020-01-01"),
        "secinfoenddt": pd.Timestamp("2030-01-01"), "ticker": [f"T{p}" for p in universe],
        "cusip": ["A"] * len(universe), "cusip9": ["A0"] * len(universe),
        "hdrcusip": ["A"] * len(universe), "securitynm": ["n"] * len(universe),
    })
    return {
        "stkdistributions": pd.DataFrame(columns=[
            "permno", "disexdt", "disseqnbr", "distype", "disfreqtype", "dispaymenttype",
            "disdetailtype", "disorigcurtype", "disdivamt", "disfacpr", "disfacshr",
            "disdeclaredt", "disrecorddt", "dispaydt", "dispermno", "dispermco"]),
        "stkdelists": pd.DataFrame(columns=[
            "permno", "delistingdt", "delactiontype", "delreasontype", "delstatustype",
            "delret", "deldivamt", "delnextdt", "delpermno", "delpermco"]),
        "stkdlycumulativeadjfactor": pd.DataFrame(
            columns=["permno", "dlycaldt", "dlycumfacpr", "dlycumfacshr"]),
        "stksecurityinfohist": hist,
    }


class FakeDB:
    def raw_sql(self, sql, params=None, date_cols=None):
        if "dsp500list" in sql:
            return pd.DataFrame({"permno": [1, 2, 3]})
        return pd.DataFrame()

    def describe_table(self, library=None, table=None):
        return pd.DataFrame({"name": ["permno", "dlycaldt"]})


@contextmanager
def fake_wrds_session():
    yield FakeDB()


class FetchWrdsTests(unittest.TestCase):
    def _patch_fetch(self, root, cache, bloomberg, *, weights_return=None):
        report = pd.DataFrame({
            "schema": ["crsp_q_stock"] * len(CA_TABLE_NAMES),
            "table": list(CA_TABLE_NAMES),
            "available": [True] * len(CA_TABLE_NAMES),
            "missing_required": [""] * len(CA_TABLE_NAMES),
            "note": [""] * len(CA_TABLE_NAMES),
        })
        discovered = {t: {"schema": "crsp_q_stock", "table": t, "columns": {"permno"}}
                      for t in CA_TABLE_NAMES}
        bundle = _bundle_tables([1, 2, 3])
        prices = pd.DataFrame({
            "permno": [1, 2], "date": pd.to_datetime(["2026-03-23", "2026-03-23"]),
            "close_price": [10.0, 20.0], "price_flag": ["TR", "TR"],
        })
        calendar = pd.DataFrame({"date": pd.to_datetime(["2026-03-23", "2026-06-30"])})

        def fake_fetch_table(db, discovered_arg, table, universe, start, end):
            return bundle[table].copy()

        patches = [
            mock.patch.object(pipeline, "CACHE_DIR", cache),
            mock.patch.object(pipeline, "PROJECT_ROOT", root),
            mock.patch.object(cache_io, "CACHE_DIR", cache),
            mock.patch("pipeline.bloomberg_raw_path", return_value=bloomberg),
            mock.patch("pipeline.wrds_session", fake_wrds_session),
            mock.patch("pipeline.get_price_bounds",
                       return_value=(pd.Timestamp("2020-01-01"), pd.Timestamp("2026-12-31"))),
            mock.patch("pipeline.ca.discover_ca_tables", return_value=(report, discovered)),
            mock.patch("pipeline.ca.fetch_wrds_ca_table", side_effect=fake_fetch_table),
            mock.patch("pipeline.fetch_daily_prices", return_value=prices),
            mock.patch("pipeline.trading_calendar", return_value=calendar),
            mock.patch("pipeline.ca.normalize_bloomberg_export",
                       return_value=_empty_ca()),
            mock.patch("pipeline.ca.normalize_wrds_distributions",
                       return_value=_empty_ca()),
            mock.patch("pipeline.ca.normalize_wrds_delists",
                       return_value=_empty_ca()),
            mock.patch("pipeline.ca.reconcile_corporate_actions",
                       return_value=_empty_ca()),
        ]
        if weights_return is not None:
            patches.append(mock.patch(
                "pipeline.sp500_cap_weights_quarterly",
                return_value=(weights_return, {"ok": True})))
        else:
            patches.append(mock.patch(
                "pipeline.sp500_cap_weights_quarterly",
                side_effect=AssertionError("weights pull should be skipped")))
        return patches

    def test_cold_start_pulls_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "data" / "cache"
            raw = root / "data" / "raw"
            cache.mkdir(parents=True)
            raw.mkdir(parents=True)
            bloomberg = raw / "bloomberg_corporate_actions.csv"
            bloomberg.write_text("event_id\n")
            weights = _weights_panel()
            cfg = pipeline.PipelineConfig(
                start_date="2026-03-23", end_date="2026-06-30",
                stock_schema="crsp_q_stock", weights_start="2022-03-31",
                input_mode="fetch_wrds", initial_cash=1.0, cash_buffer_bps=0.0,
                fee_bps=0.0, batch_size=150,
                excluded_event_policy="keep_securities",
                exclude_reverse_split_securities=False)
            patches = self._patch_fetch(root, cache, bloomberg, weights_return=weights)
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                 patches[5], patches[6], patches[7], patches[8], patches[9], \
                 patches[10], patches[11], patches[12], patches[13], patches[14]:
                info = pipeline.fetch_wrds(cfg)
            self.assertTrue(info["weights_pulled"])
            self.assertEqual(info["universe"], 3)
            self.assertTrue((cache / "weights.csv").exists())
            self.assertTrue((cache / "daily_prices.csv").exists())
            self.assertTrue((cache / "coverage.json").exists())
            self.assertTrue((cache / "manifest.json").exists())
            self.assertTrue((cache / "unified_corporate_actions.csv").exists())

    def test_existing_weights_skips_quarterly_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "data" / "cache"
            raw = root / "data" / "raw"
            cache.mkdir(parents=True)
            raw.mkdir(parents=True)
            bloomberg = raw / "bloomberg_corporate_actions.csv"
            bloomberg.write_text("event_id\n")
            weights = _weights_panel()
            # Minimal cache with weights + manifest so reuse path loads upstream.
            namespace = dict(
                weights=weights,
                wrds_corporate_actions=_empty_ca(),
                wrds_delist_events=_empty_ca(),
                wrds_adj_factors=pd.DataFrame(
                    columns=["permno", "dlycaldt", "dlycumfacpr", "dlycumfacshr"]),
                wrds_id_history=pd.DataFrame({"permno": [1]}),
                bloomberg_corporate_actions=_empty_ca(),
                unified_corporate_actions=_empty_ca(),
                wrds_table_report=pd.DataFrame(columns=["table", "available"]),
                selected_permnos=[1, 2], CA_START_DATE="2026-03-13", CA_END_DATE="2026-06-30",
            )
            cache_io.export_cache(namespace, cache)
            cfg = pipeline.PipelineConfig(
                start_date="2026-03-23", end_date="2026-06-30",
                stock_schema="crsp_q_stock", weights_start="2022-03-31",
                input_mode="fetch_wrds", initial_cash=1.0, cash_buffer_bps=0.0,
                fee_bps=0.0, batch_size=150,
                excluded_event_policy="keep_securities",
                exclude_reverse_split_securities=False)
            patches = self._patch_fetch(root, cache, bloomberg, weights_return=None)
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                 patches[5], patches[6], patches[7], patches[8], patches[9], \
                 patches[10], patches[11], patches[12], patches[13], patches[14]:
                info = pipeline.fetch_wrds(cfg)
            self.assertFalse(info["weights_pulled"])
            self.assertEqual(info["weights_rows"], len(weights))


class EnsureCacheTests(unittest.TestCase):
    def test_cache_mode_fails_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "data" / "cache"
            raw = root / "data" / "raw"
            cache.mkdir(parents=True)
            raw.mkdir(parents=True)
            bloomberg = raw / "bloomberg_corporate_actions.csv"
            bloomberg.write_text("x\n")
            with mock.patch.object(pipeline, "CACHE_DIR", cache), \
                 mock.patch("pipeline.bloomberg_raw_path", return_value=bloomberg):
                with self.assertRaises(FileNotFoundError):
                    pipeline.ensure_cache(
                        pipeline.PipelineConfig(
                            start_date="2026-03-23", end_date="2026-06-30",
                            stock_schema="crsp_q_stock", weights_start="2022-03-31",
                            input_mode="cache", initial_cash=1.0, cash_buffer_bps=0.0,
                            fee_bps=0.0, batch_size=150,
                            excluded_event_policy="keep_securities",
                            exclude_reverse_split_securities=False,
                        ))


if __name__ == "__main__":
    unittest.main()
