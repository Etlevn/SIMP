#!/usr/bin/env python3
"""Stage 2: index-timing fund daybook + synthetic flows. No WRDS fetch.

Assumes Stage 1 (data) already wrote the four core CSVs. Flow::

    ensure_inputs → run_fund → print table previews

Writes under ``output/2_fund/``. ``reference_close`` needs calendar/prices
covering the session before each ``reference_date`` (or ``run_fund`` pads
missing sessions from later closes). Refresh inputs via
``scripts/run_1_data.py``. Edit switches in ``config.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from paths import OUTPUT_DIR, ensure_data_dirs
from pipeline import PipelineConfig, ensure_inputs, run_fund
from preview import print_csv
from wrds_auth import load_project_dotenv

FUND_TABLES = (
    ("daily_nav.csv", "nav"),
    ("daily_holdings.csv", "holdings"),
    ("fills.csv", "fills"),
    ("orders.csv", "orders"),
)


def main() -> int:
    load_project_dotenv()
    ensure_data_dirs()
    cfg = PipelineConfig.from_user_config(config)

    ensure_inputs(cfg)
    result = run_fund(cfg)
    print(
        f"ending_nav={result['ending_nav']:.4f} "
        f"sessions={result['sessions']} "
        f"fund_start={result['fund_start']} "
        f"fills=reference_close"
    )
    reasons = result.get("exclusion_reasons") or {}
    reason_txt = ",".join(f"{k}:{v}" for k, v in sorted(reasons.items())) or "none"
    print(
        f"excluded_rows={result.get('excluded_rows', 0)} "
        f"excluded_permnos={result.get('excluded_permnos', 0)} "
        f"flow_nonzero={result.get('flow_sessions_nonzero', 0)} "
        f"padded_quotes={result.get('padded_quote_rows', 0)} "
        f"reasons={reason_txt}"
    )
    for name, title in FUND_TABLES:
        print_csv(OUTPUT_DIR / name, title=title, blank_before=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
