"""Run the supplied public labels using only GET requests and local OCR."""

from __future__ import annotations

import json
import keyring
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import analyze_one
from src.qiheng_api import QihengClient


LABELS = ROOT / "evals" / "public-sample-labels.json"
OUTPUT = ROOT / "evals" / "vnext-public-regression.json"


def main() -> None:
    key = os.environ.get("QIHENG_API_KEY") or keyring.get_password("qiheng-review-workbench", "api-key")
    if not key:
        raise SystemExit("Windows Credential Manager 中未找到本地 API Key。")
    labels = json.loads(LABELS.read_text(encoding="utf-8"))["claims"]
    client = QihengClient(api_key=key, base_url=os.environ.get("QIHENG_BASE_URL", "http://127.0.0.1:8081"))
    rows = []
    for index, (claim_id, expected) in enumerate(labels.items(), start=1):
        outcome = analyze_one(client, claim_id)
        actual = outcome["result"]
        actual_codes = sorted({item["code"] for item in actual.get("violations", [])})
        rows.append({
            "index": index,
            "claimId": claim_id,
            "claimNo": expected["claimNo"],
            "expectedVerdict": expected["expectedVerdict"],
            "expectedCodes": expected["violations"],
            "actualVerdict": actual["result"],
            "actualCodes": actual_codes,
            "reasons": actual["reasons"],
            "verdictMatch": expected["expectedVerdict"] == actual["result"],
            "codeMatch": set(expected["violations"]) == set(actual_codes),
        })
        print(f"{index}/30 {expected['claimNo']} {actual['result']}", flush=True)
    summary = {
        "total": len(rows),
        "verdictMatches": sum(row["verdictMatch"] for row in rows),
        "codeMatches": sum(row["codeMatch"] for row in rows),
        "differences": [row for row in rows if not row["verdictMatch"] or not row["codeMatch"]],
        "rows": rows,
    }
    OUTPUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("total", "verdictMatches", "codeMatches")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
