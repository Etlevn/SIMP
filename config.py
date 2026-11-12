"""User-editable run settings for SIMP data loading / ledger.

Edit this file; do not put experiment parameters in ``data_loader.py`` or
``src/`` modules.

Canonical flat inputs under ``data/``:
``weights.csv``, ``daily_prices.csv``, ``calendar.csv``, ``events.csv``.
``coverage.json`` is optional provenance written only after a WRDS overwrite.

Stage 1 (``scripts/run_1_data.py``) validates the four CSVs against the
experiment window; on failure it overwrites via WRDS. Stage 2 only reads
local files. To force a refresh, delete or corrupt ``data/`` then re-run
Stage 1. Prefer ``scripts/run_1_data.py`` then ``scripts/run_2_fund.py``.
"""

# =============================================================================
# Stage 1 — data (scripts/run_1_data.py → prepare_data)
# Validate local data/; on B-tier failure fetch WRDS and overwrite.
# =============================================================================
WEIGHTS_START = "2022-03-31"  # earliest date for cold-start quarterly weights pull
STOCK_SCHEMA = "crsp_q_stock"  # CRSP stock schema (indexes schema derived from this)
BATCH_SIZE = 150  # permno batch size when fetching daily prices from WRDS

# Experiment window (Stage 1 extract bounds; Stage 2 snapshot / fund end)
START_DATE = "2026-03-23"  # must be a weights.csv snapshot date (post-rebalance Monday)
END_DATE = "2026-06-30"  # ledger / experiment end

# =============================================================================
# Stage 2 — fund (scripts/run_2_fund.py → run_fund)
# Reference-close daybook + synthetic flows → output/2_fund/.
# =============================================================================
INITIAL_CASH = 10_000_000_000.0  # starting AUM / cash
CASH_BUFFER_BPS = 10.0  # cash buffer kept aside when sizing orders (basis points)
FEE_BPS = 0.0  # illustrative trading fee in bps (not calibrated TCA)
# Rebalance fills are always reference-close. Hard-name exclusions (reverse splits /
# bad prices) are always applied in Stage 2 — see README.

# Synthetic investor flows (always applied in Stage 2)
FLOW_SEED = 42
FLOW_DAILY_SIGMA_BPS = 5.0
FLOW_ANNUAL_DRIFT_BPS = 0.0
FLOW_LUMPY_PROB = 0.01
FLOW_LUMPY_BPS = (50.0, 300.0)
FLOW_MAX_REDEMPTION_BPS = 500.0
FLOW_BAND_BPS = 25.0
