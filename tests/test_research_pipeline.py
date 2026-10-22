import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (str(SRC), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from ledger import (
    benchmark_report,
    execution_comparison,
    prepare_exclusion_scenario,
    seeded_flows,
    simulate,
    validate_bmll_mapping,
)
from review import approved_events, review_candidates


class DailyReplayTests(unittest.TestCase):
    def test_excluded_delisting_security_removed_before_inception(self):
        w, prices, calendar, events = self.fixtures()
        second = w.copy(); second["permno"] = 2
        w = pd.concat([w, second], ignore_index=True)
        w["cap_weight_proxy"] = .5
        bad_quotes = prices.copy(); bad_quotes["permno"] = 2
        bad_quotes["close_price"] = 0.
        quotes = pd.concat([prices, bad_quotes], ignore_index=True)
        audit = pd.DataFrame({"decision": ["EXCLUDE"], "permno": [2]})
        kept, approved, removed = prepare_exclusion_scenario(w, events, audit)
        self.assertTrue(kept.cap_weight_proxy.eq(1.).all())
        self.assertTrue(removed.permno.eq(2).all())
        self.assertTrue(w.cap_weight_proxy.eq(.5).all())
        self.assertTrue(quotes.loc[quotes.permno.eq(2), "close_price"].eq(0.).all())
        result = simulate(kept, quotes, calendar, approved, calendar[0], calendar[-1],
                          initial_cash=1000, cash_buffer_bps=0)
        self.assertEqual(result["daily_nav"].iloc[-1].nav, 1020.)

    def fixtures(self):
        calendar = pd.bdate_range("2026-01-02", "2026-04-03")
        weights = pd.DataFrame({"date": [calendar[0], pd.Timestamp("2026-04-01")],
                                "permno": [1, 1], "cap_weight_proxy": [1., 1.]})
        prices = pd.DataFrame({"date": calendar, "permno": 1,
                               "close_price": [100. if d < pd.Timestamp("2026-02-02") else 50.
                                               for d in calendar], "price_flag": "TR"})
        events = pd.DataFrame([
            {"source_event_id": "split", "permno": 1, "event_type": "STOCK_SPLIT",
             "ex_date": "2026-02-02", "pay_date": None, "split_ratio": 2.,
             "cash_per_share": np.nan, "currency": "USD", "requires_review": False},
            {"source_event_id": "dividend", "permno": 1, "event_type": "CASH_DIVIDEND",
             "ex_date": "2026-02-06", "pay_date": "2026-02-08", "split_ratio": np.nan,
             "cash_per_share": 1., "currency": "USD", "requires_review": False}])
        return weights, prices, calendar, events

    def replay(self, **kwargs):
        w, p, c, e = self.fixtures()
        return simulate(w, p, c, e, c[0], c[-1], initial_cash=1000, cash_buffer_bps=0, **kwargs)

    def test_quarter_split_weekend_payment_and_rebalance(self):
        result = self.replay()
        nav = result["daily_nav"].set_index("date")
        self.assertEqual(nav.loc["2026-02-02", "nav"], 1000.)
        self.assertEqual(nav.loc["2026-02-06", "receivables"], 20.)
        self.assertEqual(nav.loc["2026-02-09", "receivables"], 0.)
        self.assertEqual(nav.iloc[-1].nav, 1020.)
        self.assertEqual(result["fills"].iloc[0].execution_date, pd.Timestamp("2026-01-05"))
        self.assertEqual(len(result["fills"]), 1)
        self.assertEqual(result["daily_holdings"].iloc[-1].shares, 20)

    def test_flows_do_not_create_returns(self):
        result = self.replay(flows=pd.DataFrame({"date": ["2026-01-06"], "amount": [100.]}))
        nav = result["daily_nav"].set_index("date")
        self.assertAlmostEqual(nav.loc["2026-01-06", "fund_return"], 0.)
        self.assertEqual(nav.loc["2026-01-06", "nav"], 1100.)

    def test_split_between_decision_and_execution(self):
        w, p, c, e = self.fixtures()
        e = e.iloc[:1].copy()
        e["ex_date"] = "2026-01-05"
        p["close_price"] = np.where(p.date.eq(c[0]), 100., 50.)
        r = simulate(w, p, c, e, c[0], c[-1], initial_cash=1000, cash_buffer_bps=0)
        self.assertEqual(r["fills"].iloc[0].signed_filled, 20)
        self.assertEqual(r["fills"].iloc[0].decision_price, 50.)
        self.assertEqual(r["daily_nav"].iloc[-1].nav, 1000.)

    def test_dividend_paid_after_sale(self):
        w, p, c, e = self.fixtures()
        w = pd.DataFrame({"date": [c[0], pd.Timestamp("2026-02-06")],
                          "permno": [1, 2], "cap_weight_proxy": [1., 1.]})
        p2 = p.copy(); p2["permno"] = 2; p2["close_price"] = 50.
        p = pd.concat([p, p2], ignore_index=True)
        e.loc[e.event_type.eq("CASH_DIVIDEND"), "pay_date"] = "2026-02-14"
        r = simulate(w, p, c, e, c[0], c[-1], initial_cash=1000, cash_buffer_bps=0)
        nav = r["daily_nav"].set_index("date")
        self.assertEqual(nav.loc["2026-02-09", "receivables"], 20.)
        self.assertEqual(nav.loc["2026-02-16", "cash"], 20.)
        h = r["daily_holdings"]
        self.assertFalse(h.loc[h.date.eq(pd.Timestamp("2026-02-16"))].permno.eq(1).any())

    def test_cash_shortfall_and_missing_quote(self):
        w, p, c, e = self.fixtures()
        p.loc[p.date.eq(c[1]), "close_price"] = 200.
        r = simulate(w, p, c, e.iloc[:0], c[0], c[-1], initial_cash=1000, cash_buffer_bps=0)
        self.assertEqual(r["fills"].iloc[0].signed_filled, 5)
        self.assertEqual(r["fills"].iloc[0].unfilled_quantity, 5)
        with self.assertRaisesRegex(ValueError, "Missing raw close"):
            simulate(w, p[p.date.ne(c[3])], c, e, c[0], c[-1], initial_cash=1000)

    def test_review_complex_and_fractional_actions_stop(self):
        w, p, c, e = self.fixtures()
        e.loc[0, "requires_review"] = True
        with self.assertRaisesRegex(ValueError, "Unreviewed"):
            simulate(w, p, c, e, c[0], c[-1])
        e.loc[0, "requires_review"] = False
        e.loc[0, "event_type"] = "SPINOFF"
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            simulate(w, p, c, e, c[0], c[-1])
        e.loc[0, "event_type"] = "STOCK_SPLIT"; e.loc[0, "split_ratio"] = 1.05
        with self.assertRaisesRegex(ValueError, "Fractional"):
            simulate(w, p, c, e, c[0], c[-1], initial_cash=1000, cash_buffer_bps=0)

    def test_pure_share_split_needs_no_cash_currency(self):
        w, p, c, e = self.fixtures()
        e.loc[e.event_type.eq("STOCK_SPLIT"), "currency"] = np.nan
        r = simulate(w, p, c, e, c[0], c[-1], initial_cash=1000, cash_buffer_bps=0)
        self.assertEqual(r["daily_nav"].iloc[-1].nav, 1020.)
        e.loc[e.event_type.eq("CASH_DIVIDEND"), "currency"] = np.nan
        with self.assertRaisesRegex(ValueError, "currency"):
            simulate(w, p, c, e, c[0], c[-1], initial_cash=1000, cash_buffer_bps=0)

    def test_benchmark_and_seeded_flows(self):
        r = self.replay()["daily_nav"]
        _, report = benchmark_report(r, r[["date", "fund_return"]].rename(columns={"fund_return": "benchmark_return"}))
        self.assertEqual(report["tracking_error_annualized"], 0.)
        pd.testing.assert_frame_equal(seeded_flows(r.date), seeded_flows(r.date))

    def test_tca_includes_unfilled_opportunity_and_fees(self):
        x = pd.DataFrame([dict(order_id="a", side="BUY", quantity=10, filled_quantity=6,
            average_fill_price=101, arrival_price=100, terminal_price=102,
            fees=1, vwap=100, twap=100, auction_price=100)])
        r = execution_comparison(x)
        self.assertEqual(r.iloc[0].implementation_shortfall, 15.)
        x["filled_quantity"] = 0; x["average_fill_price"] = np.nan
        self.assertEqual(execution_comparison(x).iloc[0].implementation_shortfall, 21.)

    def test_mapping_requires_one_effective_identifier(self):
        request = pd.DataFrame({"date": ["2026-01-02"], "permno": [1]})
        mapping = pd.DataFrame({"permno": [1], "bmll_id": ["vendor-1"],
                                "valid_from": ["2025-01-01"], "valid_to": [None]})
        self.assertEqual(len(validate_bmll_mapping(mapping, request)), 1)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            validate_bmll_mapping(pd.concat([mapping, mapping]), request)

    def test_review_rejects_pending_and_changed_source(self):
        unified = pd.DataFrame([dict(wrds_event_id="a", bloomberg_event_id="b",
                                     event_type="CASH_DIVIDEND", ex_date="2026-01-05")])
        sources = pd.DataFrame([dict(source_event_id=k, event_type="CASH_DIVIDEND",
                                     cash_per_share=1., currency="USD") for k in ["a", "b"]])
        candidates = review_candidates(unified, sources)
        with self.assertRaisesRegex(ValueError, "APPROVE/EXCLUDE"):
            approved_events(candidates, candidates, sources)
        decisions = candidates.copy()
        decisions["decision"] = "APPROVE"; decisions["rationale"] = "Reviewed synthetic sources"
        selected, _ = approved_events(candidates, decisions, sources)
        self.assertEqual(selected.iloc[0].source_event_id, "a")
        sources.loc[0, "cash_per_share"] = 2.
        changed = review_candidates(unified, sources)
        with self.assertRaisesRegex(ValueError, "stale"):
            approved_events(changed, decisions, sources)


if __name__ == "__main__":
    unittest.main()
