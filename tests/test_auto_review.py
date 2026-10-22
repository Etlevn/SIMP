"""Unit tests for offline auto-review helpers."""

from pathlib import Path
import inspect

import pytest

from auto_review import EVENT_EVIDENCE, SCOPE_EVIDENCE, run_auto_review

ROOT = Path(__file__).resolve().parents[1]


def test_event_evidence_table_loaded():
    assert "CRSP:DIST:84769:2026-03-23:1" in EVENT_EVIDENCE
    side, amount, payday, url, explanation = EVENT_EVIDENCE["CRSP:DIST:84769:2026-03-23:1"]
    assert side == "WRDS"
    assert amount == pytest.approx(0.25)
    assert payday == "2026-04-14"
    assert url.startswith("http")
    assert "Amphenol" in explanation


def test_scope_evidence_tickers():
    assert set(SCOPE_EVIDENCE) >= {"BAX", "FIS", "JNJ", "KDP", "KHC", "TFX"}
    assert SCOPE_EVIDENCE["BAX"]["announcement"] == "2023-01-06"
    assert SCOPE_EVIDENCE["JNJ"]["related_date"] == ""


def test_require_window_mismatch_raises():
    coverage = ROOT / "data/cache/coverage.json"
    if not coverage.exists():
        pytest.skip("local cache not present")
    with pytest.raises(ValueError, match="require window"):
        run_auto_review(
            ROOT,
            require_window=("1999-01-01", "1999-12-31"),
            verify_cache_sha256=False,
        )


def test_exclude_remaining_pending_defaults_on():
    params = inspect.signature(run_auto_review).parameters
    assert params["exclude_remaining_pending"].default is True
