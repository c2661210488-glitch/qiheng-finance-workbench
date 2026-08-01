from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


_ENGINE: Any | None = None


@dataclass
class InvoiceExtraction:
    invoice_code: str | None = None
    invoice_no: str | None = None
    issued_on: str | None = None
    buyer_name: str | None = None
    buyer_tax_no: str | None = None
    seller_name: str | None = None
    total_fen: int | None = None
    mean_confidence: float = 0.0
    raw_text: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(text: str) -> str:
    return (
        text.replace("：", ":")
        .replace("（", "(")
        .replace("）", ")")
        .replace("￥", "¥")
        .replace(" ", "")
        .strip()
    )


def _digits_after(lines: list[str], labels: tuple[str, ...], lengths: tuple[int, ...]) -> str | None:
    for idx, line in enumerate(lines):
        clean = _clean(line)
        if not any(label in clean for label in labels):
            continue
        candidates = re.findall(r"\d+", clean)
        for value in reversed(candidates):
            if len(value) in lengths:
                return value
        if idx + 1 < len(lines):
            for value in re.findall(r"\d+", _clean(lines[idx + 1])):
                if len(value) in lengths:
                    return value
    return None


def _section(lines: list[str], start_words: tuple[str, ...], end_words: tuple[str, ...]) -> list[str]:
    start = 0
    for i, line in enumerate(lines):
        if any(word in line for word in start_words):
            start = i
            break
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if any(word in lines[i] for word in end_words):
            end = i
            break
    return lines[start:end]


def _name_from_section(lines: list[str]) -> str | None:
    joined = "".join(_clean(line) for line in lines)
    # Handles OCR layouts such as “购名称：启衡精密制造有限公司买纳税人识别号…”.
    explicit = re.search(r"(?:购买方|购方|购)?(?:名称|名)[:：]([^买纳税地电]{3,40}?)(?=(?:购买|买方|买|纳税|税号|地址|电话|$))", joined)
    if explicit:
        return explicit.group(1).strip()
    for i, line in enumerate(lines):
        clean = _clean(line)
        match = re.search(r"(?:名称|名\s*称)[:：]?(.{3,})", clean)
        if match:
            value = re.sub(r"^(购买方|销售方)", "", match.group(1))
            if value:
                return value
        if clean in {"名称", "名称:", "名 称"} and i + 1 < len(lines):
            return _clean(lines[i + 1])
    return None


def _buyer_name_from_document(lines: list[str]) -> str | None:
    """Recover a buyer name when OCR splits “购 / 名称：...” over rows."""
    for index, line in enumerate(lines):
        window = "".join(_clean(value) for value in lines[index:index + 5])
        if not ("购" in _clean(line) or (index and _clean(lines[index - 1]) in {"购", "购买"})):
            continue
        match = re.search(r"(?:购买方|购方|购)?(?:名称|名)[:：]([^买纳税地电]{3,40}?)(?=(?:购买|买方|买|纳税|税号|地址|电话|$))", window)
        if match:
            return match.group(1).strip()
    return None


def _seller_name_from_document(lines: list[str]) -> str | None:
    """Read seller names from spatially-split VAT-invoice OCR rows.

    A common layout emits ``销 / 名 / 称：某公司 / … / 方`` rather than a
    contiguous ``销售方`` label.  The old generic section parser then began at
    the buyer block and incorrectly returned the buyer as the seller.
    """
    seller_markers = ("\u9500\u552e\u65b9", "\u9500\u65b9")
    name_labels = ("\u540d\u79f0", "\u540d\u79f0\uff1a", "\u79f0", "\u79f0\uff1a")
    for index, line in enumerate(lines):
        clean = _clean(line)
        is_seller_start = any(marker in clean for marker in seller_markers) or clean in {"\u9500", "\u9500\u552e"}
        if not is_seller_start:
            continue
        for candidate in lines[index:index + 8]:
            value = _clean(candidate)
            if not any(label in value for label in name_labels):
                continue
            match = re.search(r"(?:\u540d\u79f0|\u79f0)[:\uff1a]?(.{2,80})", value)
            if not match:
                continue
            name = match.group(1)
            # Remove non-name fields accidentally placed on the same OCR row.
            name = re.split(r"(?:\u6536\u6b3e\u4eba|\u590d\u6838|\u5f00\u7968\u4eba|\u7eb3\u7a0e\u4eba|\u7a0e\u53f7|\u5730\u5740)", name)[0]
            if len(name) >= 3:
                return name
    return None


def _tax_no_from_section(lines: list[str]) -> str | None:
    for i, line in enumerate(lines):
        clean = _clean(line).upper()
        if "税号" not in clean and "纳税人识别号" not in clean:
            continue
        values = re.findall(r"[0-9A-Z]{15,20}", clean)
        if values:
            return values[-1]
        if i + 1 < len(lines):
            values = re.findall(r"[0-9A-Z]{15,20}", _clean(lines[i + 1]).upper())
            if values:
                return values[0]
    return None


def _issued_on(lines: list[str]) -> str | None:
    for line in lines:
        clean = _clean(line)
        if "开票日期" not in clean and "日期" not in clean:
            continue
        m = re.search(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?", clean)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def _total_fen(lines: list[str]) -> int | None:
    # 只认“价税合计/小写/合计”附近的金额，避免把税额或单价当总额。
    for i, line in enumerate(lines):
        clean = _clean(line)
        # A VAT invoice's plain "合计" row is the amount before tax.  Do not
        # treat it as the reimbursable ticket total; only use the tax-inclusive
        # total block or its printed "小写" amount.
        if not any(label in clean for label in ("价税合计", "小写")):
            continue
        neighborhood = " ".join(_clean(x) for x in lines[i : i + 2])
        values = re.findall(r"(?:¥|RMB)?([0-9][0-9,]*\.\d{2})", neighborhood, re.I)
        if values:
            value = values[-1].replace(",", "")
            return int(round(float(value) * 100))
    return None


def parse_invoice_lines(lines: list[str], confidences: list[float] | None = None) -> InvoiceExtraction:
    buyer = _section(lines, ("购买方", "购方", "购名称"), ("销售方", "销方", "销名称"))
    seller = _section(lines, ("销售方", "销方", "销名称"), ("价税合计", "备注", "收款人"))
    return InvoiceExtraction(
        invoice_code=_digits_after(lines, ("发票代码", "代码"), (10, 12)),
        invoice_no=_digits_after(lines, ("发票号码", "号码"), (8, 20)),
        issued_on=_issued_on(lines),
        buyer_name=_buyer_name_from_document(lines) or _name_from_section(buyer),
        buyer_tax_no=_tax_no_from_section(buyer),
        # Do not fall back to the generic section parser: for spatially-split
        # layouts it can return the buyer name and create a false seller value.
        # A missing seller is safer and routes to human review.
        seller_name=_seller_name_from_document(lines),
        total_fen=_total_fen(lines),
        mean_confidence=(
            sum(confidences) / len(confidences) if confidences else 0.0
        ),
        raw_text=lines,
    )


def extract_invoice(image_path: str | Path) -> InvoiceExtraction:
    global _ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "尚未安装本地OCR。请先执行：pip install -r requirements.txt"
        ) from exc

    if _ENGINE is None:
        _ENGINE = RapidOCR()
    result, _elapsed = _ENGINE(str(image_path))
    if not result:
        return InvoiceExtraction(raw_text=[])
    lines = [str(item[1]) for item in result]
    confidences = [float(item[2]) for item in result]
    return parse_invoice_lines(lines, confidences)
