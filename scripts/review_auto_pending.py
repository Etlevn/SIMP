"""Standalone auto-review helper for any cache coverage window.

Updates only PENDING rows in ``event_decisions.csv`` / ``scope_decisions.csv``.
Does not create backups or rewrite other files under ``data/review/``.
"""

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from auto_review import run_auto_review

# --- run switches (edit here) ---
# After rule-based APPROVE/EXCLUDE, mark any still-PENDING event/scope rows as EXCLUDE
# so data_loader can pass the review gate. Default True; set False to leave unresolved PENDING.
EXCLUDE_REMAINING_PENDING = True


def main() -> int:
    checks = run_auto_review(
        ROOT,
        require_window=None,
        verify_cache_sha256=False,
        exclude_remaining_pending=EXCLUDE_REMAINING_PENDING,
        entry_script=Path(__file__),
    )
    print(json.dumps(checks, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
