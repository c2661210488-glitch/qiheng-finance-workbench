from __future__ import annotations

import unittest

from src.invoice_ocr import InvoiceExtraction
from src.reviewer import COMPANY_NAME, COMPANY_TAX_NO, review_claim


def make_claim(expense_type: str, ledger_buyer: str | None, ledger_tax_no: str | None) -> dict:
    return {
        "id": "CLAIM-TEST",
        "claimNo": "BX-TEST",
        "lines": [
            {
                "id": "LINE-1",
                "lineNo": 1,
                "expenseType": expense_type,
                "amountFen": 10_000,
                "attachment": {"id": "ATT-1"},
                "invoice": {
                    "buyer": {"name": ledger_buyer, "taxNo": ledger_tax_no},
                    "totalFen": 10_000,
                },
            }
        ],
    }


class SourceConflictReviewTests(unittest.TestCase):
    def test_long_transport_ledger_mismatch_requires_second_review(self) -> None:
        outcome = review_claim(
            make_claim("LONG_TRANSPORT", "错误公司", "WRONG-TAX"),
            {"LINE-1": InvoiceExtraction(raw_text=[])},
        )
        self.assertEqual("FLAG", outcome["result"])
        self.assertTrue(any("台账" in text and "二次人工复审" in text for text in outcome["flags"]))

    def test_long_transport_missing_ledger_never_auto_approves(self) -> None:
        outcome = review_claim(
            make_claim("LONG_TRANSPORT", None, None),
            {"LINE-1": InvoiceExtraction(raw_text=[])},
        )
        self.assertEqual("FLAG", outcome["result"])

    def test_higher_priority_invoice_and_ledger_conflict_requires_review(self) -> None:
        outcome = review_claim(
            make_claim("HOTEL", "错误公司", "WRONG-TAX"),
            {
                "LINE-1": InvoiceExtraction(
                    buyer_name=COMPANY_NAME,
                    buyer_tax_no=COMPANY_TAX_NO,
                    total_fen=10_000,
                    raw_text=["票面正确"],
                )
            },
        )
        self.assertEqual("FLAG", outcome["result"])
        self.assertTrue(any("台账" in text and "不一致" in text for text in outcome["flags"]))

    def test_manual_wrong_value_rejects_after_complete_rereview(self) -> None:
        outcome = review_claim(
            make_claim("LONG_TRANSPORT", COMPANY_NAME, COMPANY_TAX_NO),
            {"LINE-1": InvoiceExtraction(raw_text=[])},
            manual_overrides={"LINE-1": {"buyer_name": "错误公司"}},
        )
        self.assertEqual("REJECT", outcome["result"])
        self.assertIn("INVOICE_TITLE_MISMATCH", [item["code"] for item in outcome["violations"]])

    def test_ordinary_invoice_ledger_short_name_with_exact_tax_is_safe_alias(self) -> None:
        outcome = review_claim(
            make_claim("OFFICE", "启衡精密", COMPANY_TAX_NO),
            {"LINE-1": InvoiceExtraction(buyer_name=COMPANY_NAME, buyer_tax_no=COMPANY_TAX_NO, total_fen=10_000, raw_text=["票面可读"])},
        )
        self.assertEqual("APPROVE", outcome["result"])

    def test_city_transport_short_name_is_not_a_safe_alias(self) -> None:
        outcome = review_claim(
            make_claim("CITY_TRANSPORT", "启衡精密", COMPANY_TAX_NO),
            {"LINE-1": InvoiceExtraction(buyer_name="启衡精密", buyer_tax_no=COMPANY_TAX_NO, total_fen=10_000)},
        )
        self.assertEqual("REJECT", outcome["result"])


if __name__ == "__main__":
    unittest.main()
