from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from src.evaluator import evaluate
from src.evidence import build_evidence_chain
from src.invoice_ocr import InvoiceExtraction
from src.ocr_router import extract_ticket
from src.qiheng_api import QihengApiError, QihengClient
from src.reviewer import review_claim


ROOT = Path(__file__).resolve().parent
RUNS = (
    Path(os.environ.get("LOCALAPPDATA", Path.home())) / "QihengReview" / "runs"
    if getattr(sys, "frozen", False)
    else ROOT / "runs"
)
READ_SCOPES = {
    "expense:read",
    "approval:read",
    "attachment:read",
    "invoice:read",
    "master-data:read",
}


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def client_from_env() -> QihengClient:
    key = os.environ.get("QIHENG_API_KEY", "")
    if not key:
        raise SystemExit(
            "缺少 QIHENG_API_KEY。请在你自己的PowerShell会话中设置，"
            "不要把完整密钥写进代码或发到聊天里。"
        )
    return QihengClient(
        api_key=key,
        base_url=os.environ.get("QIHENG_BASE_URL", "http://127.0.0.1:8081"),
    )


def find_claim(client: QihengClient, claim_no: str | None) -> dict[str, Any]:
    for claim in client.iter_expense_claims("PENDING"):
        if claim_no is None or claim["claimNo"] == claim_no or claim["id"] == claim_no:
            return claim
    raise SystemExit(f"未找到待审报销单：{claim_no}")


def ext_for_attachment(meta: dict[str, Any]) -> str:
    suffix = Path(meta.get("fileName", "")).suffix
    if suffix:
        return suffix.lower()
    return mimetypes.guess_extension(meta.get("mimeType", "")) or ".bin"


def run_one(args: argparse.Namespace) -> None:
    client = client_from_env()
    outcome = analyze_one(client, args.claim_no)
    claim = outcome["claim"]
    result = outcome["result"]
    run_dir = outcome["run_dir"]

    print(f"\n报销单：{claim['claimNo']}｜{claim.get('employeeName')}")
    print(f"建议结果：{result['result']}")
    for reason in result["reasons"]:
        print(f"- {reason}")
    print(f"\n运行证据已保存：{run_dir}")

    if args.writeback:
        raise SystemExit("此交付版为只读 MVP，已禁用审核意见回写。")
    print("当前为DRY RUN：没有调用POST，没有修改任何ERP数据。")


def analyze_one(
    client: QihengClient,
    claim_no: str | None = None,
    *,
    summary: dict[str, Any] | None = None,
    ocr_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """只读分析一张待审单，供命令行和桌面界面共同调用。"""
    me = client.me()
    scopes = set(me.get("scopes", []))
    missing = READ_SCOPES - scopes
    if missing:
        raise SystemExit(f"当前API Key缺少权限：{', '.join(sorted(missing))}")

    # The batch queue already has a summary row. Reusing it avoids scanning all
    # PENDING claims for every single review. Direct claim IDs likewise go
    # straight to the detail endpoint instead of walking the entire queue.
    if summary is not None:
        claim = client.get_claim(summary["id"])
    elif claim_no and claim_no.startswith("BX-"):
        claim = client.get_claim(claim_no)
    else:
        summary = find_claim(client, claim_no)
        claim = client.get_claim(summary["id"])
    approvals = client.approvals_for_claim(claim["id"])

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS / f"{stamp}-{claim['claimNo']}"
    attachments_dir = run_dir / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    extracted_by_line: dict[str, InvoiceExtraction | None] = {}
    attachment_log = []
    for line in claim.get("lines", []):
        attachment = line.get("attachment")
        if not attachment:
            extracted_by_line[line["id"]] = None
            continue
        attachment_id = attachment["id"]
        try:
            meta = client.attachment_meta(attachment_id)
            content = client.attachment_content(attachment_id)
            image_path = attachments_dir / f"{line['lineNo']}-{attachment_id}{ext_for_attachment(meta)}"
            image_path.write_bytes(content)
            ocr = extract_ticket(image_path)
            extraction = ocr.extraction
            # A confirmed OCR correction is local, attributable evidence.  It
            # changes this claim's complete deterministic re-review only; it
            # never mutates ERP data or publishes a global rule/model.
            for field, value in (ocr_overrides or {}).get(line["id"], {}).items():
                if hasattr(extraction, field):
                    setattr(extraction, field, value)
            extracted_by_line[line["id"]] = extraction
            attachment_log.append(
                {
                    "lineId": line["id"],
                    "attachmentId": attachment_id,
                    "image": str(image_path),
                    "extraction": extraction.to_dict(),
                    "ocrEngine": ocr.engine,
                    "requiresHumanReview": ocr.requires_human_review,
                    "ocrNote": ocr.note,
                    "manualOverrides": (ocr_overrides or {}).get(line["id"], {}),
                }
            )
        except Exception as exc:
            # Preserve a manual field correction even if the OCR engine cannot
            # parse the attachment.  The reviewer can then re-run the complete
            # claim using the corrected local evidence plus the ERP ledger.
            fallback = InvoiceExtraction(raw_text=[])
            for field, value in (ocr_overrides or {}).get(line["id"], {}).items():
                if hasattr(fallback, field):
                    setattr(fallback, field, value)
            extracted_by_line[line["id"]] = fallback
            if isinstance(exc, QihengApiError):
                error = {"status": exc.status, "code": exc.code, "message": exc.message}
            else:
                error = {"status": None, "code": "local_ocr_error", "message": str(exc)}
            attachment_log.append(
                {
                    "lineId": line["id"],
                    "attachmentId": attachment_id,
                    "error": error,
                }
            )

    cities = client.cities().get("data", [])
    standards = client.travel_standards().get("data", [])
    duplicate_matches: dict[str, list[dict[str, Any]]] = {}
    for line in claim.get("lines", []):
        invoice = line.get("invoice") or {}
        invoice_no, invoice_code = invoice.get("invoiceNo"), invoice.get("invoiceCode")
        if not invoice_no or not invoice_code:
            continue
        candidates = client.invoices_by_number(invoice_no).get("data", [])
        exact = [item for item in candidates if item.get("invoiceCode") == invoice_code and item.get("id") != invoice.get("id")]
        if exact:
            duplicate_matches[line["id"]] = exact
    result = review_claim(
        claim,
        extracted_by_line,
        approvals=approvals.get("data", []),
        cities=cities,
        standards=standards,
        duplicate_matches=duplicate_matches,
        manual_overrides=ocr_overrides,
    )
    result["evidenceChain"] = build_evidence_chain(
        claim, result, approvals.get("data", []), attachment_log
    )
    dump_json(run_dir / "claim.json", claim)
    dump_json(run_dir / "approvals.json", approvals)
    dump_json(run_dir / "extractions.json", attachment_log)
    dump_json(run_dir / "review.json", result)
    return {
        "claim": claim,
        "approvals": approvals,
        "result": result,
        "attachments": attachment_log,
        "run_dir": run_dir,
    }


def run_eval(args: argparse.Namespace) -> None:
    report = evaluate(args.dataset)
    output = Path(args.output)
    dump_json(output, report)
    print(json.dumps({k: v for k, v in report.items() if k != "details"}, ensure_ascii=False, indent=2))
    print(f"完整评测报告：{output}")


def collect_eval(args: argparse.Namespace) -> None:
    client = client_from_env()
    labels_path = Path(args.labels)
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    requested = list(labels.get("claims", {}).values())[: args.limit]
    pending = {claim["claimNo"]: claim for claim in client.iter_expense_claims("PENDING")}

    output = Path(args.output)
    images_dir = output.parent / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for item in requested:
        claim_no = item["claimNo"]
        summary = pending.get(claim_no)
        if not summary:
            skipped.append({"claimNo": claim_no, "reason": "不在PENDING池"})
            continue
        claim = client.get_claim(summary["id"])
        for line in claim.get("lines", []):
            attachment = line.get("attachment")
            if not attachment:
                skipped.append(
                    {"claimNo": claim_no, "lineNo": line["lineNo"], "reason": "无附件"}
                )
                continue
            try:
                meta = client.attachment_meta(attachment["id"])
                content = client.attachment_content(attachment["id"])
            except QihengApiError as exc:
                skipped.append(
                    {
                        "claimNo": claim_no,
                        "lineNo": line["lineNo"],
                        "reason": f"{exc.code}: {exc.message}",
                    }
                )
                continue
            image_path = images_dir / (
                f"{claim_no}-line{line['lineNo']}-{attachment['id']}{ext_for_attachment(meta)}"
            )
            image_path.write_bytes(content)
            cases.append(
                {
                    "claimNo": claim_no,
                    "lineNo": line["lineNo"],
                    "attachmentId": attachment["id"],
                    "image": str(image_path.relative_to(output.parent)).replace("\\", "/"),
                    "expected": {},
                    "labelHint": {
                        "claimVerdict": item.get("expectedVerdict"),
                        "claimViolations": item.get("violations", []),
                    },
                }
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    dump_json(output.with_suffix(".skipped.json"), skipped)
    print(f"已收集 {len(cases)} 张票据：{images_dir}")
    print(f"待人工填写票面标准答案：{output}")
    print("注意：labelHint是单据判定提示，不是票据字段标准答案。")


def main() -> None:
    parser = argparse.ArgumentParser(description="启衡精密 D2 单张报销最小闭环")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("one", help="跑一张待审报销单，默认只读Dry Run")
    one.add_argument("--claim-no", help="报销单号或内部ID；不填则取第一张PENDING")
    one.add_argument("--writeback", action="store_true", help="保留参数；此只读 MVP 已禁用回写")
    one.add_argument("--confirm", default="", help="保留兼容参数，不会触发回写")
    one.set_defaults(func=run_one)

    ev = sub.add_parser("eval", help="运行票据要素抽取评测")
    ev.add_argument("--dataset", default="evals/extraction_cases.jsonl")
    ev.add_argument("--output", default="evals/eval-report.json")
    ev.set_defaults(func=run_eval)

    collect = sub.add_parser("collect-eval", help="下载公开样例票据，生成待人工标注的评测清单")
    collect.add_argument(
        "--labels",
        required=True,
        help="学员包public-sample-labels.json路径",
    )
    collect.add_argument("--limit", type=int, default=30, help="最多收集多少张公开样例单")
    collect.add_argument(
        "--output",
        default="evals/extraction_cases.todo.jsonl",
        help="待标注JSONL输出路径",
    )
    collect.set_defaults(func=collect_eval)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        raise SystemExit("\n已取消")
    except QihengApiError as exc:
        if exc.status == 403 and isinstance(exc.details, dict):
            print(f"缺少权限：{exc.details.get('required')}", file=sys.stderr)
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
