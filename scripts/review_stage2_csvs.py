"""Frozen March–June 2026 evidence-based review helper (not called by data_loader.py).

Pins the checked cache window and re-verifies ``coverage["extra_sha256"]``.
Shared rules live in ``auto_review``; only the two decision CSVs are updated.
"""

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from auto_review import run_auto_review

PINNED_WINDOW = ("2026-03-23", "2026-06-30")


def main() -> int:
    checks = run_auto_review(
        ROOT,
        require_window=PINNED_WINDOW,
        verify_cache_sha256=True,
        exclude_remaining_pending=False,
        entry_script=Path(__file__),
    )
    print(json.dumps(checks, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
