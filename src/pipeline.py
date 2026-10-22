"""Orchestrate cache build, review gate, and daily-fund outputs."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pandas as pd

import corporate_actions as ca
from cache_io import (
    export_cache,
    load_cache,
    load_coverage,
    read_table,
    verify_coverage,
    write_coverage,
)
from ledger import prepare_exclusion_scenario, simulate
from market_data import fetch_daily_prices, trading_calendar
from paths import (
    CACHE_DIR,
    OUTPUT_DIR,
    PROJECT_ROOT,
    REVIEW_DIR,
    bloomberg_raw_path,
    ensure_data_dirs,
)
from review import approved_events, review_candidates, scope_events
from universe import get_price_bounds, sp500_cap_weights_quarterly
from wrds_auth import wrds_session


@dataclass
class PipelineConfig:
    """Runtime settings. Prefer ``from_user_config`` (root ``config.py``); no baked-in experiment defaults."""

    start_date: str
    end_date: str
    stock_schema: str
    weights_start: str
    input_mode: str
    initial_cash: float
    cash_buffer_bps: float
    fee_bps: float
    batch_size: int
    excluded_event_policy: str
    exclude_reverse_split_securities: bool

    @classmethod
    def from_user_config(cls, user_config: ModuleType | Any) -> "PipelineConfig":
        """Build from root ``config.py`` (or any namespace with the same attributes)."""
        return cls(
            start_date=user_config.START_DATE,
            end_date=user_config.END_DATE,
            stock_schema=user_config.STOCK_SCHEMA,
            weights_start=user_config.WEIGHTS_START,
            input_mode=str(user_config.INPUT_MODE).strip().lower(),
            initial_cash=float(user_config.INITIAL_CASH),
            cash_buffer_bps=float(user_config.CASH_BUFFER_BPS),
            fee_bps=float(user_config.FEE_BPS),
            batch_size=int(user_config.BATCH_SIZE),
            excluded_event_policy=user_config.EXCLUDED_EVENT_POLICY,
            exclude_reverse_split_securities=bool(
                user_config.EXCLUDE_REVERSE_SPLIT_SECURITIES),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "PipelineConfig":
        """Load root ``config.py``. ``path`` is accepted for API compatibility and ignored."""
        del path  # experiment knobs live in root config.py only
        import config as user_config
        return cls.from_user_config(user_config)


def _load_or_none_weights() -> tuple[pd.DataFrame | None, dict]:
    """Return (weights, upstream_tables) when a local weights panel exists."""
    weights_path = CACHE_DIR / "weights.csv"
    if not weights_path.exists():
        return None, {}
    if (CACHE_DIR / "manifest.json").exists():
        _, upstream = load_cache(CACHE_DIR, verify_hash=False)
        return upstream["weights"], upstream
    return read_table(weights_path), {}


def _create_template(path: Path, frame: pd.DataFrame) -> None:
    if not path.exists():
        frame.to_csv(path, index=False)


def _review_artifacts() -> Path:
    """Generated review files under review/artifacts/ (not the two decision CSVs)."""
    return REVIEW_DIR / "artifacts"


def _build_run_provenance(cfg: PipelineConfig) -> dict:
    """_SIM-shaped ledger provenance: window knobs + sha256 of gate/cache/code files.

    Mirrors Stage3 ``run_provenance.json``: manifest, coverage, the two decision
    CSVs, ``research_pipeline.py``, and ``data_loader.py`` (entry stand-in for
    the Stage1/2 source notebook).
    """
    code_root = Path(__file__).resolve().parents[1]
    paths = [
        CACHE_DIR / "manifest.json",
        CACHE_DIR / "coverage.json",
        REVIEW_DIR / "event_decisions.csv",
        REVIEW_DIR / "scope_decisions.csv",
        code_root / "research_pipeline.py",
        code_root / "data_loader.py",
    ]
    files = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            key = str(path.relative_to(PROJECT_ROOT))
        except ValueError:
            key = str(path.relative_to(code_root))
        files[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "start": cfg.start_date,
        "end": cfg.end_date,
        "aum": cfg.initial_cash,
        "cash_buffer_bps": cfg.cash_buffer_bps,
        "fee_bps": cfg.fee_bps,
        "files": files,
    }


def _write_pending_decision_templates(
    candidates: pd.DataFrame, quarantine: pd.DataFrame, *, force: bool
) -> None:
    """Write event/scope decision CSVs as all-PENDING (force overwrites existing)."""
    artifacts = _review_artifacts()
    artifacts.mkdir(parents=True, exist_ok=True)
    decisions_path = REVIEW_DIR / "event_decisions.csv"
    scope_path = REVIEW_DIR / "scope_decisions.csv"
    candidates.to_csv(artifacts / "event_candidates.csv", index=False)
    scope_template = quarantine.copy()
    scope_template["decision"] = "PENDING"
    scope_template["rationale"] = ""
    if force:
        candidates.to_csv(decisions_path, index=False)
        scope_template.to_csv(scope_path, index=False)
    else:
        _create_template(decisions_path, candidates)
        _create_template(scope_path, scope_template)


def review_init(
    cfg: PipelineConfig | None = None, *, reset_to_pending: bool = False
) -> dict:
    """Build review candidates/templates from data/cache.

    By default, existing non-PENDING ``event_decisions`` are preserved.
    Pass ``reset_to_pending=True`` (config ``RESET_REVIEW_TO_PENDING``) to rebuild
    candidates and rewrite event/scope decisions as all PENDING.
    """
    cfg = cfg or PipelineConfig.load()
    ensure_data_dirs()
    bloomberg = bloomberg_raw_path()
    verify_coverage(CACHE_DIR, bloomberg)
    coverage = load_coverage(CACHE_DIR)
    if (coverage["start"], coverage["end"], coverage["schema"]) != (
            cfg.start_date, cfg.end_date, cfg.stock_schema):
        raise ValueError("Cache dates/schema differ from config; fetch again or update config")
    _, inputs = load_cache(CACHE_DIR)
    universe = coverage["universe"]
    wrds_parts = [inputs[name] for name in ["wrds_corporate_actions", "wrds_delist_events"]
                  if not inputs[name].empty]
    wrds_all = pd.concat(wrds_parts, ignore_index=True) if wrds_parts else inputs["wrds_corporate_actions"].copy()
    bloomberg_all = inputs["bloomberg_corporate_actions"]
    wrds_scoped, wrds_unknown = scope_events(wrds_all, universe, cfg.start_date, cfg.end_date)
    bbg_scoped, bbg_unknown = scope_events(bloomberg_all, universe, cfg.start_date, cfg.end_date)
    quarantine = pd.concat([wrds_unknown, bbg_unknown], ignore_index=True)
    sources = pd.concat([wrds_scoped, bbg_scoped], ignore_index=True)
    unified = ca.reconcile_corporate_actions(wrds_scoped, bbg_scoped)
    candidates = review_candidates(unified, sources)
    factor_checks = ca.check_split_factors(wrds_scoped, inputs["wrds_adj_factors"])
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    artifacts = _review_artifacts()
    artifacts.mkdir(parents=True, exist_ok=True)
    factor_checks.to_csv(artifacts / "split_factor_checks.csv", index=False)
    quarantine.to_csv(artifacts / "scope_quarantine.csv", index=False)
    decisions_path = REVIEW_DIR / "event_decisions.csv"

    if not reset_to_pending and decisions_path.exists():
        existing = pd.read_csv(decisions_path)
        if set(existing.get("decision", [])) - {"PENDING", ""}:
            return {
                "candidates": len(existing),
                "quarantine": len(quarantine),
                "match_status": unified.match_status.value_counts().to_dict(),
                "review_dir": str(REVIEW_DIR),
                "preserved_existing_decisions": True,
                "reset_to_pending": False,
            }

    _write_pending_decision_templates(candidates, quarantine, force=reset_to_pending)
    return {
        "candidates": len(candidates),
        "quarantine": len(quarantine),
        "match_status": unified.match_status.value_counts().to_dict(),
        "review_dir": str(REVIEW_DIR),
        "preserved_existing_decisions": False,
        "reset_to_pending": bool(reset_to_pending),
    }


def _apply_approvals(cfg: PipelineConfig):
    coverage = load_coverage(CACHE_DIR)
    _, inputs = load_cache(CACHE_DIR)
    universe = coverage["universe"]
    wrds_parts = [inputs[name] for name in ["wrds_corporate_actions", "wrds_delist_events"]
                  if not inputs[name].empty]
    wrds_all = pd.concat(wrds_parts, ignore_index=True) if wrds_parts else inputs["wrds_corporate_actions"].copy()
    bloomberg_all = inputs["bloomberg_corporate_actions"]
    wrds_scoped, wrds_unknown = scope_events(wrds_all, universe, cfg.start_date, cfg.end_date)
    bbg_scoped, bbg_unknown = scope_events(bloomberg_all, universe, cfg.start_date, cfg.end_date)
    quarantine = pd.concat([wrds_unknown, bbg_unknown], ignore_index=True)
    sources = pd.concat([wrds_scoped, bbg_scoped], ignore_index=True)
    unified = ca.reconcile_corporate_actions(wrds_scoped, bbg_scoped)
    # Always fingerprint from current cache sources. Never reuse on-disk candidates:
    # an old artifact would keep review_keys aligned with stale APPROVE rows.
    candidates = review_candidates(unified, sources)
    artifacts = _review_artifacts()
    artifacts.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(artifacts / "event_candidates.csv", index=False)

    scope_decisions = pd.read_csv(REVIEW_DIR / "scope_decisions.csv")
    if scope_decisions.source_event_id.duplicated().any() or set(scope_decisions.source_event_id) != set(quarantine.source_event_id):
        raise ValueError("Scope decisions are stale, duplicated or incomplete")
    if not scope_decisions.empty and (
        not scope_decisions.decision.eq("EXCLUDE").all()
        or scope_decisions.rationale.fillna("").str.strip().eq("").any()):
        raise ValueError("Review unknown identity/date records in scope_decisions.csv")
    decisions = pd.read_csv(REVIEW_DIR / "event_decisions.csv")
    approved, approval_audit = approved_events(candidates, decisions, sources)
    approved.to_csv(artifacts / "approved_events.csv", index=False)
    approval_audit.to_csv(artifacts / "approval_audit.csv", index=False)
    return approved, approval_audit, scope_decisions, inputs, coverage


def run_ledger(cfg: PipelineConfig | None = None) -> dict:
    """Approve gate + simulate + write output/data/."""
    cfg = cfg or PipelineConfig.load()
    ensure_data_dirs()
    bloomberg = bloomberg_raw_path()
    verify_coverage(CACHE_DIR, bloomberg)
    approved, approval_audit, scope_decisions, inputs, coverage = _apply_approvals(cfg)
    weights = inputs["weights"]
    snapshots = weights[weights.date.between(cfg.start_date, cfg.end_date)].copy()
    if snapshots.empty or snapshots.date.min() != pd.Timestamp(cfg.start_date):
        raise ValueError("START_DATE must be a decision snapshot present in the weights panel")
    prices = read_table(CACHE_DIR / "daily_prices.csv")
    calendar = read_table(CACHE_DIR / "calendar.csv").date

    replay_snapshots, replay_events = snapshots, approved
    if cfg.excluded_event_policy == "exclude_securities":
        replay_snapshots, replay_events, excluded = prepare_exclusion_scenario(
            snapshots, approved, approval_audit,
            exclude_reverse_splits=cfg.exclude_reverse_split_securities)
        excluded.to_csv(OUTPUT_DIR / "excluded_security_snapshots.csv", index=False)
    elif cfg.excluded_event_policy != "keep_securities":
        raise ValueError("Unknown excluded_event_policy")

    results = simulate(
        replay_snapshots, prices, calendar, replay_events, cfg.start_date, cfg.end_date,
        initial_cash=cfg.initial_cash, cash_buffer_bps=cfg.cash_buffer_bps, fee_bps=cfg.fee_bps)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in ["daily_nav", "daily_holdings", "fills", "orders", "action_journal"]:
        results[name].to_csv(OUTPUT_DIR / f"{name}.csv", index=False)

    nav = results["daily_nav"]
    summary = pd.Series({
        "sessions": len(nav), "first_session": nav.date.min(), "last_session": nav.date.max(),
        "snapshot_decisions": snapshots.date.nunique(), "ending_nav": nav.iloc[-1].nav,
        "ending_cash": nav.iloc[-1].cash, "ending_receivables": nav.iloc[-1].receivables,
        "minimum_cash": nav.cash.min(), "fees": nav.fees.sum(),
        "unitized_total_return": (1 + nav.fund_return).prod() - 1,
        "restricted_portfolio": cfg.excluded_event_policy == "exclude_securities",
        "explicit_event_exclusions": int(approval_audit.decision.eq("EXCLUDE").sum()),
        "scope_exclusions": len(scope_decisions), "real_quarter_manually_validated": False,
    })
    summary.to_csv(OUTPUT_DIR / "validation_summary.csv", header=["value"])
    run_provenance = _build_run_provenance(cfg)
    (OUTPUT_DIR / "run_provenance.json").write_text(json.dumps(run_provenance, indent=2, default=str))
    return {
        "ending_nav": float(nav.iloc[-1].nav),
        "sessions": len(nav),
        "approved_events": len(approved),
        "output_dir": str(OUTPUT_DIR),
    }


def fetch_wrds(cfg: PipelineConfig | None = None) -> dict:
    """Build/refresh data/cache from the research DB (and local CA CSV).

    Cold start: pulls quarterly weights when ``data/cache/weights.csv`` is absent.
    Otherwise reuses the local weights panel (same pattern as _SIM Stage3 handoff).
    Requires WRDS credentials via ``wrds_session``.
    """
    cfg = cfg or PipelineConfig.load()
    ensure_data_dirs()
    bloomberg = bloomberg_raw_path()
    member_schema = cfg.stock_schema.replace("_stock", "_indexes")
    quarterly_weights, _upstream = _load_or_none_weights()
    weights_pulled = False

    with wrds_session() as db:
        if quarterly_weights is None:
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
        extract_start = str((pd.Timestamp(cfg.start_date) - pd.Timedelta(days=10)).date())
        report, discovered = ca.discover_ca_tables(db, schemas=[cfg.stock_schema])
        available = report.groupby("table").available.any()
        if not all(bool(available.get(t, False)) for t in ca.CA_TABLE_NAMES):
            raise ValueError("Missing required WRDS table; resolve the coverage gap before replay")
        bundle = {table: ca.fetch_wrds_ca_table(
            db, discovered, table, universe, extract_start, cfg.end_date)
            for table in ca.CA_TABLE_NAMES}
        prices = fetch_daily_prices(
            db, universe, cfg.start_date, cfg.end_date, cfg.stock_schema, cfg.batch_size)
        calendar_frame = trading_calendar(
            db, cfg.start_date, cfg.end_date, cfg.stock_schema)

    raw_bloomberg = pd.read_csv(bloomberg, dtype={"cusip": "string", "ticker": "string"})
    mapped_bloomberg = ca.normalize_bloomberg_export(
        raw_bloomberg, bundle["stksecurityinfohist"])
    wrds_events = ca.normalize_wrds_distributions(bundle["stkdistributions"])
    delists = ca.normalize_wrds_delists(bundle["stkdelists"])
    unified = ca.reconcile_corporate_actions(wrds_events, mapped_bloomberg)
    refreshed = dict(
        weights=quarterly_weights, wrds_corporate_actions=wrds_events,
        wrds_delist_events=delists, wrds_adj_factors=bundle["stkdlycumulativeadjfactor"],
        wrds_id_history=bundle["stksecurityinfohist"],
        bloomberg_corporate_actions=mapped_bloomberg,
        unified_corporate_actions=unified,
        wrds_table_report=report, wrds_ca_bundle=bundle, selected_permnos=universe,
        CA_START_DATE=extract_start, CA_END_DATE=cfg.end_date)
    export_cache(refreshed, CACHE_DIR)
    prices.to_csv(CACHE_DIR / "daily_prices.csv", index=False)
    calendar_frame.to_csv(CACHE_DIR / "calendar.csv", index=False)
    write_coverage(
        CACHE_DIR, start=cfg.start_date, end=cfg.end_date, schema=cfg.stock_schema,
        universe=universe, bloomberg_path=bloomberg)
    return {
        "universe": len(universe),
        "price_rows": len(prices),
        "sessions": len(calendar_frame),
        "weights_pulled": weights_pulled,
        "weights_rows": len(quarterly_weights),
    }


def fetch_weights_quarterly(start: str, end: str, schema_stock="crsp_q_stock") -> pd.DataFrame:
    """Optional helper: pull a quarterly S&P 500 weight panel from WRDS."""
    membership = schema_stock.replace("_stock", "_indexes")
    with wrds_session() as db:
        weights, _info = sp500_cap_weights_quarterly(
            db, start, end, membership_schema=membership, stock_schema=schema_stock)
    return weights


def ensure_cache(cfg: PipelineConfig | None = None) -> dict:
    """Verify existing local ``data/cache`` for INPUT_MODE=cache (no remote, no seed)."""
    cfg = cfg or PipelineConfig.load()
    ensure_data_dirs()
    bloomberg = bloomberg_raw_path()
    if not (CACHE_DIR / "manifest.json").exists() or not (CACHE_DIR / "coverage.json").exists():
        raise FileNotFoundError(
            "data/cache is incomplete for INPUT_MODE=cache; restore local files under "
            "data/ or set config.INPUT_MODE='fetch_wrds' (WRDS credentials required)")
    verify_coverage(CACHE_DIR, bloomberg)
    coverage = load_coverage(CACHE_DIR)
    if (coverage["start"], coverage["end"], coverage["schema"]) != (
            cfg.start_date, cfg.end_date, cfg.stock_schema):
        raise ValueError("Cache dates/schema differ from config; fetch again or update config")
    return {"universe": len(coverage["universe"]), "coverage": coverage}
