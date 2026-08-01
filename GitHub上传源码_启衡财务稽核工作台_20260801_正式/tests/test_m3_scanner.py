import unittest

from src.m3_scanner import COMPANY_NAME, COMPANY_TAX_NO, scan_ledger


def invoice(invoice_id: str, kind: str, invoice_type: str, seller_tax_no: str | None = None) -> dict:
    return {
        "id": invoice_id,
        "type": invoice_type,
        "invoiceCode": f"CODE-{invoice_id}",
        "invoiceNo": f"NO-{invoice_id}",
        "invoiceKind": kind,
        "taxRate": 0.06,
        "buyer": {"name": COMPANY_NAME, "taxNo": COMPANY_TAX_NO},
        "seller": {"name": "供应商", "taxNo": seller_tax_no},
    }


class M3ScannerTests(unittest.TestCase):
    def test_expense_source_is_not_vendor_master_data_task(self):
        report = scan_ledger([invoice("EXP-1", "OTHER", "EXPENSE")], [])
        self.assertEqual(report["invoiceIssues"], [])
        self.assertFalse(any(x["issue"] == "MASTER_DATA_INCOMPLETE" for x in report["details"]))

    def test_purchase_master_data_is_local_task_not_official_issue(self):
        report = scan_ledger([invoice("PUR-1", "OTHER", "PURCHASE_INPUT")], [])
        self.assertEqual(report["invoiceIssues"], [])
        self.assertEqual(report["summary"]["masterDataTasks"], 1)
        self.assertTrue(any(x["issue"] == "MASTER_DATA_INCOMPLETE" for x in report["details"]))


if __name__ == "__main__":
    unittest.main()
