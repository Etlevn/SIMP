"""Flat ``data/`` load/verify helpers (weights, prices, calendar, events)."""
from __future__ import annotations

import hashlib
import json
import warnings
from pathlib import Path

import pandas as pd

from paths import (
    CALENDAR_COLUMNS,
    CORE_INPUT_FILES,
    DATA_DIR,
    EVENTS_COLUMNS,
    PRICES_COLUMNS,
    WEIGHTS_COLUMNS,
    ensure_data_dirs,
)


def project_columns(frame: pd.DataFrame, columns: tuple[str, ...], *, name: str) -> pd.DataFrame:
    """Return ``frame`` restricted to ``columns``; raise if any are missing."""
    missing = [col for col in columns if col not in frame.columns]
    if missing:
        raise ValueError(f"{name} missing required columns: {', '.join(missing)}")
    return frame.loc[:, list(columns)].copy()


def read_table(path):
    frame = pd.read_csv(path, dtype={"cusip": "string", "cusip9": "string",
                                    "raw_cusip": "string", "ticker": "string"})
    for col in frame:
        if col in {"date", "ex_date", "pay_date", "announce_date", "record_date",
                   "decision_date", "execution_date", "secinfostartdt", "secinfoenddt",
                   "dlycaldt", "nominal_date", "reference_date"}:
            frame[col] = pd.to_datetime(frame[col], errors="raise").dt.normalize()
    return frame


def missing_core_files(folder=None) -> list[str]:
    folder = Path(folder or DATA_DIR)
    return [name for name in CORE_INPUT_FILES if not (folder / name).exists()]


def load_inputs(folder=None) -> dict:
    """Load the four core CSVs from a flat data directory.

    Returns dict with keys ``weights``, ``prices``, ``calendar``, ``events``.
    ``calendar`` is a DatetimeIndex/Series of session dates.
    """
    folder = Path(folder or DATA_DIR)
    missing = missing_core_files(folder)
    if missing:
        raise FileNotFoundError(
            "Missing required data files under "
            f"{folder}: {', '.join(missing)}"
        )
    weights = read_table(folder / "weights.csv")
    prices = read_table(folder / "daily_prices.csv")
    calendar = read_table(folder / "calendar.csv").date
    events = read_table(folder / "events.csv")
    return {
        "weights": weights,
        "prices": prices,
        "calendar": calendar,
        "events": events,
    }


def write_coverage(folder, *, start, end, schema, universe, extra_files=None):
    """Write ``coverage.json`` provenance after a WRDS flat-input overwrite."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    extra_files = list(extra_files or ["daily_prices.csv", "calendar.csv"])
    meta = {
        "start": str(start),
        "end": str(end),
        "schema": schema,
        "universe": sorted(map(int, universe)),
        "extra_sha256": {
            name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
            for name in extra_files if (folder / name).exists()
        },
    }
    (folder / "coverage.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def load_coverage(folder=None):
    folder = Path(folder or DATA_DIR)
    path = folder / "coverage.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing coverage.json under {folder}")
    return json.loads(path.read_text())


def verify_coverage(folder=None, *, start=None, end=None, schema=None, required=False):
    """Verify coverage.json.

    When ``required`` is False and the file is missing, warn and return None
    (legacy soft path). When ``required`` is True, missing coverage is an error.
    When ``start``/``end``/``schema`` are provided, they must match coverage.
    """
    folder = Path(folder or DATA_DIR)
    path = folder / "coverage.json"
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Missing coverage.json under {folder}")
        warnings.warn(
            f"No coverage.json under {folder}; skipping coverage checks",
            stacklevel=2,
        )
        return None
    coverage = json.loads(path.read_text())
    if start is not None and end is not None and schema is not None:
        if (coverage.get("start"), coverage.get("end"), coverage.get("schema")) != (
                start, end, schema):
            raise ValueError(
                "coverage.json dates/schema differ from config; "
                "refresh data/ or update config"
            )
    for name, digest in coverage.get("extra_sha256", {}).items():
        file_path = folder / name
        if not file_path.exists():
            raise ValueError(f"coverage references missing file: {name}")
        if hashlib.sha256(file_path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Changed input: {name}; refresh data/ or coverage.json")
    return coverage


def validate_local(folder=None, *, start: str, end: str, schema: str) -> dict:
    """Local cache check for fetch_data / Phase 2 (hard essentials, soft reference pad).

    Does **not** read ``coverage.json`` (WRDS-fetch provenance only).

    Hard fail: missing cores/columns, no START_DATE snapshot, calendar/prices
    empty or not spanning ``[START_DATE, END_DATE]``.

    Soft (warn only): missing ``reference_date`` sessions before the snapshot
    window — Stage 2 ``pad_reference_close_coverage`` can fill those.
    """
    if not str(schema).strip():
        raise ValueError("schema must be non-empty")
    folder = Path(folder or DATA_DIR)
    missing = missing_core_files(folder)
    if missing:
        raise FileNotFoundError(
            "Missing required data files under "
            f"{folder}: {', '.join(missing)}"
        )

    # Column presence (tolerate extras).
    for name, cols in (
        ("weights.csv", WEIGHTS_COLUMNS),
        ("daily_prices.csv", PRICES_COLUMNS),
        ("calendar.csv", CALENDAR_COLUMNS),
        ("events.csv", EVENTS_COLUMNS),
    ):
        header = set(pd.read_csv(folder / name, nrows=0).columns)
        need = [c for c in cols if c not in header]
        if need:
            raise ValueError(f"{name} missing required columns: {', '.join(need)}")

    inputs = load_inputs(folder)
    weights, prices, calendar, events = (
        inputs["weights"], inputs["prices"], inputs["calendar"], inputs["events"])
    del events  # columns already checked; empty events OK

    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    snaps = weights[weights.date.between(start_ts, end_ts)].copy()
    if snaps.empty or snaps.date.min() != start_ts:
        raise ValueError(
            "START_DATE must be a decision snapshot present in the weights panel"
        )
    if "reference_date" not in snaps.columns or snaps.reference_date.isna().any():
        raise ValueError("weights.reference_date is required for local validation")

    cal = pd.DatetimeIndex(pd.to_datetime(calendar)).normalize().unique().sort_values()
    if cal.empty:
        raise ValueError("calendar.csv is empty")
    if start_ts not in cal or end_ts not in cal:
        raise ValueError(
            f"calendar must include START_DATE and END_DATE "
            f"({start_ts.date()} .. {end_ts.date()})"
        )

    if prices.empty:
        raise ValueError("daily_prices.csv is empty")
    px_dates = pd.DatetimeIndex(pd.to_datetime(prices.date)).normalize()
    if px_dates.min() > start_ts or px_dates.max() < end_ts:
        raise ValueError(
            "daily_prices date span must cover "
            f"{start_ts.date()} through {end_ts.date()}"
        )

    warnings_out: list[str] = []
    first_ref = pd.Timestamp(
        snaps.loc[snaps.date == start_ts, "reference_date"].iloc[0]
    ).normalize()
    if first_ref not in cal:
        warnings_out.append(
            f"reference_date {first_ref.date()} not in calendar; "
            "Phase 2 may pad reference_close sessions"
        )
    else:
        try:
            ref_i = int(cal.get_loc(first_ref))
        except (TypeError, ValueError):
            ref_i = None
        if ref_i == 0:
            warnings_out.append(
                "no calendar session before first reference_date; "
                "Phase 2 may pad the decision session"
            )
    for msg in warnings_out:
        warnings.warn(msg, stacklevel=2)

    return {
        "universe": int(snaps.permno.nunique()),
        "snapshots": int(snaps.date.nunique()),
        "warnings": warnings_out,
    }


def load_cache(*_args, **_kwargs):
    """Removed multi-table cache API. Use ``load_inputs`` for flat ``data/``."""
    raise RuntimeError(
        "load_cache was removed with the flat data/ layout; use load_inputs() instead"
    )


def export_cache(*_args, **_kwargs):
    """Removed multi-table cache API. Use ``write_flat_inputs`` instead."""
    raise RuntimeError(
        "export_cache was removed with the flat data/ layout; use write_flat_inputs() instead"
    )


def reject_unresolved_events(events: pd.DataFrame) -> None:
    """Fail Stage-1 write if upstream still flagged rows for review.

    ``requires_review`` is not persisted on the main path; quality issues must
    be resolved (or rows dropped) before ``write_flat_inputs``.
    """
    if events is None or events.empty or "requires_review" not in events.columns:
        return
    flagged = events["requires_review"].fillna(True).astype(bool)
    if not flagged.any():
        return
    bad = events.loc[flagged]
    sample = bad["source_event_id"].astype(str).head(5).tolist() if "source_event_id" in bad.columns else []
    hint = f" examples={sample}" if sample else ""
    raise ValueError(
        f"{int(flagged.sum())} event(s) still require review; resolve upstream "
        f"before writing data/{hint}"
    )


def write_flat_inputs(
    folder,
    *,
    weights: pd.DataFrame,
    prices: pd.DataFrame,
    calendar: pd.DataFrame | pd.Series,
    events: pd.DataFrame,
    start: str,
    end: str,
    schema: str,
    universe,
) -> dict:
    """Persist the four core tables and a WRDS-fetch ``coverage.json`` snapshot.

    Frames are projected to the slim main-path schemas before write.
    ``coverage.json`` is provenance only (not read by ``validate_local``).
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if isinstance(calendar, pd.DataFrame):
        calendar_frame = calendar
    else:
        calendar_frame = pd.DataFrame({"date": calendar})
    reject_unresolved_events(events)
    project_columns(weights, WEIGHTS_COLUMNS, name="weights").to_csv(
        folder / "weights.csv", index=False)
    project_columns(prices, PRICES_COLUMNS, name="prices").to_csv(
        folder / "daily_prices.csv", index=False)
    project_columns(calendar_frame, CALENDAR_COLUMNS, name="calendar").to_csv(
        folder / "calendar.csv", index=False)
    project_columns(events, EVENTS_COLUMNS, name="events").to_csv(
        folder / "events.csv", index=False)
    return write_coverage(
        folder, start=start, end=end, schema=schema, universe=universe,
        extra_files=["daily_prices.csv", "calendar.csv"],
    )
