"""User-editable run settings for SIMP data loading / ledger.

Edit this file; do not put experiment parameters in ``data_loader.py`` or
``src/`` modules.

Normal use (one run): set ``INPUT_MODE`` only, leave ``RESET_REVIEW_TO_PENDING=False``.
``data_loader`` will load/fetch data, keep existing review decisions, then write ``outputs/``.

To wipe approvals and start a fresh review: set ``RESET_REVIEW_TO_PENDING=True`` once
(that run resets decisions, clears stale ``output/data/``, and stops; edit the CSVs,
set it back to False, run again).
"""

# --- data source ---
INPUT_MODE = "cache"  # "cache" = local data/; "fetch_wrds" = live DB pull into data/cache

# --- review ---
RESET_REVIEW_TO_PENDING = False  # True = wipe decisions to PENDING, clear output/data/, stop (no ledger)

# --- experiment window & research DB ---
START_DATE = "2026-03-23"  # ledger / experiment start (must be a weights snapshot date)
END_DATE = "2026-06-30"  # ledger / experiment end
WEIGHTS_START = "2022-03-31"  # earliest date for cold-start quarterly weights pull
STOCK_SCHEMA = "crsp_q_stock"  # CRSP stock schema (indexes schema derived from this)
BATCH_SIZE = 150  # permno batch size when fetching daily prices from WRDS

# --- fund / ledger ---
INITIAL_CASH = 10_000_000_000.0  # starting AUM / cash
CASH_BUFFER_BPS = 10.0  # cash buffer kept aside when sizing orders (basis points)
FEE_BPS = 0.0  # illustrative trading fee in bps (not calibrated TCA)
EXCLUDED_EVENT_POLICY = "exclude_securities"  # or "keep_securities": drop vs retain names with EXCLUDE events
EXCLUDE_REVERSE_SPLIT_SECURITIES = True  # also drop reverse-split names (no fractional cash-in-lieu)
