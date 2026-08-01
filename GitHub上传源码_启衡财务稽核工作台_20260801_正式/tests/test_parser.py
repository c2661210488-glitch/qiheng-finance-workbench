import unittest

from src.invoice_ocr import parse_invoice_lines


class InvoiceParserTests(unittest.TestCase):
    def test_extracts_key_fields(self):
        lines = [
            "增值税普通发票",
            "发票代码：998698351590",
            "发票号码：05777879",
            "开票日期：2026年06月21日",
            "购买方",
            "名称：启衡精密制造有限公司",
            "纳税人识别号：91320594MA1TXXXX7Q",
            "销售方",
            "名称：苏州市出租汽车有限公司",
            "价税合计（小写）¥115.00",
        ]
        actual = parse_invoice_lines(lines, [0.95] * len(lines))
        self.assertEqual(actual.invoice_code, "998698351590")
        self.assertEqual(actual.invoice_no, "05777879")
        self.assertEqual(actual.issued_on, "2026-06-21")
        self.assertEqual(actual.buyer_name, "启衡精密制造有限公司")
        self.assertEqual(actual.buyer_tax_no, "91320594MA1TXXXX7Q")
        self.assertEqual(actual.total_fen, 11500)


if __name__ == "__main__":
    unittest.main()

