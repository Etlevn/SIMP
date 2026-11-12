"""Neutral market-tape interface for Phase 1 / Phase 3.

Public default: enhanced ``SyntheticMarketTape`` (CRSP closes when present).
Optional vendor backends load from a gitignored plugin path — never committed
to the public tree.

Canonical tables (same shape for synthetic and plugin):

``stock_days``  consolidated per (date, permno)
``venue_days``  continuous volume / VWAP per (date, permno, mic)
"""
from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

from paths import DATA_DIR, PRICES_CSV
from schema_example import VENUE_WEIGHTS

_FALLBACK_PERMNOS = (10104, 10107, 10138, 14593, 84788, 93002)

VENUE_COLS = ["date", "permno", "mic", "is_primary", "volume", "vwap"]
STOCK_COLS = [
    "date",
    "permno",
    "primary_mic",
    "prev_close",
    "arrival_price",
    "close_price",
    "close_auction_volume",
    "day_volume",
    "day_vwap",
    "spread_bps",
    "sigma_daily",
    "adv_shares",
    "is_index_event",
]

# Typical share of continuous volume by venue for large US stocks; DARK = off-exchange.
BASE_VENUE_SHARE = {
    "PRIMARY": 0.17,
    "ARCX": 0.07,
    "BATS": 0.06,
    "EDGX": 0.07,
    "MEMX": 0.04,
    "IEXG": 0.03,
    "BATY": 0.02,
    "EDGA": 0.02,
    "XCHI": 0.01,
    "DARK": 0.45,
    "OTHER_LIT": 0.06,
}


@dataclass(frozen=True)
class TapeMeta:
    source: str  # synthetic_offline | plugin
    mics: tuple[str, ...]
    seed: int | None = None


class MarketTape(Protocol):
    """Minimal tape surface for pricing and multi-MIC routing."""

    meta: TapeMeta

    def universe(self) -> list[int]:
        """PERMNO list eligible for random order generation."""

    def stock_days(self, dates: pd.DatetimeIndex, permnos: list[int]) -> pd.DataFrame:
        """One row per (date, permno) with pricing / ADV fields."""

    def venue_days(self, dates: pd.DatetimeIndex, permnos: list[int]) -> pd.DataFrame:
        """Continuous venue volume / VWAP rows for the same keys."""

    def venue_weights(self) -> list[tuple[str, float]]:
        """Static MIC → continuous-volume share (fallback when venue_days empty)."""


def u_shaped_curve(n_bins: int = 13) -> pd.Series:
    """Default intraday continuous-volume curve over n equal bins (09:30–16:00)."""
    x = (np.arange(n_bins) + 0.5) / n_bins
    w = 1 + 2.2 * (x - 0.5) ** 2 * 4 + 1.2 * np.exp(-((1 - x) / 0.06))
    return pd.Series(w / w.sum(), index=pd.RangeIndex(n_bins, name="bin"), name="volume_share")


def check_market(venue_days: pd.DataFrame, stock_days: pd.DataFrame) -> bool:
    for df, cols, name in (
        (venue_days, VENUE_COLS, "venue_days"),
        (stock_days, STOCK_COLS, "stock_days"),
    ):
        miss = set(cols) - set(df)
        if miss:
            raise ValueError(f"{name} missing {sorted(miss)}")
    if stock_days[["date", "permno"]].duplicated().any():
        raise ValueError("stock_days must be unique per date/permno")
    if venue_days[["date", "permno", "mic"]].duplicated().any():
        raise ValueError("venue_days must be unique per date/permno/mic")
    pos = [
        "prev_close",
        "arrival_price",
        "close_price",
        "day_vwap",
        "sigma_daily",
        "adv_shares",
    ]
    if (stock_days[pos] <= 0).any().any() or stock_days[pos].isna().any().any():
        raise ValueError("stock_days needs positive prices/volatility/ADV")
    return True


def synthetic_market(
    anchors: pd.DataFrame,
    *,
    seed: int = 11,
    index_event_dates=(),
    n_steps: int = 78,
    mics=None,
    auction_share_normal: float = 0.08,
    auction_share_event: float = 0.22,
    sigma_median: float = 0.016,
    adv_usd_median: float = 6e8,
    spread_bps_median: float = 3.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build venue_days / stock_days anchored on real closes (mechanics only)."""
    rng = np.random.default_rng(seed)
    a = anchors.copy()
    a["date"] = pd.to_datetime(a["date"]).dt.normalize()
    a["permno"] = a["permno"].astype(int)
    if a[["date", "permno"]].duplicated().any():
        raise ValueError("anchors must be unique per date/permno")
    n = len(a)
    if n == 0:
        empty_v = pd.DataFrame(columns=VENUE_COLS)
        empty_s = pd.DataFrame(columns=STOCK_COLS)
        return empty_v, empty_s
    events = set(pd.to_datetime(list(index_event_dates)).normalize())
    sigma = (
        a["sigma_daily"].to_numpy(float)
        if "sigma_daily" in a
        else sigma_median * np.exp(rng.normal(0, 0.35, n))
    )
    if "adv_usd" in a:
        adv_usd = a["adv_usd"].to_numpy(float)
    elif "market_cap_usd" in a:
        adv_usd = (
            0.0075
            * a["market_cap_usd"].to_numpy(float)
            * np.exp(rng.normal(0, 0.3, n))
        )
    else:
        adv_usd = adv_usd_median * np.exp(rng.normal(0, 1.0, n))
    spread = (
        spread_bps_median
        * np.exp(rng.normal(0, 0.45, n))
        * (sigma / sigma_median) ** 0.5
    )
    is_event = a["date"].isin(events).to_numpy()
    prev = a["prev_close"].to_numpy(float)
    close = a["close_price"].to_numpy(float)
    curve = u_shaped_curve(n_steps).to_numpy()
    gap = rng.normal(0, 0.3, n) * sigma
    log_open = np.log(np.maximum(prev, 1e-6)) + gap
    t = np.linspace(0, 1, n_steps + 1)
    w = rng.normal(0, 1, (n, n_steps)) * np.sqrt(1 / n_steps)
    bm = np.concatenate([np.zeros((n, 1)), np.cumsum(w, axis=1)], axis=1)
    bridge = bm - t * bm[:, [-1]]
    path = (
        log_open[:, None]
        + t * (np.log(np.maximum(close, 1e-6)) - log_open)[:, None]
        + 0.9 * sigma[:, None] * bridge
    )
    mid = np.exp(0.5 * (path[:, 1:] + path[:, :-1]))
    day_vwap = (mid * curve).sum(axis=1)
    arrival = np.exp(log_open)
    vol_mult = np.where(is_event, 1.8, 1.0) * np.exp(rng.normal(0, 0.2, n))
    day_volume = adv_usd / np.maximum(prev, 1e-6) * vol_mult
    auction_share = (
        np.where(is_event, auction_share_event, auction_share_normal)
        * np.exp(rng.normal(0, 0.15, n))
    )
    auction_vol = day_volume * auction_share
    continuous = np.maximum(day_volume - auction_vol, 0.0)
    if "primary_mic" in a:
        primary = a["primary_mic"].astype(str).to_numpy()
    else:
        primary = rng.choice(["XNYS", "XNAS"], n, p=[0.55, 0.45])
    stock = pd.DataFrame(
        {
            "date": a["date"].values,
            "permno": a["permno"].astype(int).values,
            "primary_mic": primary,
            "prev_close": prev,
            "arrival_price": arrival,
            "close_price": close,
            "close_auction_volume": np.round(auction_vol),
            "day_volume": np.round(day_volume),
            "day_vwap": day_vwap,
            "spread_bps": spread,
            "sigma_daily": sigma,
            "adv_shares": np.round(adv_usd / np.maximum(prev, 1e-6)),
            "is_index_event": is_event,
        }
    )
    names = list(BASE_VENUE_SHARE)
    base = np.array([BASE_VENUE_SHARE[k] for k in names])
    shares = rng.dirichlet(base * 200, n)
    rows = []
    for i in range(n):
        for j, k in enumerate(names):
            if k == "PRIMARY":
                mic = primary[i]
            elif k == "DARK":
                mic = "XADF"
            elif k == "OTHER_LIT":
                mic = "XBOS"
            else:
                mic = k
            if mics is not None and mic not in mics:
                continue
            noise = rng.normal(0, 0.15 * spread[i]) / 1e4
            rows.append(
                (
                    stock.date.iat[i],
                    stock.permno.iat[i],
                    mic,
                    k == "PRIMARY",
                    round(continuous[i] * shares[i, j]),
                    day_vwap[i] * (1 + noise),
                )
            )
    venue = pd.DataFrame(rows, columns=VENUE_COLS)
    if len(venue):
        venue = venue.groupby(["date", "permno", "mic"], as_index=False).agg(
            is_primary=("is_primary", "max"),
            volume=("volume", "sum"),
            vwap=("vwap", "mean"),
        )
        pv = (venue.vwap * venue.volume).groupby([venue.date, venue.permno]).sum() / (
            venue.volume.groupby([venue.date, venue.permno]).sum().replace(0, np.nan)
        )
        reindexed = pv.reindex(
            pd.MultiIndex.from_frame(stock[["date", "permno"]])
        )
        stock["day_vwap"] = reindexed.to_numpy()
        missing = ~np.isfinite(stock["day_vwap"].to_numpy(float))
        if missing.any():
            stock.loc[missing, "day_vwap"] = day_vwap[missing]
    check_market(venue, stock)
    return venue[VENUE_COLS].reset_index(drop=True), stock[STOCK_COLS].reset_index(
        drop=True
    )


def _try_load_prices() -> pd.DataFrame | None:
    path = PRICES_CSV if PRICES_CSV.is_file() else DATA_DIR / "daily_prices.csv"
    if not path.is_file():
        return None
    p = pd.read_csv(path)
    p["date"] = pd.to_datetime(p["date"]).dt.normalize()
    p["permno"] = p["permno"].astype(int)
    p["close_price"] = pd.to_numeric(p["close_price"], errors="coerce")
    return p.dropna(subset=["close_price"]).sort_values(["permno", "date"])


def anchors_from_prices(
    prices: pd.DataFrame,
    dates: pd.DatetimeIndex,
    permnos: list[int],
    *,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    """Build synthetic_market anchors from a CRSP-style close panel."""
    rng = rng or np.random.default_rng(0)
    dates = pd.DatetimeIndex(pd.to_datetime(dates)).normalize().unique().sort_values()
    permnos = [int(p) for p in permnos]
    px = prices.copy()
    px["date"] = pd.to_datetime(px["date"]).dt.normalize()
    rows = []
    for day in dates:
        for permno in permnos:
            hist = px[(px.permno == permno) & (px.date <= day)].copy()
            hist = hist[np.isfinite(hist.close_price) & (hist.close_price > 0)]
            if len(hist):
                close = float(hist.iloc[-1].close_price)
                prev = (
                    float(hist.iloc[-2].close_price) if len(hist) > 1 else close
                )
            else:
                close = float(50 + (permno % 500) + rng.normal(0, 1))
                close = max(close, 1.0)
                prev = close
            if not np.isfinite(prev) or prev <= 0:
                prev = close
            rows.append(
                {
                    "date": day,
                    "permno": permno,
                    "prev_close": prev,
                    "close_price": close,
                }
            )
    return pd.DataFrame(rows)


class SyntheticMarketTape:
    """Offline tape: enhanced synthetic venue/stock tables from local closes."""

    def __init__(
        self,
        *,
        seed: int = 7,
        prices: pd.DataFrame | None = None,
        anchors: pd.DataFrame | None = None,
        index_event_dates=(),
        mics: tuple[str, ...] | None = None,
    ) -> None:
        self._rng = np.random.default_rng(seed)
        self._seed = int(seed)
        self._index_event_dates = tuple(
            pd.to_datetime(list(index_event_dates)).normalize()
        )
        self._prices = prices if prices is not None else _try_load_prices()
        self._anchors = (
            None
            if anchors is None
            else anchors.assign(
                date=lambda d: pd.to_datetime(d["date"]).dt.normalize(),
                permno=lambda d: d["permno"].astype(int),
            )
        )
        weights = mics or tuple(m for m, _ in VENUE_WEIGHTS)
        self._mics = tuple(weights)
        self._venue_fallback = (
            list(VENUE_WEIGHTS)
            if mics is None
            else [(m, 1.0 / len(self._mics)) for m in self._mics]
        )
        self._cache: dict[tuple, tuple[pd.DataFrame, pd.DataFrame]] = {}
        self.meta = TapeMeta(
            source="synthetic_offline",
            mics=self._mics,
            seed=int(seed),
        )

    def universe(self) -> list[int]:
        if self._anchors is not None and len(self._anchors):
            return sorted(int(x) for x in self._anchors.permno.unique().tolist())
        if self._prices is not None and len(self._prices):
            return sorted(int(x) for x in self._prices.permno.unique().tolist())
        return list(_FALLBACK_PERMNOS)

    def venue_weights(self) -> list[tuple[str, float]]:
        return list(self._venue_fallback)

    def _tables(
        self, dates: pd.DatetimeIndex, permnos: list[int]
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        dates = pd.DatetimeIndex(pd.to_datetime(dates)).normalize().unique().sort_values()
        permnos = sorted({int(p) for p in permnos})
        key = (tuple(str(d.date()) for d in dates), tuple(permnos), self._index_event_dates)
        if key in self._cache:
            return self._cache[key]
        if self._anchors is not None:
            a = self._anchors[
                self._anchors.date.isin(dates) & self._anchors.permno.isin(permnos)
            ].copy()
            # Fill any missing keys from prices / RNG
            have = set(zip(a.date, a.permno)) if len(a) else set()
            need = [(d, p) for d in dates for p in permnos if (d, p) not in have]
            if need and self._prices is not None:
                extra = anchors_from_prices(
                    self._prices,
                    pd.DatetimeIndex([d for d, _ in need]),
                    sorted({p for _, p in need}),
                    rng=self._rng,
                )
                extra = extra[
                    extra.apply(lambda r: (r.date, int(r.permno)) in set(need), axis=1)
                ]
                a = pd.concat([a, extra], ignore_index=True) if len(a) else extra
            elif need:
                rows = []
                for d, p in need:
                    close = float(50 + (p % 500) + self._rng.normal(0, 1))
                    close = max(close, 1.0)
                    rows.append(
                        {
                            "date": d,
                            "permno": p,
                            "prev_close": close,
                            "close_price": close,
                        }
                    )
                extra = pd.DataFrame(rows)
                a = pd.concat([a, extra], ignore_index=True) if len(a) else extra
        else:
            if self._prices is None:
                rows = []
                for day in dates:
                    for permno in permnos:
                        close = float(50 + (permno % 500) + self._rng.normal(0, 1))
                        close = max(close, 1.0)
                        rows.append(
                            {
                                "date": day,
                                "permno": permno,
                                "prev_close": close,
                                "close_price": close,
                            }
                        )
                a = pd.DataFrame(rows)
            else:
                a = anchors_from_prices(
                    self._prices, dates, permnos, rng=self._rng
                )
        venue, stock = synthetic_market(
            a,
            seed=self._seed,
            index_event_dates=self._index_event_dates,
            mics=None,  # full BASE_VENUE_SHARE set; desk reads volumes
        )
        self._cache[key] = (venue, stock)
        return venue, stock

    def stock_days(self, dates: pd.DatetimeIndex, permnos: list[int]) -> pd.DataFrame:
        _, stock = self._tables(dates, permnos)
        return stock.reset_index(drop=True)

    def venue_days(self, dates: pd.DatetimeIndex, permnos: list[int]) -> pd.DataFrame:
        venue, _ = self._tables(dates, permnos)
        return venue.reset_index(drop=True)


def stock_days_for_pm(
    pm: pd.DataFrame,
    tape: MarketTape,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (stock_days, venue_days) covering every PM (trade_date, permno)."""
    if pm is None or pm.empty:
        raise ValueError("pm_orders empty — nothing to build stock_days for")
    keys = (
        pm[["trade_date", "permno"]]
        .assign(trade_date=lambda d: pd.to_datetime(d.trade_date).dt.normalize())
        .drop_duplicates()
    )
    dates = pd.DatetimeIndex(keys.trade_date.unique()).sort_values()
    permnos = sorted(int(p) for p in keys.permno.unique())
    stock = tape.stock_days(dates, permnos)
    venue = tape.venue_days(dates, permnos)
    stock = stock.merge(
        keys.rename(columns={"trade_date": "date"}),
        on=["date", "permno"],
        how="inner",
    )
    venue = venue.merge(
        keys.rename(columns={"trade_date": "date"}),
        on=["date", "permno"],
        how="inner",
    )
    if stock.empty:
        raise ValueError("tape returned no stock_days for requested PM keys")
    return stock.reset_index(drop=True), venue.reset_index(drop=True)


def load_market_tape(
    *,
    seed: int = 7,
    mode: str = "synthetic",
    plugin_dir: str | Path | None = None,
    index_event_dates=(),
    anchors: pd.DataFrame | None = None,
    prices: pd.DataFrame | None = None,
) -> MarketTape:
    """Load synthetic or plugin tape.

    ``mode='plugin'`` requires a loadable ``tape_plugin.py`` (no silent fallback).
    ``mode='synthetic'`` ignores missing plugins.
    """
    mode = (mode or "synthetic").strip().lower()
    if mode not in {"synthetic", "plugin"}:
        raise ValueError("TAPE_MODE must be 'synthetic' or 'plugin'")

    raw = plugin_dir if plugin_dir is not None else os.environ.get("SIMP_TAPE_PLUGIN_DIR")
    if mode == "plugin":
        if not raw:
            raise FileNotFoundError(
                "TAPE_MODE=plugin requires SIMP_TAPE_PLUGIN_DIR or plugin_dir "
                "pointing at a directory with tape_plugin.py"
            )
        path = Path(raw).expanduser().resolve()
        mod_path = path / "tape_plugin.py"
        if not mod_path.is_file():
            raise FileNotFoundError(f"Missing plugin module: {mod_path}")
        spec = importlib.util.spec_from_file_location("simp_tape_plugin", mod_path)
        if not spec or not spec.loader:
            raise RuntimeError(f"Cannot import tape plugin from {mod_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "build_tape"):
            raise RuntimeError(f"{mod_path} must expose build_tape(...)")
        tape = mod.build_tape(
            seed=seed,
            index_event_dates=index_event_dates,
            anchors=anchors,
            prices=prices,
        )
        return tape

    # synthetic: optionally use plugin if present and env set without forcing mode
    if raw:
        path = Path(raw).expanduser().resolve()
        mod_path = path / "tape_plugin.py"
        if mod_path.is_file():
            # Still honor explicit synthetic mode — do not auto-switch.
            pass

    return SyntheticMarketTape(
        seed=seed,
        prices=prices,
        anchors=anchors,
        index_event_dates=index_event_dates,
    )
