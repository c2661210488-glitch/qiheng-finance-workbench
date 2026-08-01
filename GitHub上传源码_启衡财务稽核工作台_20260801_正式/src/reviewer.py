from __future__ import annotations

"""Deterministic, read-only reimbursement rules for the D2 workbench."""

import re
from dataclasses import asdict, dataclass
from typing import Any

from .invoice_ocr import InvoiceExtraction

COMPANY_NAME = "启衡精密制造有限公司"
COMPANY_TAX_NO = "91320594MA1TXXXX7Q"
# Real-name rail/air tickets normally have no corporate buyer field.  Taxi and
# ride-hailing VAT invoices are different: when the ERP invoice ledger contains
# an explicit buyer, that buyer is admissible evidence and must be checked.
# Keeping CITY_TRANSPORT here caused BX202606-5773 (ledger buyer “启衡精密”) to
# bypass the title rule even though the official public label requires REJECT.
NO_BUYER_FIELDS = {"LONG_TRANSPORT"}


def _recovered_amount(extraction: InvoiceExtraction, claimed_fen: int | None) -> int | None:
    """Accept a fallback only when the exact claimed money string appears in OCR text.

    This helps receipts where the amount label is split across OCR lines, while
    still refusing a truncated number such as ``641.00`` for a ¥1,641 ticket.
    """
    if extraction.total_fen is not None or claimed_fen is None or not extraction.raw_text:
        return extraction.total_fen
    expected = f"{claimed_fen // 100}.{claimed_fen % 100:02d}"
    compact = "".join(extraction.raw_text).replace(",", "")
    return claimed_fen if expected in compact else None


def _buyer_is_company(extraction: InvoiceExtraction) -> bool:
    if extraction.buyer_name == COMPANY_NAME:
        return True
    if not extraction.raw_text:
        return False
    # OCR often splits “购方名称” across lines.  Allow the full legal name only
    # when it immediately follows the buyer-name label.  A substring match would
    # incorrectly accept “启衡精密” and “苏州启衡精密制造有限公司”.
    raw = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", "".join(extraction.raw_text))
    index = raw.find(COMPANY_NAME)
    if index < 0:
        return False
    before = raw[max(0, index - 10):index]
    after = raw[index + len(COMPANY_NAME):index + len(COMPANY_NAME) + 12]
    return before.endswith(("购名称", "名称", "购名", "名")) and after.startswith(("买", "纳税", "方"))


def _ledger_is_company_buyer(ledger_buyer: str | None, ledger_tax_no: str | None, expense_type: str) -> bool:
    """Whether an ERP ledger buyer can corroborate the company identity.

    ``启衡精密`` is a historical short-name value in the ERP ledger.  It is
    accepted only for ordinary VAT invoices when the tax number is exact; it is
    not accepted for taxi/ride-hailing receipts, where the public acceptance
    labels require the full legal buyer name.  This deliberately never changes
    the stricter original-ticket/OCR title validation.
    """
    if ledger_tax_no != COMPANY_TAX_NO:
        return False
    if ledger_buyer == COMPANY_NAME:
        return True
    return expense_type != "CITY_TRANSPORT" and ledger_buyer == "启衡精密"


def _resolve_ticket_amount(line: dict[str, Any], extraction: InvoiceExtraction) -> tuple[int | None, str]:
    """Resolve a ticket total without mistaking VAT-exclusive amount for total.

    The ticket image remains the primary evidence.  The API invoice ledger is
    used only to disambiguate a common OCR failure: reading the amount-before-
    tax field while missing the printed tax-inclusive total.  A mismatch is
    never rejected from that ambiguous OCR result alone.
    """
    claimed = line.get("amountFen")
    extracted = _recovered_amount(extraction, claimed)
    invoice = line.get("invoice") or {}
    invoice_total = invoice.get("totalFen")
    invoice_net = invoice.get("amountExclTaxFen")
    if extracted is not None and extracted == invoice_net and invoice_total == claimed:
        return invoice_total, "invoice_ledger_total_after_ocr_net_amount"
    return extracted, "ocr_ticket_total"


def _buyer_is_verified(line: dict[str, Any], extraction: InvoiceExtraction) -> bool:
    """Verify the ticket face; ledger fields may corroborate but never override it."""
    return _buyer_is_company(extraction)


@dataclass
class Violation:
    code: str
    reason: str
    evidence: dict[str, Any]


def _has_special_approval(approvals: list[dict[str, Any]]) -> bool:
    return any(item.get("action") == "SPECIAL_APPROVE" for item in approvals)


def _special_approval_for(approvals: list[dict[str, Any]], *terms: str) -> dict[str, Any] | None:
    """Return a special approval whose note explicitly covers this travel exception."""
    for item in approvals:
        if item.get("action") != "SPECIAL_APPROVE":
            continue
        note = str(item.get("comment") or "")
        if "据实报销" in note or any(term in note for term in terms):
            return item
    return None


TRANSPORT_RANK = {
    "TRAIN_2ND": 1,
    "TRAIN_1ST": 2,
    "TRAIN_BIZ": 3,
    "FLIGHT_ECON": 4,
    "FLIGHT_BIZ": 5,
}


def _transport_class(extraction: InvoiceExtraction) -> str | None:
    text = "".join(extraction.raw_text or [])
    for token, value in (("商务座", "TRAIN_BIZ"), ("一等座", "TRAIN_1ST"), ("二等座", "TRAIN_2ND"), ("公务舱", "FLIGHT_BIZ"), ("经济舱", "FLIGHT_ECON")):
        if token in text:
            return value
    return None


def _standard(claim: dict[str, Any], cities: list[dict[str, Any]], standards: list[dict[str, Any]]) -> dict[str, Any] | None:
    trip = claim.get("trip") or {}
    city = next((item for item in cities if item.get("name") == trip.get("city")), None)
    if not city:
        return None
    return next(
        (item for item in standards if item.get("jobLevel") == claim.get("jobLevel") and item.get("cityTier") == city.get("tier")),
        None,
    )


def _add_cap_check(
    violations: list[Violation], flags: list[str], evidence: list[dict[str, Any]], *, code: str,
    label: str, amount: int | None, cap: int | None, line_ids: list[str], special: bool,
) -> None:
    if amount is None:
        flags.append(f"{label}票面金额无法可靠取得，需要人工复核")
        return
    if cap is None:
        flags.append(f"{label}缺少可匹配的职级或城市标准，需要人工复核")
        return
    evidence.append({"type": "policy_cap", "rule": code, "actualFen": amount, "capFen": cap, "lineIds": line_ids})
    if amount > cap and not special:
        violations.append(Violation(code, f"{label}超出制度标准", {"actualFen": amount, "capFen": cap, "lineIds": line_ids}))
    elif amount > cap:
        evidence.append({"type": "approval", "rule": code, "note": "特批备注明确覆盖本次超标，规则通过"})


def review_claim(
    claim: dict[str, Any],
    extracted_by_line: dict[str, InvoiceExtraction | None],
    *,
    approvals: list[dict[str, Any]] | None = None,
    cities: list[dict[str, Any]] | None = None,
    standards: list[dict[str, Any]] | None = None,
    duplicate_matches: dict[str, list[dict[str, Any]]] | None = None,
    manual_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a human-review recommendation; never writes to the ERP."""
    approvals, cities, standards, duplicate_matches, manual_overrides = approvals or [], cities or [], standards or [], duplicate_matches or {}, manual_overrides or {}
    violations: list[Violation] = []
    flags: list[str] = []
    evidence: list[dict[str, Any]] = []
    amounts: dict[str, int | None] = {}
    line_ids: dict[str, list[str]] = {}

    for line in claim.get("lines", []):
        line_id = line["id"]
        line_no = line.get("lineNo", "?")
        expense_type = line.get("expenseType", "OTHER")
        attachment = line.get("attachment")
        extraction = extracted_by_line.get(line_id)
        line_ids.setdefault(expense_type, []).append(line_id)

        if not attachment:
            violations.append(Violation("MISSING_ATTACHMENT", f"费用行{line_no}未上传票据附件", {"lineId": line_id, "lineNo": line_no}))
            amounts[line_id] = None
            continue
        if extraction is None:
            # Keep evaluating ERP-side evidence even when OCR cannot open a
            # ticket.  In particular, a long-transport ticket must still be
            # checked against its linked invoice ledger instead of being
            # silently excluded from the whole decision.
            extraction = InvoiceExtraction(raw_text=[])
        if not extraction.raw_text:
            flags.append(f"费用行{line_no}附件无法读取，需要人工检查")

        actual_amount, amount_source = _resolve_ticket_amount(line, extraction)
        amounts[line_id] = actual_amount
        evidence.append({
            "type": "attachment_ocr", "lineId": line_id, "lineNo": line_no,
            "attachmentId": attachment["id"], "expenseType": expense_type,
            "invoiceCode": extraction.invoice_code, "invoiceNo": extraction.invoice_no,
            "buyerName": extraction.buyer_name, "buyerTaxNo": extraction.buyer_tax_no,
            "totalFen": actual_amount, "amountSource": amount_source,
            "ocrConfidence": round(extraction.mean_confidence, 4),
        })
        if actual_amount is None:
            flags.append(f"费用行{line_no}未能可靠提取票面金额")
        elif actual_amount != line.get("amountFen"):
            violations.append(Violation("AMOUNT_MISMATCH", f"费用行{line_no}报销金额与票面金额不一致", {"lineId": line_id, "claimedFen": line.get("amountFen"), "invoiceTotalFen": actual_amount}))

        # The invoice ledger is an *independent corroborating source*, not a
        # substitute for the original ticket.  In particular, taxi/train tickets
        # often have no buyer field on their face.  An OCR disagreement with a
        # ledger record that says the buyer is our company is data conflict /
        # human review, never a title-error rejection.  This prevents an OCR
        # miss from being misreported as an employee violation.
        ledger_buyer = ((line.get("invoice") or {}).get("buyer") or {}).get("name")
        ledger_tax_no = ((line.get("invoice") or {}).get("buyer") or {}).get("taxNo")
        confirmed_fields = manual_overrides.get(line_id, {})
        confirmed_buyer = confirmed_fields.get("buyer_name")
        confirmed_tax_no = confirmed_fields.get("buyer_tax_no")
        # Electronic rail/air tickets are not VAT invoices: their image normally
        # has no buyer name or tax number.  For those tickets the linked ERP
        # invoice ledger is the mandatory comparison source.  A missing or
        # conflicting ledger record must never fall through to APPROVE merely
        # because OCR cannot read buyer fields.
        if expense_type in NO_BUYER_FIELDS:
            if confirmed_buyer is not None and str(confirmed_buyer).strip() != COMPANY_NAME:
                violations.append(Violation(
                    "INVOICE_TITLE_MISMATCH",
                    f"费用行{line_no}人工确认的台账购买方与公司全称不符",
                    {"lineId": line_id, "actual": confirmed_buyer, "expected": COMPANY_NAME, "source": "human_confirmed_ledger"},
                ))
            if confirmed_tax_no is not None and str(confirmed_tax_no).strip() != COMPANY_TAX_NO:
                violations.append(Violation(
                    "INVOICE_TAXNO_MISMATCH",
                    f"费用行{line_no}人工确认的台账税号与公司税号不符",
                    {"lineId": line_id, "actual": confirmed_tax_no, "expected": COMPANY_TAX_NO, "source": "human_confirmed_ledger"},
                ))
            if not ledger_buyer or not ledger_tax_no:
                flags.append(
                    f"费用行{line_no}为电子客票/车票，原票不含购买方字段；关联 ERP 台账购买方或税号缺失，转二次人工复审"
                )
            elif ledger_buyer != COMPANY_NAME or ledger_tax_no != COMPANY_TAX_NO:
                flags.append(
                    f"费用行{line_no}为电子客票/车票，ERP 台账购买方/税号与公司主数据不一致"
                    f"（台账：{ledger_buyer} / {ledger_tax_no}；正确：{COMPANY_NAME} / {COMPANY_TAX_NO}），转二次人工复审"
                )
        # A finance user can confirm the original invoice face after opening the
        # image.  That attributable truth outranks ledger fields and OCR.  It
        # only affects this ticket/claim; it does not create a global alias.
        elif confirmed_buyer is not None:
            if str(confirmed_buyer).strip() != COMPANY_NAME:
                violations.append(Violation(
                    "INVOICE_TITLE_MISMATCH",
                    f"费用行{line_no}人工核对原票确认发票抬头与公司全称不符",
                    {"lineId": line_id, "actual": confirmed_buyer, "expected": COMPANY_NAME, "source": "human_confirmed_ticket_face"},
                ))
        elif _ledger_is_company_buyer(ledger_buyer, ledger_tax_no, expense_type):
            if extraction.buyer_name and not _buyer_is_company(extraction):
                flags.append(
                    f"费用行{line_no}原票 OCR 购买方与 ERP 发票台账冲突；"
                    "台账购买方和税号正确，需打开原票人工复核，不能判定抬头错误"
                )
            # The user-confirmed review hierarchy treats a matching invoice
            # ledger buyer + tax number as sufficient corroboration when OCR
            # simply cannot read the buyer.  OCR absence is not an exception by
            # itself and must not downgrade an otherwise clean claim to FLAG.
            # A positive OCR conflict above still requires human review.
        elif ledger_buyer and ledger_buyer != COMPANY_NAME:
            # A non-company ledger buyer can corroborate a clearly non-company
            # ticket face.  If OCR says otherwise or cannot read it, preserve the
            # evidence conflict as FLAG rather than letting hand-entered fields
            # overrule the original image.
            if _buyer_is_company(extraction):
                # The source document is correct but ERP ledger data differs.
                # This is not an employee-side rejection, but it is still an
                # auditable source conflict and must never be auto-approved.
                flags.append(
                    f"费用行{line_no}原票购买方与 ERP 台账不一致"
                    f"（原票：{COMPANY_NAME}；台账：{ledger_buyer}），转二次人工复审"
                )
            elif extraction.buyer_name:
                violations.append(Violation("INVOICE_TITLE_MISMATCH", f"费用行{line_no}发票抬头与公司名称不符", {"lineId": line_id, "actual": extraction.buyer_name, "expected": COMPANY_NAME, "source": "ticket_ocr_and_invoice_ledger", "ledgerActual": ledger_buyer}))
            else:
                violations.append(Violation("INVOICE_TITLE_MISMATCH", f"费用行{line_no}发票台账购买方不是公司全称", {"lineId": line_id, "actual": ledger_buyer, "expected": COMPANY_NAME, "source": "invoice_ledger; ticket_buyer_unreadable"}))
        # Real-name transport tickets and taxi receipts normally do not have buyer fields.
        elif expense_type not in NO_BUYER_FIELDS:
            if not extraction.buyer_name and not _buyer_is_verified(line, extraction):
                flags.append(f"费用行{line_no}未能可靠提取购买方名称")
            elif not _buyer_is_verified(line, extraction):
                violations.append(Violation("INVOICE_TITLE_MISMATCH", f"费用行{line_no}发票抬头与公司名称不符", {"lineId": line_id, "actual": extraction.buyer_name, "expected": COMPANY_NAME}))

        # Tax number is independent of the title branch.  A ledger field that
        # looks correct must not hide a different tax number read from the
        # original ticket image.
        if expense_type not in NO_BUYER_FIELDS and confirmed_tax_no is not None:
            if str(confirmed_tax_no).strip() != COMPANY_TAX_NO:
                violations.append(Violation(
                    "INVOICE_TAXNO_MISMATCH",
                    f"费用行{line_no}人工核对原票确认购方税号与公司税号不符",
                    {"lineId": line_id, "actual": confirmed_tax_no, "expected": COMPANY_TAX_NO, "source": "human_confirmed_ticket_face"},
                ))
        elif expense_type not in NO_BUYER_FIELDS:
            if not extraction.buyer_tax_no and not _ledger_is_company_buyer(ledger_buyer, ledger_tax_no, expense_type):
                flags.append(f"费用行{line_no}未能可靠提取购买方税号")
            elif extraction.buyer_tax_no and extraction.buyer_tax_no != COMPANY_TAX_NO:
                violations.append(Violation("INVOICE_TAXNO_MISMATCH", f"费用行{line_no}购方税号与公司税号不符", {"lineId": line_id, "actual": extraction.buyer_tax_no, "expected": COMPANY_TAX_NO, "source": "ticket_ocr"}))

        # Reconcile the independent ERP ledger even when a higher-priority
        # original invoice (or human-confirmed original value) is correct.  A
        # source disagreement is a finance-data issue requiring a second review,
        # not a silent APPROVE and not automatic overwrite of ERP data.
        if expense_type not in NO_BUYER_FIELDS and (ledger_buyer or ledger_tax_no):
            ledger_conflict = not _ledger_is_company_buyer(ledger_buyer, ledger_tax_no, expense_type)
            higher_source_confirms_company = (
                _buyer_is_company(extraction)
                or extraction.buyer_tax_no == COMPANY_TAX_NO
                or confirmed_buyer == COMPANY_NAME
                or confirmed_tax_no == COMPANY_TAX_NO
            )
            if ledger_conflict and higher_source_confirms_company:
                flags.append(
                    f"费用行{line_no}原票/人工确认字段与 ERP 台账不一致"
                    f"（台账：{ledger_buyer or '缺失'} / {ledger_tax_no or '缺失'}；"
                    f"正确：{COMPANY_NAME} / {COMPANY_TAX_NO}），转二次人工复审"
                )

        if extraction.mean_confidence and extraction.mean_confidence < 0.72:
            flags.append(f"费用行{line_no}OCR置信度偏低，需要人工复核")

        duplicates = duplicate_matches.get(line_id, [])
        if duplicates:
            violations.append(Violation(
                "DUPLICATE_INVOICE",
                f"费用行{line_no}的发票代码/号码已在历史发票台账中出现",
                {"lineId": line_id, "invoiceCode": (line.get("invoice") or {}).get("invoiceCode"), "invoiceNo": (line.get("invoice") or {}).get("invoiceNo"), "matchedInvoiceIds": [item.get("id") for item in duplicates]},
            ))

        description = str(line.get("description", ""))
        if expense_type == "CITY_TRANSPORT" and "加班" in description:
            has_preapproval = any(item.get("action") in {"APPROVE", "SPECIAL_APPROVE"} for item in approvals)
            if not has_preapproval:
                violations.append(Violation("MISSING_APPROVAL_OVERTIME_TAXI", f"费用行{line_no}标注为加班用车但未找到事前审批", {"lineId": line_id, "approvals": approvals}))

    trip = claim.get("trip") or {}
    standard = _standard(claim, cities, standards)
    nights = trip.get("nights")
    if claim.get("claimType") == "TRAVEL" and any(key in line_ids for key in ("HOTEL", "MEAL", "CITY_TRANSPORT")):
        if not isinstance(nights, int) or nights <= 0:
            flags.append("差旅行程的住宿晚数缺失，无法完整核验差旅标准")
        elif standard:
            hotel = [amounts.get(item) for item in line_ids.get("HOTEL", [])]
            if hotel:
                total = sum(item for item in hotel if item is not None) if all(item is not None for item in hotel) else None
                per_night = total // nights if total is not None else None
                _add_cap_check(violations, flags, evidence, code="OVER_STANDARD_HOTEL", label="住宿费", amount=per_night, cap=standard.get("hotelCapPerNightFen"), line_ids=line_ids["HOTEL"], special=bool(_special_approval_for(approvals, "住宿", "酒店", "差旅")))
            meal = [amounts.get(item) for item in line_ids.get("MEAL", [])]
            if meal:
                total = sum(item for item in meal if item is not None) if all(item is not None for item in meal) else None
                _add_cap_check(violations, flags, evidence, code="OVER_STANDARD_MEAL", label="伙食补助", amount=total, cap=standard.get("mealAllowancePerDayFen", 0) * (nights + 1), line_ids=line_ids["MEAL"], special=bool(_special_approval_for(approvals, "伙食", "餐", "差旅")))
            city = [amounts.get(item) for item in line_ids.get("CITY_TRANSPORT", [])]
            if city:
                total = sum(item for item in city if item is not None) if all(item is not None for item in city) else None
                _add_cap_check(violations, flags, evidence, code="OVER_STANDARD_CITY_TRANSPORT", label="市内交通", amount=total, cap=standard.get("cityTransportPerDayFen", 0) * (nights + 1), line_ids=line_ids["CITY_TRANSPORT"], special=bool(_special_approval_for(approvals, "交通", "用车", "差旅")))
        else:
            flags.append("未找到匹配的城市档次或差旅标准，需要人工复核")

    if claim.get("claimType") == "TRAVEL" and standard:
        for line_id in line_ids.get("LONG_TRANSPORT", []):
            extraction = extracted_by_line.get(line_id)
            if extraction is None:
                continue
            actual_class = _transport_class(extraction)
            allowed_class = standard.get("longDistanceClass")
            if not actual_class:
                flags.append(f"费用行{next((line.get('lineNo') for line in claim['lines'] if line['id'] == line_id), '—')}未能可靠识别长途交通舱位，需要人工复核")
                continue
            evidence.append({"type": "transport_class", "rule": "OVER_STANDARD_TRANSPORT_CLASS", "lineId": line_id, "actualClass": actual_class, "allowedClass": allowed_class})
            if TRANSPORT_RANK.get(actual_class, 999) > TRANSPORT_RANK.get(allowed_class, -1) and not _special_approval_for(approvals, "舱", "交通", "差旅"):
                violations.append(Violation("OVER_STANDARD_TRANSPORT_CLASS", "长途交通舱位高于员工职级标准", {"lineId": line_id, "actualClass": actual_class, "allowedClass": allowed_class}))

    result = "REJECT" if violations else "FLAG" if flags else "APPROVE"
    reasons = [item.reason for item in violations] + flags
    if not reasons:
        reasons = ["票据读取成功，已通过当前版本的固定规则检查"]
    confidence_parts = [item.mean_confidence for item in extracted_by_line.values() if item is not None and item.mean_confidence]
    confidence = sum(confidence_parts) / len(confidence_parts) if confidence_parts else 0.5
    return {
        "claimId": claim["id"], "claimNo": claim["claimNo"], "result": result,
        "violations": [asdict(item) for item in violations], "flags": flags, "reasons": reasons,
        "evidence": evidence, "confidence": round(confidence, 4),
        "requiresHumanReview": True,
        "scopeNote": "只读 MVP：固定规则用于初步建议；不调用审核回写接口，不改变单据状态。",
    }


def to_review_payload(review: dict[str, Any]) -> dict[str, Any]:
    return {"result": review["result"], "reasons": review["reasons"], "evidence": review["evidence"], "confidence": review["confidence"]}
