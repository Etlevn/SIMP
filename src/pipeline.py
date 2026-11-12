"""Orchestrate flat data/ load (or WRDS fetch) and daily-fund outputs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pandas as pd

import corporate_actions as ca
from cache_io import (
    load_inputs,
    validate_local,
    write_flat_inputs,
)
from fund_simulator import (
    pad_reference_close_coverage,
    rebalance_schedule,
    simulate_fund,
    synthetic_flows,
)
from ledger import prepare_exclusion_scenario, simulate, temporal_price_exclusions
from market_data import fetch_daily_prices, trading_calendar
from paths import (
    DATA_DIR,
    FUND_OUTPUT_TABLES,
    OUTPUT_DIR,
    clear_output_dir,
    ensure_data_dirs,
)
from universe import get_price_bounds, sp500_cap_weights_quarterly


@dataclass
class PipelineConfig:
    """Runtime settings. Prefer ``from_user_config`` (root ``config.py``); no baked-in experiment defaults."""

    start_date: str
    end_date: str
    stock_schema: str
    weights_start: str
    initial_cash: float
    cash_buffer_bps: float
    fee_bps: float
    batch_size: int
    flow_seed: int = 7
    flow_daily_sigma_bps: float = 5.0
    flow_annual_drift_bps: float = 0.0
    flow_lumpy_prob: float = 0.01
    flow_lumpy_bps: tuple[float, float] = (50.0, 300.0)
    flow_max_redemption_bps: float = 500.0
    flow_band_bps: float = 25.0

    @classmethod
    def from_user_config(cls, user_config: ModuleType | Any) -> "PipelineConfig":
        """Build from root ``config.py`` (or any namespace with the same attributes)."""
        lumpy = getattr(user_config, "FLOW_LUMPY_BPS", (50.0, 300.0))
        return cls(
            start_date=user_config.START_DATE,
            end_date=user_config.END_DATE,
            stock_schema=user_config.STOCK_SCHEMA,
            weights_start=user_config.WEIGHTS_START,
            initial_cash=float(user_config.INITIAL_CASH),
            cash_buffer_bps=float(user_config.CASH_BUFFER_BPS),
            fee_bps=float(user_config.FEE_BPS),
            batch_size=int(user_config.BATCH_SIZE),
            flow_seed=int(getattr(user_config, "FLOW_SEED", 7)),
            flow_daily_sigma_bps=float(getattr(user_config, "FLOW_DAILY_SIGMA_BPS", 5.0)),
            flow_annual_drift_bps=float(getattr(user_config, "FLOW_ANNUAL_DRIFT_BPS", 0.0)),
            flow_lumpy_prob=float(getattr(user_config, "FLOW_LUMPY_PROB", 0.01)),
            flow_lumpy_bps=(float(lumpy[0]), float(lumpy[1])),
            flow_max_redemption_bps=float(getattr(user_config, "FLOW_MAX_REDEMPTION_BPS", 500.0)),
            flow_band_bps=float(getattr(user_config, "FLOW_BAND_BPS", 25.0)),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "PipelineConfig":
        """Load root ``config.py``. ``path`` is accepted for API compatibility and ignored."""
        del path  # experiment knobs live in root config.py only
        import config as user_config
        return cls.from_user_config(user_config)


def ensure_inputs(cfg: PipelineConfig | None = None) -> dict:
    """B-tier validate local ``data/`` (Stage 2; no WRDS)."""
    cfg = cfg or PipelineConfig.load()
    ensure_data_dirs()
    return validate_local(
        DATA_DIR, start=cfg.start_date, end=cfg.end_date, schema=cfg.stock_schema)


# Back-compat alias used by older scripts/tests
ensure_cache = ensure_inputs


def prepare_data(cfg: PipelineConfig | None = None) -> dict:
    """Stage 1: prefer local cache; on B-tier failure overwrite via WRDS.

    Force refresh: delete or corrupt ``data/`` then re-run Stage 1.
    """
    cfg = cfg or PipelineConfig.load()
    ensure_data_dirs()
    try:
        info = validate_local(
            DATA_DIR, start=cfg.start_date, end=cfg.end_date, schema=cfg.stock_schema)
        info = dict(info)
        info["source"] = "cache"
        return info
    except Exception as exc:
        print(
            f"Local data/ failed validation ({type(exc).__name__}: {exc}); "
            "fetching WRDS overwrite",
            flush=True,
        )
        info = fetch_wrds(cfg)
        info = dict(info)
        info["source"] = "fetch_wrds"
        return info


def apply_hard_exclusions(
    snapshots: pd.DataFrame,
    events: pd.DataFrame,
    prices: pd.DataFrame,
    calendar,
    *,
    for_fund: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, list[pd.DataFrame], pd.DataFrame | None]:
    """Always drop reverse-split names and bad-price snapshot rows; renormalize.

    When ``for_fund`` is True, build a reference-close rebalance schedule and use
    it for temporal price exclusions. When False (ledger helper), use the
    next-session holding-window defaults inside ``temporal_price_exclusions``.

    Returns ``(weights, events, cut_frames, schedule)``; ``schedule`` may be None.
    """
    cut_parts: list[pd.DataFrame] = []
    weights, events, excluded = prepare_exclusion_scenario(
        snapshots, events, exclude_reverse_splits=True)
    if not excluded.empty:
        cut_parts.append(excluded)

    schedule = None
    if for_fund:
        schedule = rebalance_schedule(weights, calendar)
        weights, price_cut = temporal_price_exclusions(
            weights, prices, calendar, schedule=schedule)
        if not price_cut.empty:
            cut_parts.append(price_cut)
        schedule = rebalance_schedule(weights, calendar)
    else:
        weights, price_cut = temporal_price_exclusions(weights, prices, calendar)
        if not price_cut.empty:
            cut_parts.append(price_cut)
    return weights, events, cut_parts, schedule


def _exclusion_summary(cut_parts: list[pd.DataFrame]) -> tuple[int, int, dict]:
    if not cut_parts:
        return 0, 0, {}
    cut_all = pd.concat(cut_parts, ignore_index=True)
    reason_counts = (
        cut_all["exclusion_reason"].astype(str).value_counts().astype(int).to_dict()
        if "exclusion_reason" in cut_all.columns else {})
    return int(len(cut_all)), int(cut_all["permno"].nunique()), reason_counts


def run_ledger(cfg: PipelineConfig | None = None) -> dict:
    """Library/test helper: next-session ``simulate`` → ``OUTPUT_DIR``.

    Not a user stage. Prefer ``run_fund`` / ``scripts/run_2_fund.py``.
    """
    cfg = cfg or PipelineConfig.load()
    ensure_inputs(cfg)
    inputs = load_inputs(DATA_DIR)
    weights = inputs["weights"]
    prices = inputs["prices"]
    calendar = inputs["calendar"]
    events = inputs["events"]

    snapshots = weights[weights.date.between(cfg.start_date, cfg.end_date)].copy()
    if snapshots.empty or snapshots.date.min() != pd.Timestamp(cfg.start_date):
        raise ValueError("START_DATE must be a decision snapshot present in the weights panel")

    replay_snapshots, replay_events, cut_parts, _schedule = apply_hard_exclusions(
        snapshots, events, prices, calendar)

    results = simulate(
        replay_snapshots, prices, calendar, replay_events, cfg.start_date, cfg.end_date,
        initial_cash=cfg.initial_cash, cash_buffer_bps=cfg.cash_buffer_bps, fee_bps=cfg.fee_bps)
    clear_output_dir()
    for name in FUND_OUTPUT_TABLES:
        results[name].to_csv(OUTPUT_DIR / f"{name}.csv", index=False)

    excluded_rows, excluded_permnos, reason_counts = _exclusion_summary(cut_parts)
    nav = results["daily_nav"]
    return {
        "ending_nav": float(nav.iloc[-1].nav),
        "sessions": len(nav),
        "approved_events": len(replay_events),
        "excluded_rows": excluded_rows,
        "excluded_permnos": excluded_permnos,
        "exclusion_reasons": reason_counts,
        "output_dir": str(OUTPUT_DIR),
    }


def run_fund(cfg: PipelineConfig | None = None) -> dict:
    """Stage 2 fund: reference-close + synthetic flows → ``output/2_fund/``."""
    cfg = cfg or PipelineConfig.load()
    ensure_inputs(cfg)
    inputs = load_inputs(DATA_DIR)
    weights = inputs["weights"]
    prices = inputs["prices"]
    calendar = inputs["calendar"]
    events = inputs["events"]

    snapshots = weights[weights.date.between(cfg.start_date, cfg.end_date)].copy()
    if snapshots.empty or snapshots.date.min() != pd.Timestamp(cfg.start_date):
        raise ValueError("START_DATE must be a decision snapshot present in the weights panel")

    prices, calendar, pad_rows = pad_reference_close_coverage(
        snapshots, prices, calendar)

    replay_snapshots, replay_events, cut_parts, schedule = apply_hard_exclusions(
        snapshots, events, prices, calendar, for_fund=True)
    assert schedule is not None

    fund_start = pd.Timestamp(schedule.decision_date.iloc[0]).normalize()
    fund_end = pd.Timestamp(cfg.end_date).normalize()
    cal_index = pd.DatetimeIndex(pd.to_datetime(calendar)).normalize().sort_values().unique()
    flow_days = cal_index[(cal_index >= fund_start) & (cal_index <= fund_end)]
    flows = synthetic_flows(
        flow_days,
        seed=cfg.flow_seed,
        daily_sigma_bps=cfg.flow_daily_sigma_bps,
        annual_drift_bps=cfg.flow_annual_drift_bps,
        lumpy_prob=cfg.flow_lumpy_prob,
        lumpy_bps=cfg.flow_lumpy_bps,
        max_redemption_bps=cfg.flow_max_redemption_bps,
    )

    results = simulate_fund(
        replay_snapshots, prices, calendar, replay_events, fund_start, fund_end,
        initial_cash=cfg.initial_cash, cash_buffer_bps=cfg.cash_buffer_bps,
        fee_bps=cfg.fee_bps, flows=flows, flow_band_bps=cfg.flow_band_bps)
    clear_output_dir()
    for name in FUND_OUTPUT_TABLES:
        results[name].to_csv(OUTPUT_DIR / f"{name}.csv", index=False)

    excluded_rows, excluded_permnos, reason_counts = _exclusion_summary(cut_parts)
    nav = results["daily_nav"]
    flow_nonzero = int(flows["flow_bps"].ne(0).sum()) if len(flows) else 0
    return {
        "ending_nav": float(nav.iloc[-1].nav),
        "sessions": len(nav),
        "fund_start": str(fund_start.date()),
        "approved_events": len(replay_events),
        "excluded_rows": excluded_rows,
        "excluded_permnos": excluded_permnos,
        "exclusion_reasons": reason_counts,
        "flow_sessions_nonzero": flow_nonzero,
        "padded_quote_rows": int(len(pad_rows)),
        "schedule": results["schedule"],
        "output_dir": str(OUTPUT_DIR),
    }


def fetch_wrds(cfg: PipelineConfig | None = None) -> dict:
    """Build/refresh flat ``data/`` from WRDS (minimal four-file path).

    Writes ``weights.csv``, ``daily_prices.csv``, ``calendar.csv``,
    ``events.csv`` (from ``stkdistributions`` only: CASH_DIVIDEND / STOCK_SPLIT),
    and a ``coverage.json`` fetch snapshot (not used as a validation gate).
    Does not pull delists, adj factors, or id history.
    """
    from wrds_auth import wrds_session

    cfg = cfg or PipelineConfig.load()
    ensure_data_dirs()
    member_schema = cfg.stock_schema.replace("_stock", "_indexes")
    dist_table = "stkdistributions"

    with wrds_session() as db:
        quarterly_weights, _info = sp500_cap_weights_quarterly(
            db, cfg.weights_start, cfg.end_date,
            membership_schema=member_schema, stock_schema=cfg.stock_schema)
        weights_pulled = True

        snapshots = quarterly_weights[
            quarterly_weights.date.between(cfg.start_date, cfg.end_date)].copy()
        if snapshots.empty or snapshots.date.min() != pd.Timestamp(cfg.start_date):
            raise ValueError(
                "START_DATE must be a decision snapshot present in the quarterly panel; "
                f"widen weights_start (now {cfg.weights_start}) or check WRDS coverage")

        first_price, last_price = get_price_bounds(db, f"{cfg.stock_schema}.dsf_v2")
        if pd.Timestamp(cfg.start_date) < first_price or pd.Timestamp(cfg.end_date) > last_price:
            raise ValueError(
                f"Selected release only covers {first_price.date()} through {last_price.date()}")
        members = db.raw_sql(
            f"SELECT DISTINCT permno FROM {member_schema}.dsp500list_v2 "
            "WHERE mbrstartdt <= %(end)s AND (mbrenddt IS NULL OR mbrenddt >= %(start)s)",
            params={"start": cfg.start_date, "end": cfg.end_date})
        universe = sorted(
            set(members.permno.dropna().astype(int)) | set(snapshots.permno.astype(int)))
        # Pad before START_DATE so reference_close has Thursday decision / Friday
        # reference sessions ahead of the Monday snapshot.
        extract_start = str((pd.Timestamp(cfg.start_date) - pd.DateOffset(days=14)).date())
        session_start = max(pd.Timestamp(extract_start), first_price)
        session_start_s = str(session_start.date())
        report, discovered = ca.discover_ca_tables(
            db, schemas=[cfg.stock_schema], tables=[dist_table])
        available = report.groupby("table").available.any()
        if not bool(available.get(dist_table, False)) or dist_table not in discovered:
            raise ValueError(
                f"Missing required WRDS table {dist_table}; "
                "resolve the coverage gap before replay")
        distributions = ca.fetch_wrds_ca_table(
            db, discovered, dist_table, universe, extract_start, cfg.end_date)
        prices = fetch_daily_prices(
            db, universe, session_start_s, cfg.end_date, cfg.stock_schema, cfg.batch_size)
        calendar_frame = trading_calendar(
            db, session_start_s, cfg.end_date, cfg.stock_schema)

    wrds_events = ca.normalize_wrds_distributions(distributions)
    # Ledger only supports ordinary cash dividends and forward/reverse splits.
    events = wrds_events[wrds_events.event_type.isin(["CASH_DIVIDEND", "STOCK_SPLIT"])].copy()
    write_flat_inputs(
        DATA_DIR,
        weights=quarterly_weights,
        prices=prices,
        calendar=calendar_frame,
        events=events,
        start=cfg.start_date,
        end=cfg.end_date,
        schema=cfg.stock_schema,
        universe=universe,
    )
    return {
        "universe": len(universe),
        "price_rows": len(prices),
        "sessions": len(calendar_frame),
        "weights_pulled": weights_pulled,
        "weights_rows": len(quarterly_weights),
        "event_rows": len(events),
        "files": [
            "weights.csv",
            "daily_prices.csv",
            "calendar.csv",
            "events.csv",
            "coverage.json",
        ],
    }


def fetch_weights_quarterly(start: str, end: str, schema_stock="crsp_q_stock") -> pd.DataFrame:
    """Optional helper: pull a quarterly S&P 500 weight panel from WRDS."""
    from wrds_auth import wrds_session

    membership = schema_stock.replace("_stock", "_indexes")
    with wrds_session() as db:
        weights, _info = sp500_cap_weights_quarterly(
            db, start, end, membership_schema=membership, stock_schema=schema_stock)
    return weights
