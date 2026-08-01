"""Run one fresh, read-only 300-claim M2 batch for final acceptance.

It deliberately requires an explicit API key in the process environment.  No
key is written to the run folder, source tree, EXE, or submission JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app  # noqa: E402
from src.qiheng_api import QihengClient  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Fresh read-only M2 batch for isolated final acceptance.")
    parser.add_argument("--base-url", default=os.environ.get("QIHENG_BASE_URL", "http://127.0.0.1:18081"))
    parser.add_argument("--out-root", type=Path, default=ROOT / "formal-m2" / "fresh-runs")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    key = os.environ.get("QIHENG_API_KEY")
    if not key:
        raise SystemExit("QIHENG_API_KEY is required. It is read only from this process environment and is never persisted.")
    if args.workers < 1 or args.workers > 4:
        raise SystemExit("workers must be between 1 and 4 to respect the ERP gateway limit.")

    client = QihengClient(api_key=key, base_url=args.base_url)
    claims = list(client.iter_expense_claims("PENDING"))
    if len(claims) != 300 or len({c.get("id") for c in claims}) != 300:
        raise SystemExit(f"Expected exactly 300 unique PENDING claims in the isolated ERP; got {len(claims)}.")

    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_root = args.out_root / batch_id
    run_root.mkdir(parents=True, exist_ok=False)
    # analyze_one writes its normal evidence folder beneath this explicit,
    # batch-scoped directory. It performs GET/OCR work only.
    app.RUNS = run_root
    manifest = {
        "batchId": batch_id,
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "baseUrl": args.base_url,
        "mode": "GET/OCR only; no ERP writeback",
        "expectedClaims": 300,
        "claims": [{"id": c["id"], "claimNo": c.get("claimNo")} for c in claims],
        "completed": [],
        "failures": [],
    }
    manifest_path = run_root / "batch-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_claim(claim: dict[str, object]) -> dict[str, object]:
        return app.analyze_one(client, summary=claim)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_claim, claim): claim for claim in claims}
        for number, future in enumerate(as_completed(futures), start=1):
            claim = futures[future]
            try:
                outcome = future.result()
                # app.analyze_one returns an evidence envelope.  Store the
                # actual review verdict, not that nested object, so the
                # manifest is independently auditable and compact.
                review = outcome.get("result", outcome)
                verdict = review.get("result") if isinstance(review, dict) else None
                if verdict not in {"APPROVE", "REJECT", "FLAG"}:
                    raise RuntimeError(f"analysis returned no valid verdict: {verdict!r}")
                manifest["completed"].append({"id": claim["id"], "claimNo": claim.get("claimNo"), "result": verdict})
                print(f"{number}/300 {claim.get('claimNo')} {verdict}", flush=True)
            except Exception as exc:  # retain evidence of an incomplete batch; never fabricate a result
                manifest["failures"].append({"id": claim.get("id"), "claimNo": claim.get("claimNo"), "error": str(exc)})
                print(f"{number}/300 {claim.get('claimNo')} FAILED: {exc}", flush=True)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest["finishedAt"] = datetime.now().isoformat(timespec="seconds")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if manifest["failures"] or len(manifest["completed"]) != 300:
        raise SystemExit(f"Batch incomplete: completed={len(manifest['completed'])}, failures={len(manifest['failures'])}. No submission may be generated.")
    print(json.dumps({"batchId": batch_id, "runRoot": str(run_root), "manifest": str(manifest_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
