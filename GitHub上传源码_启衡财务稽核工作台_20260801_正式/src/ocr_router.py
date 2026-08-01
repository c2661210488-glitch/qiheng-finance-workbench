from __future__ import annotations

"""Local OCR routing with a bundled-model PaddleOCR preference and safe fallback."""

import json
import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .invoice_ocr import InvoiceExtraction, extract_invoice, parse_invoice_lines


@dataclass
class OcrResult:
    extraction: InvoiceExtraction
    engine: str
    requires_human_review: bool
    note: str | None = None


def _paddle_model_dirs() -> tuple[Path, Path] | None:
    configured = os.environ.get("QIHENG_PADDLE_MODELS")
    root = Path(configured) if configured else Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "models"
    if not root.exists():
        return None
    det, rec = root / "det", root / "rec"
    return (det, rec) if det.exists() and rec.exists() else None


def _extract_with_paddle(image_path: str | Path, model_dirs: tuple[Path, Path]) -> InvoiceExtraction:
    """Run PaddleOCR only against models bundled beside the EXE.

    No online model discovery/download is permitted at run time.  PaddleOCR 3.x
    result objects vary slightly between releases, hence the small defensive
    decoder below.
    """
    # Dynamic import keeps the D2 RapidOCR-only EXE lean.  A controlled future
    # delivery that actually bundles both Paddle runtime and local models may
    # activate this branch; the current build deliberately has neither.
    PaddleOCR = importlib.import_module("paddleocr").PaddleOCR

    det, rec = model_dirs
    ocr = PaddleOCR(
        text_detection_model_dir=str(det),
        text_recognition_model_dir=str(rec),
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )
    outputs = list(ocr.predict(str(image_path)))
    lines: list[str] = []
    scores: list[float] = []
    for output in outputs:
        raw: Any = output.json if hasattr(output, "json") else output
        if isinstance(raw, str):
            raw = json.loads(raw)
        texts = raw.get("rec_texts", []) if isinstance(raw, dict) else []
        confidences = raw.get("rec_scores", []) if isinstance(raw, dict) else []
        lines.extend(str(item) for item in texts)
        scores.extend(float(item) for item in confidences)
    return parse_invoice_lines(lines, scores)


def extract_ticket(image_path: str | Path) -> OcrResult:
    """Prefer bundled PaddleOCR for paper receipts; never silently use cloud OCR."""
    model_dirs = _paddle_model_dirs()
    if model_dirs:
        try:
            extraction = _extract_with_paddle(image_path, model_dirs)
            return OcrResult(
                extraction=extraction,
                engine="PaddleOCR (bundled local model)",
                requires_human_review=True,
                note="纸质/扫描票据已由本地 PaddleOCR 识别，仍需人工查看原图。",
            )
        except Exception as exc:
            fallback = extract_invoice(image_path)
            return OcrResult(
                extraction=fallback,
                engine="RapidOCR fallback",
                requires_human_review=True,
                note=f"PaddleOCR 本地模型运行失败，已使用离线回退识别：{exc}",
            )
    fallback = extract_invoice(image_path)
    return OcrResult(
        extraction=fallback,
        engine="RapidOCR fallback",
        requires_human_review=True,
        note="未检测到已打包的 PaddleOCR 模型；当前使用离线回退识别，需人工复核原图。",
    )
