import unittest

from src.invoice_ocr import InvoiceExtraction
from src.reviewer import review_claim, to_review_payload


def claim_with_line(*, attachment=True, amount_fen=11500):
    return {
        "id": "BX-TEST",
        "claimNo": "BX202606-TEST",
        "lines": [
            {
                "id": "LINE-1",
                "lineNo": 1,
                "amountFen": amount_fen,
                "attachment": {"id": "ATT-1"} if attachment else None,
            }
        ],
    }


class ReviewerTests(unittest.TestCase):
    def test_approve_when_minimal_checks_pass(self):
        extraction = InvoiceExtraction(
            buyer_name="启衡精密制造有限公司",
            buyer_tax_no="91320594MA1TXXXX7Q",
            total_fen=11500,
            mean_confidence=0.96,
            raw_text=["发票"],
        )
        result = review_claim(claim_with_line(), {"LINE-1": extraction})
        self.assertEqual(result["result"], "APPROVE")
        self.assertEqual(result["violations"], [])

    def test_rejects_amount_and_title_mismatch(self):
        extraction = InvoiceExtraction(
            buyer_name="启衡精密",
            buyer_tax_no="91320594MA1TXXXX7Q",
            total_fen=9900,
            mean_confidence=0.95,
            raw_text=["发票"],
        )
        result = review_claim(claim_with_line(), {"LINE-1": extraction})
        codes = {item["code"] for item in result["violations"]}
        self.assertEqual(result["result"], "REJECT")
        self.assertEqual(codes, {"AMOUNT_MISMATCH", "INVOICE_TITLE_MISMATCH"})

    def test_flags_when_tax_number_cannot_be_extracted(self):
        extraction = InvoiceExtraction(
            buyer_name="启衡精密制造有限公司",
            total_fen=11500,
            mean_confidence=0.95,
            raw_text=["发票"],
        )
        result = review_claim(claim_with_line(), {"LINE-1": extraction})
        self.assertEqual(result["result"], "FLAG")

    def test_does_not_reject_when_ocr_reads_net_amount_but_ledger_total_matches(self):
        claim = claim_with_line(amount_fen=21600)
        claim["lines"][0]["invoice"] = {
            "totalFen": 21600,
            "amountExclTaxFen": 20377,
            "buyer": {"name": "启衡精密制造有限公司", "taxNo": "91320594MA1TXXXX7Q"},
        }
        extraction = InvoiceExtraction(
            buyer_name="启衡精密制造有限公司",
            buyer_tax_no="91320594MA1TXXXX7Q",
            total_fen=20377,
            mean_confidence=0.99,
            raw_text=["价税合计区域未可靠识别"],
        )
        result = review_claim(claim, {"LINE-1": extraction})
        self.assertEqual(result["result"], "APPROVE")
        self.assertEqual(result["violations"], [])

    def test_ledger_correct_buyer_prevents_ocr_title_false_reject(self):
        claim = claim_with_line()
        claim["lines"][0]["invoice"] = {
            "buyer": {"name": "启衡精密制造有限公司", "taxNo": "91320594MA1TXXXX7Q"},
        }
        extraction = InvoiceExtraction(
            buyer_name="启衡精密", buyer_tax_no="91320594MA1TXXXX7Q",
            total_fen=11500, mean_confidence=0.95, raw_text=["购买方名称：启衡精密"],
        )
        result = review_claim(claim, {"LINE-1": extraction})
        self.assertEqual(result["result"], "FLAG")
        self.assertNotIn("INVOICE_TITLE_MISMATCH", {item["code"] for item in result["violations"]})
        self.assertTrue(any("台账" in flag for flag in result["flags"]))

    def test_human_confirmed_wrong_ticket_title_overrides_correct_ledger(self):
        claim = claim_with_line()
        claim["lines"][0]["invoice"] = {
            "buyer": {"name": "启衡精密制造有限公司", "taxNo": "91320594MA1TXXXX7Q"},
        }
        extraction = InvoiceExtraction(
            buyer_name="启衡精密机械有限公司", buyer_tax_no="91320594MA1TXXXX7Q",
            total_fen=11500, mean_confidence=0.98, raw_text=["购买方：启衡精密机械有限公司"],
        )
        result = review_claim(
            claim,
            {"LINE-1": extraction},
            manual_overrides={"LINE-1": {"buyer_name": "启衡精密机械有限公司"}},
        )
        self.assertEqual(result["result"], "REJECT")
        violation = next(x for x in result["violations"] if x["code"] == "INVOICE_TITLE_MISMATCH")
        self.assertEqual(violation["evidence"]["source"], "human_confirmed_ticket_face")

    def test_ledger_correct_buyer_and_tax_allows_unreadable_ocr_buyer(self):
        claim = claim_with_line()
        claim["lines"][0]["invoice"] = {
            "buyer": {"name": "启衡精密制造有限公司", "taxNo": "91320594MA1TXXXX7Q"},
        }
        extraction = InvoiceExtraction(
            buyer_name=None, buyer_tax_no=None, total_fen=11500,
            mean_confidence=0.95, raw_text=["购买方区域未包含可提取名称"],
        )
        result = review_claim(claim, {"LINE-1": extraction})
        self.assertEqual(result["result"], "APPROVE")
        self.assertEqual(result["violations"], [])

    def test_city_transport_explicit_wrong_buyer_is_rejected(self):
        claim = claim_with_line()
        claim["lines"][0]["expenseType"] = "CITY_TRANSPORT"
        claim["lines"][0]["invoice"] = {
            "buyer": {"name": "启衡精密机械有限公司", "taxNo": "91320594MA1TXXXX8Q"},
        }
        extraction = InvoiceExtraction(
            buyer_name="启衡精密机械有限公司",
            buyer_tax_no="91320594MA1TXXXX8Q",
            total_fen=11500, mean_confidence=0.98,
            raw_text=["购买方：启衡精密机械有限公司"],
        )
        result = review_claim(claim, {"LINE-1": extraction})
        self.assertEqual(result["result"], "REJECT")
        self.assertIn("INVOICE_TITLE_MISMATCH", {item["code"] for item in result["violations"]})

    def test_missing_attachment_is_reject(self):
        result = review_claim(claim_with_line(attachment=False), {"LINE-1": None})
        self.assertEqual(result["result"], "REJECT")
        self.assertEqual(result["violations"][0]["code"], "MISSING_ATTACHMENT")

    def test_rejects_duplicate_invoice_from_read_only_ledger_match(self):
        claim = claim_with_line()
        claim["lines"][0]["invoice"] = {"invoiceCode": "CODE", "invoiceNo": "NO", "id": "CURRENT"}
        extraction = InvoiceExtraction(
            buyer_name="启衡精密制造有限公司", buyer_tax_no="91320594MA1TXXXX7Q",
            total_fen=11500, mean_confidence=0.95, raw_text=["发票"],
        )
        result = review_claim(claim, {"LINE-1": extraction}, duplicate_matches={"LINE-1": [{"id": "HISTORY"}]})
        self.assertEqual(result["result"], "REJECT")
        self.assertIn("DUPLICATE_INVOICE", {item["code"] for item in result["violations"]})

    def test_review_payload_only_contains_api_fields(self):
        result = review_claim(
            claim_with_line(attachment=False), {"LINE-1": None}
        )
        payload = to_review_payload(result)
        self.assertEqual(
            set(payload), {"result", "reasons", "evidence", "confidence"}
        )


if __name__ == "__main__":
    unittest.main()
