"""Corporate-action extract, normalize, and dual-source reconcile helpers."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

CA_QUERY_BATCH_SIZE = 150
CA_STOCK_SCHEMAS = ["crsp_q_stock", "crsp_a_stock", "crsp"]
CA_TABLE_NAMES = ["stkdistributions", "stkdelists", "stkdlycumulativeadjfactor", "stksecurityinfohist"]
CA_FIELDS = {
    "stkdistributions": ["permno", "disexdt", "disseqnbr", "distype", "disfreqtype", "dispaymenttype", "disdetailtype", "disorigcurtype", "disdivamt", "disfacpr", "disfacshr", "disdeclaredt", "disrecorddt", "dispaydt", "dispermno", "dispermco"],
    "stkdelists": ["permno", "delistingdt", "delactiontype", "delreasontype", "delstatustype", "delret", "deldivamt", "delnextdt", "delpermno", "delpermco"],
    "stkdlycumulativeadjfactor": ["permno", "dlycaldt", "dlycumfacpr", "dlycumfacshr"],
    "stksecurityinfohist": ["permno", "secinfostartdt", "secinfoenddt", "ticker", "cusip", "cusip9", "hdrcusip", "securitynm"],
}
CA_DATE_FIELD = {"stkdistributions": "disexdt", "stkdelists": "delistingdt", "stkdlycumulativeadjfactor": "dlycaldt"}
CA_REQUIRED = {
    "stkdistributions": {"permno", "disexdt", "distype"},
    "stkdelists": {"permno", "delistingdt"},
    "stkdlycumulativeadjfactor": {"permno", "dlycaldt", "dlycumfacshr"},
    "stksecurityinfohist": {"permno", "secinfostartdt", "secinfoenddt"},
}

CA_CANONICAL_COLUMNS = [
    "source", "source_event_id", "permno", "event_type", "ex_date", "pay_date",
    "announce_date", "record_date", "cash_per_share", "split_ratio", "currency",
    "raw_ticker", "raw_cusip", "new_permno", "source_detail", "requires_review", "review_reason"
]

def empty_ca_frame():
    return pd.DataFrame(columns=CA_CANONICAL_COLUMNS)


def discover_ca_tables(db, schemas=CA_STOCK_SCHEMAS, tables=None):
    """Inspect actual WRDS entitlements and column names; never guess a schema.

    ``tables`` defaults to ``CA_TABLE_NAMES``. Pass a subset (e.g. only
    ``stkdistributions``) for the minimal flat-data fetch path.
    """
    wanted = list(CA_TABLE_NAMES if tables is None else tables)
    unknown = [t for t in wanted if t not in CA_TABLE_NAMES]
    if unknown:
        raise ValueError(f"Unknown CA table(s): {unknown}")
    rows, locations = [], {}
    for schema in schemas:
        try:
            available = {t.lower() for t in db.list_tables(library=schema)}
        except Exception as exc:
            rows.append({"schema": schema, "table": "(library)", "available": False,
                         "missing_required": "not accessible", "note": str(exc)[:120]})
            continue
        for table in wanted:
            if table not in available:
                rows.append({"schema": schema, "table": table, "available": False,
                             "missing_required": "table missing", "note": ""})
                continue
            try:
                observed = set(db.describe_table(library=schema, table=table)["name"].str.lower())
                missing = sorted(CA_REQUIRED[table] - observed)
                rows.append({"schema": schema, "table": table, "available": not missing,
                             "missing_required": ",".join(missing),
                             "note": f"{len(observed)} columns"})
                if not missing and table not in locations:
                    locations[table] = {"schema": schema, "table": table, "columns": observed}
            except Exception as exc:
                rows.append({"schema": schema, "table": table, "available": False,
                             "missing_required": "describe failed", "note": str(exc)[:120]})
    return pd.DataFrame(rows), locations




def _ca_sql_chunks(values, chunk_size=CA_QUERY_BATCH_SIZE):
    vals = sorted({int(p) for p in values if pd.notna(p)})
    if not vals:
        raise ValueError("PERMNO universe cannot be empty.")
    if chunk_size <= 0 or chunk_size > 500:
        raise ValueError("Batch size must be 1–500.")
    for i in range(0, len(vals), chunk_size):
        yield vals[i:i + chunk_size]


def fetch_wrds_ca_table(db, discovered, table, permnos, start_date, end_date):
    """Fetch one table, bounded by security universe and date window."""
    if table not in CA_TABLE_NAMES or table not in discovered:
        raise ValueError(f"Table {table} unavailable. Run discover_ca_tables() and inspect its report.")
    meta = discovered[table]
    schema, observed = meta["schema"], meta["columns"]
    if schema not in CA_STOCK_SCHEMAS or table not in CA_TABLE_NAMES:
        raise ValueError("Unexpected schema/table, refusing SQL interpolation.")
    begin, finish = pd.Timestamp(start_date).normalize(), pd.Timestamp(end_date).normalize()
    if pd.isna(begin) or pd.isna(finish) or begin > finish:
        raise ValueError("Invalid corporate-action date window.")
    cols = [c for c in CA_FIELDS[table] if c in observed]
    missing = CA_REQUIRED[table] - set(cols)
    if missing:
        raise ValueError(f"Missing required {table} columns: {sorted(missing)}")
    # Aliases stay as native CRSP column names; missing optional fields added as nulls later.
    parts = []
    for securities in _ca_sql_chunks(permnos):
        args = {f"p_{i}": p for i, p in enumerate(securities)}
        args.update({"begin": begin.date(), "finish": finish.date()})
        placeholders = ",".join(f"%(p_{i})s" for i in range(len(securities)))
        if table == "stksecurityinfohist":
            window = "secinfostartdt <= %(finish)s AND (secinfoenddt IS NULL OR secinfoenddt >= %(begin)s)"
        else:
            window = f"{CA_DATE_FIELD[table]} BETWEEN %(begin)s AND %(finish)s"
        sql = (f"SELECT {', '.join(cols)} FROM {schema}.{table} "
               f"WHERE permno IN ({placeholders}) AND {window}")
        parts.append(db.raw_sql(sql, params=args, date_cols=[c for c in cols if c.endswith("dt")]))
    result = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)
    for field in CA_FIELDS[table]:
        if field not in result:
            result[field] = pd.NA
    result.attrs["wrds_table"] = f"{schema}.{table}"
    result.attrs["start_date"] = str(begin.date())
    result.attrs["end_date"] = str(finish.date())
    return result


def fetch_wrds_ca_bundle(db, permnos, start_date, end_date):
    report, discovered = discover_ca_tables(db)
    results = {}
    for table in CA_TABLE_NAMES:
        if table not in discovered:
            print(f"UNAVAILABLE: {table}. Keep as a coverage gap; do not substitute guessed columns.")
            results[table] = pd.DataFrame(columns=CA_FIELDS[table])
            continue
        results[table] = fetch_wrds_ca_table(db, discovered, table, permnos, start_date, end_date)
        print(f"{table}: {len(results[table]):,} rows from {discovered[table]['schema']}")
    return report, results

CA_EVENT_TYPES = {
    "CD": "CASH_DIVIDEND", "FRS": "STOCK_SPLIT", "SD": "SPECIAL_CASH_DIVIDEND",
    "ROC": "RETURN_OF_CAPITAL", "SP": "SECURITY_DISTRIBUTION", "CP": "CAPITAL_PAYMENT",
    "CG": "CAPITAL_GAINS"
}


def _ca_ts(x):
    return pd.NaT if pd.isna(x) or str(x).strip() == "" else pd.Timestamp(x).normalize()


def _ca_num(x):
    return float(x) if pd.notna(x) and str(x).strip() != "" else np.nan


def normalize_wrds_distributions(raw):
    rows = []
    for index, r in raw.reset_index(drop=True).iterrows():
        p = int(r["permno"])
        d = _ca_ts(r["disexdt"])
        typ = str(r.get("distype", "")).strip().upper()
        event_type = CA_EVENT_TYPES.get(typ, "OTHER")
        if typ == "FRS" and str(r.get("disdetailtype", "")).strip().upper() == "STKDIV":
            event_type = "STOCK_DIVIDEND"
        fac = _ca_num(r.get("disfacshr", np.nan))
        cash = _ca_num(r.get("disdivamt", np.nan)) if typ in {"CD", "SD", "ROC", "CP", "CG"} else np.nan
        # disfacshr is incremental (2:1 split often reports 1.0); multiplier is 1+disfacshr.
        split_ratio = (1.0 + fac) if event_type in {"STOCK_SPLIT", "STOCK_DIVIDEND"} and np.isfinite(fac) and fac > -1 else np.nan
        pay = _ca_ts(r.get("dispaydt", pd.NaT))
        issues = []
        if event_type not in {"CASH_DIVIDEND", "STOCK_SPLIT"}: issues.append("unsupported_or_review_event_type")
        if event_type in {"CASH_DIVIDEND", "SPECIAL_CASH_DIVIDEND"} and (not np.isfinite(cash) or cash < 0): issues.append("missing_cash_amount")
        if event_type in {"CASH_DIVIDEND", "SPECIAL_CASH_DIVIDEND"} and (pd.isna(pay) or (pd.notna(d) and pay < d)): issues.append("invalid_or_missing_pay_date")
        if event_type in {"STOCK_SPLIT", "STOCK_DIVIDEND"} and (not np.isfinite(split_ratio) or split_ratio <= 0):
            issues.append("missing_share_factor")
        if pd.isna(d): issues.append("missing_ex_date")
        seq = r.get("disseqnbr", pd.NA)
        seqtext = str(int(seq)) if pd.notna(seq) and str(seq).replace(".", "", 1).isdigit() else str(index)
        rows.append({
            "source": "WRDS_CRSP", "source_event_id": f"CRSP:DIST:{p}:{str(d.date()) if pd.notna(d) else 'NA'}:{seqtext}",
            "permno": p, "event_type": event_type, "ex_date": d, "pay_date": pay,
            "announce_date": _ca_ts(r.get("disdeclaredt", pd.NaT)), "record_date": _ca_ts(r.get("disrecorddt", pd.NaT)),
            "cash_per_share": cash, "split_ratio": split_ratio,
            "currency": str(r.get("disorigcurtype", "")).strip().upper() if pd.notna(r.get("disorigcurtype", pd.NA)) else pd.NA,
            "raw_ticker": pd.NA, "raw_cusip": pd.NA,
            "new_permno": r.get("dispermno", pd.NA), "source_detail": str(typ) + ":" + str(r.get("disdetailtype", "")),
            "requires_review": bool(issues), "review_reason": ";".join(issues),
        })
    return pd.DataFrame(rows, columns=CA_CANONICAL_COLUMNS)


def normalize_wrds_delists(raw):
    rows = []
    for _, r in raw.iterrows():
        p, d = int(r["permno"]), _ca_ts(r["delistingdt"])
        rows.append({
            "source": "WRDS_CRSP", "source_event_id": f"CRSP:DELIST:{p}:{d.date() if pd.notna(d) else 'NA'}",
            "permno": p, "event_type": "DELISTING", "ex_date": d, "pay_date": pd.NaT,
            "announce_date": pd.NaT, "record_date": pd.NaT,
            "cash_per_share": np.nan, "split_ratio": np.nan, "currency": pd.NA,
            "raw_ticker": pd.NA, "raw_cusip": pd.NA, "new_permno": r.get("delpermno", pd.NA),
            "source_detail": f"action={r.get('delactiontype', '')};reason={r.get('delreasontype', '')};delret={r.get('delret', '')}",
            "requires_review": True, "review_reason": "delisting_requires_specific_payoff_and_position_handling"
        })
    return pd.DataFrame(rows, columns=CA_CANONICAL_COLUMNS)


def normalize_crsp_events(raw_distributions, raw_delists):
    events = pd.concat([normalize_wrds_distributions(raw_distributions), normalize_wrds_delists(raw_delists)], ignore_index=True)
    if events["source_event_id"].duplicated().any():
        raise ValueError("Duplicate CRSP event IDs; inspect distribution sequences and delist rows.")
    return events.sort_values(["ex_date", "permno", "event_type"]).reset_index(drop=True)

BLOOMBERG_COLUMNS = ["event_id", "permno", "ticker", "cusip", "event_type", "ex_date", "pay_date", "announce_date", "record_date", "cash_per_share", "split_ratio", "currency", "notes"]
BLOOMBERG_EVENT_MAP = {
    "SPECIAL DIVIDEND": "SPECIAL_CASH_DIVIDEND", "SPECIAL_CASH_DIVIDEND": "SPECIAL_CASH_DIVIDEND",
    "DIVIDEND": "CASH_DIVIDEND", "CASH DIVIDEND": "CASH_DIVIDEND", "CASH_DIVIDEND": "CASH_DIVIDEND",
    "SPLIT": "STOCK_SPLIT", "STOCK SPLIT": "STOCK_SPLIT", "STOCK_SPLIT": "STOCK_SPLIT",
    "STOCK DIVIDEND": "STOCK_DIVIDEND", "STOCK_DIVIDEND": "STOCK_DIVIDEND",
    "DELIST": "DELISTING", "DELISTING": "DELISTING", "MERGER": "MERGER",
    "SPINOFF": "SPINOFF", "SPIN-OFF": "SPINOFF", "RIGHTS": "RIGHTS",
    "RETURN OF CAPITAL": "RETURN_OF_CAPITAL", "RETURN_OF_CAPITAL": "RETURN_OF_CAPITAL",
}


def export_bloomberg_csv_template(path="bloomberg_corporate_actions_template.csv"):
    pd.DataFrame(columns=BLOOMBERG_COLUMNS).to_csv(path, index=False)
    return str(Path(path).resolve())


def _canonical_cusip(value):
    if pd.isna(value): return ""
    s = "".join(c for c in str(value).upper() if c.isalnum())
    return s[:8] if len(s) in {8, 9} else ""


def _canonical_ticker(value):
    if pd.isna(value): return ""
    return str(value).strip().upper().split(" ")[0].replace("/", ".")


def _resolve_bloomberg_permno(bloomberg_row, id_history):
    """Return (permno or NA, reason). NEVER auto-resolve ambiguous mapping."""
    explicit = bloomberg_row.get("permno", pd.NA)
    if pd.notna(explicit) and str(explicit).strip():
        return int(explicit), "explicit_permno_unverified"
    d = bloomberg_row.get("ex_date", pd.NaT)
    if pd.isna(d): return pd.NA, "missing_ex_date"
    if id_history.empty: return pd.NA, "missing_historical_id_mapping"
    h = id_history.copy()
    for col in ["secinfostartdt", "secinfoenddt"]:
        if col not in h: h[col] = pd.NaT
        h[col] = pd.to_datetime(h[col], errors="coerce")
    h = h[(h["secinfostartdt"].isna() | (h["secinfostartdt"] <= d)) &
          (h["secinfoenddt"].isna() | (h["secinfoenddt"] >= d))].copy()
    cusip = _canonical_cusip(bloomberg_row.get("cusip", pd.NA))
    if cusip:
        ids = set()
        for col in ["cusip", "cusip9"]:
            if col in h:
                ids |= set(h.loc[h[col].apply(_canonical_cusip).eq(cusip), "permno"].dropna().astype(int))
        if len(ids) == 1: return next(iter(ids)), "historical_cusip"
        if len(ids) > 1: return pd.NA, "ambiguous_historical_cusip"
    ticker = _canonical_ticker(bloomberg_row.get("ticker", pd.NA))
    if ticker and "ticker" in h:
        ids = set(h.loc[h["ticker"].apply(_canonical_ticker).eq(ticker), "permno"].dropna().astype(int))
        if len(ids) == 1: return next(iter(ids)), "ticker_only_review"
        if len(ids) > 1: return pd.NA, "ambiguous_historical_ticker"
    return pd.NA, "unmatched_security"


def normalize_bloomberg_export(bloomberg_frame, id_history):
    raw = bloomberg_frame.copy()
    missing = {"event_type", "ex_date"} - set(raw.columns)
    if missing: raise ValueError(f"Bloomberg export missing: {sorted(missing)}")
    if not ({"permno", "cusip", "ticker"} & set(raw.columns)):
        raise ValueError("Bloomberg export needs one of permno / cusip / ticker.")
    for col in BLOOMBERG_COLUMNS:
        if col not in raw: raw[col] = pd.NA
    rows = []
    for n, r in raw.iterrows():
        event_type = BLOOMBERG_EVENT_MAP.get(str(r["event_type"]).strip().upper().replace("-", "-"), "OTHER")
        ex_date = _ca_ts(r["ex_date"])
        permno, mapping_reason = _resolve_bloomberg_permno({**r.to_dict(), "ex_date": ex_date}, id_history)
        issues = []
        if mapping_reason != "historical_cusip" and mapping_reason != "explicit_permno_unverified":
            issues.append(mapping_reason)
        if mapping_reason == "explicit_permno_unverified": issues.append("check_explicit_permno")
        if event_type not in {"CASH_DIVIDEND", "STOCK_SPLIT"}: issues.append("unsupported_or_review_event_type")
        cash, ratio = _ca_num(r["cash_per_share"]), _ca_num(r["split_ratio"])
        if event_type in {"CASH_DIVIDEND", "SPECIAL_CASH_DIVIDEND"} and (not np.isfinite(cash) or cash < 0): issues.append("missing_cash_amount")
        pay_date = _ca_ts(r["pay_date"])
        if event_type in {"CASH_DIVIDEND", "SPECIAL_CASH_DIVIDEND"} and (pd.isna(pay_date) or pd.notna(ex_date) and pay_date < ex_date):
            issues.append("invalid_or_missing_pay_date")
        if event_type == "STOCK_SPLIT" and (not np.isfinite(ratio) or ratio <= 0): issues.append("missing_split_ratio")
        if pd.isna(ex_date): issues.append("missing_ex_date")
        source_id = str(r["event_id"]) if pd.notna(r["event_id"]) and str(r["event_id"]).strip() else f"BLOOMBERG:ROW:{n}"
        rows.append({
            "source": "BLOOMBERG", "source_event_id": source_id, "permno": permno,
            "event_type": event_type, "ex_date": ex_date, "pay_date": pay_date,
            "announce_date": _ca_ts(r["announce_date"]), "record_date": _ca_ts(r["record_date"]),
            "cash_per_share": cash, "split_ratio": ratio, "currency": r["currency"],
            "raw_ticker": r["ticker"], "raw_cusip": r["cusip"], "new_permno": pd.NA,
            "source_detail": str(r["notes"]) if pd.notna(r["notes"]) else "",
            "requires_review": bool(issues), "review_reason": ";".join(issues),
        })
    result = pd.DataFrame(rows, columns=CA_CANONICAL_COLUMNS)
    if result["source_event_id"].duplicated().any():
        raise ValueError("Bloomberg event IDs must be unique; review duplicated rows.")
    return result

def _ca_differs(a, b, atol=1e-6):
    if pd.isna(a) and pd.isna(b): return False
    if pd.isna(a) != pd.isna(b): return True
    return not np.isclose(float(a), float(b), rtol=0, atol=atol)


def _ca_dates_differ(a, b):
    if pd.isna(a) and pd.isna(b): return False
    if pd.isna(a) != pd.isna(b): return True
    return pd.Timestamp(a).normalize() != pd.Timestamp(b).normalize()


def reconcile_corporate_actions(wrds_events, bloomberg_events, cash_tol=0.005, ratio_tol=1e-6):
    """Outer reconciliation; one row per event or unique cross-source pair."""
    a, b = wrds_events.reset_index(drop=True).copy(), bloomberg_events.reset_index(drop=True).copy()
    for df in (a, b):
        for col in CA_CANONICAL_COLUMNS:
            if col not in df: df[col] = pd.NA
        df["ex_date"] = pd.to_datetime(df["ex_date"], errors="coerce").dt.normalize()
        df["permno"] = pd.to_numeric(df["permno"], errors="coerce").astype("Int64")
    key = ["permno", "event_type", "ex_date"]
    def as_key(row):
        return tuple(row[c] for c in key) if pd.notna(row["permno"]) and pd.notna(row["ex_date"]) else None
    akw = [as_key(r) for _, r in a.iterrows()]
    bkw = [as_key(r) for _, r in b.iterrows()]
    ca, cb = Counter(k for k in akw if k is not None), Counter(k for k in bkw if k is not None)
    taken = set()
    output = []
    for i, ra in a.iterrows():
        k = akw[i]
        choices = [j for j, kb in enumerate(bkw) if kb == k] if k is not None else []
        unique = bool(k is not None and ca[k] == cb[k] == 1 and len(choices) == 1)
        j = choices[0] if unique else None
        if j is not None:
            taken.add(j)
            rb = b.iloc[j]
            issues = [str(x) for x in [ra["review_reason"], rb["review_reason"]] if pd.notna(x) and str(x).strip()]
            et = ra["event_type"]
            if et in {"CASH_DIVIDEND", "SPECIAL_CASH_DIVIDEND"} and _ca_differs(ra["cash_per_share"], rb["cash_per_share"], cash_tol):
                issues.append("cash_amount_conflict")
            if et == "STOCK_SPLIT" and _ca_differs(ra["split_ratio"], rb["split_ratio"], ratio_tol):
                issues.append("split_ratio_conflict")
            if _ca_dates_differ(ra["pay_date"], rb["pay_date"]): issues.append("pay_date_conflict")
            if pd.notna(ra["currency"]) and pd.notna(rb["currency"]) and str(ra["currency"]).upper() != str(rb["currency"]).upper():
                issues.append("currency_conflict")
            row = {"match_status": "MATCHED_CONFLICT" if issues else "MATCHED_AGREE",
                   "wrds_event_id": ra["source_event_id"], "bloomberg_event_id": rb["source_event_id"],
                   "permno": ra["permno"], "event_type": et, "ex_date": ra["ex_date"],
                   "wrds_cash_per_share": ra["cash_per_share"], "bloomberg_cash_per_share": rb["cash_per_share"],
                   "wrds_split_ratio": ra["split_ratio"], "bloomberg_split_ratio": rb["split_ratio"],
                   "wrds_pay_date": ra["pay_date"], "bloomberg_pay_date": rb["pay_date"],
                   "review_reason": ";".join(dict.fromkeys(";".join(issues).split(";"))) if issues else "",
                   "requires_review": bool(issues), "review_status": "PENDING"}
        else:
            ambiguous = k is not None and (ca[k] > 1 or cb[k] > 1)
            reason = "ambiguous_same_day_events" if ambiguous else "wrds_only_not_cross_checked"
            if pd.notna(ra["review_reason"]) and str(ra["review_reason"]).strip(): reason += ";" + str(ra["review_reason"])
            row = {"match_status": "AMBIGUOUS" if ambiguous else "WRDS_ONLY", "wrds_event_id": ra["source_event_id"],
                   "bloomberg_event_id": pd.NA, "permno": ra["permno"], "event_type": ra["event_type"],
                   "ex_date": ra["ex_date"], "wrds_cash_per_share": ra["cash_per_share"],
                   "bloomberg_cash_per_share": np.nan, "wrds_split_ratio": ra["split_ratio"],
                   "bloomberg_split_ratio": np.nan, "wrds_pay_date": ra["pay_date"],
                   "bloomberg_pay_date": pd.NaT, "review_reason": reason, "requires_review": True, "review_status": "PENDING"}
        output.append(row)
    for j, rb in b.iterrows():
        if j in taken: continue
        k = bkw[j]
        ambiguous = k is not None and (cb[k] > 1 or ca[k] > 1)
        reason = "ambiguous_same_day_events" if ambiguous else "bloomberg_only_not_cross_checked"
        if pd.notna(rb["review_reason"]) and str(rb["review_reason"]).strip(): reason += ";" + str(rb["review_reason"])
        output.append({
            "match_status": "AMBIGUOUS" if ambiguous else "BLOOMBERG_ONLY",
            "wrds_event_id": pd.NA, "bloomberg_event_id": rb["source_event_id"],
            "permno": rb["permno"], "event_type": rb["event_type"], "ex_date": rb["ex_date"],
            "wrds_cash_per_share": np.nan, "bloomberg_cash_per_share": rb["cash_per_share"],
            "wrds_split_ratio": np.nan, "bloomberg_split_ratio": rb["split_ratio"],
            "wrds_pay_date": pd.NaT, "bloomberg_pay_date": rb["pay_date"],
            "review_reason": reason, "requires_review": True, "review_status": "PENDING",
        })
    columns = ["match_status", "wrds_event_id", "bloomberg_event_id", "permno", "event_type", "ex_date",
               "wrds_cash_per_share", "bloomberg_cash_per_share", "wrds_split_ratio", "bloomberg_split_ratio",
               "wrds_pay_date", "bloomberg_pay_date", "review_reason", "requires_review", "review_status"]
    return pd.DataFrame(output, columns=columns).sort_values(["ex_date", "permno"], na_position="last").reset_index(drop=True)


def prepare_review_queue(unified):
    return unified.loc[unified["requires_review"] | unified["review_status"].ne("APPROVED")].copy()


def select_approved_actions(unified, wrds_events, bloomberg_events):
    """Explicit approval is required. Preserve the chosen primary source in the approved record."""
    ready = unified.loc[unified["review_status"].eq("APPROVED")].copy()
    if ready.empty: return empty_ca_frame()
    if ready["requires_review"].any():
        raise ValueError("APPROVED rows still contain conflicts/review flags. Resolve, document, then clear requires_review.")
    source_index = pd.concat([wrds_events, bloomberg_events], ignore_index=True).set_index("source_event_id", drop=False)
    records = []
    for _, r in ready.iterrows():
        chosen_id = r["wrds_event_id"] if pd.notna(r["wrds_event_id"]) else r["bloomberg_event_id"]
        if chosen_id not in source_index.index: raise ValueError("Missing source event " + str(chosen_id))
        event = source_index.loc[chosen_id].copy()
        if isinstance(event, pd.DataFrame): raise ValueError("Nonunique source event ID")
        if event["event_type"] not in {"CASH_DIVIDEND", "STOCK_SPLIT"}:
            raise ValueError(f"Unsupported automatic ledger event: {event['event_type']}")
        event["requires_review"] = False
        event["review_reason"] = ""
        records.append(event.to_dict())
    return pd.DataFrame(records, columns=CA_CANONICAL_COLUMNS)

def empty_ca_receivables():
    return pd.DataFrame(columns=["source_event_id", "permno", "pay_date", "receivable_amount"])


def step_corporate_action_ledger(positions, cash, receivables, approved_events, asof_date):
    """Positions are pre-ex-date integer-share holdings; return (positions, cash, receivables)."""
    day = pd.Timestamp(asof_date).normalize()
    h = positions.copy()
    if not {"permno", "shares"}.issubset(h.columns):
        raise ValueError("Positions need permno and shares.")
    if h["permno"].duplicated().any(): raise ValueError("Duplicate PERMNO positions")
    h["shares"] = pd.to_numeric(h["shares"], errors="raise").astype(float)
    if (h["shares"] < 0).any(): raise ValueError("Long-only ledger cannot start with short positions.")
    cash = float(cash)
    recv = receivables.copy()
    events = approved_events.copy()
    if events.empty: events = empty_ca_frame()
    for datecol in ["ex_date", "pay_date"]:
        events[datecol] = pd.to_datetime(events[datecol], errors="coerce").dt.normalize()
    if events["source_event_id"].duplicated().any(): raise ValueError("Duplicate approved event IDs")
    today = events.loc[events["ex_date"].eq(day)].copy()
    same_security = today.groupby("permno")["event_type"].agg(set)
    if any({"CASH_DIVIDEND", "STOCK_SPLIT"} <= types for types in same_security):
        raise ValueError("Same-day split + dividend: sequencing/entitlement must be manually verified.")
    if today["requires_review"].fillna(True).any():
        raise ValueError("Unverified corporate action passed to ledger.")
    shares = h.set_index("permno")["shares"].to_dict()
    pending = []
    for _, e in today.iterrows():
        p = int(e["permno"])
        n = float(shares.get(p, 0))
        if n <= 0: continue
        if e["event_type"] == "CASH_DIVIDEND":
            amt = _ca_num(e["cash_per_share"])
            paydate = e["pay_date"]
            if not np.isfinite(amt) or amt < 0 or pd.isna(paydate) or paydate < day:
                raise ValueError("Invalid dividend amount/payment date; cannot create entitlement.")
            if e["source_event_id"] in set(recv.get("source_event_id", pd.Series(dtype=object)).values):
                raise ValueError("Dividend already accrued; do not replay an event twice")
            pending.append({"source_event_id": e["source_event_id"], "permno": p,
                            "pay_date": paydate, "receivable_amount": n * amt})
        elif e["event_type"] == "STOCK_SPLIT":
            ratio = _ca_num(e["split_ratio"])
            if not np.isfinite(ratio) or ratio <= 0: raise ValueError("Invalid split ratio")
            updated = n * ratio
            if not np.isclose(updated, round(updated), atol=1e-7):
                raise ValueError(f"Fractional split position for {p}; cash-in-lieu model required.")
            shares[p] = int(round(updated))
        else:
            raise NotImplementedError(f"Corporate action {e['event_type']} requires separate treatment")
    h["shares"] = h["permno"].map(shares).astype("int64")
    recv = pd.concat([recv, pd.DataFrame(pending)], ignore_index=True)
    if not recv.empty:
        recv["pay_date"] = pd.to_datetime(recv["pay_date"]).dt.normalize()
        paytoday = recv["pay_date"].eq(day)
        cash += float(recv.loc[paytoday, "receivable_amount"].sum())
        recv = recv.loc[~paytoday].reset_index(drop=True)
    if cash < -1e-8: raise ValueError("Negative cash after corporate actions")
    return h, cash, recv


def economic_nav(raw_position_values, cash, receivables):
    """Raw share mark-to-market + cash + unpaid dividend receivables."""
    return (float(raw_position_values) + float(cash) +
            (float(receivables["receivable_amount"].sum()) if not receivables.empty else 0.0))

def check_split_factors(events, factor_panel, atol=1e-5):
    """Cross-check event-level share ratio with adjacent daily cumulative factors."""
    columns = ["source_event_id", "permno", "ex_date", "reported_split_ratio", "implied_split_ratio", "status"]
    if factor_panel.empty or events.empty: return pd.DataFrame(columns=columns)
    factors = factor_panel.copy()
    factors["dlycaldt"] = pd.to_datetime(factors["dlycaldt"], errors="coerce").dt.normalize()
    factors["dlycumfacshr"] = pd.to_numeric(factors["dlycumfacshr"], errors="coerce")
    rows = []
    for _, e in events.loc[events["event_type"].eq("STOCK_SPLIT")].iterrows():
        d = pd.Timestamp(e["ex_date"]).normalize()
        series = factors.loc[factors["permno"].eq(e["permno"])].sort_values("dlycaldt")
        earlier = series.loc[series["dlycaldt"] < d]
        later = series.loc[series["dlycaldt"] >= d]
        ratio = np.nan
        if len(earlier) and len(later):
            before, after = earlier["dlycumfacshr"].iat[-1], later["dlycumfacshr"].iat[0]
            if np.isfinite(before) and np.isfinite(after) and after != 0: ratio = before / after
        reported = _ca_num(e["split_ratio"])
        status = ("INSUFFICIENT_FACTOR_COVERAGE" if not np.isfinite(ratio) else
                  "AGREE" if np.isfinite(reported) and np.isclose(reported, ratio, atol=atol, rtol=0) else "REVIEW_FACTOR_MISMATCH")
        rows.append({"source_event_id": e["source_event_id"], "permno": e["permno"],
                     "ex_date": d, "reported_split_ratio": reported,
                     "implied_split_ratio": ratio, "status": status})
    return pd.DataFrame(rows, columns=columns)


__all__ = [
    "BLOOMBERG_COLUMNS",
    "BLOOMBERG_EVENT_MAP",
    "CA_CANONICAL_COLUMNS",
    "CA_DATE_FIELD",
    "CA_FIELDS",
    "CA_QUERY_BATCH_SIZE",
    "CA_REQUIRED",
    "CA_STOCK_SCHEMAS",
    "CA_TABLE_NAMES",
    "check_split_factors",
    "discover_ca_tables",
    "empty_ca_frame",
    "empty_ca_receivables",
    "economic_nav",
    "export_bloomberg_csv_template",
    "fetch_wrds_ca_bundle",
    "fetch_wrds_ca_table",
    "normalize_bloomberg_export",
    "normalize_crsp_events",
    "normalize_wrds_delists",
    "normalize_wrds_distributions",
    "prepare_review_queue",
    "reconcile_corporate_actions",
    "select_approved_actions",
    "step_corporate_action_ledger",
]
