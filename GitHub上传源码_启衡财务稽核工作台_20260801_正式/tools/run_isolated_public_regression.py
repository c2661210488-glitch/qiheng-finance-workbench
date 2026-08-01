"""Run the public 30-claim regression against the isolated acceptance ERP."""

from __future__ import annotations

import os

import keyring


def main() -> None:
    key = keyring.get_password("qiheng-isolated-final-test", "api-key")
    if not key:
        raise SystemExit("Isolated acceptance key is unavailable in OS Credential Manager.")
    os.environ["QIHENG_API_KEY"] = key
    os.environ["QIHENG_BASE_URL"] = "http://127.0.0.1:18081"
    from run_public_regression import main as run

    run()


if __name__ == "__main__":
    main()
