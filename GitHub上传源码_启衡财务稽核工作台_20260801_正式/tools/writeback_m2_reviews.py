"""Controlled, resumable write-back of a validated M2 batch.

The ERP review API stores an AI opinion only; it must not be used to approve,
reject, or pay a claim.  This script is deliberately opt-in via ``--apply``
and checks existing ERP review records before every POST.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import keyring

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.qiheng_api import QihengClient  # noqa: E402


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def review_artifact(submission: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    return {
        "result": review["result"],
        "reasons": review.get("reasons", []),
        "evidence": review.get("evidence", []),
        "confidence": review.get("confidence", 0.5),
    }


def existing_matches(existing: list[dict[str, Any]], payload: dict[str, Any]) -> bool:
    return any(
        row.get("result") == payload["result"]
        and row.get("reasons") == payload["reasons"]
        for row in existing
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("submission", type=Path)
    parser.add_argument("--apply", action="store_true", help="required before any POST")
    parser.add_argument("--limit", type=int, default=0, help="optional safety limit; 0 means all")
    parser.add_argument("--claim-id", action="append", default=[], help="explicit claim ID(s) for a controlled write-back test")
    parser.add_argument("--runs-dir", type=Path, default=Path(os.environ.get("LOCALAPPDATA", Path.home())) / "QihengReview" / "runs",
                        help="evidence-run root used to create the submission")
    parser.add_argument("--base-url", default=os.environ.get("QIHENG_BASE_URL", "http://127.0.0.1:8081"),
                        help="ERP API address; defaults to the delivery endpoint")
    args = parser.parse_args()

    submission = load_json(args.submission)
    reviews = submission.get("m2", {}).get("reviews", [])
    sources = {row["claimId"]: row["run"] for row in submission.get("_audit", {}).get("sourceRuns", [])}
    if len(reviews) != 300 or len({row.get("claimId") for row in reviews}) != 300:
        raise SystemExit("Refusing write-back: submission must contain exactly 300 unique M2 reviews.")
    if not args.apply:
        print(json.dumps({"dryRun": True, "reviews": len(reviews), "message": "No POST executed; rerun with --apply after authorization."}, ensure_ascii=False))
        return

    key = os.environ.get("QIHENG_API_KEY") or keyring.get_password("qiheng-review-workbench", "api-key")
    if not key:
        raise SystemExit("未找到 API Key。请临时设置 QIHENG_API_KEY，或先在工作台登录。")
    client = QihengClient(key, base_url=args.base_url)
    allowed = set(client.me().get("scopes", []))
    if "expense:review" not in allowed:
        raise SystemExit("Refusing write-back: API Key lacks expense:review.")

    output_dir = ROOT / "formal-m2" / "writeback-runs"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / f"writeback-{datetime.now():%Y%m%d-%H%M%S}.json"
    manifest: dict[str, Any] = {
        "startedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "submission": str(args.submission.resolve()),
        "mode": "apply", "scope": "AI review opinion only; no status transition",
        "written": [], "skippedExisting": [], "conflicts": [], "failed": [],
    }

    if args.claim_id:
        requested = set(args.claim_id)
        selected = [row for row in reviews if row["claimId"] in requested]
        if len(selected) != len(requested):
            raise SystemExit("Refusing write-back: one or more --claim-id values are absent from the validated submission.")
    else:
        selected = reviews if not args.limit else reviews[:args.limit]
    for index, row in enumerate(selected, start=1):
        claim_id = row["claimId"]
        run_name = sources.get(claim_id)
        if not run_name:
            manifest["failed"].append({"claimId": claim_id, "error": "source run not found in submission audit"})
            continue
        review_path = args.runs_dir / run_name / "review.json"
        try:
            payload = review_artifact(submission, load_json(review_path))
            existing = client.request(f"/v1/expense-claims/{claim_id}/reviews").get("data", [])
            if existing_matches(existing, payload):
                manifest["skippedExisting"].append({"claimId": claim_id, "reason": "identical opinion already present"})
            elif existing:
                manifest["conflicts"].append({"claimId": claim_id, "existing": existing, "reason": "existing non-identical opinion; not overwritten"})
            else:
                response = client.request(f"/v1/expense-claims/{claim_id}/review", method="POST", body=payload)
                confirmed = client.request(f"/v1/expense-claims/{claim_id}/reviews").get("data", [])
                if not existing_matches(confirmed, payload):
                    raise RuntimeError("POST returned but a matching review was not found on read-back")
                manifest["written"].append({"claimId": claim_id, "result": payload["result"], "response": response})
        except Exception as exc:
            manifest["failed"].append({"claimId": claim_id, "error": str(exc)})
        finally:
            manifest["lastProcessedIndex"] = index
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"current": index, "total": len(selected), "written": len(manifest["written"]), "skipped": len(manifest["skippedExisting"]), "conflicts": len(manifest["conflicts"]), "failed": len(manifest["failed"])}, ensure_ascii=False), flush=True)

    manifest["completedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path), **{key: len(manifest[key]) for key in ("written", "skippedExisting", "conflicts", "failed")}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
