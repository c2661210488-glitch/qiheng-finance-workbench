"""Conservative, explainable M3 invoice-ledger checks (GET data only)."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


COMPANY_NAME = "启衡精密制造有限公司"
COMPANY_TAX_NO = "91320594MA1TXXXX7Q"
RATE_BY_INVOICE_KIND = {"HOTEL": 0.06, "TRAIN": 0.09, "FLIGHT": 0.09, "TAXI": 0.03}
RATE_BY_VENDOR_CATEGORY = {"MATERIAL": 0.13, "OUTSOURCE": 0.13, "SERVICE": 0.06, "OFFICE": 0.13}


def _clean(value: Any) -> str:
    return str(value or "").replace(" ", "").replace("　", "").strip().upper()


def scan_ledger(invoices: list[dict[str, Any]], vendors: list[dict[str, Any]]) -> dict[str, Any]:
    """Return only high-confidence M3 findings and their reproducible evidence."""
    vendor_category = {
        _clean(v.get("taxNo")): v.get("category")
        for v in vendors
        if _clean(v.get("taxNo")) and v.get("active", True)
    }
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for invoice in invoices:
        code, number = _clean(invoice.get("invoiceCode")), _clean(invoice.get("invoiceNo"))
        if code and number:
            groups[(code, number)].append(invoice)

    duplicates = []
    for (code, number), records in groups.items():
        # Same code+number twice is the policy definition of duplicate. Keep
        # ledger record IDs as evidence; claim IDs are not present on this API.
        if len(records) > 1:
            duplicates.append({
                "invoiceCode": code,
                "invoiceNo": number,
                "invoiceIds": sorted(item["id"] for item in records),
                "claimIds": [],
                "evidence": {"count": len(records), "rule": "发票合规指引 第三部分第1项"},
            })

    issues = []
    details = []
    skipped_rate = Counter()
    for invoice in invoices:
        # Sales invoices must have external customers as buyers; assessing their
        # buyer against our reimbursement company would be a systematic trap.
        if invoice.get("type") == "SALES_OUTPUT":
            continue
        buyer = invoice.get("buyer") or {}
        invoice_issues = []
        if _clean(buyer.get("name")) != _clean(COMPANY_NAME):
            invoice_issues.append(("TITLE_WRONG", {"actual": buyer.get("name"), "expected": COMPANY_NAME}))
        if _clean(buyer.get("taxNo")) != _clean(COMPANY_TAX_NO):
            invoice_issues.append(("TAXNO_WRONG", {"actual": buyer.get("taxNo"), "expected": COMPANY_TAX_NO}))

        kind = str(invoice.get("invoiceKind") or "")
        seller = invoice.get("seller") or {}
        category = vendor_category.get(_clean(seller.get("taxNo")))
        # A tax-rate conclusion without a known invoice kind or supplier
        # category would be speculation.  Surface the missing master data as
        # a separate task instead of silently treating the invoice as normal.
        # Expense-source receipts are not supplier-master records.  They must
        # never be mass-labelled as vendor master-data defects.  For purchase
        # input invoices, retain a local task for finance, but do not emit it
        # into the official invoiceIssues list (which accepts only the three
        # precisely-scored invoice-field codes).
        if invoice.get("type") == "PURCHASE_INPUT" and (not _clean(seller.get("taxNo")) or not category):
            details.append({
                "invoiceId": invoice["id"], "invoiceCode": invoice.get("invoiceCode"), "invoiceNo": invoice.get("invoiceNo"),
                "type": invoice.get("type"), "invoiceKind": kind, "buyer": buyer, "seller": seller,
                "vendorCategory": category, "taxRate": invoice.get("taxRate"), "issue": "MASTER_DATA_INCOMPLETE",
                "evidence": {
                    "actual": "供应商税号缺失" if not _clean(seller.get("taxNo")) else "供应商类别未维护",
                    "expected": "维护供应商税号和有效类别后重新稽核",
                    "basis": "供应商主数据完整性要求",
                },
            })
        if False and (not _clean(seller.get("taxNo")) or not category):
            invoice_issues.append(("MASTER_DATA_INCOMPLETE", {
                "actual": "供应商税号缺失" if not _clean(seller.get("taxNo")) else "供应商类别未维护",
                "expected": "维护供应商税号及有效类别后重新扫描",
                "basis": "供应商主数据完整性要求",
            }))
        expected_rate = RATE_BY_INVOICE_KIND.get(kind)
        rate_basis = f"invoiceKind:{kind}" if expected_rate is not None else None
        if expected_rate is None:
            expected_rate = RATE_BY_VENDOR_CATEGORY.get(str(category or ""))
            rate_basis = f"vendor.category:{category}" if expected_rate is not None else None
        if expected_rate is None:
            skipped_rate[kind or "UNKNOWN"] += 1
        elif abs(float(invoice.get("taxRate") or 0) - expected_rate) > 0.00001:
            invoice_issues.append(("TAX_RATE_WRONG", {"actual": invoice.get("taxRate"), "expected": expected_rate, "basis": rate_basis}))

        for issue, evidence in invoice_issues:
            issues.append({"invoiceId": invoice["id"], "issue": issue})
            details.append({
                "invoiceId": invoice["id"], "invoiceCode": invoice.get("invoiceCode"), "invoiceNo": invoice.get("invoiceNo"),
                "type": invoice.get("type"), "invoiceKind": kind, "buyer": buyer, "seller": seller,
                "vendorCategory": category, "taxRate": invoice.get("taxRate"), "issue": issue, "evidence": evidence,
            })

    # Duplicate groups are first-class audit tasks.  The old response exposed
    # them only in a side array, so the desktop task list accidentally hid the
    # most important M3 finding.
    for group in duplicates:
        sample = next((x for x in invoices if x.get("id") in group["invoiceIds"]), {})
        details.append({
            "invoiceId": group["invoiceIds"][0], "invoiceCode": group["invoiceCode"],
            "invoiceNo": group["invoiceNo"], "invoiceIds": group["invoiceIds"],
            "type": sample.get("type"), "invoiceKind": sample.get("invoiceKind"),
            "buyer": sample.get("buyer") or {}, "seller": sample.get("seller") or {},
            "issue": "DUPLICATE_INVOICE",
            "evidence": {
                "actual": f"同代码同号码出现 {group['evidence']['count']} 次",
                "expected": "同一发票仅应保留一条有效台账记录",
                "basis": group["evidence"]["rule"],
                "invoiceIds": group["invoiceIds"],
            },
        })

    return {
        "summary": {
            "totalInvoices": len(invoices),
            "typeCounts": dict(Counter(str(x.get("type")) for x in invoices)),
            "duplicateGroups": len(duplicates),
            "invoiceIssues": len(issues),
            "issueCounts": dict(Counter(x["issue"] for x in issues)),
            "taxRateNotEvaluated": dict(skipped_rate),
            "masterDataTasks": sum(1 for x in details if x.get("issue") == "MASTER_DATA_INCOMPLETE"),
            "scope": "All ledger rows fetched from GET /v1/invoices; buyer/tax/rate checks exclude SALES_OUTPUT by design.",
        },
        "duplicateInvoices": sorted(duplicates, key=lambda x: (x["invoiceCode"], x["invoiceNo"])),
        "invoiceIssues": sorted(issues, key=lambda x: (x["invoiceId"], x["issue"])),
        "details": sorted(details, key=lambda x: (x["invoiceId"], x["issue"])),
    }
