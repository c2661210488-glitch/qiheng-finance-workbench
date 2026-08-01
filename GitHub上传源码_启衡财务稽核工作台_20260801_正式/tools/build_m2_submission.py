"""Build an M2-only submission draft from one complete local read-only batch.

This tool deliberately does not call the ERP.  It refuses to create an M2
submission unless exactly 300 unique claim IDs are present in the selected
batch.  Team/member placeholders must be replaced before final course upload.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXPECTED_COUNT = 300
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "QihengReview" / "runs"
OFFICIAL_CODES = {
    "OVER_STANDARD_HOTEL", "OVER_STANDARD_MEAL", "OVER_STANDARD_CITY_TRANSPORT",
    "OVER_STANDARD_TRANSPORT_CLASS", "INVOICE_TITLE_MISMATCH", "INVOICE_TAXNO_MISMATCH",
    "DUPLICATE_INVOICE", "MISSING_APPROVAL_OVERTIME_TAXI", "MISSING_ATTACHMENT",
    "AMOUNT_MISMATCH",
}


def load_batch(runs_dir: Path, prefix: str) -> list[dict[str, Any]]:
    # A user may open and re-run one claim while a batch is executing.  Keep the
    # newest run for each claim, but still require a complete 300-claim set.
    latest_by_claim: dict[str, dict[str, Any]] = {}
    for run_dir in sorted(runs_dir.glob(f"{prefix}*")):
        try:
            claim = json.loads((run_dir / "claim.json").read_text(encoding="utf-8"))
            review = json.loads((run_dir / "review.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        codes = [item.get("code") for item in review.get("violations", [])]
        unknown = set(codes) - OFFICIAL_CODES
        if unknown:
            raise ValueError(f"{claim.get('claimNo')} contains non-official M2 code(s): {sorted(unknown)}")
        latest_by_claim[claim["id"]] = {
            "claimId": claim["id"],
            "claimNo": claim["claimNo"],
            "result": review["result"],
            "violations": sorted(set(codes)),
            "reasons": review.get("reasons", []),
            "confidence": review.get("confidence"),
            "sourceRun": run_dir.name,
        }
    return sorted(latest_by_claim.values(), key=lambda row: row["claimId"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-prefix", required=True, help="e.g. 20260728-211; use empty string only for one isolated batch directory")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--team", default="启衡报销审核工作台项目组（提交前填写）")
    parser.add_argument("--member", action="append", default=["项目负责人（提交前填写真实姓名）"])
    args = parser.parse_args()

    rows = load_batch(args.runs_dir, args.batch_prefix)
    ids = [row["claimId"] for row in rows]
    if len(rows) != EXPECTED_COUNT or len(set(ids)) != EXPECTED_COUNT:
        raise SystemExit(f"batch must contain exactly {EXPECTED_COUNT} unique reviews; got rows={len(rows)}, unique={len(set(ids))}")

    m2_reviews = [{key: row[key] for key in ("claimId", "result", "violations", "reasons", "confidence")} for row in rows]
    counts = Counter(row["result"] for row in rows)
    payload = {
        "version": 1,
        "meta": {
            "team": args.team,
            "members": args.member,
            "seed": "qiheng-2026-v1",
            "submittedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "repoUrl": "",
            "aiModels": [{
                "provider": "RapidOCR", "model": "RapidOCR ONNX Runtime（本地运行）",
                "purpose": "票据原图文字与字段提取",
            }],
            "dataEgress": [],
            "apiScopes": [
                "expense:read", "expense:review", "approval:read", "attachment:read",
                "master-data:read", "invoice:read",
            ],
        },
        "m2": {"reviews": m2_reviews},
        "eval": {
            "testSetSize": 30,
            "notes": "公开30单离线回归：结论30/30一致，官方问题代码30/30一致。OCR字段级评测待单独测试集补充。",
        },
        "_audit": {
            "kind": "M2-only draft; not final course package",
            "batchPrefix": args.batch_prefix,
            "reviewCount": len(m2_reviews),
            "resultCounts": dict(counts),
            "sourceRuns": [{"claimId": r["claimId"], "claimNo": r["claimNo"], "run": r["sourceRun"]} for r in rows],
            "note": "Do not alter m2.reviews after validation. Replace team/member placeholders before final upload.",
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(args.out), "reviews": len(m2_reviews), "resultCounts": dict(counts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
