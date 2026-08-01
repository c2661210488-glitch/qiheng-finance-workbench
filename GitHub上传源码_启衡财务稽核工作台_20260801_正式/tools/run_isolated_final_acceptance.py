"""Launch the final isolated acceptance batch without writing its API key.

The one-time key lives only in the operating-system credential manager under
the dedicated test service name.  This wrapper is intentionally unsuitable
for delivery use: it is an internal acceptance helper, not an EXE feature.
"""

from __future__ import annotations

import os
import sys

import keyring


def main() -> None:
    # Windows background processes commonly inherit a GBK text stream.  Audit
    # evidence and reasons contain Chinese currency symbols, so force UTF-8
    # before app.analyze_one emits its normal run summary.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    key = keyring.get_password("qiheng-isolated-final-test", "api-key")
    if not key:
        raise SystemExit("Isolated acceptance key is unavailable in OS Credential Manager.")
    os.environ["QIHENG_API_KEY"] = key
    os.environ["QIHENG_BASE_URL"] = "http://127.0.0.1:18081"
    from run_m2_fresh_batch import main as run_m2

    sys.argv = [
        "run_m2_fresh_batch.py", "--base-url", "http://127.0.0.1:18081",
        "--out-root", "formal-m2/fresh-runs", "--workers", "3",
    ]
    run_m2()


if __name__ == "__main__":
    main()
