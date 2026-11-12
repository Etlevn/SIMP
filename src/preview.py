"""Terminal table previews for root phase entrypoints (short titles, no write paths)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def print_frame(
    df: pd.DataFrame,
    *,
    title: str | None = None,
    head: int = 10,
    tail: int = 10,
    blank_before: bool = False,
) -> None:
    """Print a DataFrame preview; optional short title and leading blank line."""
    if blank_before:
        print()
    if title:
        print(title)
    n = len(df)
    if n <= head + tail:
        print(df.to_string(index=False))
        return
    print(df.head(head).to_string(index=False))
    print("···")
    print(df.tail(tail).to_string(index=False))


def print_csv(
    path: Path,
    *,
    title: str | None = None,
    head: int = 10,
    tail: int = 10,
    blank_before: bool = False,
) -> None:
    """Load ``path`` and print a table preview. Missing files are skipped silently."""
    if not path.is_file():
        return
    with path.open("rb") as handle:
        n_lines = sum(1 for _ in handle)
    n_rows = max(n_lines - 1, 0)
    if blank_before:
        print()
    if title:
        print(title)
    if n_rows <= head + tail:
        print(pd.read_csv(path).to_string(index=False))
        return
    head_df = pd.read_csv(path, nrows=head)
    # Keep header (row 0) and the last ``tail`` data rows.
    skip = list(range(1, n_rows - tail + 1))
    tail_df = pd.read_csv(path, skiprows=skip)
    print(head_df.to_string(index=False))
    print("···")
    print(tail_df.to_string(index=False))
