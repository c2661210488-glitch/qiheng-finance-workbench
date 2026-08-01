from pathlib import Path

from PyInstaller.utils.hooks import collect_all


ROOT = Path.cwd()
rapid_datas, rapid_bins, rapid_hidden = collect_all("rapidocr_onnxruntime")
keyring_datas, keyring_bins, keyring_hidden = collect_all("keyring")

# Bundled material is documentation and evaluation/reference material only.
# The live M2 queue and M3 results are always fetched from the ERP selected at
# login; no API key, claim cache or precomputed live decision is bundled.
datas = rapid_datas + keyring_datas + [
    (str(ROOT / "evals" / "public-sample-labels.json"), "evals"),
    (str(ROOT / "evals" / "vnext-public-regression.json"), "evals"),
    (str(ROOT / "evals" / "ocr-field-review-pending.jsonl"), "evals"),
    (str(ROOT / "evals" / "ocr-field-report.json"), "evals"),
    (str(ROOT / "evals" / "anomaly-coverage-108.json"), "evals"),
]

a = Analysis(
    [str(ROOT / "m4-final-20260731" / "finance_workbench.py")],
    pathex=[str(ROOT)],
    binaries=rapid_bins + keyring_bins,
    datas=datas,
    hiddenimports=rapid_hidden + keyring_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "unittest", "paddle", "paddleocr", "paddlex", "modelscope"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="启衡财务稽核工作台_M1M2M3_交付版_20260801",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
