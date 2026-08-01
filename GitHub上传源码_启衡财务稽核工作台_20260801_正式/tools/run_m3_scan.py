"""Run a fresh, read-only M3 full-ledger scan and build a submission fragment."""
from __future__ import annotations

import json
import keyring
import os
import sys
import argparse
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.m3_scanner import scan_ledger  # noqa: E402
from src.qiheng_api import QihengClient  # noqa: E402


OUT = ROOT / "formal-m3"


def main() -> None:
    parser = argparse.ArgumentParser(description="Fresh, read-only M3 ledger scan.")
    parser.add_argument("--base-url", default=os.environ.get("QIHENG_BASE_URL", "http://127.0.0.1:18081"))
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    key = os.environ.get("QIHENG_API_KEY") or keyring.get_password("qiheng-review-workbench", "api-key")
    if not key:
        raise RuntimeError("Windows 凭据管理器中未找到本地 API Key。请先在工作台完成一次连接。")
    client = QihengClient(key, base_url=args.base_url)
    invoices = list(client.iter_invoices())
    vendors = list(client.iter_vendors())
    result = scan_ledger(invoices, vendors)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    args.out.mkdir(parents=True, exist_ok=True)
    snapshot = args.out / f"m3-ledger-snapshot-{stamp}.json"
    report = args.out / f"m3-scan-{stamp}.json"
    snapshot.write_text(json.dumps({"invoices": invoices, "vendors": vendors}, ensure_ascii=False), encoding="utf-8")
    result["run"] = {"at": datetime.now().isoformat(timespec="seconds"), "mode": "GET only", "snapshot": str(snapshot), "apiPaths": ["GET /v1/invoices", "GET /v1/vendors"]}
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = args.out / "m3-latest.json"
    latest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": result["summary"], "report": str(report), "snapshot": str(snapshot)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
