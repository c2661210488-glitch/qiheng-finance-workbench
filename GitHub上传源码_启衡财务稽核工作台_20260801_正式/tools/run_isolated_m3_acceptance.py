"""Run the M3 scan against the dedicated isolated-acceptance ERP only."""

from __future__ import annotations

import os
import sys

import keyring


def main() -> None:
    key = keyring.get_password("qiheng-isolated-final-test", "api-key")
    if not key:
        raise SystemExit("Isolated acceptance key is unavailable in OS Credential Manager.")
    os.environ["QIHENG_API_KEY"] = key
    os.environ["QIHENG_BASE_URL"] = "http://127.0.0.1:18081"
    from run_m3_scan import main as run_m3

    sys.argv = ["run_m3_scan.py", "--base-url", "http://127.0.0.1:18081", "--out", "formal-m3/final-isolated"]
    run_m3()


if __name__ == "__main__":
    main()
