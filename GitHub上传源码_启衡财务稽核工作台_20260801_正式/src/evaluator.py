from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .invoice_ocr import extract_invoice


FIELDS = (
    "invoice_code",
    "invoice_no",
    "issued_on",
    "buyer_name",
    "buyer_tax_no",
    "seller_name",
    "total_fen",
)


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace(" ", "").strip().upper()
    return value


def evaluate(dataset_path: str | Path) -> dict[str, Any]:
    dataset_path = Path(dataset_path)
    cases = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not cases:
        raise ValueError("评测集为空")

    field_hits = {field: 0 for field in FIELDS}
    field_totals = {field: 0 for field in FIELDS}
    complete_hits = 0
    details = []

    for case in cases:
        image_path = Path(case["image"])
        if not image_path.is_absolute():
            image_path = (dataset_path.parent / image_path).resolve()
        actual = extract_invoice(image_path).to_dict()
        expected = case["expected"]
        labeled_fields = [field for field in FIELDS if field in expected]
        if not labeled_fields:
            raise ValueError(
                f"{case.get('image')} 尚未填写expected字段，不能计入评测"
            )
        all_correct = True
        comparisons = {}
        for field in FIELDS:
            if field not in expected:
                continue
            field_totals[field] += 1
            ok = _norm(expected[field]) == _norm(actual.get(field))
            field_hits[field] += int(ok)
            all_correct = all_correct and ok
            comparisons[field] = {
                "expected": expected[field],
                "actual": actual.get(field),
                "correct": ok,
            }
        complete_hits += int(all_correct)
        details.append({"image": str(image_path), "comparisons": comparisons})

    field_accuracy = {
        field: (
            round(field_hits[field] / field_totals[field], 4)
            if field_totals[field]
            else None
        )
        for field in FIELDS
    }
    return {
        "testSetSize": len(cases),
        "completeTicketAccuracy": round(complete_hits / len(cases), 4),
        "fieldAccuracy": field_accuracy,
        "details": details,
    }
