"""Orchestrate flat data/ load (or WRDS fetch) and daily-fund outputs."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pandas as pd

import corporate_actions as ca
from benchmark import (
    fetch_sp500_total_return,
    load_benchmark,
    performance_report,
    save_benchmark,
)
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
from order_schema import SCHEMA_VERSION, notional_summary, pm_orders_from_blotter
from paths import (
    BENCHMARK_CSV,
    CALENDAR_CSV,
    CORE_INPUT_FILES,
    DATA_DIR,
    OUTPUT_DIR,
    RUN_MANIFEST_JSON,
    WEIGHTS_CSV,
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
    write_extra: bool = False  # PHASE2_WRITE_EXTRA — audit / secondary CSVs

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
            write_extra=bool(getattr(user_config, "PHASE2_WRITE_EXTRA", False)),
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
    """``fetch_data``: prefer local cache; on B-tier failure overwrite via WRDS.

    Always ensures ``data/benchmark_sp500_tr.csv`` covers the experiment window.
    Force refresh: delete or corrupt ``data/`` then re-run ``fetch_data.py``.
    """
    cfg = cfg or PipelineConfig.load()
    ensure_data_dirs()
    try:
        info = validate_local(
            DATA_DIR, start=cfg.start_date, end=cfg.end_date, schema=cfg.stock_schema)
        info = dict(info)
        info["source"] = "cache"
    except Exception as exc:
        print(
            f"Local data/ failed validation ({type(exc).__name__}: {exc}); "
            "fetching WRDS overwrite",
            flush=True,
        )
        info = fetch_wrds(cfg)
        info = dict(info)
        info["source"] = "fetch_wrds"
    bench_info = ensure_benchmark(cfg)
    info["benchmark"] = bench_info
    return info


def _benchmark_calendar_window(cfg: PipelineConfig) -> pd.DatetimeIndex:
    """Sessions the S&P 500 TR series must cover, including reference-close pads.

    Offline caches often omit the Thursday/Friday before a Monday snapshot.
    Benchmark coverage must still include those decision/reference sessions so
    Stage 2 NAV days after ``pad_reference_close_coverage`` are comparable.
    """
    if not CALENDAR_CSV.exists():
        raise FileNotFoundError(f"Missing {CALENDAR_CSV}")
    if not WEIGHTS_CSV.exists():
        raise FileNotFoundError(f"Missing {WEIGHTS_CSV}")

    cal = pd.to_datetime(pd.read_csv(CALENDAR_CSV)["date"]).dt.normalize()
    pad_start = (pd.Timestamp(cfg.start_date) - pd.DateOffset(days=14)).normalize()
    end = pd.Timestamp(cfg.end_date).normalize()
    window = set(cal[(cal >= pad_start) & (cal <= end)].tolist())

    weights = pd.read_csv(WEIGHTS_CSV)
    weights["date"] = pd.to_datetime(weights["date"]).dt.normalize()
    snaps = weights[weights.date.between(cfg.start_date, cfg.end_date)]
    if "reference_date" in snaps.columns:
        snaps = snaps.copy()
        snaps["reference_date"] = pd.to_datetime(snaps["reference_date"]).dt.normalize()
        for ref in snaps.reference_date.dropna().unique():
            ref = pd.Timestamp(ref).normalize()
            prior = pd.bdate_range(end=ref, periods=2)[0]
            if prior <= end:
                window.add(prior)
            if ref <= end:
                window.add(ref)
    out = pd.DatetimeIndex(sorted(window))
    if out.empty:
        raise ValueError("No calendar sessions in the benchmark coverage window")
    return out


def ensure_benchmark(cfg: PipelineConfig | None = None) -> dict:
    """Reuse local S&P 500 TR CSV when valid; otherwise fetch from WRDS and overwrite."""
    from benchmark import validate_benchmark
    from wrds_auth import wrds_session

    cfg = cfg or PipelineConfig.load()
    cal = _benchmark_calendar_window(cfg)
    start, end = cal.min(), cal.max()
    try:
        bench = validate_benchmark(load_benchmark(DATA_DIR), calendar=cal, start=start, end=end)
        return {
            "source": "cache",
            "path": str(BENCHMARK_CSV),
            "rows": len(bench),
            "first": str(bench.date.min().date()),
            "last": str(bench.date.max().date()),
        }
    except Exception as exc:
        print(
            f"Benchmark cache unusable ({type(exc).__name__}: {exc}); "
            "fetching CRSP S&P 500 total return",
            flush=True,
        )
        with wrds_session() as db:
            bench = fetch_sp500_total_return(db, start, end, calendar=cal)
        save_benchmark(bench, DATA_DIR)
        return {
            "source": "fetch_wrds",
            "path": str(BENCHMARK_CSV),
            "rows": len(bench),
            "first": str(bench.date.min().date()),
            "last": str(bench.date.max().date()),
        }


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=DATA_DIR.parent,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip() or None
    except Exception:
        return None


def _excluded_weight_by_snapshot(
    snapshots: pd.DataFrame, cut_parts: list[pd.DataFrame]
) -> dict[str, float]:
    if not cut_parts:
        return {}
    cut = pd.concat(cut_parts, ignore_index=True)
    if cut.empty:
        return {}
    snap = snapshots.copy()
    snap["date"] = pd.to_datetime(snap["date"]).dt.normalize()
    cut = cut.copy()
    cut["date"] = pd.to_datetime(cut["date"]).dt.normalize()
    if "cap_weight_proxy" not in cut.columns:
        cut = cut.merge(
            snap[["date", "permno", "cap_weight_proxy"]],
            on=["date", "permno"],
            how="left",
        )
    weights = pd.to_numeric(cut["cap_weight_proxy"], errors="coerce").fillna(0.0)
    cut = cut.assign(_w=weights)
    by = cut.groupby(cut["date"].dt.strftime("%Y-%m-%d"))["_w"].sum()
    return {k: float(v) for k, v in by.items()}


def _write_fund_artifacts(
    *,
    results: dict,
    flows: pd.DataFrame,
    cut_parts: list[pd.DataFrame],
    pad_rows: pd.DataFrame,
    schedule: pd.DataFrame,
    snapshots: pd.DataFrame,
    cfg: PipelineConfig,
    fund_start: pd.Timestamp,
    fund_end: pd.Timestamp,
    excluded_rows: int,
    excluded_permnos: int,
    reason_counts: dict,
    flow_nonzero: int,
) -> dict:
    """Persist core tables; optional audit extras when ``cfg.write_extra``."""
    try:
        bench = load_benchmark(DATA_DIR)
    except Exception as exc:
        raise ValueError(
            f"Phase 2 requires a usable S&P 500 TR benchmark ({type(exc).__name__}: {exc}). "
            "Re-run fetch_data.py and confirm WRDS index entitlements."
        ) from exc
    daily, summary, by_year = performance_report(
        results["daily_nav"], bench, evaluation_start=fund_start, evaluation_end=fund_end
    )
    pm_orders = pm_orders_from_blotter(results["orders"])
    notion = notional_summary(pm_orders)

    if cut_parts:
        excluded = pd.concat(cut_parts, ignore_index=True)
    else:
        excluded = pd.DataFrame(
            columns=["date", "permno", "exclusion_reason", "cap_weight_proxy"]
        )
    if not excluded.empty and "cap_weight_proxy" not in excluded.columns:
        snap = snapshots.copy()
        snap["date"] = pd.to_datetime(snap["date"]).dt.normalize()
        excluded = excluded.copy()
        excluded["date"] = pd.to_datetime(excluded["date"]).dt.normalize()
        excluded = excluded.merge(
            snap[["date", "permno", "cap_weight_proxy"]],
            on=["date", "permno"],
            how="left",
        )

    pad = pad_rows.copy() if pad_rows is not None else pd.DataFrame()
    if pad.empty:
        pad = pd.DataFrame(columns=["date", "permno", "source_date"])

    clear_output_dir()
    # Core handoff (NAV / fills / PM orders)
    results["daily_nav"].to_csv(OUTPUT_DIR / "daily_nav.csv", index=False)
    results["fills"].to_csv(OUTPUT_DIR / "fills.csv", index=False)
    pm_orders.to_csv(OUTPUT_DIR / "pm_orders.csv", index=False)

    if cfg.write_extra:
        results["daily_holdings"].to_csv(OUTPUT_DIR / "daily_holdings.csv", index=False)
        results["orders"].to_csv(OUTPUT_DIR / "orders.csv", index=False)
        schedule.to_csv(OUTPUT_DIR / "rebalance_schedule.csv", index=False)
        flows.to_csv(OUTPUT_DIR / "synthetic_flows.csv", index=False)
        results["action_journal"].to_csv(OUTPUT_DIR / "action_journal.csv", index=False)
        excluded.to_csv(OUTPUT_DIR / "excluded_security_snapshots.csv", index=False)
        pad.to_csv(OUTPUT_DIR / "padded_quotes.csv", index=False)
        daily.to_csv(OUTPUT_DIR / "benchmark_daily.csv", index=False)
        by_year.to_csv(OUTPUT_DIR / "benchmark_by_year.csv", index=False)
        notion.to_csv(OUTPUT_DIR / "order_notional_summary.csv", index=False)

    input_hashes = {
        name: _file_sha256(DATA_DIR / name)
        for name in (*CORE_INPUT_FILES, BENCHMARK_CSV.name)
    }
    nav = results["daily_nav"]
    all_row = notion.loc[notion.reason.eq("ALL")]
    total_notional = float(all_row.notional_usd.iloc[0]) if len(all_row) else 0.0
    manifest = {
        "phase": "2_fund",
        "stage": "2_fund",
        "schema_version": SCHEMA_VERSION,
        "write_extra": bool(cfg.write_extra),
        "git_commit": _git_commit(),
        "config": asdict(cfg),
        "fund_start": str(fund_start.date()),
        "fund_end": str(fund_end.date()),
        "ending_nav": float(nav.iloc[-1].nav),
        "sessions": int(len(nav)),
        "approved_events": None,  # filled by caller via update
        "padded_quote_rows": int(len(pad_rows)) if pad_rows is not None else 0,
        "excluded_rows": excluded_rows,
        "excluded_permnos": excluded_permnos,
        "exclusion_reasons": reason_counts,
        "excluded_original_weight_by_snapshot": _excluded_weight_by_snapshot(
            snapshots, cut_parts
        ),
        "flow_sessions_nonzero": flow_nonzero,
        "pm_orders": int(len(pm_orders)),
        "order_notional_usd": total_notional,
        "input_sha256": input_hashes,
        "benchmark_summary": {
            k: (None if isinstance(v, float) and (v != v) else v)
            for k, v in summary.items()
        },
    }
    return {
        "manifest": manifest,
        "benchmark_summary": summary,
        "benchmark_daily": daily,
        "pm_orders": pm_orders,
        "order_notional_summary": notion,
        "write_extra": bool(cfg.write_extra),
    }


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

    Not a user phase. Prefer ``run_fund`` / ``main_2_fund.py``.
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

    excluded_rows, excluded_permnos, reason_counts = _exclusion_summary(cut_parts)
    flow_nonzero = int(flows["flow_bps"].ne(0).sum()) if len(flows) else 0
    written = _write_fund_artifacts(
        results=results,
        flows=flows,
        cut_parts=cut_parts,
        pad_rows=pad_rows,
        schedule=results["schedule"],
        snapshots=snapshots,
        cfg=cfg,
        fund_start=fund_start,
        fund_end=fund_end,
        excluded_rows=excluded_rows,
        excluded_permnos=excluded_permnos,
        reason_counts=reason_counts,
        flow_nonzero=flow_nonzero,
    )
    manifest = written["manifest"]
    manifest["approved_events"] = len(replay_events)
    if cfg.write_extra:
        (OUTPUT_DIR / RUN_MANIFEST_JSON).write_text(
            json.dumps(manifest, indent=2, default=str) + "\n"
        )

    nav = results["daily_nav"]
    summary = written["benchmark_summary"]
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
        "pm_orders": int(len(written["pm_orders"])),
        "order_notional_usd": float(manifest.get("order_notional_usd") or 0.0),
        "write_extra": bool(cfg.write_extra),
        "schedule": results["schedule"],
        "tracking_difference_annualized_bps": summary[
            "tracking_difference_annualized_bps"
        ],
        "tracking_error_annualized_bps": summary["tracking_error_annualized_bps"],
        "benchmark_summary": summary,
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
