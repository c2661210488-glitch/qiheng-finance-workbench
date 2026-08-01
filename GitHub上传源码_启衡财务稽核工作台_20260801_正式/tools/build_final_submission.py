"""Build a final M1-M3 submission from *fresh* M2/M3 artifacts.

This command never calls ERP and never changes review results.  It exists to
make the final hand-off reproducible: M2 results must come from one complete
300-claim batch, and M3 results must come from a freshly saved ledger scan.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT.parent / "validate-submission.mjs"


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read JSON: {path}: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a final M1-M3 submission JSON from fresh evidence.")
    parser.add_argument("--m2", type=Path, required=True, help="output of build_m2_submission.py")
    parser.add_argument("--m3", type=Path, required=True, help="fresh output of run_m3_scan.py")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repo-url", required=True, help="published repository URL")
    parser.add_argument("--team", default="启衡财务稽核工作台项目组")
    parser.add_argument("--member", action="append", default=[], help="repeat for each member; defaults to 林泽锟")
    parser.add_argument("--ocr-report", type=Path, help="optional human-confirmed OCR field evaluation report")
    args = parser.parse_args()

    m2_source = read_json(args.m2)
    m3_source = read_json(args.m3)
    reviews = m2_source.get("m2", {}).get("reviews", [])
    ids = [row.get("claimId") for row in reviews]
    if len(reviews) != 300 or len(set(ids)) != 300 or any(not x for x in ids):
        raise SystemExit("Final submission requires exactly 300 unique M2 reviews from one fresh batch.")
    if not isinstance(m3_source.get("duplicateInvoices"), list) or not isinstance(m3_source.get("invoiceIssues"), list):
        raise SystemExit("M3 input must be a scan report containing duplicateInvoices and invoiceIssues arrays.")
    run = m3_source.get("run", {})
    if run.get("mode") != "GET only":
        raise SystemExit("M3 input is not a read-only ERP scan report.")

    members = args.member or ["林泽锟"]
    payload: dict[str, Any] = {
        "version": 1,
        "meta": {
            "team": args.team,
            "members": members,
            "seed": "qiheng-2026-v1",
            "submittedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "repoUrl": args.repo_url,
            "aiModels": [{
                "provider": "RapidOCR",
                "model": "RapidOCR ONNX Runtime（本地运行）",
                "purpose": "票据原图文字与字段提取；最终判责由规则和人工复核完成",
            }],
            "dataEgress": [],
            "apiScopes": [
                "expense:read", "approval:read", "attachment:read", "invoice:read",
                "master-data:read", "expense:review",
            ],
        },
        "m2": {"reviews": reviews},
        "m3": {
            "duplicateInvoices": m3_source["duplicateInvoices"],
            "invoiceIssues": m3_source["invoiceIssues"],
        },
        "_audit": {
            "kind": "final M1-M3 submission generated from fresh isolated-environment evidence",
            "m2Source": str(args.m2),
            "m3Source": str(args.m3),
            "m3Run": run,
            "reviewCount": len(reviews),
            "writebackBoundary": "Only blank ERP review opinions may be written after a final re-read; conflicts are not overwritten.",
        },
    }
    if args.ocr_report:
        report = read_json(args.ocr_report)
        fields = report.get("fields", {})
        values = [x.get("accuracy") for x in fields.values() if isinstance(x, dict) and x.get("accuracy") is not None]
        payload["eval"] = {
            "extractionAccuracy": round(sum(values) / len(values), 4) if values else 0,
            "testSetSize": report.get("sampleSize", 0),
            "notes": "OCR 字段准确率只统计已人工确认样本；不可从原票判断的字段不作为自动判责依据。",
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if not VALIDATOR.exists():
        raise SystemExit(f"Submission written, but official validator is missing: {VALIDATOR}")
    check = subprocess.run(["node", str(VALIDATOR), str(args.out)], cwd=ROOT, text=True, capture_output=True)
    print(check.stdout, end="")
    if check.returncode:
        print(check.stderr, end="")
        raise SystemExit(check.returncode)
    print(json.dumps({"output": str(args.out), "m2Reviews": len(reviews), "m3Issues": len(payload["m3"]["invoiceIssues"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
