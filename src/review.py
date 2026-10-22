"""Corporate-action review gate."""
from __future__ import annotations

import hashlib

import pandas as pd


def scope_events(events, permnos, start, end):
    """Return comparable records and quarantined unknown identities/dates separately."""
    e = events.copy()
    e["ex_date"] = pd.to_datetime(e["ex_date"], errors="coerce")
    ids = pd.to_numeric(e["permno"], errors="coerce")
    dates = e["ex_date"].between(pd.Timestamp(start), pd.Timestamp(end))
    unknown = (ids.isna() & dates) | (e["ex_date"].isna() & (ids.isna() | ids.isin(permnos)))
    return e.loc[dates & ids.isin(permnos)].copy(), e.loc[unknown].copy()


def review_candidates(unified, sources=None):
    """One stable review key per reconciled event; preserve source choices."""
    r = unified.copy()
    fingerprints = {} if sources is None else {
        row.source_event_id: row.to_json(date_format="iso") for _, row in sources.iterrows()}

    def key(row):
        payload = row.to_json(date_format="iso") + "|" + fingerprints.get(
            row.wrds_event_id, "") + "|" + fingerprints.get(row.bloomberg_event_id, "")
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    r["review_key"] = pd.Series([key(row) for _, row in r.iterrows()], index=r.index, dtype=str)
    if r["review_key"].duplicated().any():
        raise ValueError("Nonunique review keys")
    r["decision"] = "PENDING"
    r["chosen_source_event_id"] = r["wrds_event_id"].fillna(r["bloomberg_event_id"])
    r["rationale"] = ""
    return r


def approved_events(candidates, decisions, sources):
    """Every candidate must be approved or explicitly excluded with a rationale."""
    required = {"review_key", "decision", "chosen_source_event_id", "rationale"}
    if not required <= set(decisions):
        raise ValueError(f"Decisions need {required}")
    if decisions["review_key"].duplicated().any():
        raise ValueError("Duplicate review decisions")
    if set(decisions.review_key) != set(candidates.review_key):
        raise ValueError("Decisions are stale or incomplete; regenerate against this candidate set")
    if sources.source_event_id.duplicated().any():
        raise ValueError("Source event IDs must be unique")
    indexed = sources.set_index("source_event_id", drop=False)
    joined = candidates.drop(columns=["decision", "chosen_source_event_id", "rationale"]).merge(
        decisions[list(required)], on="review_key", validate="one_to_one")
    records = []
    for _, r in joined.iterrows():
        if r.decision not in {"APPROVE", "EXCLUDE"} or pd.isna(r.rationale) or not str(r.rationale).strip():
            raise ValueError("All events need APPROVE/EXCLUDE and a documented rationale")
        if r.decision == "EXCLUDE":
            continue
        chosen = r.chosen_source_event_id
        if chosen not in {r.wrds_event_id, r.bloomberg_event_id} or chosen not in indexed.index:
            raise ValueError("Chosen event is not a source of this candidate")
        e = indexed.loc[chosen].copy()
        if e.event_type not in {"CASH_DIVIDEND", "STOCK_SPLIT"}:
            raise ValueError(f"Unsupported action {e.event_type}; resolve before replay")
        if e.event_type == "CASH_DIVIDEND" and str(e.currency).upper() != "USD":
            raise ValueError("USD accounting requires verified USD event currency")
        e["requires_review"] = False
        e["review_reason"] = ""
        records.append(e)
    return pd.DataFrame(records, columns=sources.columns), joined
