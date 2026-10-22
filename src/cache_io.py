"""Read/write data/cache with manifest and coverage hashes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from paths import (
    CACHE_DIR,
    CACHE_TABLES,
    EXTRA_CACHE_FILES,
    ensure_data_dirs,
)


def read_table(path):
    frame = pd.read_csv(path, dtype={"cusip": "string", "cusip9": "string",
                                    "raw_cusip": "string", "ticker": "string"})
    for col in frame:
        if col in {"date", "ex_date", "pay_date", "announce_date", "record_date",
                   "decision_date", "execution_date", "secinfostartdt", "secinfoenddt",
                   "dlycaldt", "nominal_date", "reference_date"}:
            frame[col] = pd.to_datetime(frame[col], errors="raise").dt.normalize()
    return frame


def export_cache(namespace, folder=None):
    """Export full in-memory tables into data/cache."""
    folder = Path(folder or CACHE_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 1, "tables": {}, "ca_start": namespace.get("CA_START_DATE"),
                "ca_end": namespace.get("CA_END_DATE")}
    for name in CACHE_TABLES:
        frame = namespace.get(name)
        if not isinstance(frame, pd.DataFrame):
            raise ValueError(f"Missing cache table: {name}")
        path = folder / f"{name}.csv"
        frame.to_csv(path, index=False)
        manifest["tables"][name] = {"rows": len(frame),
                                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    bundle = namespace.get("wrds_ca_bundle", {})
    manifest["extracted_permnos"] = sorted(set(
        pd.to_numeric(bundle.get("stksecurityinfohist", pd.DataFrame()).get(
            "permno", pd.Series(dtype=float)), errors="raise").dropna().astype(int)))
    manifest["selected_permnos"] = sorted(map(int, namespace.get("selected_permnos", [])))
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def load_cache(folder=None, *, verify_hash=True):
    folder = Path(folder or CACHE_DIR)
    manifest = json.loads((folder / "manifest.json").read_text())
    tables = {}
    for name, meta in manifest["tables"].items():
        path = folder / f"{name}.csv"
        if verify_hash and hashlib.sha256(path.read_bytes()).hexdigest() != meta["sha256"]:
            raise ValueError(f"Cache changed since export: {name}; refresh or re-seed")
        tables[name] = read_table(path)
        if len(tables[name]) != meta["rows"]:
            raise ValueError(f"Cache row count mismatch: {name}")
    return manifest, tables


def write_coverage(folder, *, start, end, schema, universe, bloomberg_path, extra_files=None):
    folder = Path(folder)
    bloomberg_path = Path(bloomberg_path)
    extra_files = list(extra_files or ["daily_prices.csv", "calendar.csv"])
    meta = {
        "start": str(start),
        "end": str(end),
        "schema": schema,
        "universe": sorted(map(int, universe)),
        "bloomberg_sha256": hashlib.sha256(bloomberg_path.read_bytes()).hexdigest(),
        "extra_sha256": {
            name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
            for name in extra_files if (folder / name).exists()
        },
    }
    (folder / "coverage.json").write_text(json.dumps(meta, indent=2))
    return meta


def load_coverage(folder=None):
    folder = Path(folder or CACHE_DIR)
    return json.loads((folder / "coverage.json").read_text())


def verify_coverage(folder, bloomberg_path):
    folder = Path(folder)
    coverage = load_coverage(folder)
    bloomberg_path = Path(bloomberg_path)
    if hashlib.sha256(bloomberg_path.read_bytes()).hexdigest() != coverage["bloomberg_sha256"]:
        raise ValueError("Corporate-action raw CSV changed; refresh cache")
    for name, digest in coverage.get("extra_sha256", {}).items():
        path = folder / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Changed input: {name}; refresh cache")
    return coverage

