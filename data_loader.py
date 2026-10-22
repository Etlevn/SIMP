#!/usr/bin/env python3
"""SIMP data loader entrypoint.

One-shot flow (default)::

    load/fetch data → review_init (preserve decisions) → run_ledger → output/data/

Only when ``config.RESET_REVIEW_TO_PENDING`` is True does the run stop after
resetting decision CSVs to PENDING and clearing stale ``output/data/`` artifacts
(edit the CSVs, set the flag False, run again).

Edit parameters in ``config.py``, not in this file.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import config
from paths import OUTPUT_DIR, bloomberg_raw_path, clear_output_dir, ensure_data_dirs
from pipeline import (
    PipelineConfig,
    ensure_cache,
    fetch_wrds,
    review_init,
    run_ledger,
)


def main() -> int:
    ensure_data_dirs()
    cfg = PipelineConfig.from_user_config(config)
    mode = str(config.INPUT_MODE).strip().lower()
    reset = bool(config.RESET_REVIEW_TO_PENDING)

    bloomberg = bloomberg_raw_path()
    print(f"[raw] corporate-action CSV: {bloomberg}")
    print(f"[mode] INPUT_MODE={mode}")

    if mode == "fetch_wrds":
        fetch_info = fetch_wrds(cfg)
        print(f"[cache] WRDS fetch: universe={fetch_info['universe']} "
              f"price_rows={fetch_info['price_rows']} sessions={fetch_info['sessions']} "
              f"weights_pulled={fetch_info['weights_pulled']} "
              f"weights_rows={fetch_info['weights_rows']}")
    elif mode == "cache":
        cache_info = ensure_cache(cfg)
        print(f"[cache] reuse local data/: universe={cache_info['universe']}")
    else:
        raise ValueError('config.INPUT_MODE must be "fetch_wrds" or "cache"')

    review_info = review_init(cfg, reset_to_pending=reset)
    print(f"[review] candidates={review_info['candidates']} "
          f"quarantine={review_info['quarantine']} "
          f"preserved={review_info.get('preserved_existing_decisions')} "
          f"reset_to_pending={review_info.get('reset_to_pending')}")

    if reset:
        removed = clear_output_dir()
        print(
            f"[output] cleared {OUTPUT_DIR} because RESET_REVIEW_TO_PENDING=True "
            f"(removed {len(removed)} item(s); kept .gitkeep)"
        )
        print("[review] Decisions reset to PENDING. Edit "
              "data/review/event_decisions.csv and scope_decisions.csv "
              "(or run scripts/review_auto_pending.py), "
              "set RESET_REVIEW_TO_PENDING=False in config.py, then run again.")
        return 0

    result = run_ledger(cfg)
    print(f"[output] ending_nav={result['ending_nav']:.4f} "
          f"sessions={result['sessions']} "
          f"approved_events={result['approved_events']}")
    print(f"[output] written under: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
