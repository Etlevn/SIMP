# Market-tape plugin contract

SIMP Phase 1 and Phase 3 load a **market tape** through a neutral interface
(`src/market_tape.py`). The public default is an enhanced **synthetic** tape
anchored on local CRSP closes. A private plugin may replace it for delivery
runs that use a closing-auction / multi-MIC feed.

## Modes

| `config.TAPE_MODE` | Behavior |
| --- | --- |
| `synthetic` | `SyntheticMarketTape` (default; offline) |
| `plugin` | Load `$SIMP_TAPE_PLUGIN_DIR/tape_plugin.py` → `build_tape(...)`; **hard fail** if missing |

## Plugin entrypoint

```text
$SIMP_TAPE_PLUGIN_DIR/
  tape_plugin.py   # def build_tape(**kwargs) -> MarketTape
```

`build_tape` should accept at least `seed`, `index_event_dates`, `anchors`,
`prices` (extra kwargs ignored). Returned object must provide:

- `meta` with `source` (use `plugin`), `mics`, optional `seed`
- `universe() -> list[int]`
- `stock_days(dates, permnos) -> DataFrame` with columns:

  `date, permno, primary_mic, prev_close, arrival_price, close_price,
  close_auction_volume, day_volume, day_vwap, spread_bps, sigma_daily,
  adv_shares, is_index_event`

- `venue_days(dates, permnos) -> DataFrame` with columns:

  `date, permno, mic, is_primary, volume, vwap`

- `venue_weights() -> list[tuple[str, float]]` (static fallback)

## Desk usage

Continuous trader legs allocate child shares by same-day `venue_days.volume`.
If no venue rows match, the desk falls back to static `VENUE_WEIGHTS`.

## Privacy

Do not commit licensed extracts, credentials, or vendor-specific identifiers
into the public tree. Keep private backends under a gitignored directory
(e.g. `/vendor_tape/`).
