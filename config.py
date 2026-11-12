"""User-editable run settings for SIMP data loading / fund replay.

Edit this file; do not put experiment parameters in ``src/`` modules.

Canonical flat inputs under ``data/``:
``weights.csv``, ``daily_prices.csv``, ``calendar.csv``, ``events.csv``.
``coverage.json`` is optional provenance written only after a WRDS overwrite.

``fetch_data.py`` validates the four CSVs against the experiment window; on
failure it overwrites via WRDS and ensures the S&P 500 TR benchmark.
``main_2_fund.py`` only reads local ``data/``. To force a refresh, delete or
corrupt ``data/`` then re-run ``fetch_data.py``.
"""

# =============================================================================
# fetch_data.py — prepare data/ (validate; on B-tier failure WRDS overwrite)
# =============================================================================
WEIGHTS_START = "2022-03-31"  # earliest date for cold-start quarterly weights pull
STOCK_SCHEMA = "crsp_q_stock"  # CRSP stock schema (indexes schema derived from this)
BATCH_SIZE = 150  # permno batch size when fetching daily prices from WRDS

# Experiment window (fetch extract bounds; Phase 2 snapshot / fund end)
START_DATE = "2026-03-23"  # must be a weights.csv snapshot date (post-rebalance Monday)
END_DATE = "2026-06-30"  # ledger / experiment end

# =============================================================================
# Output policy — default = core only; one switch per phase for extras
# =============================================================================
# Phase 0 core: example_{pm,trader,broker,fills}.csv
# Extra: field_dictionary, conservation_report, schema_version.txt, validation_report.json
PHASE0_WRITE_EXTRA = False
# Phase 1 stub (reserved)
PHASE1_WRITE_EXTRA = False
# Phase 2 core: daily_nav.csv, fills.csv, pm_orders.csv
# Extra: holdings, blotter orders, schedule/flows/journal/exclusions/padded,
#        benchmark panels, notional summary, run_manifest.json
PHASE2_WRITE_EXTRA = False
# Phase 3 core: order_tca.csv, strategy_summary.csv
# Extra: per-strategy trader/broker/fills, stock_days, impact_model.json
PHASE3_WRITE_EXTRA = False

# =============================================================================
# Phase 2 — main_2_fund.py → output/2_fund/
# Reference-close daybook + synthetic flows.
# =============================================================================
INITIAL_CASH = 10_000_000_000.0  # starting AUM / cash
CASH_BUFFER_BPS = 10.0  # cash buffer kept aside when sizing orders (basis points)
FEE_BPS = 0.0  # illustrative trading fee in bps (not calibrated TCA)
# Rebalance fills are always reference-close. Hard-name exclusions (reverse splits /
# bad prices) are always applied in Phase 2 — see README.

# Synthetic investor flows (always applied in Phase 2)
FLOW_SEED = 42
FLOW_DAILY_SIGMA_BPS = 5.0
FLOW_ANNUAL_DRIFT_BPS = 0.0
FLOW_LUMPY_PROB = 0.01
FLOW_LUMPY_BPS = (50.0, 300.0)
FLOW_MAX_REDEMPTION_BPS = 500.0
FLOW_BAND_BPS = 25.0

# =============================================================================
# Phase 3 — main_3_tca.py → output/3_tca/
# One-date MOC / VWAP / ARRIVAL_IS comparison on Phase 2 pm_orders (synthetic tape).
# =============================================================================
# None → largest REBALANCE trade_date in output/2_fund/pm_orders.csv
TCA_TRADE_DATE = None
TCA_REASONS = ("REBALANCE",)  # filter pm_orders.reason
TCA_STRATEGIES = ("CLOSE_AUCTION", "VWAP", "ARRIVAL_IS")
# Uncalibrated impact knobs (mechanics only — not vendor-fitted)
TCA_IMPACT_ETA = 0.6
TCA_IMPACT_AUCTION_ETA = 0.25
TCA_IMPACT_CROSS = 0.5
