from __future__ import annotations

"""Finance-first desktop shell for the final M2/M3 workflow.

This is deliberately a human-in-the-loop client: deterministic review results
are evidence-backed, manual decisions live locally until explicit ERP writeback,
and M3 never writes ERP.
"""

import csv
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import keyring
from PySide6.QtCore import QObject, Qt, Signal, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QInputDialog, QLineEdit, QMainWindow, QMessageBox, QPushButton, QSplitter,
    QDialog, QProgressBar, QScrollArea, QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget, QMenu, QStyle, QSystemTrayIcon,
)

from app import READ_SCOPES, analyze_one
from src.m3_scanner import scan_ledger
from src.qiheng_api import QihengClient

APP = "启衡财务稽核工作台"
KEY_SERVICE, KEY_USER = "qiheng-review-workbench", "api-key"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "QihengReview" / "finance-workbench"
STATE = DATA_DIR / "manual-state.json"
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
PUBLIC_LABELS = RESOURCE_ROOT / "evals" / "public-sample-labels.json"
PUBLIC_REPORT = RESOURCE_ROOT / "evals" / "vnext-public-regression.json"
OCR_BASELINE = RESOURCE_ROOT / "evals" / "ocr-field-review-pending.jsonl"
RULE_VERSION = "m2-fixed-rules-20260730-v3-human-truth"
ANOMALY_MATRIX = RESOURCE_ROOT / "evals" / "anomaly-coverage-108.json"
LOCAL_OCR_REPORT = DATA_DIR.parent / "evals" / "ocr-field-report.json"
BUNDLED_OCR_REPORT = RESOURCE_ROOT / "evals" / "ocr-field-report.json"


class Signals(QObject):
    done = Signal(object)
    error = Signal(str)
    progress = Signal(int, int, str)
    item_done = Signal(str, object)


class PanImageLabel(QLabel):
    """Image label that supports click-to-open and mouse-drag panning."""
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._press_pos = None
        self._start_h = 0
        self._start_v = 0
        self._dragged = False
        self.setCursor(Qt.OpenHandCursor)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        self._press_pos = event.globalPosition().toPoint()
        scroll = self.parent()
        while scroll and not isinstance(scroll, QScrollArea):
            scroll = scroll.parent()
        self._scroll = scroll
        if scroll:
            self._start_h = scroll.horizontalScrollBar().value()
            self._start_v = scroll.verticalScrollBar().value()
        self._dragged = False
        self.setCursor(Qt.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event: Any) -> None:
        if self._press_pos is None:
            return super().mouseMoveEvent(event)
        delta = event.globalPosition().toPoint() - self._press_pos
        if abs(delta.x()) + abs(delta.y()) > 5:
            self._dragged = True
        if getattr(self, "_scroll", None):
            self._scroll.horizontalScrollBar().setValue(self._start_h - delta.x())
            self._scroll.verticalScrollBar().setValue(self._start_v - delta.y())
        event.accept()

    def mouseReleaseEvent(self, event: Any) -> None:
        if self._press_pos is None:
            return super().mouseReleaseEvent(event)
        self.setCursor(Qt.OpenHandCursor)
        if not self._dragged:
            self.clicked.emit()
        self._press_pos = None
        event.accept()


class LocalState:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {
            "manual": {},
            "calibrations": {},
            "confirmedSamples": [],
            "truthSamples": [],
            "ruleSuggestions": [],
            "m3Tasks": {},
            "m4Tasks": {},
            "auditLog": [],
            "analysisCache": {},
            "analysisSignatures": {},
            "calibrationRevision": 0,
        }
        try:
            self.data.update(json.loads(STATE.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except Exception:
            pass
        for key, default in (("manual", {}), ("calibrations", {}), ("confirmedSamples", []), ("truthSamples", []), ("ruleSuggestions", []), ("m3Tasks", {}), ("m4Tasks", {}), ("auditLog", []), ("analysisCache", {}), ("analysisSignatures", {}), ("calibrationRevision", 0)):
            self.data.setdefault(key, default)
        # One-time migration: older daily calibration records already are
        # human truth.  Bring them into the unified sample library without
        # changing the historical calibration log.
        known={str(item.get("sampleKey")) for item in self.data["truthSamples"]}
        migrated=False
        for claim_id, logs in self.data["calibrations"].items():
            packet=self.data["analysisCache"].get(claim_id,{})
            claim=packet.get("claim") or {}
            for item in logs:
                line_id=item.get("lineId"); overrides=item.get("overrides") or {}
                sample_key=f"{claim_id}:{line_id}"
                if not line_id or not overrides or sample_key in known:
                    continue
                attachment=next((x for x in packet.get("attachments",[]) if x.get("lineId")==line_id),{})
                self.data["truthSamples"].append({
                    "sampleKey":sample_key,
                    "claimId":claim_id,
                    "claimNo":claim.get("claimNo") or claim_id,
                    "lineId":line_id,
                    "image":attachment.get("image"),
                    "prediction":dict(attachment.get("extraction") or {}),
                    "gold":dict(overrides),
                    "reviewer":item.get("reviewer") or "周晓",
                    "reviewedAt":item.get("at"),
                    "reason":item.get("reason"),
                    "source":"历史日常人工校准迁移",
                })
                known.add(sample_key)
                migrated=True
        if migrated:
            self.save()

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def result_label(value: str | None) -> str:
    return {"REJECT": "建议驳回", "FLAG": "待人工复核", "APPROVE": "建议通过"}.get(value or "", "待初审")


def status_color(value: str | None) -> str:
    return {"REJECT": "#c8443c", "FLAG": "#a66a08", "APPROVE": "#278455"}.get(value or "", "#5d7083")


def claim_employee_name(claim: dict[str, Any]) -> str:
    """Accept both summary and detail response shapes from the official SDK."""
    return str(claim.get("employeeName") or (claim.get("employee") or {}).get("name") or "—")


def claim_department_name(claim: dict[str, Any]) -> str:
    return str(claim.get("departmentName") or (claim.get("department") or {}).get("name") or "—")


def claim_total_yuan(claim: dict[str, Any]) -> float:
    raw = claim.get("totalFen")
    if raw is None:
        raw = claim.get("total") or claim.get("amountFen") or 0
    try:
        return float(raw) / 100
    except (TypeError, ValueError):
        return 0.0


def expense_label(value: Any) -> str:
    return {
        "HOTEL": "住宿费", "MEAL": "餐饮费", "LONG_TRANSPORT": "长途交通",
        "CITY_TRANSPORT": "市内交通", "DAILY": "日常费用", "TRAVEL": "差旅费",
    }.get(str(value or ""), str(value or "—"))


def approval_label(value: Any) -> str:
    return {
        "SUBMIT": "提交", "APPROVE": "同意", "REJECT": "驳回", "DEPARTMENT_APPROVE": "部门负责人同意",
        "FINANCE_APPROVE": "财务同意", "PRE_APPROVAL": "事前审批", "SPECIAL_APPROVAL": "特批",
    }.get(str(value or ""), str(value or "—"))


class Login(QWidget):
    connected = Signal(object, str)

    def __init__(self) -> None:
        super().__init__()
        box = QVBoxLayout(self); box.setContentsMargins(60, 70, 60, 60); box.setSpacing(12)
        title = QLabel(APP); title.setObjectName("title"); box.addWidget(title)
        box.addWidget(QLabel("首次连接 ERP。登录后连接信息不再占用工作台主页。"))
        form = QFormLayout(); self.url = QLineEdit(); self.url.setPlaceholderText("例如：http://127.0.0.1:8081"); self.key = QLineEdit(); self.key.setEchoMode(QLineEdit.Password)
        form.addRow("API 地址", self.url); form.addRow("API Key", self.key); box.addLayout(form)
        self.button = QPushButton("登录并进入工作台"); self.button.clicked.connect(self.login_to_erp); box.addWidget(self.button)
        self.note = QLabel("演示或正式环境均只在你点击“批量写回 ERP”并二次确认后，才会产生 M2 审核意见写入。")
        self.note.setWordWrap(True); self.note.setObjectName("hint"); box.addWidget(self.note); box.addStretch()

    def login_to_erp(self, *_: Any) -> None:
        url, key = self.url.text().strip(), self.key.text().strip()
        if not url or not key:
            self.note.setText("请填写 API 地址和 API Key。"); return
        self.button.setEnabled(False); self.note.setText("正在验证连接和只读权限…")
        sig = Signals(); self._sig = sig
        def work() -> None:
            try:
                client = QihengClient(key, url); me = client.me(); missing = READ_SCOPES - set(me.get("scopes", []))
                if missing: raise RuntimeError("缺少只读权限：" + ", ".join(sorted(missing)))
                sig.done.emit((client, key))
            except Exception as exc: sig.error.emit(str(exc))
        sig.done.connect(lambda pair: self._ok(pair[0], pair[1])); sig.error.connect(self._bad)
        threading.Thread(target=work, daemon=True).start()

    def _ok(self, client: QihengClient, key: str) -> None:
        # 交付版不把 API Key 持久化到本机凭据管理器，避免换人/换机时误带凭据。
        self.connected.emit(client, self.url.text().strip())

    def _bad(self, message: str) -> None:
        self.button.setEnabled(True)
        lower = message.lower()
        if any(token in lower for token in ("connection refused", "failed to establish", "max retries", "10061", "连接被拒绝", "无法连接")):
            self.note.setText("暂未检测到启衡 ERP，请先启动 ERP/API 环境后再登录。")
        else:
            self.note.setText("连接失败，请检查 API 地址、权限或 API Key。\n" + message)


class Workbench(QMainWindow):
    def __init__(self, client: QihengClient, base_url: str) -> None:
        super().__init__(); self.client, self.base_url = client, base_url; self.store = LocalState(); self._allow_exit = False
        self.claims: list[dict[str, Any]] = []
        # Never seed the live M2 queue from a prior local run.  Historical
        # analysisCache remains available for audit/sample learning only; every
        # current recommendation must originate from the ERP selected at login.
        self.outcomes: dict[str, dict[str, Any]] = {}
        self.runtime_status: dict[str, str] = {}
        self.claim_payloads: dict[str, dict[str, Any]] = {}
        self._selection_token = 0
        self.current_id: str | None = None; self.m3: dict[str, Any] | None = None
        self.invoices: list[dict[str, Any]] = []; self.bank_rows: list[dict[str, Any]] = []; self.receivables: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []; self.customers: list[dict[str, Any]] = []; self.sales_invoices: list[dict[str, Any]] = []
        self.payments: list[dict[str, Any]] = []; self.m4_matches: list[dict[str, Any]] = []
        self._m4_receipt_allocations: dict[str, list[dict[str, Any]]] = {}
        self._m4_erp_receipt_index: dict[tuple[str,int,str],dict[str,Any]] = {}
        self._m4_sales_ids: set[str] = set(); self._m4_sales_numbers: set[str] = set()
        self._m4_loaded = False; self._m4_loading = False
        self._build(); self._style(); self._base_style = self.styleSheet(); self._setup_tray(); self.load_claims()

    def _setup_tray(self) -> None:
        """Keep a visible, explicit exit path when the main window is hidden."""
        icon = self.style().standardIcon(QStyle.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        menu = QMenu(self)
        show_action = QAction("打开工作台", self); show_action.triggered.connect(self._restore_from_tray)
        exit_action = QAction("退出并彻底结束程序", self); exit_action.triggered.connect(self.exit_application)
        menu.addAction(show_action); menu.addSeparator(); menu.addAction(exit_action)
        self.tray.setContextMenu(menu); self.tray.activated.connect(lambda reason: self._restore_from_tray() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.setToolTip("启衡财务稽核工作台（右键可彻底退出）")
        self.tray.show()

    def _restore_from_tray(self) -> None:
        self.showNormal(); self.raise_(); self.activateWindow()

    def exit_application(self) -> None:
        self._allow_exit = True
        if hasattr(self, "tray"):
            self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event: Any) -> None:
        if self._allow_exit:
            event.accept(); return
        event.ignore(); self.hide()
        if hasattr(self, "tray"):
            self.tray.showMessage(APP, "工作台仍在右下角运行。右键托盘图标选择“退出并彻底结束程序”可完全退出。", QSystemTrayIcon.Information, 4500)

    def _build(self) -> None:
        self.setWindowTitle(APP); self.resize(1580, 940); self.setMinimumSize(1220, 760)
        root = QWidget(); self.setCentralWidget(root); layout = QHBoxLayout(root); layout.setContentsMargins(0, 0, 0, 0)
        side = QFrame(); side.setObjectName("side"); side.setFixedWidth(205); s = QVBoxLayout(side); s.setContentsMargins(10, 18, 10, 10)
        brand = QLabel("启衡财务稽核\n工作台"); brand.setObjectName("sideBrand"); s.addWidget(brand)
        self.nav: dict[str, QPushButton] = {}
        for group, entries in [("费用管理", [("m2", "费用报销单"), ("public", "公开 30 单验收"), ("write", "批量写回 ERP")]), ("发票与往来", [("m3", "发票台账稽核")]), ("审核学习闭环", [("cal", "人工校准与样本"), ("quality", "质量与效率看板")]), ("系统", [("settings", "系统设置与退出")])]:
            s.addWidget(QLabel(group, objectName="navGroup"))
            for key, label in entries:
                b = QPushButton(label); b.setObjectName("nav"); b.setCheckable(True); b.clicked.connect(lambda checked=False, k=key: self.show_page(k)); s.addWidget(b); self.nav[key] = b
        s.addStretch(); layout.addWidget(side)
        center = QWidget(); c = QVBoxLayout(center); c.setContentsMargins(20, 16, 20, 16)
        top = QHBoxLayout(); h = QLabel(APP); h.setObjectName("heading"); top.addWidget(h); top.addWidget(QLabel("原票优先 · 固定规则 · 财务最终决定", objectName="subtitle")); top.addStretch()
        self.quality_badge = QPushButton(); self.quality_badge.setObjectName("qualityBadge"); self.quality_badge.clicked.connect(lambda: self.show_page("quality")); top.addWidget(self.quality_badge); c.addLayout(top)
        self.pages = QStackedWidget(); c.addWidget(self.pages, 1); layout.addWidget(center, 1)
        self.pages.addWidget(self._m2_page()); self.pages.addWidget(self._public_page()); self.pages.addWidget(self._write_page()); self.pages.addWidget(self._m3_page()); self.pages.addWidget(self._cal_page()); self.pages.addWidget(self._quality_page()); self.pages.addWidget(self._settings_page())
        self.order = {"m2": 0, "public": 1, "write": 2, "m3": 3, "cal": 4, "quality": 5, "settings": 6}
        self.refresh_quality_metrics(); self.show_page("m2")

    def _page_title(self, crumb: str, title: str, note: str) -> QVBoxLayout:
        v = QVBoxLayout(); v.setSpacing(3); v.addWidget(QLabel(crumb, objectName="crumb")); q = QLabel(title); q.setObjectName("pageTitle"); v.addWidget(q); n = QLabel(note); n.setObjectName("hint"); n.setWordWrap(True); v.addWidget(n); return v

    def _m2_page(self) -> QWidget:
        page = QWidget(); v = QVBoxLayout(page); v.addLayout(self._page_title("费用管理 / 费用报销单", "费用报销单", "先核对 AI 建议与原票；正确则确认，错误则进入人工校准并整单重判。只有二次确认后的单据才进入批量写回。"))
        bar = QHBoxLayout(); self.refresh = QPushButton("刷新待审单据"); self.refresh.clicked.connect(self.load_claims); bar.addWidget(self.refresh); self.one = QPushButton("重新初审本单"); self.one.clicked.connect(self.analyze_current); bar.addWidget(self.one); self.batch = QPushButton("一键审核全部"); self.batch.clicked.connect(self.batch_review_all); bar.addWidget(self.batch); audit = QPushButton("导出本单审计包"); audit.clicked.connect(self.export_current_audit_package); bar.addWidget(audit); self.batch_progress = QProgressBar(); self.batch_progress.setVisible(False); self.batch_progress.setFixedWidth(220); bar.addWidget(self.batch_progress); self.m2_filter = QComboBox(); self.m2_filter.addItems(["风险待处理（默认）", "校准后待二次确认", "写回退回 / 待二次确认", "暂存待补充", "待初审", "待人工复核", "建议驳回", "建议通过", "全部待审"]); self.m2_filter.currentIndexChanged.connect(self.render_queue); bar.addWidget(self.m2_filter); self.search = QLineEdit(); self.search.setPlaceholderText("搜索单号 / 报销人"); self.search.textChanged.connect(self.render_queue); bar.addWidget(self.search); bar.addStretch(); v.addLayout(bar)
        self.cards = QLabel(); self.cards.setObjectName("cards"); v.addWidget(self.cards)
        split = QSplitter(Qt.Horizontal); self.queue = QTableWidget(0, 6); self.queue.setHorizontalHeaderLabels(["初审建议", "证据状态", "单号", "报销人", "部门", "金额"]); self.queue.setSelectionBehavior(QTableWidget.SelectRows); self.queue.setEditTriggers(QTableWidget.NoEditTriggers); self.queue.itemSelectionChanged.connect(self.select_claim); split.addWidget(self.queue)
        # The middle pane is the claim's working surface: selecting a cost line
        # synchronises the ticket preview on the right instead of hiding it in a dialog.
        detail_pane = QWidget(); detail_layout = QVBoxLayout(detail_pane); detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail = QTextEdit(); self.detail.setReadOnly(True); self.detail.setMaximumHeight(145); detail_layout.addWidget(self.detail)
        detail_layout.addWidget(QLabel("费用明细（选择一行查看对应原票）", objectName="panelTitle"))
        self.expense_lines = QTableWidget(0, 6); self.expense_lines.setHorizontalHeaderLabels(["行", "费用类别", "摘要", "会计科目", "申报金额", "票据编号"])
        self.expense_lines.setSelectionBehavior(QTableWidget.SelectRows); self.expense_lines.setEditTriggers(QTableWidget.NoEditTriggers); self.expense_lines.itemSelectionChanged.connect(self.select_ticket_from_line); detail_layout.addWidget(self.expense_lines, 1)
        detail_layout.addWidget(QLabel("审批与特批记录", objectName="panelTitle"))
        self.approval_detail = QTextEdit(); self.approval_detail.setReadOnly(True); self.approval_detail.setMaximumHeight(130); detail_layout.addWidget(self.approval_detail)
        split.addWidget(detail_pane)
        right = QWidget(); rv = QVBoxLayout(right); caption=QLabel("初审意见与原票"); caption.setObjectName("panelTitle"); rv.addWidget(caption)
        self.evidence = QTextEdit(); self.evidence.setReadOnly(True); self.evidence.setObjectName("evidence"); self.evidence.setPlainText("选择单据后查看 AI 初审依据。"); rv.addWidget(self.evidence, 1); self.ticket_title = QLabel("原票：未选择"); self.ticket_title.setObjectName("ticketTitle"); rv.addWidget(self.ticket_title)
        self.ticket_preview = QLabel("选择费用行后加载对应原票\n点击图片可放大查看"); self.ticket_preview.setObjectName("ticketPreview"); self.ticket_preview.setAlignment(Qt.AlignCenter); self.ticket_preview.setMinimumHeight(235); self.ticket_preview.setCursor(Qt.PointingHandCursor); self.ticket_preview.mousePressEvent = lambda _event: self.zoom_ticket(); rv.addWidget(self.ticket_preview)
        actions = QHBoxLayout()
        self.confirm_ai_button = QPushButton("确认 AI 建议")
        self.confirm_ai_button.setObjectName("primaryAction")
        self.confirm_ai_button.clicked.connect(self.confirm_ai_decision)
        approve = QPushButton("同意这张单据")
        approve.clicked.connect(lambda: self.finalize_current_claim("APPROVE"))
        reject = QPushButton("驳回这张单据")
        reject.setObjectName("danger")
        reject.clicked.connect(lambda: self.finalize_current_claim("REJECT"))
        cal = QPushButton("人工校准")
        cal.clicked.connect(self.open_current_calibration)
        hold = QPushButton("暂存待补充")
        hold.clicked.connect(self.hold_current_claim)
        actions.addWidget(self.confirm_ai_button, 2); actions.addWidget(approve, 2); actions.addWidget(reject, 2); actions.addWidget(cal, 2); actions.addWidget(hold)
        rv.addLayout(actions); split.addWidget(right); split.setSizes([345, 660, 500]); v.addWidget(split, 1); return page

    def _public_page(self) -> QWidget:
        page = QWidget(); v = QVBoxLayout(page)
        v.addLayout(self._page_title("费用管理 / 公开 30 单验收", "公开 30 单验收", "官方答案只读且不可修改；系统结果单独运行并逐单显示差异。该页面不写回 ERP，也不等于 270 单隐藏评分。"))
        bar = QHBoxLayout()
        run = QPushButton("重新运行公开 30 单")
        run.clicked.connect(self.run_public_30)
        bar.addWidget(run)
        self.public_progress = QProgressBar(); self.public_progress.setVisible(False); self.public_progress.setFixedWidth(260); bar.addWidget(self.public_progress)
        bar.addStretch(); self.public_summary = QLabel("正在读取已保存的验收报告…"); bar.addWidget(self.public_summary); v.addLayout(bar)
        self.public_table = QTableWidget(0, 7)
        self.public_table.setHorizontalHeaderLabels(["单号", "官方结论", "系统结论", "官方违规代码", "系统违规代码", "结论一致", "代码一致"])
        self.public_table.setSelectionBehavior(QTableWidget.SelectRows); self.public_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.public_table.itemSelectionChanged.connect(self.show_public_detail); v.addWidget(self.public_table, 1)
        self.public_detail = QTextEdit(); self.public_detail.setReadOnly(True); self.public_detail.setMaximumHeight(165); v.addWidget(self.public_detail)
        self._load_public_report()
        return page

    def _write_page(self) -> QWidget:
        page = QWidget(); v = QVBoxLayout(page); v.addLayout(self._page_title("费用管理 / 批量写回 ERP", "批量写回 ERP", "勾选需要处理的单据，再执行“预检所选”或“写回所选”；结论相同记为一致，只有通过与驳回相反才属于冲突。"))
        bar = QHBoxLayout()
        select_all = QPushButton("全选"); select_all.clicked.connect(lambda: self.set_all_write_checks(True)); bar.addWidget(select_all)
        clear_all = QPushButton("取消全选"); clear_all.clicked.connect(lambda: self.set_all_write_checks(False)); bar.addWidget(clear_all)
        pre = QPushButton("预检所选"); pre.clicked.connect(self.precheck); bar.addWidget(pre)
        put = QPushButton("写回所选"); put.clicked.connect(self.writeback); bar.addWidget(put)
        reopen = QPushButton("退回待复核"); reopen.clicked.connect(self.return_selected_to_review); bar.addWidget(reopen)
        bar.addStretch(); self.write_status = QLabel("请先勾选需要处理的单据"); bar.addWidget(self.write_status); v.addLayout(bar)
        self.write_table = QTableWidget(0, 10); self.write_table.setHorizontalHeaderLabels(["选择", "关联单据", "报销人", "AI 初审意见", "AI 审批理由", "人工最终意见", "是否人工复核", "复核时间", "回写状态", "冲突 / 失败原因"]); self.write_table.setEditTriggers(QTableWidget.NoEditTriggers); self.write_table.setSelectionBehavior(QTableWidget.SelectRows); self.write_table.itemSelectionChanged.connect(self.show_writeback_detail); self.write_table.setColumnWidth(0, 54); self.write_table.setColumnWidth(9, 520); v.addWidget(self.write_table, 1)
        self.write_detail = QTextEdit(); self.write_detail.setReadOnly(True); self.write_detail.setMaximumHeight(170); self.write_detail.setPlainText("选择一条记录后，这里会完整显示 ERP 原意见、本次拟写意见、冲突原因和历史写回记录。写回前后均可退回 M2 二次复核。"); v.addWidget(self.write_detail); return page

    def _ledger_page(self) -> QWidget:
        page = QWidget(); v = QVBoxLayout(page); v.addLayout(self._page_title("发票与往来 / 发票台账", "发票台账", "只读浏览入口。全量异常发现和处理请进入“发票台账稽核”。")); bar=QHBoxLayout(); refresh=QPushButton("读取全量发票台账（只读）"); refresh.clicked.connect(self.load_ledger); bar.addWidget(refresh); bar.addStretch(); self.ledger_status=QLabel("尚未加载台账"); bar.addWidget(self.ledger_status); v.addLayout(bar); self.ledger = QTableWidget(0, 6); self.ledger.setHorizontalHeaderLabels(["发票代码/号码", "进项/销项", "销售方", "购买方", "金额", "税率"]); self.ledger.setEditTriggers(QTableWidget.NoEditTriggers); v.addWidget(self.ledger, 1); return page

    def _m3_page(self) -> QWidget:
        page = QWidget(); v = QVBoxLayout(page); v.addLayout(self._page_title("发票与往来 / 发票台账稽核", "发票台账稽核", "读取全量发票台账和供应商主数据后开始稽核。只展示需处理的异常；处理结果仅保留本地稽核任务，不写回 ERP。")); bar = QHBoxLayout(); run = QPushButton("读取全量发票，开始稽核（只读）"); run.clicked.connect(self.scan_m3); bar.addWidget(run); export = QPushButton("导出稽核清单"); export.clicked.connect(self.export_m3); bar.addWidget(export); bar.addStretch(); self.m3_status = QLabel("请读取全量发票并开始稽核"); bar.addWidget(self.m3_status); v.addLayout(bar)
        self.m3_summary = QLabel("尚未开始稽核"); self.m3_summary.setObjectName("cards"); v.addWidget(self.m3_summary)
        split = QSplitter(Qt.Vertical); upper = QSplitter(Qt.Horizontal); self.m3_table = QTableWidget(0, 9); self.m3_table.setHorizontalHeaderLabels(["发票代码/号码", "来源", "销售方", "待核查事项", "台账实际值", "参考口径", "差异", "核查依据", "建议处理"]); self.m3_table.setSelectionBehavior(QTableWidget.SelectRows); self.m3_table.setEditTriggers(QTableWidget.NoEditTriggers); self.m3_table.itemSelectionChanged.connect(self.show_m3_task); upper.addWidget(self.m3_table); self.m3_detail = QTextEdit(); self.m3_detail.setReadOnly(True); upper.addWidget(self.m3_detail); upper.setSizes([980, 420]); split.addWidget(upper)
        self.m3_duplicates = QTableWidget(0, 5); self.m3_duplicates.setHorizontalHeaderLabels(["发票代码", "发票号码", "重复情况", "全部关联台账记录", "处理提示"]); self.m3_duplicates.setEditTriggers(QTableWidget.NoEditTriggers); self.m3_duplicates.setSelectionBehavior(QTableWidget.SelectRows); self.m3_duplicates.itemSelectionChanged.connect(self.show_m3_duplicate); split.addWidget(self.m3_duplicates); split.setSizes([470, 190]); v.addWidget(split, 1); task_actions=QHBoxLayout(); a=QPushButton("确认问题属实并分派"); a.clicked.connect(lambda:self.m3_action("确认问题属实")); e=QPushButton("核查后确认无异常"); e.clicked.connect(lambda:self.m3_action("确认无异常")); m=QPushButton("交给主数据维护"); m.clicked.connect(lambda:self.m3_action("交给主数据维护")); task_actions.addWidget(a); task_actions.addWidget(e); task_actions.addWidget(m); task_actions.addStretch(); v.addLayout(task_actions); return page

    def _m4_page(self) -> QWidget:
        page = QWidget(); v = QVBoxLayout(page)
        v.addLayout(self._page_title(
            "发票与往来 / 应收台账", "应收台账",
            "通过 ERP API 读取客户、销项发票、应收台账与已登记回款。按客户和月份查看应收、已收、待收、逾期及是否开票；本页只读，不执行自动核销。"
        ))
        bar=QHBoxLayout()
        self.m4_load_button=QPushButton("刷新应收台账（只读）"); self.m4_load_button.clicked.connect(self.load_m4_data)
        export_ar=QPushButton("导出应收台账"); export_ar.clicked.connect(self.export_m4_receivables)
        bar.addWidget(self.m4_load_button); bar.addWidget(export_ar)
        bar.addStretch(); self.m4_status=QLabel("进入本页后自动读取 ERP 应收、回款、客户和销项发票"); bar.addWidget(self.m4_status); v.addLayout(bar)
        self.m4_summary=QLabel("尚未读取 ERP 应收台账"); self.m4_summary.setObjectName("cards"); self.m4_summary.setWordWrap(True); v.addWidget(self.m4_summary)

        self.m4_tabs=QTabWidget()
        customer_page=QWidget(); cv=QVBoxLayout(customer_page)
        cbar=QHBoxLayout()
        self.m4_customer_search=QLineEdit(); self.m4_customer_search.setPlaceholderText("搜索客户名称或客户编号"); self.m4_customer_search.textChanged.connect(self.render_m4_customers)
        cbar.addWidget(QLabel("客户筛选")); cbar.addWidget(self.m4_customer_search, 1); cv.addLayout(cbar)
        csplit=QSplitter(Qt.Horizontal)
        self.m4_customer=QTableWidget(0, 8); self.m4_customer.setHorizontalHeaderLabels(["客户", "应收总额", "已收金额", "待收金额", "逾期金额", "逾期笔数", "开票记录", "最早到期日"])
        self.m4_customer.setSelectionBehavior(QTableWidget.SelectRows); self.m4_customer.setEditTriggers(QTableWidget.NoEditTriggers); self.m4_customer.itemSelectionChanged.connect(self.show_m4_customer_detail); csplit.addWidget(self.m4_customer)
        customer_right=QWidget(); crv=QVBoxLayout(customer_right)
        self.m4_customer_detail=QLabel("选择左侧客户后，右侧显示该客户的月份；双击月份查看全部应收明细。")
        self.m4_customer_detail.setObjectName("hint"); self.m4_customer_detail.setWordWrap(True); crv.addWidget(self.m4_customer_detail)
        crv.addWidget(QLabel("所选客户的月份（双击查看全部明细）", objectName="panelTitle"))
        self.m4_customer_months=QTableWidget(0, 7); self.m4_customer_months.setHorizontalHeaderLabels(["月份", "客户", "应收总额", "已收金额", "待收金额", "逾期金额", "应收笔数"])
        self.m4_customer_months.setSelectionBehavior(QTableWidget.SelectRows); self.m4_customer_months.setEditTriggers(QTableWidget.NoEditTriggers); self.m4_customer_months.setAlternatingRowColors(True)
        self.m4_customer_months.cellDoubleClicked.connect(self.open_m4_month_detail); crv.addWidget(self.m4_customer_months, 1)
        open_month=QPushButton("查看所选月份全部明细"); open_month.clicked.connect(self.open_selected_m4_month); crv.addWidget(open_month)
        csplit.addWidget(customer_right); csplit.setSizes([900,500]); cv.addWidget(csplit,1)
        self.m4_tabs.addTab(customer_page, "客户汇总")

        bank_page=QWidget(); bv=QVBoxLayout(bank_page)
        bank_bar=QHBoxLayout()
        import_bank=QPushButton("导入银行全部流水（可多选）"); import_bank.clicked.connect(self.import_bank_csv)
        self.m4_filter=QComboBox(); self.m4_filter.addItems(["全部流水", "贷方到账", "借方付款", "已人工确认", "候选可对应", "待人工核对", "未匹配", "已排除"])
        self.m4_filter.currentIndexChanged.connect(self.render_m4)
        self.m4_bank_search=QLineEdit(); self.m4_bank_search.setPlaceholderText("搜索付款方、流水号、候选应收ID"); self.m4_bank_search.textChanged.connect(self.render_m4)
        bank_bar.addWidget(import_bank); bank_bar.addWidget(QLabel("查看")); bank_bar.addWidget(self.m4_filter)
        bank_bar.addWidget(self.m4_bank_search,1); bv.addLayout(bank_bar)
        self.m4_bank_summary=QLabel("银行流水尚未导入。支持作业包 bank 文件夹中的 CSV，也支持同格式银行导出表。必须包含：交易日期、交易流水号、对方户名、对方账号、借贷标志、发生额、摘要/用途。全部流水可查看；只有贷方到账进入客户应收匹配。")
        self.m4_bank_summary.setObjectName("cards"); self.m4_bank_summary.setWordWrap(True); bv.addWidget(self.m4_bank_summary)
        bank_split=QSplitter(Qt.Horizontal)
        self.m4=QTableWidget(0, 10); self.m4.setHorizontalHeaderLabels(["来源账户", "日期", "方向", "对方", "金额", "银行事实", "ERP回款", "应收匹配", "候选应收", "人工状态"])
        self.m4.setSelectionBehavior(QTableWidget.SelectRows); self.m4.setEditTriggers(QTableWidget.NoEditTriggers)
        self.m4.itemSelectionChanged.connect(self.show_m4_detail); bank_split.addWidget(self.m4)
        self.m4_detail=QTextEdit(); self.m4_detail.setReadOnly(True); self.m4_detail.setPlainText("选择一笔银行到账后，查看付款人、附言、ERP回款和候选应收证据。")
        bank_split.addWidget(self.m4_detail); bank_split.setSizes([980,420]); bv.addWidget(bank_split,1)
        bank_actions=QHBoxLayout()
        confirm=QPushButton("确认到账匹配（仅本地）"); confirm.clicked.connect(lambda:self.m4_action("已人工确认"))
        review=QPushButton("转人工复核"); review.clicked.connect(lambda:self.m4_action("待人工核对"))
        exclude=QPushButton("排除为非客户回款"); exclude.clicked.connect(lambda:self.m4_action("已排除"))
        export_bank=QPushButton("导出银行核对清单"); export_bank.clicked.connect(self.export_m4)
        export_official=QPushButton("导出官方 M4 JSON"); export_official.clicked.connect(self.export_m4_official)
        bank_actions.addWidget(confirm); bank_actions.addWidget(review); bank_actions.addWidget(exclude); bank_actions.addWidget(export_bank); bank_actions.addWidget(export_official); bank_actions.addStretch(); bv.addLayout(bank_actions)
        bv.addWidget(QLabel("银行已到账是原始流水事实；ERP回款、应收匹配和人工确认分别展示。本页不自动核销、不写回ERP。", objectName="hint"))
        self.m4_tabs.addTab(bank_page, "银行到账核对（试验）")
        v.addWidget(self.m4_tabs,1)
        return page

    def _cal_page(self) -> QWidget:
        page = QWidget(); v = QVBoxLayout(page)
        v.addLayout(self._page_title("审核学习闭环 / 人工校准与样本", "人工校准与样本", "同屏核对原票、ERP、台账、OCR 和人工正确值。保存后只重判本单；不会自动训练或改变全局规则。"))
        bar = QHBoxLayout()
        self.cal_claim = QComboBox(); self.cal_claim.currentIndexChanged.connect(self.refresh_calibration_lines); bar.addWidget(QLabel("报销单")); bar.addWidget(self.cal_claim, 2)
        self.cal_line = QComboBox(); self.cal_line.currentIndexChanged.connect(self.render_calibration); bar.addWidget(QLabel("费用行 / 原票")); bar.addWidget(self.cal_line, 2)
        self.cal_type = QComboBox(); self.cal_type.addItems(["OCR 字段纠正", "审核结论改判"]); bar.addWidget(QLabel("校准类型")); bar.addWidget(self.cal_type)
        self.cal_sample_count = QLabel(); bar.addWidget(self.cal_sample_count); v.addLayout(bar)
        split = QSplitter(Qt.Horizontal)
        left = QWidget(); lv = QVBoxLayout(left); self.cal_invoice_title = QLabel("请选择一张原票"); self.cal_invoice_title.setObjectName("panelTitle"); lv.addWidget(self.cal_invoice_title)
        lv.addWidget(QLabel("单击图片打开原始分辨率；按住图片拖动可横向/纵向查看。", objectName="hint"))
        self.cal_scroll = QScrollArea(); self.cal_scroll.setWidgetResizable(False); self.cal_image = PanImageLabel(); self.cal_image.clicked.connect(self.zoom_calibration_ticket); self.cal_image.setText("先在费用报销单打开并自动初审，再到此处校准。"); self.cal_image.setAlignment(Qt.AlignCenter); self.cal_image.setMinimumSize(460, 520); self.cal_image.setObjectName("ticketPreview"); self.cal_scroll.setWidget(self.cal_image); lv.addWidget(self.cal_scroll, 1); split.addWidget(left)
        right = QWidget(); rv = QVBoxLayout(right); rv.addWidget(QLabel("字段对照与人工正确值", objectName="panelTitle"))
        self.cal_compare = QTableWidget(8, 5); self.cal_compare.setHorizontalHeaderLabels(["字段", "ERP 手填", "发票台账", "OCR 识别", "人工正确值"])
        fields = ["发票代码", "发票号码", "开票日期", "购买方名称", "购买方税号", "销售方名称", "价税合计（元）", "税率"]
        for row, label in enumerate(fields):
            name = QTableWidgetItem(label); name.setFlags(name.flags() & ~Qt.ItemIsEditable); self.cal_compare.setItem(row, 0, name)
            for col in (1, 2, 3):
                cell = QTableWidgetItem("—"); cell.setFlags(cell.flags() & ~Qt.ItemIsEditable); self.cal_compare.setItem(row, col, cell)
            self.cal_compare.setItem(row, 4, QTableWidgetItem(""))
        rv.addWidget(self.cal_compare, 1)
        form = QFormLayout(); self.cal_note = QLineEdit(); self.cal_note.setPlaceholderText("必填：依据原票确认、台账核对、人工改判原因等"); self.cal_reason = QComboBox(); self.cal_reason.addItems(["按固定规则重新判责", "人工改判为通过（需二次确认）", "人工改判为驳回（需二次确认）", "暂存待补充"]); form.addRow("校准依据 / 改判原因", self.cal_note); form.addRow("校准后动作", self.cal_reason); rv.addLayout(form)
        buttons = QHBoxLayout(); save = QPushButton("保存校准并重新判责本单"); save.clicked.connect(self.save_calibration); log = QPushButton("刷新历史校准记录"); log.clicked.connect(self.render_calibration_history); buttons.addWidget(save); buttons.addWidget(log); buttons.addStretch(); rv.addLayout(buttons)
        rv.addWidget(QLabel("历史校准记录（自动加载，不覆盖旧记录）", objectName="panelTitle"))
        self.cal_history = QTableWidget(0, 5); self.cal_history.setHorizontalHeaderLabels(["时间", "费用行", "校准类型", "修改内容 / 结论", "依据 / 操作人"]); self.cal_history.setEditTriggers(QTableWidget.NoEditTriggers); self.cal_history.setSelectionBehavior(QTableWidget.SelectRows); self.cal_history.setMaximumHeight(190); rv.addWidget(self.cal_history)
        self.cal_status = QTextEdit(); self.cal_status.setReadOnly(True); self.cal_status.setMaximumHeight(105); rv.addWidget(self.cal_status); split.addWidget(right); split.setSizes([560, 760]); v.addWidget(split, 1)
        return page

    def _quality_page(self) -> QWidget:
        page=QWidget(); v=QVBoxLayout(page)
        v.addLayout(self._page_title(
            "审核学习闭环 / 质量与效率看板", "质量与效率看板",
            "用于验收系统是否判得准、业务运行是否有效：展示公开30单、OCR人工真值、300单处理效率、规则版本和内部异常覆盖；不处理具体报销单。"
        ))
        bar=QHBoxLayout()
        refresh=QPushButton("刷新验收指标"); refresh.clicked.connect(self.refresh_quality_metrics)
        export=QPushButton("导出验收与运营报告"); export.clicked.connect(self.export_quality_report)
        matrix=QPushButton("导出108类覆盖矩阵"); matrix.clicked.connect(self.export_108_matrix)
        bar.addWidget(refresh); bar.addWidget(export); bar.addWidget(matrix); bar.addStretch(); v.addLayout(bar)
        self.quality_cards=QLabel(); self.quality_cards.setObjectName("cards"); self.quality_cards.setWordWrap(True); v.addWidget(self.quality_cards)
        split=QSplitter(Qt.Horizontal)
        self.quality_text=QTextEdit(); self.quality_text.setReadOnly(True); split.addWidget(self.quality_text)
        self.quality_fields=QTableWidget(0,5); self.quality_fields.setHorizontalHeaderLabels(["OCR字段","已标注","正确","错误/缺失","基线准确率"]); self.quality_fields.setEditTriggers(QTableWidget.NoEditTriggers); split.addWidget(self.quality_fields); split.setSizes([760,620]); v.addWidget(split,1)
        return page

    def _settings_page(self) -> QWidget:
        page = QWidget(); v = QVBoxLayout(page); v.addLayout(self._page_title("系统 / 系统设置与退出", "系统设置与退出", "API Key 不在主页展示；关闭窗口后工作台会留在右下角图标，需选择彻底退出才会结束进程。")); text = QLabel(f"当前 API 地址：{self.base_url}\nAPI Key：••••••••••••••••\n规则版本：{RULE_VERSION}\n\nM2：仅人工确认后可写审核意见；不会改变单据状态。\nM3：本地稽核任务，不回写 ERP。") ; text.setObjectName("settings"); v.addWidget(text)
        theme_row=QHBoxLayout(); theme_row.addWidget(QLabel("界面主题")); self.theme_combo=QComboBox(); self.theme_combo.addItems(["ERP 浅色（推荐）", "深蓝办公", "夜间高对比"]); self.theme_combo.currentTextChanged.connect(self._theme_changed); theme_row.addWidget(self.theme_combo, 1); theme_row.addWidget(QLabel("主题只改变显示，不改变业务规则或数据。"), 2); v.addLayout(theme_row)
        quit = QPushButton("退出并彻底结束程序"); quit.clicked.connect(self.exit_application); v.addWidget(quit); v.addStretch(); return page

    def _public_labels(self) -> dict[str, Any]:
        try: return json.loads(PUBLIC_LABELS.read_text(encoding="utf-8"))
        except Exception: return {"claims": {}, "meta": {}}

    def _load_public_report(self) -> None:
        try:
            report = json.loads(PUBLIC_REPORT.read_text(encoding="utf-8"))
        except Exception:
            report = {"rows": []}
        self.public_rows = report.get("rows", [])
        self.render_public_report(report)

    def render_public_report(self, report: dict[str, Any]) -> None:
        rows = report.get("rows", [])
        self.public_rows = rows
        self.public_table.setRowCount(len(rows))
        for r, item in enumerate(rows):
            values = [
                item.get("claimNo", "—"), result_label(item.get("expectedVerdict")), result_label(item.get("actualVerdict")),
                "、".join(item.get("expectedCodes", [])) or "无", "、".join(item.get("actualCodes", [])) or "无",
                "一致" if item.get("verdictMatch") else "不一致", "一致" if item.get("codeMatch") else "不一致",
            ]
            for c, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if c in (5, 6):
                    cell.setForeground(QColor("#278455" if value == "一致" else "#c8443c"))
                self.public_table.setItem(r, c, cell)
        total = report.get("total", len(rows))
        verdict = report.get("verdictMatches", sum(bool(x.get("verdictMatch")) for x in rows))
        codes = report.get("codeMatches", sum(bool(x.get("codeMatch")) for x in rows))
        self.public_summary.setText(f"公开答案只读｜结论 {verdict}/{total}｜违规代码 {codes}/{total}")
        if rows: self.public_table.selectRow(0)

    def show_public_detail(self) -> None:
        row = self.public_table.currentRow()
        if row < 0 or row >= len(getattr(self, "public_rows", [])): return
        item = self.public_rows[row]
        self.public_detail.setPlainText(
            f"报销单：{item.get('claimNo', '—')}\n"
            f"官方答案：{result_label(item.get('expectedVerdict'))}｜{', '.join(item.get('expectedCodes', [])) or '无违规代码'}\n"
            f"系统结果：{result_label(item.get('actualVerdict'))}｜{', '.join(item.get('actualCodes', [])) or '无违规代码'}\n"
            f"系统解释：{'；'.join(item.get('reasons', [])) or '—'}\n"
            f"验收结果：结论{'一致' if item.get('verdictMatch') else '不一致'}，违规代码{'一致' if item.get('codeMatch') else '不一致'}。\n\n"
            "说明：官方标签是不可修改的评测基准；人工 OCR 校准样本不会覆盖官方答案。"
        )

    def run_public_30(self) -> None:
        labels = self._public_labels().get("claims", {})
        targets = [(claim_id, item) for claim_id, item in labels.items()]
        if not targets:
            QMessageBox.warning(self, "公开 30 单验收", "未找到内置官方公开答案文件。"); return
        self.public_progress.setVisible(True); self.public_progress.setRange(0, len(targets)); self.public_progress.setValue(0)
        self.public_summary.setText("正在运行公开 30 单只读验收…")
        sig = Signals(); self._public_signal = sig
        results: list[dict[str, Any]] = []
        def progress(current: int, total: int, claim_no: str) -> None:
            self.public_progress.setValue(current); self.public_summary.setText(f"正在验收 {current}/{total}：{claim_no}")
        def finished(report: dict[str, Any]) -> None:
            self.public_progress.setVisible(False); self.render_public_report(report)
        def failed(message: str) -> None:
            self.public_progress.setVisible(False); self.public_summary.setText("验收失败：" + message)
        sig.progress.connect(progress); sig.done.connect(finished); sig.error.connect(failed)
        def work() -> None:
            try:
                for index, (claim_id, expected) in enumerate(targets, 1):
                    summary = {"id": claim_id, "claimNo": expected.get("claimNo")}
                    packet = analyze_one(self.client, summary=summary)
                    result = packet.get("result", {})
                    codes = sorted({str(x.get("code")) for x in result.get("violations", []) if isinstance(x, dict) and x.get("code")})
                    expected_codes = sorted(expected.get("violations", []))
                    row = {
                        "index": index, "claimId": claim_id, "claimNo": expected.get("claimNo"),
                        "expectedVerdict": expected.get("expectedVerdict"), "expectedCodes": expected_codes,
                        "actualVerdict": result.get("result"), "actualCodes": codes,
                        "reasons": result.get("reasons", []),
                        "verdictMatch": result.get("result") == expected.get("expectedVerdict"),
                        "codeMatch": codes == expected_codes,
                    }
                    results.append(row); sig.progress.emit(index, len(targets), expected.get("claimNo", claim_id))
                report = {
                    "total": len(results),
                    "verdictMatches": sum(x["verdictMatch"] for x in results),
                    "codeMatches": sum(x["codeMatch"] for x in results),
                    "differences": [x for x in results if not x["verdictMatch"] or not x["codeMatch"]],
                    "rows": results,
                }
                out = DATA_DIR / "public-30-latest.json"; out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                sig.done.emit(report)
            except Exception as exc: sig.error.emit(str(exc))
        threading.Thread(target=work, daemon=True).start()

    def show_page(self, name: str) -> None:
        self.pages.setCurrentIndex(self.order[name]);
        for key, button in self.nav.items(): button.setChecked(key == name)
        if name == "write": self.render_writeback()
        if name == "cal": self.refresh_calibration_choices()
        if name == "quality": self.refresh_quality_metrics()
        if name == "ledger" and not self.invoices: self.load_ledger()

    def _run(self, fn: Callable[[], Any], done: Callable[[Any], None]) -> None:
        sig = Signals(); self._last_signal = sig; sig.done.connect(done); sig.error.connect(lambda e: QMessageBox.warning(self, "操作失败", e))
        def work() -> None:
            try: sig.done.emit(fn())
            except Exception as exc: sig.error.emit(str(exc))
        threading.Thread(target=work, daemon=True).start()

    def load_claims(self) -> None:
        self.refresh.setEnabled(False)
        def work() -> list[dict[str, Any]]: return list(self.client.iter_expense_claims("PENDING"))
        def done(rows: list[dict[str, Any]]) -> None:
            self.claims = rows
            pending_ids = {str(item.get("id")) for item in rows}
            archived = 0
            for claim_id, item in self.store.data["manual"].items():
                if claim_id not in pending_ids and self._workflow_state(claim_id) in {"CONFIRMED", "WRITTEN", "RETURNED", "RECONFIRM", "HOLD"}:
                    item["workflowState"] = "ARCHIVED"
                    item["writeState"] = "ERP 已完成 / 已归档"
                    item["archivedAt"] = datetime.now().isoformat(timespec="seconds")
                    archived += 1
            if archived:
                self.store.save()
            self.refresh.setEnabled(True)
            self.render_queue()
            self.render_writeback()
            self.refresh_calibration_choices()
        self._run(work, done)

    def _outcome_for(self, claim: dict[str, Any]) -> str | None:
        packet = self.outcomes.get(claim["id"], {})
        result = packet.get("result") or {}
        return result.get("result") if isinstance(result, dict) else None

    def _workflow_state(self, claim_id: str) -> str:
        """Return the local human workflow state while preserving legacy data."""
        item = self.store.data["manual"].get(claim_id, {})
        explicit = item.get("workflowState")
        if explicit:
            return str(explicit)
        if item.get("decision") in {"APPROVE", "REJECT"}:
            return "WRITTEN" if str(item.get("writeState", "")).startswith("已写回") else "CONFIRMED"
        if item.get("decision") == "FLAG":
            return "HOLD"
        return "PENDING"

    def _queue_status(self, claim: dict[str, Any]) -> str:
        state = self._workflow_state(claim["id"])
        if state == "RECONFIRM":
            return "校准后待二次确认"
        if state == "RETURNED":
            return "写回退回 / 待二次确认"
        if state == "HOLD":
            return "暂存待补充"
        if self.runtime_status.get(claim["id"]) == "RUNNING":
            return "正在初审"
        return result_label(self._outcome_for(claim))

    def _candidate_decision(self, claim_id: str) -> str | None:
        item = self.store.data["manual"].get(claim_id, {})
        if self._workflow_state(claim_id) in {"RECONFIRM", "RETURNED"} and item.get("overrideDecision") in {"APPROVE", "REJECT"}:
            return str(item["overrideDecision"])
        packet = self.outcomes.get(claim_id, {})
        result = packet.get("result") or {}
        return result.get("result") if isinstance(result, dict) else None

    def _evidence_status(self, claim: dict[str, Any]) -> str:
        claim_id = claim["id"]
        state = self._workflow_state(claim_id)
        if state == "RECONFIRM": return "已重判 · 待二次确认"
        if state == "RETURNED": return "保留历史 · 待再次确认"
        if state == "HOLD": return "待补票 / 补材料"
        runtime = self.runtime_status.get(claim_id)
        if runtime == "RUNNING": return "正在读取原票"
        if runtime == "FAILED": return "读取失败 · 待重试"
        result = self._outcome_for(claim)
        packet = self.outcomes.get(claim_id, {})
        review = packet.get("result") or {}
        evidence_text = " ".join(
            [str(x) for x in review.get("reasons", [])]
            + [json.dumps(x, ensure_ascii=False, default=str) for x in (review.get("evidenceChain") or review.get("cards") or [])]
        )
        if result == "FLAG":
            if any(word in evidence_text for word in ("下载失败", "接口", "损坏", "无法打开", "技术")):
                return "技术失败 · 待重试"
            if any(word in evidence_text for word in ("冲突", "不一致")):
                return "证据冲突 · 人工复核"
            return "证据不足 · 人工复核"
        if result == "REJECT": return "证据充分 · 明确异常"
        if result == "APPROVE": return "证据充分 · 无明确异常"
        return "待初审"

    def _analysis_signature(self, claim: dict[str, Any], approvals: list[dict[str, Any]]) -> str:
        lines = []
        for line in claim.get("lines", []):
            invoice, attachment = line.get("invoice") or {}, line.get("attachment") or {}
            lines.append({
                "id": line.get("id"), "amountFen": line.get("amountFen"), "description": line.get("description"),
                "expenseType": line.get("expenseType"), "invoice": invoice,
                "attachment": {k: attachment.get(k) for k in ("id", "fileName", "size", "uploadedAt", "updatedAt")},
            })
        raw = {
            "claim": {k: claim.get(k) for k in ("id", "claimNo", "updatedAt", "totalFen", "jobLevel", "departmentName")},
            "lines": lines,
            "approvals": approvals,
            "rules": RULE_VERSION,
            "calibrationRevision": len(self.store.data.get("calibrations", {}).get(claim.get("id"), [])),
        }
        return hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def render_queue(self) -> None:
        wanted = self.m2_filter.currentText(); query = self.search.text().strip()
        # Confirmed/writeback claims leave M2. Returned, calibrated and held claims
        # remain visible until a human confirms the current candidate conclusion.
        rows = [c for c in self.claims if self._workflow_state(c["id"]) not in {"CONFIRMED", "WRITTEN"}]
        if wanted == "风险待处理（默认）":
            rows = [c for c in rows if self._workflow_state(c["id"]) in {"RECONFIRM", "RETURNED", "HOLD"} or self._outcome_for(c) in {None, "FLAG", "REJECT"}]
        elif wanted == "校准后待二次确认":
            rows = [c for c in rows if self._workflow_state(c["id"]) == "RECONFIRM"]
        elif wanted == "写回退回 / 待二次确认":
            rows = [c for c in rows if self._workflow_state(c["id"]) == "RETURNED"]
        elif wanted == "暂存待补充":
            rows = [c for c in rows if self._workflow_state(c["id"]) == "HOLD"]
        elif wanted == "待初审":
            rows = [c for c in rows if self._outcome_for(c) is None]
        elif wanted != "全部待审":
            rows = [c for c in rows if result_label(self._outcome_for(c)) == wanted]
        if query: rows = [c for c in rows if query.lower() in str(c.get("claimNo", "")).lower() or query in str(c.get("employeeName") or (c.get("employee") or {}).get("name", ""))]
        state_rank = {"RETURNED": 0, "RECONFIRM": 1, "HOLD": 2}
        verdict_rank = {"FLAG": 3, "REJECT": 4, None: 5, "APPROVE": 6}
        rows.sort(key=lambda x: state_rank.get(self._workflow_state(x["id"]), verdict_rank.get(self._outcome_for(x), 6))); self._visible = rows
        reconfirm = sum(self._workflow_state(c["id"]) in {"RECONFIRM", "RETURNED"} for c in rows)
        counts = {x: sum(self._outcome_for(c) == x for c in rows) for x in ("FLAG", "REJECT", "APPROVE")}
        self.cards.setText(f"待审 {len(rows)}    待二次确认 {reconfirm}    待人工复核 {counts['FLAG']}    建议驳回 {counts['REJECT']}    建议通过 {counts['APPROVE']}")
        selected_id = self.current_id
        self.queue.blockSignals(True)
        self.queue.setRowCount(len(rows))
        for r,c in enumerate(rows):
            employee_name = claim_employee_name(c)
            department_name = claim_department_name(c)
            verdict = self._outcome_for(c)
            label = self._queue_status(c)
            values = [label, self._evidence_status(c), c.get("claimNo", ""), employee_name, department_name, f"¥{claim_total_yuan(c):,.2f}"]
            workflow = self._workflow_state(c["id"])
            color = "#2463a0" if self.runtime_status.get(c["id"]) == "RUNNING" else ("#7b3fa1" if workflow in {"RECONFIRM", "RETURNED"} else ("#a66a08" if workflow == "HOLD" else status_color(verdict)))
            for col,value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col in (0, 1):
                    item.setForeground(QColor(color))
                self.queue.setItem(r, col, item)
        self.queue.blockSignals(False)
        if selected_id:
            for row, claim in enumerate(rows):
                if claim["id"] == selected_id:
                    self.queue.selectRow(row); break

    def select_claim(self) -> None:
        row = self.queue.currentRow()
        if row < 0 or row >= len(getattr(self, "_visible", [])): return
        c = self._visible[row]
        if self.current_id == c["id"] and self.runtime_status.get(c["id"]) == "RUNNING":
            return
        self.current_id = c["id"]
        state = self._workflow_state(c["id"])
        if hasattr(self, "confirm_ai_button"):
            self.confirm_ai_button.setText("确认校准后结论" if state in {"RECONFIRM", "RETURNED"} else "确认 AI 建议")
        self._selection_token += 1
        token = self._selection_token
        self.runtime_status[c["id"]] = "RUNNING"
        self.detail.setPlainText(f"报销单 {c.get('claimNo')}\n报销人：{claim_employee_name(c)}    部门：{claim_department_name(c)}\n总额：{claim_total_yuan(c):,.2f} 元\n\n正在读取完整单据与审批记录…")
        self.evidence.setText("正在自动初审：读取单据 → 审批/特批 → 原票/OCR → 发票台账 → 固定规则判责。")
        self.render_queue()
        def work() -> dict[str, Any]:
            claim = self.client.get_claim(c["id"]); approvals = self.client.approvals_for_claim(c["id"]).get("data", [])
            return {"claim": claim, "approvals": approvals, "tickets": [], "summary": c, "token": token}
        self._run(work, self._claim_loaded)

    def _claim_loaded(self, payload: dict[str, Any]) -> None:
        claim = payload["claim"]; claim_id = claim["id"]
        self.claim_payloads[claim_id] = payload
        if claim_id == self.current_id:
            self.show_claim(payload)
        signature = self._analysis_signature(claim, payload["approvals"])
        # A queue selection is a fresh ERP read, not a replay of a local cache.
        # Historical analysisCache is retained only for audit/learning reference;
        # it must never replace the current attachment, ledger, approval or OCR
        # evidence used to form a new recommendation.
        self._start_analysis(payload["summary"], claim_id, payload["token"], signature)

    def _ocr_overrides(self, claim_id: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for item in self.store.data["calibrations"].get(claim_id, []):
            if item.get("type") == "OCR 字段纠正" and item.get("lineId"):
                result.setdefault(item["lineId"], {}).update({k: v for k, v in item.get("overrides", {}).items() if v is not None})
        return result

    def _start_analysis(self, summary: dict[str, Any], claim_id: str, token: int, signature: str) -> None:
        started_at = time.monotonic()
        def done(value: dict[str, Any]) -> None:
            value["durationSeconds"] = round(time.monotonic() - started_at, 3)
            self.outcomes[claim_id] = value
            self.store.data["analysisCache"][claim_id] = value
            self.store.data["analysisSignatures"][claim_id] = signature
            if self._workflow_state(claim_id) == "HOLD":
                current = self.store.data["manual"].get(claim_id, {})
                self._merge_manual(
                    claim_id,
                    decision=None,
                    overrideDecision=None,
                    ai=(value.get("result") or {}).get("result"),
                    aiReasons=list((value.get("result") or {}).get("reasons", [])),
                    workflowState="RECONFIRM",
                    writeState="校准后待二次确认",
                    reason=current.get("reason") or "补充材料后已重新初审，待二次确认。",
                )
            self.store.save()
            self.runtime_status.pop(claim_id, None)
            if claim_id == self.current_id and token == self._selection_token:
                self.current_claim = value.get("claim") or self.current_claim
                self._set_tickets_from_outcome(value)
                self._show_outcome(value)
            self.render_queue()
            self.refresh_quality_metrics()
        def failed(message: str) -> None:
            self.runtime_status[claim_id] = "FAILED"
            if claim_id == self.current_id and token == self._selection_token:
                self.evidence.setPlainText(f"自动初审失败：{message}\n\n请检查 ERP 连接或原票附件，然后点击“重新初审本单”。")
            self.render_queue()
        sig = Signals(); self._analysis_signal = sig; sig.done.connect(done); sig.error.connect(failed)
        def work() -> None:
            try: sig.done.emit(analyze_one(self.client, summary=summary, ocr_overrides=self._ocr_overrides(claim_id)))
            except Exception as exc: sig.error.emit(str(exc))
        threading.Thread(target=work, daemon=True).start()

    def show_claim(self, payload: dict[str, Any]) -> None:
        claim, approvals = payload["claim"], payload["approvals"]; self.current_claim = claim; self.tickets=payload.get("tickets", []); self.ticket_index=0
        trip=claim.get("trip") or {}; destination=trip.get("destination") or trip.get("location") or trip.get("city") or claim.get("destination") or "—"; start=trip.get("startDate") or trip.get("start") or trip.get("startOn") or "—"; end=trip.get("endDate") or trip.get("end") or trip.get("endOn") or "—"
        lines = claim.get("lines", []); out = [f"报销单 {claim.get('claimNo')}", f"报销人：{claim_employee_name(claim)}    部门：{claim_department_name(claim)}", f"事由：{claim.get('title') or claim.get('purpose') or '—'}", f"出差地点：{destination}    出差起止：{start} ~ {end}", f"总额：{claim_total_yuan(claim):,.2f} 元", "", "费用明细："]
        for x in lines:
            gl=x.get('glAccount') or {}; invoice=x.get('invoice') or {}; out.append(f"{x.get('lineNo')}｜{expense_label(x.get('expenseType'))}｜{x.get('description') or '—'}｜{gl.get('code','')} {gl.get('name','')}｜申报 {(x.get('amountFen',0) or 0)/100:,.2f} 元｜票据 {invoice.get('invoiceCode','—')} / {invoice.get('invoiceNo','—')}")
        self.detail.setPlainText("\n".join(out[:5]))
        self.expense_lines.setRowCount(len(lines))
        for row, x in enumerate(lines):
            gl=x.get('glAccount') or {}; invoice=x.get('invoice') or {}
            values=[x.get('lineNo','—'), expense_label(x.get('expenseType')), x.get('description') or '—', f"{gl.get('code','')} {gl.get('name','')}", f"¥{(x.get('amountFen',0) or 0)/100:,.2f}", f"{invoice.get('invoiceCode','—')} / {invoice.get('invoiceNo','—')}"]
            for col, value in enumerate(values): self.expense_lines.setItem(row, col, QTableWidgetItem(str(value)))
        approval_lines=[f"[{approval_label(x.get('action'))}] {x.get('approverName','—')} · {x.get('actedAt','—')} · {x.get('comment') or '无备注'}" for x in approvals]
        self.approval_detail.setPlainText("\n".join(approval_lines) or "未取得审批记录")
        outcome = self.outcomes.get(claim["id"])
        self.show_ticket(0)
        if outcome: self._show_outcome(outcome)
        else: self.evidence.setText("正在自动初审：只读获取原票、OCR、台账与审批记录，并生成固定规则结论。")

    def analyze_current(self) -> None:
        if not self.current_id: return
        summary = next((x for x in self.claims if x["id"] == self.current_id), None)
        if not summary: return
        self.store.data["analysisSignatures"].pop(self.current_id, None)
        self.runtime_status[self.current_id] = "RUNNING"
        self._selection_token += 1
        token = self._selection_token
        self.evidence.setText("正在强制重新初审：读取原票、OCR、台账与审批记录…")
        self.render_queue()
        payload = self.claim_payloads.get(self.current_id)
        if payload:
            signature = self._analysis_signature(payload["claim"], payload["approvals"])
            self._start_analysis(summary, self.current_id, token, signature)
        else:
            claim_id = self.current_id
            def loaded(payload: dict[str, Any]) -> None:
                self.claim_payloads[claim_id] = payload
                if claim_id == self.current_id: self.show_claim(payload)
                self._start_analysis(summary, claim_id, token, self._analysis_signature(payload["claim"], payload["approvals"]))
            self._run(lambda: {"claim": self.client.get_claim(claim_id), "approvals": self.client.approvals_for_claim(claim_id).get("data", []), "tickets": []}, loaded)

    def _set_tickets_from_outcome(self, outcome: dict[str, Any]) -> None:
        lines = {line.get("id"): line for line in (outcome.get("claim") or {}).get("lines", [])}
        tickets = []
        for item in outcome.get("attachments", []):
            path = item.get("image")
            if not path: continue
            line = lines.get(item.get("lineId"), {})
            tickets.append({"lineNo": line.get("lineNo"), "type": line.get("expenseType"), "amount": (line.get("amountFen") or 0) / 100, "path": str(path), "lineId": item.get("lineId")})
        self.tickets = tickets
        self.ticket_index = 0
        self.show_ticket(0)

    def batch_review_all(self) -> None:
        """Run the same evidence-backed M2 review for every pending claim.

        This deliberately performs GET/OCR work only.  It cannot create a review
        in ERP; human confirmation and the writeback pre-check remain a separate
        later step.
        """
        targets = [item for item in self.claims if self._workflow_state(item["id"]) not in {"CONFIRMED", "WRITTEN"}]
        if not targets:
            QMessageBox.information(self, "一键审核全部", "当前没有待人工处理的报销单。"); return
        answer = QMessageBox.question(self, "确认一键审核", f"将逐张读取 {len(targets)} 张待审报销单、审批记录和原票，并在本地执行 OCR 与规则初审。\n\n本操作只调用读取接口，不会写回 ERP；完成后请优先复核异常与待人工复核单据。是否继续？", QMessageBox.Yes | QMessageBox.No)
        if answer != QMessageBox.Yes: return
        self.batch.setEnabled(False); self.one.setEnabled(False); self.batch_progress.setVisible(True); self.batch_progress.setRange(0, len(targets)); self.batch_progress.setValue(0)
        sig = Signals(); self._batch_signal = sig; started = time.monotonic()
        def update(current: int, total: int, claim_no: str) -> None:
            self.batch_progress.setValue(current)
            elapsed = max(time.monotonic() - started, 0.1); remaining = int(elapsed / current * (total-current)) if current else 0
            self.cards.setText(f"正在一键审核 {current}/{total}：{claim_no}（只读；预计剩余 {remaining//60} 分 {remaining%60} 秒）")
        def item_done(claim_id: str, value: object) -> None:
            if isinstance(value, dict):
                self.outcomes[claim_id] = value
                self.store.data["analysisCache"][claim_id] = value
                claim = value.get("claim") or {}
                approvals = (value.get("approvals") or {}).get("data", [])
                if claim:
                    self.store.data["analysisSignatures"][claim_id] = self._analysis_signature(claim, approvals)
                self.store.save()
                if claim_id == self.current_id:
                    self._set_tickets_from_outcome(value); self._show_outcome(value)
                self.render_queue()
        def completed(results: list[tuple[str, object]]) -> None:
            failed = sum(1 for _, value in results if isinstance(value, Exception))
            self.batch.setEnabled(True); self.one.setEnabled(True); self.batch_progress.setVisible(False); self.render_queue()
            QMessageBox.information(self, "一键审核完成", f"已完成 {len(results)-failed} 张，失败 {failed} 张。请优先处理“待人工复核”和“建议驳回”。")
        def failed(message: str) -> None:
            self.batch.setEnabled(True); self.one.setEnabled(True); self.batch_progress.setVisible(False); QMessageBox.warning(self, "一键审核失败", message)
        sig.progress.connect(update); sig.item_done.connect(item_done); sig.done.connect(completed); sig.error.connect(failed)
        def work() -> None:
            try:
                results: list[tuple[str, object]] = []
                with ThreadPoolExecutor(max_workers=3, thread_name_prefix="qiheng-review") as pool:
                    # Reuse the same saved human-confirmed fields as single
                    # review and calibration re-review.  Without this, a batch
                    # run could overwrite a corrected claim with stale OCR.
                    futures = {
                        pool.submit(
                            analyze_one,
                            self.client,
                            summary=item,
                            ocr_overrides=self._ocr_overrides(item["id"]),
                        ): item
                        for item in targets
                    }
                    for index, future in enumerate(as_completed(futures), 1):
                        item = futures[future]
                        try: value: object = future.result()
                        except Exception as exc: value = exc
                        results.append((item["id"], value)); sig.item_done.emit(item["id"], value); sig.progress.emit(index, len(targets), item.get("claimNo", item["id"]))
                sig.done.emit(results)
            except Exception as exc: sig.error.emit(str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _show_outcome(self, value: dict[str, Any]) -> None:
        result = value.get("result", {}); cards = result.get("evidenceChain") or result.get("cards", [])
        state = self._workflow_state(self.current_id) if self.current_id else "PENDING"
        stage = "二审建议" if state in {"RECONFIRM", "RETURNED"} else "初审建议"
        current_claim = next((c for c in self.claims if c.get("id") == self.current_id), {"id": self.current_id}) if self.current_id else {}
        text = [
            f"{stage}：{result_label(result.get('result'))}",
            f"证据状态：{self._evidence_status(current_claim) if self.current_id else '—'}",
            f"规则版本：{RULE_VERSION}",
            "",
            f"{stage}依据与纠正建议：",
        ]
        if not cards:
            reasons = result.get("reasons", [])
            text.extend([f"• {reason}" for reason in reasons] or ["• 未生成结构化证据卡；该单不得直接作为驳回依据，请转人工复核。"])
        for card in cards:
            title = card.get("title") or card.get("ruleCode") or "审核事项"
            text += [f"\n【{title}】", f"内部诊断：{card.get('ruleCode') or card.get('code') or '按官方规则映射'}", f"问题：{card.get('summary') or card.get('detail') or '—'}", f"制度依据：{card.get('policy') or card.get('basis') or '—'}", f"原始证据：{card.get('source') or '请查看关联原票、审批记录或台账'}"]
            if card.get("actual") or card.get("limit") or card.get("difference"):
                text.append(f"实际值：{card.get('actual') or '—'}\n正确口径：{card.get('limit') or '—'}\n差额/影响：{card.get('difference') or '—'}")
            if card.get("correction"): text.append(f"纠正建议：{card['correction']}")
            if card.get("nextAction"): text.append(f"下一步：{card['nextAction']}")
        text.append("\n证据优先级：原票 > 已确认同票台账 > ERP 字段 > OCR > 模型建议。")
        if self.current_id:
            if state == "RECONFIRM":
                text.append(f"\n当前流程：人工真值已保存并完成整单重判；上方为当前{stage}。请再次核对原票和新结论，然后点击“确认校准后结论”。")
            elif state == "RETURNED":
                text.append("\n当前流程：该单由批量写回区退回，历史写回与校准记录均已保留；请复核后再次确认。")
            elif state == "HOLD":
                text.append("\n当前流程：暂存待补充；补齐原票或审批证据后请重新初审。")
        self.evidence.setPlainText("\n".join(text))

    def show_ticket(self, direction: int = 0) -> None:
        tickets=getattr(self, "tickets", [])
        if not tickets:
            self.ticket_title.setText("原票：当前单据没有可预览附件"); return
        if direction:
            self.ticket_index=(getattr(self,"ticket_index",0)+direction)%len(tickets)
        item=tickets[getattr(self,"ticket_index",0)]
        self.ticket_title.setText(f"原票 {self.ticket_index+1}/{len(tickets)}：费用行 {item.get('lineNo')} · {expense_label(item.get('type'))} · ¥{item.get('amount')}")
        pix=QPixmap(str(item.get("path", "")))
        if pix.isNull():
            self.ticket_preview.setPixmap(QPixmap())
            self.ticket_preview.setText("该附件不是可预览图片\n点击“放大查看原票”打开原始文件")
        else:
            self.ticket_preview.setText("")
            self.ticket_preview.setPixmap(pix.scaled(430, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def select_ticket_from_line(self) -> None:
        row = self.expense_lines.currentRow()
        if row < 0: return
        line_no = self.expense_lines.item(row, 0).text() if self.expense_lines.item(row, 0) else ""
        for index, ticket in enumerate(getattr(self, "tickets", [])):
            if str(ticket.get("lineNo")) == line_no:
                self.ticket_index = index; self.show_ticket(0); return

    def zoom_ticket(self) -> None:
        tickets=getattr(self, "tickets", [])
        if not tickets:
            QMessageBox.information(self,"原票", "当前单据没有可预览附件。"); return
        item=tickets[getattr(self,"ticket_index",0)]; path=Path(item["path"]); pix=QPixmap(str(path))
        if pix.isNull():
            if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                return
            QMessageBox.warning(self,"原票", "无法打开该附件："+str(path)); return
        dialog=QDialog(self); dialog.setWindowTitle(f"原票预览｜费用行 {item.get('lineNo')} · {expense_label(item.get('type'))}"); dialog.resize(min(max(pix.width()+40,720),1500),min(max(pix.height()+80,620),1000))
        layout=QVBoxLayout(dialog)
        note=QLabel(f"原始文件：{path.name}　按住图片可左右/上下拖动；滚动条也可定位。"); note.setObjectName("hint"); layout.addWidget(note)
        scroll=QScrollArea(); scroll.setWidgetResizable(False)
        image=PanImageLabel(); image.setAlignment(Qt.AlignCenter); image.setPixmap(pix); image.resize(pix.size()); scroll.setWidget(image); layout.addWidget(scroll,1)
        dialog.exec()

    def _merge_manual(self, claim_id: str, **changes: Any) -> dict[str, Any]:
        """Update workflow state without discarding calibration/write history."""
        item = dict(self.store.data["manual"].get(claim_id, {}))
        item.update(changes)
        self.store.data["manual"][claim_id] = item
        return item

    def _audit(self, module: str, action: str, ref_id: str, detail: str) -> None:
        self.store.data.setdefault("auditLog", []).append({
            "at": datetime.now().isoformat(timespec="seconds"),
            "operator": "周晓",
            "module": module,
            "action": action,
            "refId": ref_id,
            "detail": detail,
            "ruleVersion": RULE_VERSION,
        })

    def _finish_m2_and_stay(self, status_text: str) -> None:
        """Move the processed claim to writeback while keeping finance in M2."""
        next_row=max(self.queue.currentRow(), 0)
        self.current_id=None
        self.render_queue()
        self.render_writeback()
        self.write_status.setText(status_text)
        self.pages.setCurrentIndex(self.order["m2"])
        for key, button in self.nav.items():
            button.setChecked(key == "m2")
        if self._visible:
            self.queue.selectRow(min(next_row, len(self._visible)-1))
            self.select_claim()
        else:
            self.detail.setPlainText("当前筛选下的待审单据已处理完毕。可切换筛选条件，或进入“批量写回 ERP”进行预检。")
            self.evidence.setPlainText("已处理的单据已进入批量写回队列；尚未调用 POST。")
            self.ticket_preview.setText("暂无待审原票")

    def confirm_ai_decision(self) -> None:
        if not self.current_id:
            return
        decision = self._candidate_decision(self.current_id)
        if decision not in {"APPROVE", "REJECT"}:
            self.hold_current_claim()
            self.evidence.append("\n\n当前 AI 结论为“待人工复核”，证据尚不足，不能直接进入 ERP 写回。已暂存待补充，请先校准字段或补齐材料。")
            return
        result = self.outcomes.get(self.current_id, {}).get("result", {})
        source = result.get("result", "FLAG")
        existing = self.store.data["manual"].get(self.current_id, {})
        override = existing.get("overrideDecision")
        if override in {"APPROVE", "REJECT"}:
            reason = f"人工二次复核确认校准后的人工改判：{result_label(decision)}；AI 重判结果为{result_label(source)}。"
            decision_source = "HUMAN_OVERRIDE"
        else:
            reason = f"人工复核确认当前 AI 结论：{result_label(decision)}。"
            decision_source = "AI_CONFIRMED"
        self._merge_manual(
            self.current_id,
            decision=decision, ai=source, reason=reason, reviewer="周晓",
            aiReasons=list(result.get("reasons", [])),
            evidenceRun=self.outcomes.get(self.current_id, {}).get("run_dir", ""),
            at=datetime.now().isoformat(timespec="seconds"),
            workflowState="CONFIRMED", decisionSource=decision_source,
            writeState="待预检", conflictReason="",
        )
        self._audit("M2", "人工确认审核建议", self.current_id, reason)
        self.store.save()
        self._finish_m2_and_stay(f"已确认AI意见：{result_label(decision)}。该单已进入写回队列，尚未调用POST。")

    def finalize_current_claim(self, decision: str) -> None:
        """Record the finance user's explicit final decision without a popup."""
        if not self.current_id or decision not in {"APPROVE", "REJECT"}:
            return
        source = self.outcomes.get(self.current_id, {}).get("result", {})
        ai_decision = source.get("result") or "FLAG"
        state = self._workflow_state(self.current_id)
        stage = "二审" if state in {"RECONFIRM", "RETURNED"} else "初审"
        reason = (
            f"财务人工{stage}确认{result_label(decision)}；"
            f"当前规则建议为{result_label(ai_decision)}。原票、审批链和校准记录已保留。"
        )
        existing = self.store.data["manual"].get(self.current_id, {})
        self._merge_manual(
            self.current_id,
            decision=decision,
            overrideDecision=decision if decision != ai_decision else existing.get("overrideDecision"),
            ai=ai_decision,
            aiReasons=list(source.get("reasons", [])),
            reason=reason,
            reviewer="周晓",
            at=datetime.now().isoformat(timespec="seconds"),
            workflowState="CONFIRMED",
            decisionSource="HUMAN_FINAL",
            writeState="待预检",
            conflictReason="",
            writeHistory=existing.get("writeHistory", []),
        )
        self._audit("M2", f"财务{stage}最终决定", self.current_id, reason)
        self.store.save()
        self._finish_m2_and_stay(f"已{result_label(decision)}，该单已进入批量写回队列；尚未调用POST。")

    def open_current_calibration(self) -> None:
        if not self.current_id:
            return
        self.show_page("cal")
        index = self.cal_claim.findData(self.current_id)
        if index >= 0:
            self.cal_claim.setCurrentIndex(index)

    def hold_current_claim(self) -> None:
        if not self.current_id:
            return
        source = self._outcome_for(next((c for c in self.claims if c["id"] == self.current_id), {"id": self.current_id}))
        self._merge_manual(
            self.current_id, ai=source, decision=None, workflowState="HOLD",
            writeState="暂存待补充", reason="原票、审批或台账证据待补充，暂不进入写回。",
            reviewer="周晓", at=datetime.now().isoformat(timespec="seconds"),
        )
        self.store.save(); self.render_queue(); self.render_writeback()

    def render_writeback(self) -> None:
        checked = self.checked_write_ids()
        rows = []
        for claim_id, item in self.store.data["manual"].items():
            if item.get("decision") not in {"APPROVE", "REJECT"}:
                continue
            if self._workflow_state(claim_id) not in {"CONFIRMED", "WRITTEN"}:
                continue
            claim = next((x for x in self.claims if x["id"] == claim_id), {}); rows.append((claim, item))
        self._write_rows = rows
        if hasattr(self, "nav") and "write" in self.nav:
            self.nav["write"].setText(f"批量写回 ERP（{len(rows)}）")
        self.write_table.blockSignals(True)
        self.write_table.setRowCount(len(rows))
        for r,(claim,item) in enumerate(rows):
            claim_id = claim.get("id") or next((key for key, value in self.store.data["manual"].items() if value is item), "")
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Checked if claim_id in checked else Qt.Unchecked)
            check.setData(Qt.UserRole, claim_id)
            self.write_table.setItem(r, 0, check)
            state = item.get("writeState", "待预检")
            conflict = item.get("conflictReason", "")
            vals = [claim.get("claimNo", claim.get("id", "")), claim_employee_name(claim), result_label(item.get("ai")), "；".join(item.get("aiReasons", [])) or "—", result_label(item.get("decision")), "是", item.get("at", ""), state, conflict]
            for c,val in enumerate(vals, 1): self.write_table.setItem(r,c,QTableWidgetItem(str(val)))
        self.write_table.blockSignals(False)
        if rows and self.write_table.currentRow() < 0:
            self.write_table.selectRow(0)

    def checked_write_ids(self) -> set[str]:
        result: set[str] = set()
        if not hasattr(self, "write_table"): return result
        for row in range(self.write_table.rowCount()):
            item = self.write_table.item(row, 0)
            if item and item.checkState() == Qt.Checked and item.data(Qt.UserRole):
                result.add(str(item.data(Qt.UserRole)))
        return result

    def set_all_write_checks(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.write_table.rowCount()):
            item = self.write_table.item(row, 0)
            if item: item.setCheckState(state)
        self.write_status.setText(f"已选择 {len(self.checked_write_ids())} 条单据")

    def selected_write_targets(self) -> list[tuple[str, dict[str, Any]]]:
        selected = self.checked_write_ids()
        return [(claim_id, item) for claim_id, item in self.store.data["manual"].items() if claim_id in selected and item.get("decision") in {"APPROVE", "REJECT"}]

    def show_writeback_detail(self) -> None:
        row = self.write_table.currentRow()
        if row < 0 or row >= len(getattr(self, "_write_rows", [])): return
        claim, item = self._write_rows[row]
        detail = item.get("conflictReason") or "尚未预检。点击“写回前预检”后，系统会读取 ERP 已有审核意见并逐条比对。"
        history = item.get("writeHistory", [])
        history_text = "\n".join(
            f"• {entry.get('at', '—')}｜{result_label(entry.get('decision'))}｜{entry.get('preWriteState', '—')}"
            for entry in history
        ) or "• 尚无实际写回记录"
        self.write_detail.setPlainText(
            f"关联单据：{claim.get('claimNo', claim.get('id', '—'))}\n"
            f"AI 初审意见：{result_label(item.get('ai'))}\n"
            f"人工最终意见：{result_label(item.get('decision'))}\n"
            f"本次人工确认说明：{item.get('reason', '—')}\n"
            f"当前回写状态：{item.get('writeState', '待预检')}\n\n"
            f"预检详情：{detail}\n\n"
            f"历史写回记录（只追加、不覆盖）：\n{history_text}"
        )

    def return_selected_to_review(self) -> None:
        targets = self.selected_write_targets()
        if not targets:
            QMessageBox.information(self, "退回待复核", "请先勾选至少一张需要退回 M2 的单据。")
            return
        for claim_id, item in targets:
            already_written = self._workflow_state(claim_id) == "WRITTEN" or str(item.get("writeState", "")).startswith("已写回")
            return_note = "该单此前已写回过审核意见；ERP 旧记录不会删除，下次写回将追加新意见。" if already_written else "该单尚未写回 ERP，仅从本地写回队列退回。"
            self._merge_manual(
                claim_id,
                workflowState="RETURNED",
                returnStatus="写回退回 / 待二次确认",
                returnedAt=datetime.now().isoformat(timespec="seconds"),
                returnedAfterWrite=already_written,
                conflictReason=return_note,
                writeState="写回退回 / 待二次确认",
            )
        self.store.save()
        self.render_writeback(); self.render_queue()
        self.write_status.setText(f"已将 {len(targets)} 条退回 M2 待二次确认；历史校准和写回记录均已保留。")

    def precheck(self) -> None:
        targets = self.selected_write_targets()
        if not targets:
            QMessageBox.information(self, "预检所选", "请先勾选至少一张需要预检的单据。"); return
        self.write_status.setText(f"正在只读预检所选 {len(targets)} 条…")
        def work() -> list[tuple[str, str, str]]:
            result=[]
            for claim_id,item in targets:
                old=self.client.request(f"/v1/expense-claims/{claim_id}/reviews").get("data",[])
                intended_reasons = [f"财务人工确认（{item.get('reviewer', '财务复核人')}）：{item.get('reason', '')}"] + list(item.get("aiReasons", []))
                same_verdict = [x for x in old if x.get("result") == item["decision"]]
                exact = any((x.get("reasons") or []) == intended_reasons for x in same_verdict)
                if exact:
                    result.append((claim_id, "内容已一致", "ERP 已存在与本次完全相同的审核意见；财务仍可点击“确认批量写回”再次追加本次意见。"))
                elif same_verdict:
                    latest_same = max(same_verdict, key=lambda x: str(x.get("createdAt") or x.get("reviewedAt") or x.get("actedAt") or x.get("updatedAt") or ""))
                    old_reasons = "；".join(latest_same.get("reasons") or []) or latest_same.get("comment") or "ERP 未记录原因"
                    new_reasons = "；".join(intended_reasons) or "本次未生成原因"
                    detail = f"结论一致：ERP 与本次均为{result_label(item['decision'])}。ERP 原原因：{old_reasons}；本次原因：{new_reasons}。ERP 已有审核意见，系统不重复写回。"
                    result.append((claim_id, "结论一致", detail))
                elif not old:
                    result.append((claim_id, "可写回", "ERP 当前没有审核意见，可安全写入本次人工最终意见。"))
                else:
                    latest = max(old, key=lambda x: str(x.get("createdAt") or x.get("reviewedAt") or x.get("actedAt") or x.get("updatedAt") or ""))
                    old_result = result_label(latest.get("result"))
                    old_reasons = "；".join(latest.get("reasons") or []) or latest.get("comment") or "ERP 未记录原因"
                    new_result = result_label(item.get("decision"))
                    new_reasons = "；".join(intended_reasons) or "本次未生成原因"
                    detail = f"ERP 原意见：{old_result}（{old_reasons}）；本次拟写：{new_result}（{new_reasons}）。新旧意见不一致，已明确标红；系统不会覆盖或追加，请到 ERP 人工处理。"
                    result.append((claim_id, "意见冲突", detail))
            return result
        def done(rows: list[tuple[str,str,str]]) -> None:
            for key,state,detail in rows:
                self.store.data["manual"][key]["writeState"] = state
                self.store.data["manual"][key]["conflictReason"] = detail
            self.store.save(); self.write_status.setText(f"已完成所选 {len(rows)} 条预检；仅更新勾选单据。"); self.render_writeback()
        self._run(work, done)

    def writeback(self) -> None:
        # ERP already has an opinion: never append or overwrite it.  The
        # screen may retain it for comparison, but only a genuinely blank ERP
        # opinion is eligible for this client-side writeback.
        eligible = {"可写回"}
        selected = self.selected_write_targets()
        targets=[(k,v) for k,v in selected if v.get("writeState") in eligible]
        not_prechecked = len(selected) - len(targets)
        if not selected: QMessageBox.information(self, "写回所选", "请先勾选至少一张需要写回的单据。"); return
        if not targets: QMessageBox.information(self, "写回所选", "勾选单据尚未完成预检，请先点击“预检所选”。"); return
        message = (
            f"已勾选 {len(selected)} 条，其中 {len(targets)} 条已预检并可写回"
            + (f"，{not_prechecked} 条未预检将跳过" if not_prechecked else "") + "。\n"
            "仅 ERP 当前无审核意见的单据可写回；已有意见（无论相同或冲突）均不会覆盖，须在 ERP 人工处理。\n\n"
            "本操作不支付、不删除，也不改变报销单业务状态。是否继续？"
        )
        if QMessageBox.question(self,"确认批量写回",message,QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
        def work() -> dict[str, list[str]]:
            if "expense:review" not in set(self.client.me().get("scopes", [])):
                raise PermissionError("当前 API Key 缺少 expense:review 权限；只能只读审核，不能写回 ERP。")
            ok=[]; skipped=[]
            for claim_id,item in targets:
                # Re-read immediately before POST.  A concurrent ERP user may
                # have added a review after the precheck; such a record must
                # never be appended or overwritten by this client.
                existing = self.client.request(f"/v1/expense-claims/{claim_id}/reviews").get("data", [])
                if existing:
                    skipped.append(claim_id)
                    continue
                reasons = [f"财务人工确认（{item.get('reviewer', '财务复核人')}）：{item.get('reason', '')}"] + list(item.get("aiReasons", []))
                evidence = [{"source": "local-evidence-chain", "path": item["evidenceRun"]}] if item.get("evidenceRun") else []
                self.client.request(f"/v1/expense-claims/{claim_id}/review",method="POST",body={"result":item["decision"],"reasons":reasons,"evidence":evidence,"confidence":1.0}); ok.append(claim_id)
            return {"written": ok, "skipped": skipped}
        def done(payload: dict[str, list[str]]) -> None:
            ok = payload["written"]
            skipped = payload["skipped"]
            for key in ok:
                item = self.store.data["manual"][key]
                previous = item.get("writeState", "")
                item["preWriteState"] = previous
                suffix = {"意见冲突": "（原意见冲突）", "内容已一致": "（重复确认）", "结论一致": "（结论重复确认）"}.get(previous, "")
                item.setdefault("writeHistory", []).append({
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "decision": item.get("decision"),
                    "preWriteState": previous,
                    "conflictReason": item.get("conflictReason", ""),
                })
                item["writeState"]="已写回" + suffix
                item["workflowState"]="WRITTEN"
            for key in skipped:
                item = self.store.data["manual"][key]
                item["writeState"] = "意见冲突"
                item["conflictReason"] = "写回前最终复查发现 ERP 已存在审核意见；系统未写回，请到 ERP 人工处理。"
            self.store.save(); self.write_status.setText(f"已写回 {len(ok)} 条；写回前发现 ERP 已有意见并跳过 {len(skipped)} 条。"); self.render_writeback()
        self._run(work, done)

    def refresh_calibration_choices(self) -> None:
        current=self.cal_claim.currentData(); self.cal_claim.blockSignals(True); self.cal_claim.clear()
        for c in self.claims: self.cal_claim.addItem(f"{c.get('claimNo')} · {c.get('employeeName') or (c.get('employee') or {}).get('name','')}", c["id"])
        if current:
            index=self.cal_claim.findData(current); self.cal_claim.setCurrentIndex(max(index,0))
        self.cal_claim.blockSignals(False)
        sample_size = confirmed = unreadable = 0
        try:
            report_path=LOCAL_OCR_REPORT if LOCAL_OCR_REPORT.exists() else BUNDLED_OCR_REPORT
            report=json.loads(report_path.read_text(encoding="utf-8"))
            sample_size=int(report.get("sampleSize",0)); confirmed=int(report.get("humanConfirmedTickets",0)); unreadable=int(report.get("unreadableOnOriginal",0))
        except Exception:
            try: sample_size=sum(1 for line in OCR_BASELINE.read_text(encoding="utf-8").splitlines() if line.strip())
            except Exception: pass
        self.cal_sample_count.setText(f"统一人工真值库：基线已确认 {confirmed}/{sample_size}｜不可读 {unreadable}｜日常新增 {len(self.store.data.get('truthSamples', []))}")
        self.refresh_calibration_lines()
        self.render_calibration_history()

    def refresh_calibration_lines(self) -> None:
        claim_id=self.cal_claim.currentData(); self.cal_line.clear()
        packet = self.outcomes.get(claim_id, {})
        claim = packet.get("claim") or (getattr(self, "current_claim", None) if getattr(self, "current_claim", {}).get("id") == claim_id else None)
        if not claim:
            self.cal_line.addItem("请先在费用报销单打开并完成本单初审", None)
            self.render_calibration()
            self.render_calibration_history()
            return
        for line in claim.get("lines", []):
            invoice=line.get("invoice") or {}
            self.cal_line.addItem(f"行 {line.get('lineNo')}｜{line.get('expenseType')}｜¥{line.get('amount')}｜{invoice.get('invoiceCode','')} / {invoice.get('invoiceNo','')}", line.get("id"))
        self.render_calibration()
        self.render_calibration_history()

    def render_calibration(self) -> None:
        if not hasattr(self, "cal_compare"): return
        claim_id, line_id = self.cal_claim.currentData(), self.cal_line.currentData()
        packet = self.outcomes.get(claim_id, {})
        claim = packet.get("claim") or {}
        line = next((x for x in claim.get("lines", []) if x.get("id") == line_id), {})
        invoice = line.get("invoice") or {}
        attachment = next((x for x in packet.get("attachments", []) if x.get("lineId") == line_id), {})
        ocr = attachment.get("extraction") or {}
        ledger = invoice.get("ledger") or invoice
        rows = [
            (invoice.get("invoiceCode"), ledger.get("invoiceCode"), ocr.get("invoice_code")),
            (invoice.get("invoiceNo"), ledger.get("invoiceNo"), ocr.get("invoice_no")),
            (invoice.get("issuedOn"), ledger.get("issuedOn"), ocr.get("issued_on")),
            ((invoice.get("buyer") or {}).get("name"), (ledger.get("buyer") or {}).get("name"), ocr.get("buyer_name")),
            ((invoice.get("buyer") or {}).get("taxNo"), (ledger.get("buyer") or {}).get("taxNo"), ocr.get("buyer_tax_no")),
            ((invoice.get("seller") or {}).get("name"), (ledger.get("seller") or {}).get("name"), ocr.get("seller_name")),
            (f"{(line.get('amountFen') or 0)/100:.2f}", f"{(ledger.get('totalFen') or invoice.get('totalFen') or 0)/100:.2f}", f"{(ocr.get('total_fen') or 0)/100:.2f}" if ocr.get("total_fen") is not None else None),
            ("—", f"{float(ledger.get('taxRate'))*100:g}%" if ledger.get("taxRate") is not None else None, "—"),
        ]
        existing = self._ocr_overrides(claim_id).get(line_id, {})
        keys = ["invoice_code", "invoice_no", "issued_on", "buyer_name", "buyer_tax_no", "seller_name", "total_fen", "tax_rate"]
        for row, values in enumerate(rows):
            for col, value in enumerate(values, 1):
                self.cal_compare.item(row, col).setText(str(value if value not in (None, "") else "—"))
            override = existing.get(keys[row])
            if keys[row] == "total_fen" and override is not None: override = f"{override/100:.2f}"
            self.cal_compare.item(row, 4).setText("" if override is None else str(override))
        path = attachment.get("image")
        self.cal_image_path = str(path or "")
        self.cal_invoice_title.setText(f"原票｜{claim.get('claimNo','—')}｜费用行 {line.get('lineNo','—')}｜{expense_label(line.get('expenseType'))}")
        pix = QPixmap(str(path or ""))
        if pix.isNull():
            self.cal_image.setPixmap(QPixmap()); self.cal_image.resize(460, 520); self.cal_image.setText("该费用行没有可预览原票，或尚未完成初审下载。")
        else:
            thumb = pix.scaled(760, 760, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.cal_image.setText(""); self.cal_image.setPixmap(thumb); self.cal_image.resize(thumb.size())
            self.cal_scroll.horizontalScrollBar().setValue(0); self.cal_scroll.verticalScrollBar().setValue(0)

    def zoom_calibration_ticket(self) -> None:
        path = Path(getattr(self, "cal_image_path", ""))
        pix = QPixmap(str(path))
        if pix.isNull():
            QMessageBox.information(self, "查看原票", "当前费用行没有可打开的原始票据。"); return
        dialog = QDialog(self); dialog.setWindowTitle(f"原票原始分辨率｜{path.name}"); dialog.resize(1280, 850)
        layout = QVBoxLayout(dialog)
        hint = QLabel("原始分辨率显示｜按住图片拖动可左右/上下查看；滚动条也可定位。"); hint.setObjectName("hint"); layout.addWidget(hint)
        scroll = QScrollArea(); scroll.setWidgetResizable(False)
        image = PanImageLabel(); image.setAlignment(Qt.AlignCenter); image.setPixmap(pix); image.resize(pix.size()); scroll.setWidget(image); layout.addWidget(scroll, 1)
        dialog.exec()

    def render_calibration_history(self) -> None:
        if not hasattr(self, "cal_history"): return
        claim_id = self.cal_claim.currentData()
        logs = list(self.store.data.get("calibrations", {}).get(claim_id, []))
        self.cal_history.setRowCount(len(logs))
        for row, item in enumerate(reversed(logs)):
            if item.get("overrides"):
                detail = "；".join(f"{key}={value}" for key, value in item["overrides"].items() if value is not None)
            elif item.get("decision"):
                detail = f"{result_label(item.get('aiOriginal'))} → {result_label(item.get('decision'))}"
            else:
                detail = item.get("action") or "历史校准"
            values = [
                item.get("at", "—"), item.get("lineId") or "整单",
                item.get("type") or "历史校准", detail or "—",
                f"{item.get('reason') or '—'}｜{item.get('reviewer') or '—'}",
            ]
            for col, value in enumerate(values):
                self.cal_history.setItem(row, col, QTableWidgetItem(str(value)))
        if not logs:
            self.cal_status.setPlainText("本单尚无历史校准记录；保存后会在右侧按时间倒序追加，旧记录不会被覆盖。")

    def save_calibration(self) -> None:
        claim_id=self.cal_claim.currentData(); line_id=self.cal_line.currentData(); kind=self.cal_type.currentText(); note=self.cal_note.text().strip()
        if not claim_id or not note:
            QMessageBox.warning(self,"校准", "请选择单据并填写原票位置、制度依据或改判原因。"); return
        if kind == "审核结论改判":
            claim=next((x for x in self.claims if x["id"]==claim_id), {})
            decision={"人工改判为通过（需二次确认）":"APPROVE","人工改判为驳回（需二次确认）":"REJECT"}.get(self.cal_reason.currentText())
            if self.cal_reason.currentText() == "暂存待补充":
                entry={"at":datetime.now().isoformat(timespec="seconds"),"type":kind,"lineId":line_id,"reason":note,"reviewer":"周晓","aiOriginal":self._outcome_for(claim) or "FLAG","action":"暂存待补充"}
                self.store.data["calibrations"].setdefault(claim_id,[]).append(entry)
                self._merge_manual(claim_id, decision=None, overrideDecision=None, ai=entry["aiOriginal"], reason=note, reviewer="周晓", at=entry["at"], workflowState="HOLD", writeState="暂存待补充", calibration=True)
                self.store.save(); self.render_queue(); self.render_writeback(); self.render_calibration_history()
                self.cal_status.setPlainText("已暂存待补充。该单仍在费用报销单队列，不会进入写回区。补齐证据后请重新初审或继续校准。")
                return
            if decision not in {"APPROVE", "REJECT"}:
                QMessageBox.warning(self, "校准", "审核结论改判请选择“人工改判为通过”或“人工改判为驳回”；如只需字段纠正，请选择 OCR 字段纠正。")
                return
            entry={"at":datetime.now().isoformat(timespec="seconds"),"type":kind,"lineId":line_id,"reason":note,"reviewer":"周晓","aiOriginal":self._outcome_for(claim) or "FLAG","decision":decision}
            self.store.data["calibrations"].setdefault(claim_id,[]).append(entry)
            self._merge_manual(
                claim_id, decision=None, overrideDecision=decision, ai=entry["aiOriginal"], reason=note,
                reviewer="周晓", at=entry["at"], writeState="校准后待二次确认",
                workflowState="RECONFIRM", calibration=True, conflictReason="",
            )
            self.store.data["analysisSignatures"].pop(claim_id, None)
            self.store.save(); self.render_queue(); self.render_writeback()
            self.render_calibration_history()
            self.cal_status.setPlainText("已保存人工改判依据，正在用现有校准字段重新执行整单规则判责…")
            def override_done(value: dict[str, Any]) -> None:
                self.outcomes[claim_id] = value
                self.store.data["analysisCache"][claim_id] = value
                manual = self.store.data["manual"][claim_id]
                manual["ai"] = (value.get("result") or {}).get("result")
                manual["aiReasons"] = list((value.get("result") or {}).get("reasons", []))
                manual["workflowState"] = "RECONFIRM"
                manual["writeState"] = "校准后待二次确认"
                manual["secondReviewResult"] = manual["ai"]
                manual["secondReviewReasons"] = list(manual["aiReasons"])
                manual["secondReviewAt"] = datetime.now().isoformat(timespec="seconds")
                self._audit("M2", "人工改判后整单二审完成", claim_id, f"规则二审：{result_label(manual['ai'])}；人工建议：{result_label(decision)}")
                self.store.save(); self.render_queue(); self.render_writeback(); self.refresh_quality_metrics()
                if claim_id == self.current_id:
                    self._set_tickets_from_outcome(value); self._show_outcome(value)
                self.render_calibration_history()
                self.cal_status.setPlainText(
                    f"整单重判完成：AI 当前结论 {result_label(manual.get('ai'))}；人工改判建议 {result_label(decision)}。"
                    "该单已返回 M2“校准后待二次确认”，尚未进入写回区。"
                )
            self._run(lambda: analyze_one(self.client, summary=claim, ocr_overrides=self._ocr_overrides(claim_id)), override_done)
            return
        if not line_id:
            QMessageBox.warning(self,"校准", "OCR 字段纠正必须选择具体费用行及关联原票。"); return
        keys = ["invoice_code", "invoice_no", "issued_on", "buyer_name", "buyer_tax_no", "seller_name", "total_fen"]
        override: dict[str, Any] = {}
        for row, key in enumerate(keys):
            value = self.cal_compare.item(row, 4).text().strip()
            if not value: continue
            if key == "total_fen":
                try: value = round(float(value)*100)
                except ValueError:
                    QMessageBox.warning(self,"校准", "价税合计的人工正确值必须填写数字（元）。"); return
            override[key] = value
        if not override:
            QMessageBox.warning(self, "校准", "请至少填写一个“人工正确值”。"); return
        entry={"at":datetime.now().isoformat(timespec="seconds"),"type":kind,"lineId":line_id,"overrides":override,"reason":note,"reviewer":"周晓"}
        logs=self.store.data["calibrations"].setdefault(claim_id,[]); logs.append(entry); self.store.data["confirmedSamples"].append(entry)
        packet = self.outcomes.get(claim_id, {})
        claim_detail = packet.get("claim") or {}
        claim_no = claim_detail.get("claimNo") or next((c.get("claimNo") for c in self.claims if c.get("id") == claim_id), claim_id)
        attachment = next((x for x in packet.get("attachments", []) if x.get("lineId") == line_id), {})
        prediction = dict(attachment.get("extraction") or {})
        truth_key = f"{claim_id}:{line_id}"
        truth = {
            "sampleKey": truth_key,
            "claimId": claim_id,
            "claimNo": claim_no,
            "lineId": line_id,
            "image": attachment.get("image"),
            "prediction": prediction,
            "gold": dict(override),
            "reviewer": "周晓",
            "reviewedAt": entry["at"],
            "reason": note,
            "source": "日常人工校准",
        }
        samples = self.store.data.setdefault("truthSamples", [])
        samples[:] = [item for item in samples if item.get("sampleKey") != truth_key]
        samples.append(truth)
        self._audit("M2", "保存OCR人工真值并整单重判", claim_id, f"费用行{line_id}：{note}")
        self.store.data["calibrationRevision"] = int(self.store.data.get("calibrationRevision", 0)) + 1
        self.store.data["analysisSignatures"].pop(claim_id, None)
        self.store.save()
        all_overrides={}
        for item in logs:
            if item.get("type") == "OCR 字段纠正" and item.get("lineId"):
                all_overrides.setdefault(item["lineId"],{}).update({k:v for k,v in item.get("overrides",{}).items() if v is not None})
        claim=next((x for x in self.claims if x["id"]==claim_id), None)
        if not claim: QMessageBox.warning(self,"校准", "找不到待审单摘要，无法重新判责。"); return
        self.cal_status.setPlainText("已保存人工确认样本，正在按原票优先级重新读取并完整判责本单……")
        def done(value: dict[str, Any]) -> None:
            self.outcomes[claim_id]=value
            self.store.data["analysisCache"][claim_id]=value
            new_result = (value.get("result") or {}).get("result")
            existing = self.store.data["manual"].get(claim_id, {})
            self._merge_manual(
                claim_id,
                decision=None,
                overrideDecision=None,
                ai=new_result,
                aiReasons=list((value.get("result") or {}).get("reasons", [])),
                reason=f"OCR 字段校准后已完成整单重判：{note}",
                reviewer="周晓",
                at=datetime.now().isoformat(timespec="seconds"),
                workflowState="RECONFIRM",
                writeState="校准后待二次确认",
                calibration=True,
                conflictReason="",
                writeHistory=existing.get("writeHistory", []),
                secondReviewResult=new_result,
                secondReviewReasons=list((value.get("result") or {}).get("reasons", [])),
                secondReviewAt=datetime.now().isoformat(timespec="seconds"),
            )
            self._audit("M2", "校准后整单二审完成", claim_id, f"二审建议：{result_label(new_result)}")
            self.store.save(); self.render_queue(); self.render_writeback(); self.refresh_quality_metrics()
            if claim_id == self.current_id: self._set_tickets_from_outcome(value); self._show_outcome(value)
            self.refresh_calibration_choices()
            self.cal_status.setPlainText("OCR 字段纠正已保存到本地人工确认样本库，并已对整张单据完成重新判责。该单已返回 M2“校准后待二次确认”；再次确认前不会进入写回区，也不会自动发布为全局规则。")
        self._run(lambda: analyze_one(self.client, summary=claim, ocr_overrides=all_overrides), done)

    def show_cal_log(self) -> None:
        self.render_calibration_history()
        claim_id=self.cal_claim.currentData(); logs=self.store.data["calibrations"].get(claim_id,[])
        if not logs:
            self.cal_status.setPlainText("本单尚无人工校准记录。"); return
        rows=[]
        for item in logs:
            kind=item.get("type") or item.get("action") or "历史校准"
            if item.get("overrides"):
                detail="；".join(f"{k}={v}" for k,v in item["overrides"].items() if v is not None)
            elif item.get("decision"): detail=f"人工结论：{result_label(item['decision'])}"
            else: detail=f"票面金额：{item.get('before','—')} → {item.get('amount','—')}"
            rows.append(f"{item.get('at','—')}｜{kind}｜{detail}｜{item.get('reviewer','—')}｜{item.get('reason','—')}")
        self.cal_status.setPlainText("本单人工校准记录：\n" + "\n".join(rows))

    def load_m3_cache(self, silent: bool = False) -> None:
        candidates=[DATA_DIR.parent / "m3" / "m3-latest.json", RESOURCE_ROOT / "formal-m3" / "m3-latest.json", Path(__file__).resolve().parent / "formal-m3" / "m3-latest.json"]
        for path in candidates:
            if path.exists(): self.m3=json.loads(path.read_text(encoding="utf-8")); self.render_m3(); return
        if not silent: QMessageBox.information(self,"M3", "未找到本地 M3 缓存，请执行全量只读扫描。")

    def load_ledger(self) -> None:
        self.ledger_status.setText("正在读取全量发票台账（仅 GET）…")
        def done(rows: list[dict[str, Any]]) -> None:
            self.invoices = rows; self.ledger.setRowCount(len(rows))
            for r, x in enumerate(rows):
                buyer=x.get("buyer") or {}; seller=x.get("seller") or {}
                values=[f"{x.get('invoiceCode','')} / {x.get('invoiceNo','')}", "销项" if x.get("type")=="SALES_OUTPUT" else "进项/费用", seller.get("name","—"), buyer.get("name","—"), f"¥{(x.get('amountFen', x.get('amount', 0)) or 0)/100:,.2f}" if x.get("amountFen") is not None else str(x.get("amount","—")), f"{(x.get('taxRate') or 0)*100:g}%"]
                for c, value in enumerate(values): self.ledger.setItem(r,c,QTableWidgetItem(str(value)))
            self.ledger_status.setText(f"已加载 {len(rows):,} 条台账（只读）")
        self._run(lambda: list(self.client.iter_invoices()), done)

    def scan_m3(self) -> None:
        self.m3_status.setText("正在读取全量发票台账和供应商主数据（仅 GET）…")
        def work() -> dict[str,Any]:
            result=scan_ledger(list(self.client.iter_invoices()),list(self.client.iter_vendors())); DATA_DIR.mkdir(parents=True,exist_ok=True); (DATA_DIR/"m3-latest.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8"); return result
        self._run(work, lambda x:(setattr(self,"m3",x),self.render_m3()))

    def render_m3(self) -> None:
        if not self.m3:return
        details=self.m3.get("details",[]); self.m3_table.setRowCount(len(details))
        labels={"TITLE_WRONG":"购方抬头不符合", "TAXNO_WRONG":"购方税号不符合", "TAX_RATE_WRONG":"开票税率待核查", "MASTER_DATA_INCOMPLETE":"供应商主数据待完善", "DUPLICATE_INVOICE":"疑似重复发票"}
        category_labels={"MATERIAL":"原材料供应商", "OUTSOURCE":"外协加工供应商", "SERVICE":"服务类供应商", "OFFICE":"办公用品供应商"}
        actions={"TITLE_WRONG":"核实后走红冲/重开或更正流程", "TAXNO_WRONG":"核实后走红冲/重开或更正流程", "TAX_RATE_WRONG":"先核合同及业务实质，再判断开票或主数据问题", "MASTER_DATA_INCOMPLETE":"补充供应商类别和税号主数据", "DUPLICATE_INVOICE":"核查重复入账、重复报销或合理两笔业务"}
        for r,x in enumerate(details):
            ev=x.get("evidence",{}); side="销项" if x.get("type") == "SALES_OUTPUT" else ("费用报销来源" if x.get("type") == "EXPENSE" else "进项")
            actual=ev.get("actual","—"); expected=ev.get("expected","—")
            difference="—"
            if x.get("issue") == "TAX_RATE_WRONG":
                actual_rate=float(actual); expected_rate=float(expected)
                difference=f"{(actual_rate-expected_rate)*100:+g} 个百分点"
                actual=f"{actual_rate*100:g}%"; expected=f"{expected_rate*100:g}%"
            basis=ev.get("basis", "")
            if basis.startswith("vendor.category:"):
                supplier_type=category_labels.get(x.get("vendorCategory"), x.get("vendorCategory") or "类别待完善")
                basis=f"ERP 主数据类型：{supplier_type}（仅作初筛线索）"
            elif x.get("issue") == "TITLE_WRONG":
                basis="台账购买方名称与启衡公司名称主数据交叉核对"
            elif x.get("issue") == "TAXNO_WRONG":
                basis="台账购买方税号与启衡公司税号主数据交叉核对"
            vals=[f"{x.get('invoiceCode','')} / {x.get('invoiceNo','')}",side,(x.get('seller') or {}).get('name','—'),labels.get(x.get('issue'),x.get('issue','—')),actual,expected,difference,basis,actions.get(x.get('issue'),'人工复核后处理')]
            for c,v in enumerate(vals):
                cell=QTableWidgetItem(str(v))
                if c == 3: cell.setForeground(QColor("#a66a08"))
                self.m3_table.setItem(r,c,cell)
        duplicates=self.m3.get("duplicateInvoices",[]); self.m3_duplicates.setRowCount(len(duplicates))
        for r,x in enumerate(duplicates):
            ev=x.get("evidence",{}); count=int(ev.get("count") or len(x.get("invoiceIds", [])) or 0); repeat_text=f"重复 {count} 条" if count else "待核查"
            vals=[x.get("invoiceCode","—"),x.get("invoiceNo","—"),repeat_text,"、".join(x.get("invoiceIds",[])),"点击本行查看全部重复记录与处理依据"]
            for c,v in enumerate(vals): self.m3_duplicates.setItem(r,c,QTableWidgetItem(str(v)))
        s=self.m3.get("summary",{}); counts=s.get("issueCounts",{}); self.m3_summary.setText(f"已扫描 {s.get('totalInvoices',0):,} 条历史发票台账（进项 {s.get('typeCounts',{}).get('PURCHASE_INPUT',0):,} · 销项 {s.get('typeCounts',{}).get('SALES_OUTPUT',0):,} · 费用来源 {s.get('typeCounts',{}).get('EXPENSE',0):,}）    待核查 {len(details)} 项：抬头 {counts.get('TITLE_WRONG',0)} · 税号 {counts.get('TAXNO_WRONG',0)} · 税率 {counts.get('TAX_RATE_WRONG',0)}    重复发票组 {len(duplicates)}")
        self.m3_status.setText(f"稽核完成：展示 {len(details)} 条待核查事项及 {len(duplicates)} 个重复发票组。选择一条异常可记录本地处理任务。")

    def show_m3_duplicate(self) -> None:
        row = self.m3_duplicates.currentRow()
        if not self.m3 or row < 0: return
        groups = self.m3.get("duplicateInvoices", [])
        if row >= len(groups): return
        item = groups[row]; evidence = item.get("evidence", {})
        invoice_ids = item.get("invoiceIds", [])
        claim_ids = item.get("claimIds", [])
        count = int(evidence.get("count") or len(invoice_ids) or 0)
        group_type = "两条记录重复" if count == 2 else f"多条记录重复（共 {count} 条）"
        records = "\n".join(f"  {index}. {invoice_id}" for index, invoice_id in enumerate(invoice_ids, 1)) or "  未返回台账记录ID"
        claims = "、".join(claim_ids) if claim_ids else "API 未返回关联报销单，需按台账记录继续核查"
        self.m3_detail.setPlainText(
            f"重复发票组｜{group_type}\n\n"
            f"发票代码：{item.get('invoiceCode', '—')}\n"
            f"发票号码：{item.get('invoiceNo', '—')}\n"
            f"重复次数：{count}\n"
            f"制度依据：{evidence.get('rule') or '《发票合规指引》第三部分第1项'}\n\n"
            f"全部关联台账记录：\n{records}\n\n"
            f"关联报销单：{claims}\n\n"
            "判断说明：代码和号码组合相同只代表疑似重复线索，不能直接认定重复报销。\n"
            "建议动作：逐条核对业务日期、金额、供应商和入账凭证；确认重复入账/报销后再由财务冲销或调整，合理的两笔业务应记录排除依据。"
        )

    def show_m3_task(self) -> None:
        row=self.m3_table.currentRow()
        if not self.m3 or row<0:return
        x=self.m3.get("details",[])[row]; ev=x.get("evidence",{}); issue={"TITLE_WRONG":"购方抬头不符合", "TAXNO_WRONG":"购方税号不符合", "TAX_RATE_WRONG":"开票税率待核查", "MASTER_DATA_INCOMPLETE":"供应商主数据待完善", "DUPLICATE_INVOICE":"疑似重复发票"}.get(x.get('issue'),x.get('issue'))
        key=x.get("invoiceId") or f"{x.get('invoiceCode')}-{x.get('invoiceNo')}"; task=self.store.data["m3Tasks"].get(key, {})
        actual=ev.get("actual", "—"); expected=ev.get("expected", "—")
        category_labels={"MATERIAL":"原材料供应商", "OUTSOURCE":"外协加工供应商", "SERVICE":"服务类供应商", "OFFICE":"办公用品供应商"}
        if x.get("issue") == "TAX_RATE_WRONG":
            actual_rate=float(actual); expected_rate=float(expected)
            difference=f"{(actual_rate-expected_rate)*100:+g} 个百分点"
            actual=f"{actual_rate*100:g}%"; expected=f"{expected_rate*100:g}%"
        else:
            difference="—"
        handling={"TITLE_WRONG":"核实购方信息。确认票面错误后，由应付/税务发起红冲、重开或更正；不可在本系统直接改票。", "TAXNO_WRONG":"核实税号主数据。确认票面错误后，由应付/税务发起红冲、重开或更正。", "TAX_RATE_WRONG":"先核合同与业务实质；确认是开票错误才要求红冲/重开，若类别错误则转主数据维护。", "MASTER_DATA_INCOMPLETE":"补充供应商类别、税号等主数据后重新扫描。", "DUPLICATE_INVOICE":"核实是否重复入账、重复报销或合理两笔业务；确认重复后由业务与财务冲销/调整。"}
        if x.get("issue") == "TAX_RATE_WRONG":
            supplier_type=category_labels.get(x.get("vendorCategory"), x.get("vendorCategory") or "类别待完善")
            basis=f"ERP 供应商主数据将该销售方归类为“{supplier_type}”；该类别仅用于初筛。"
            judgement=f"ERP 主数据类型为“{supplier_type}”，同类业务通常参考 {expected}，本票为 {actual}。供应商类别不能单独证明本张发票的法定税率，必须结合合同、订单、实际商品或服务内容及适用税收政策核查。"
        elif x.get("issue") == "TAXNO_WRONG":
            basis="台账/票面购买方税号与启衡公司税号主数据交叉核对。"
            judgement=f"台账/票面购买方税号为 {actual}，启衡公司主数据税号为 {expected}，两者不一致。先核对原票：若原票错误，联系开票方红冲、重开或更正；若仅 ERP 台账录入错误，则修正台账。没有明确手填证据时，不判定为“报销人填错”。"
        elif x.get("issue") == "TITLE_WRONG":
            basis="台账/票面购买方名称与启衡公司名称主数据交叉核对。"
            judgement=f"台账/票面购买方名称为“{actual}”，公司主数据名称为“{expected}”。先核对原票和适用法人主体，再决定更正台账或联系开票方处理。"
        else:
            basis=str(ev.get("basis") or "ERP 发票台账与主数据交叉核对。")
            judgement="当前项目是核查线索，需由财务结合原始凭证和业务背景确认后处理。"
        task_text = "尚未分派" if not task else f"{task.get('action','—')}｜负责人：{task.get('owner','—')}｜状态：{task.get('status','待处理')}｜备注：{task.get('note','—')}｜{task.get('at','—')}"
        self.m3_detail.setPlainText(f"稽核任务：{issue}\n状态：待核查\n\n发票：{x.get('invoiceCode','')} / {x.get('invoiceNo','')}\n来源：{'销项' if x.get('type') == 'SALES_OUTPUT' else ('费用报销来源' if x.get('type') == 'EXPENSE' else '进项')}\n销售方：{(x.get('seller') or {}).get('name','—')}\n台账实际值：{actual}\n参考口径：{expected}\n差异：{difference}\n核查依据：{basis}\n\n判断说明：{judgement}\n\n建议处理：{handling.get(x.get('issue'),'人工核实后处理。')}\n\n本地处理记录：{task_text}\n\nM3 不写回 ERP；这里只保存稽核任务、负责人、备注与处理留痕。")

    def m3_action(self, action: str) -> None:
        row=self.m3_table.currentRow()
        if not self.m3 or row < 0:
            QMessageBox.information(self, "M3", "请先选择一条稽核任务。"); return
        item=self.m3.get("details", [])[row]; key=item.get("invoiceId") or f"{item.get('invoiceCode')}-{item.get('invoiceNo')}"
        old=self.store.data["m3Tasks"].get(key, {}); owner, ok=QInputDialog.getText(self, "分派稽核任务", "负责人", text=old.get("owner", "周晓"))
        if not ok or not owner.strip(): return
        note, ok=QInputDialog.getMultiLineText(self, "分派稽核任务", "处理备注（必填，记录核查范围或下一步）", text=old.get("note", ""))
        if not ok or not note.strip():
            QMessageBox.information(self, "M3", "请填写处理备注，确保任务可追溯。"); return
        status={"确认问题属实":"待业务/税务处理", "确认无异常":"已核查关闭", "交给主数据维护":"待主数据维护"}.get(action, "待处理")
        self.store.data["m3Tasks"][key]={"action":action,"status":status,"at":datetime.now().isoformat(timespec="seconds"),"owner":owner.strip(),"note":note.strip()}
        self._audit("M3", action, key, f"{status}｜{owner.strip()}｜{note.strip()}")
        self.store.save(); self.show_m3_task(); self.refresh_quality_metrics()

    def export_m3(self) -> None:
        if not self.m3:
            QMessageBox.information(self, "导出稽核清单", "请先完成一次发票台账稽核。"); return
        path, _ = QFileDialog.getSaveFileName(self, "导出稽核清单", str(Path.home() / "启衡发票台账稽核清单.csv"), "CSV 文件 (*.csv)")
        if not path: return
        labels={"TITLE_WRONG":"购方抬头不符合", "TAXNO_WRONG":"购方税号不符合", "TAX_RATE_WRONG":"开票税率待核查", "MASTER_DATA_INCOMPLETE":"供应商主数据待完善", "DUPLICATE_INVOICE":"疑似重复发票"}
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer=csv.writer(handle); writer.writerow(["发票代码", "发票号码", "来源", "销售方", "待核查事项", "实际值", "参考口径", "差异", "处理状态", "负责人", "备注"])
            for item in self.m3.get("details", []):
                key=item.get("invoiceId") or f"{item.get('invoiceCode')}-{item.get('invoiceNo')}"; task=self.store.data["m3Tasks"].get(key, {}); ev=item.get("evidence", {})
                source="销项" if item.get("type")=="SALES_OUTPUT" else ("费用报销来源" if item.get("type")=="EXPENSE" else "进项")
                if item.get("issue")=="TAX_RATE_WRONG":
                    actual=f"{float(ev.get('actual'))*100:g}%"; expected=f"{float(ev.get('expected'))*100:g}%"; difference=f"{(float(ev.get('actual'))-float(ev.get('expected')))*100:+g}个百分点"
                else:
                    actual=ev.get("actual"); expected=ev.get("expected"); difference="—"
                writer.writerow([item.get("invoiceCode"), item.get("invoiceNo"), source, (item.get("seller") or {}).get("name"), labels.get(item.get("issue"), item.get("issue")), actual, expected, difference, task.get("status", "待核查"), task.get("owner", ""), task.get("note", "")])
        QMessageBox.information(self, "导出完成", f"稽核清单已保存：\n{path}")

    def import_bank_csv(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择一个或多个银行流水 CSV", "", "CSV 文件 (*.csv)")
        if not paths: return
        self._load_bank_paths(paths, audit=True)

    def _load_bank_paths(self, paths: list[str], *, audit: bool=False) -> None:
        rows: list[dict[str, Any]] = []
        for path in paths:
            rows.extend(read_bank_csv_all(path))
        unique: dict[tuple[str,str,str,int],dict[str,Any]]={}
        for row in rows:
            key=(str(row.get("sourceFile") or ""),str(row.get("id") or ""),str(row.get("date") or ""),int(row.get("amountFen") or 0))
            unique[key]=row
        self.bank_rows=list(unique.values())
        self.m4_matches=match_all(self.bank_rows, self.receivables, include_settled=True, allow_partial=False)
        if audit:
            self._audit("M4", "导入银行贷方流水", ",".join(Path(x).name for x in paths), f"共{len(self.bank_rows)}条贷方流水")
            self.store.save()
        credits=sum(str(x.get("direction") or "") in {"贷","收入","收"} for x in self.bank_rows)
        self.m4_status.setText(f"已导入 {len(self.bank_rows):,} 条银行流水，其中贷方到账 {credits:,} 条；ERP应收读取后会自动重算候选")
        self.render_m4()

    def _discover_bank_csvs(self) -> list[str]:
        candidates=[]
        configured=os.environ.get("QIHENG_BANK_DIR")
        roots=[
            Path(configured) if configured else None,
            Path(sys.executable).resolve().parent/"bank",
            Path.cwd()/"bank",
            Path.cwd().parent/"bank",
            Path(__file__).resolve().parents[2]/"bank",
        ]
        seen=set()
        for root in roots:
            if root is None or not root.exists():
                continue
            for path in sorted(root.glob("*.csv")):
                resolved=str(path.resolve())
                if resolved not in seen:
                    seen.add(resolved); candidates.append(resolved)
        return candidates

    def load_m4_data(self, *_: Any) -> None:
        """Load the ERP receivables, posted receipts, customers and sales invoices."""
        if self._m4_loading:
            return
        self._m4_loading=True
        self.m4_load_button.setEnabled(False)
        self.m4_status.setText("正在读取 ERP 应收、客户回款、客户主数据和销项发票（全部只读）…")
        sig=Signals(); self._m4_signal=sig
        def work() -> None:
            try:
                sig.done.emit({
                    "receivables": list(self.client.iter_receivables()),
                    "receipts": list(self.client.iter_receipts()),
                    "customers": list(self.client.iter_customers()),
                    "salesInvoices": list(self.client.iter_invoices("SALES_OUTPUT")),
                })
            except Exception as exc:
                sig.error.emit(str(exc))
        def done(data: dict[str, Any]) -> None:
            self.receivables=list(data["receivables"])
            self.receipts=list(data["receipts"]); self.customers=list(data["customers"]); self.sales_invoices=list(data["salesInvoices"])
            self.payments=[]  # /v1/payments is supplier payment data and is outside this customer-AR page.
            self._rebuild_m4_indexes()
            self._m4_loading=False; self._m4_loaded=True; self.m4_load_button.setEnabled(True)
            open_rows=[x for x in self.receivables if int(x.get("outstandingFen") or 0)>0]
            overdue=sum(str(x.get("status") or "").upper()=="OVERDUE" for x in open_rows)
            self.m4_status.setText(f"API读取完成：应收 {len(self.receivables):,} 条｜客户回款 {len(self.receipts):,} 条｜销项发票 {len(self.sales_invoices):,} 条｜逾期 {overdue:,} 条")
            self.render_m4_dashboard()
            if not self.bank_rows:
                discovered=self._discover_bank_csvs()
                if discovered:
                    self._load_bank_paths(discovered)
            else:
                self.m4_matches=match_all(self.bank_rows,self.receivables,include_settled=True,allow_partial=False); self.render_m4()
            self.refresh_quality_metrics()
        def failed(message: str) -> None:
            self._m4_loading=False; self.m4_load_button.setEnabled(True)
            self.m4_status.setText("应收台账读取失败，请检查 receivable:read、payment:read、invoice:read、master-data:read 权限或连接")
            QMessageBox.warning(self, "M4读取失败", message)
        sig.done.connect(done); sig.error.connect(failed)
        threading.Thread(target=work, daemon=True).start()

    def _rebuild_m4_indexes(self) -> None:
        self._m4_sales_ids={str(x.get("id")) for x in self.sales_invoices if x.get("id")}
        self._m4_sales_numbers={str(x.get("invoiceNo")) for x in self.sales_invoices if x.get("invoiceNo")}
        self._m4_erp_receipt_index={}
        allocations: dict[str, list[dict[str, Any]]] = {}
        for receipt in self.receipts:
            receipt_date=str(receipt.get("receivedOn") or "")[:10].replace("/","-")
            receipt_amount=int(receipt.get("amountFen") or round(float(receipt.get("amount") or 0)*100))
            receipt_name=normalize_name(receipt.get("payerName"))
            receipt_account="".join(ch for ch in str(receipt.get("bankAccountNo") or "") if ch.isdigit())
            if receipt_name:
                self._m4_erp_receipt_index[(receipt_date,receipt_amount,f"name:{receipt_name}")]=receipt
            if receipt_account:
                self._m4_erp_receipt_index[(receipt_date,receipt_amount,f"account:{receipt_account}")]=receipt
            for allocation in receipt.get("allocations") or []:
                receivable_id=str(allocation.get("receivableId") or "")
                if not receivable_id:
                    continue
                allocations.setdefault(receivable_id, []).append({"receipt":receipt, "allocation":allocation})
        self._m4_receipt_allocations=allocations

    def _m4_invoice_state(self, item: dict[str, Any]) -> str:
        invoice_id=str(item.get("invoiceId") or "")
        invoice_no=str(item.get("invoiceNo") or "")
        if (invoice_id and invoice_id in self._m4_sales_ids) or (invoice_no and invoice_no in self._m4_sales_numbers):
            return "已开票"
        if invoice_id or invoice_no:
            return "发票记录缺失"
        return "未关联发票"

    def _m4_invoice_for_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Return the linked sales-invoice ledger row for an AR item, if present."""
        invoice_id=str(item.get("invoiceId") or "")
        invoice_no=str(item.get("invoiceNo") or "")
        for invoice in self.sales_invoices:
            if invoice_id and str(invoice.get("id") or "") == invoice_id:
                return invoice
            if invoice_no and str(invoice.get("invoiceNo") or "") == invoice_no:
                return invoice
        return {}

    def render_m4_dashboard(self) -> None:
        if not hasattr(self, "m4_summary"):
            return
        total=sum(int(x.get("amountFen") or 0) for x in self.receivables)
        settled=sum(int(x.get("settledFen") or 0) for x in self.receivables)
        outstanding=sum(int(x.get("outstandingFen") or 0) for x in self.receivables)
        overdue_rows=[x for x in self.receivables if int(x.get("outstandingFen") or 0)>0 and str(x.get("status") or "").upper()=="OVERDUE"]
        overdue=sum(int(x.get("outstandingFen") or 0) for x in overdue_rows)
        customer_count=len({str(x.get("customerId") or x.get("customerName") or "") for x in self.receivables if x.get("customerId") or x.get("customerName")})
        self.m4_summary.setText(
            f"客户 {customer_count:,}｜应收总额 ¥{total/100:,.2f}｜已收 ¥{settled/100:,.2f}｜待收 ¥{outstanding/100:,.2f}｜"
            f"已逾期 ¥{overdue/100:,.2f}（{len(overdue_rows):,} 笔）｜ERP回款 {len(self.receipts):,} 条｜销项发票 {len(self.sales_invoices):,} 条"
        )
        self.render_m4_customers(); self.render_m4_months(); self.render_m4_receivables(); self.render_m4_receipts(); self.render_m4()

    def _m4_group_values(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        overdue_rows=[x for x in rows if int(x.get("outstandingFen") or 0)>0 and str(x.get("status") or "").upper()=="OVERDUE"]
        return {
            "total":sum(int(x.get("amountFen") or 0) for x in rows),
            "settled":sum(int(x.get("settledFen") or 0) for x in rows),
            "outstanding":sum(int(x.get("outstandingFen") or 0) for x in rows),
            "overdue":sum(int(x.get("outstandingFen") or 0) for x in overdue_rows),
            "overdueCount":len(overdue_rows),
            "invoiceCount":sum(self._m4_invoice_state(x)=="已开票" for x in rows),
            "earliestDue":min((str(x.get("dueOn")) for x in rows if x.get("dueOn")), default="—"),
        }

    def render_m4_customers(self) -> None:
        if not hasattr(self, "m4_customer"):
            return
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in self.receivables:
            key=str(item.get("customerId") or item.get("customerName") or "未识别客户")
            grouped.setdefault(key, []).append(item)
        keyword=self.m4_customer_search.text().strip().lower()
        result=[]
        for key, rows in grouped.items():
            name=str(rows[0].get("customerName") or key)
            if keyword and keyword not in f"{key} {name}".lower():
                continue
            summary=self._m4_group_values(rows)
            result.append((key,name,rows,summary))
        result.sort(key=lambda x:(-x[3]["overdue"],-x[3]["outstanding"],x[1]))
        self._m4_customer_visible=result
        self.m4_customer.setRowCount(len(result))
        for r,(key,name,rows,summary) in enumerate(result):
            values=[name,f"¥{summary['total']/100:,.2f}",f"¥{summary['settled']/100:,.2f}",f"¥{summary['outstanding']/100:,.2f}",f"¥{summary['overdue']/100:,.2f}",summary["overdueCount"],f"{summary['invoiceCount']}/{len(rows)}",summary["earliestDue"]]
            for c,value in enumerate(values):
                cell=QTableWidgetItem(str(value))
                if c in {3,4,5}:
                    cell.setForeground(QColor("#c8443c" if summary["overdue"] else ("#a66a08" if summary["outstanding"] else "#278455")))
                self.m4_customer.setItem(r,c,cell)
        if result and self.m4_customer.currentRow()<0:
            self.m4_customer.selectRow(0)
        elif not result:
            self.m4_customer_months.setRowCount(0); self.m4_customer_detail.setText("当前筛选条件下没有客户应收记录。")

    def show_m4_customer_detail(self) -> None:
        row=self.m4_customer.currentRow()
        if row<0 or row>=len(getattr(self,"_m4_customer_visible",[])):
            return
        key,name,rows,summary=self._m4_customer_visible[row]
        months: dict[str,list[dict[str,Any]]]={}
        for item in rows:
            month=str(item.get("issuedOn") or "日期缺失")[:7]
            months.setdefault(month,[]).append(item)
        month_rows=[]
        for month,items in months.items():
            month_rows.append((month,items,self._m4_group_values(items)))
        month_rows.sort(key=lambda x:x[0],reverse=True)
        self.m4_customer_months.setRowCount(len(month_rows))
        for r,(month,items,data) in enumerate(month_rows):
            values=[month,name,f"¥{data['total']/100:,.2f}",f"¥{data['settled']/100:,.2f}",f"¥{data['outstanding']/100:,.2f}",f"¥{data['overdue']/100:,.2f}",len(items)]
            for c,value in enumerate(values): self.m4_customer_months.setItem(r,c,QTableWidgetItem(str(value)))
        self._m4_customer_month_visible=month_rows
        missing=sum(self._m4_invoice_state(x)!="已开票" for x in rows)
        self.m4_customer_detail.setText(
            f"{name}（{key}）｜应收 {len(rows)} 笔｜已开票 {summary['invoiceCount']} 笔｜待核查发票 {missing} 笔｜"
            f"待收 ¥{summary['outstanding']/100:,.2f}｜逾期 ¥{summary['overdue']/100:,.2f}。"
        )

    def open_selected_m4_month(self) -> None:
        row=self.m4_customer_months.currentRow()
        if row<0:
            QMessageBox.information(self,"查看月份明细","请先选择右侧一个月份。"); return
        self.open_m4_month_detail(row,0)

    def open_m4_month_detail(self, row: int, _column: int=0) -> None:
        months=getattr(self,"_m4_customer_month_visible",[])
        customer_row=self.m4_customer.currentRow()
        if row<0 or row>=len(months) or customer_row<0 or customer_row>=len(getattr(self,"_m4_customer_visible",[])):
            return
        month,items,summary=months[row]
        _key,customer_name,_all_rows,_customer_summary=self._m4_customer_visible[customer_row]
        dialog=QDialog(self); dialog.setWindowTitle(f"{customer_name}｜{month}｜全部应收明细"); dialog.resize(1420,780)
        layout=QVBoxLayout(dialog)
        title=QLabel(f"{customer_name} · {month} 全部应收明细"); title.setObjectName("pageTitle"); layout.addWidget(title)
        metrics=QLabel(
            f"应收 {len(items)} 笔｜应收总额 ¥{summary['total']/100:,.2f}｜已收 ¥{summary['settled']/100:,.2f}｜"
            f"待收 ¥{summary['outstanding']/100:,.2f}｜逾期 ¥{summary['overdue']/100:,.2f}"
        )
        metrics.setObjectName("cards"); layout.addWidget(metrics)
        split=QSplitter(Qt.Vertical)
        table=QTableWidget(0,11); table.setHorizontalHeaderLabels(["台账编号","客户名称","发票号码","开票日期","到期日","应收金额","已核销","未收金额","状态","是否开票","ERP是否回款"])
        table.setSelectionBehavior(QTableWidget.SelectRows); table.setEditTriggers(QTableWidget.NoEditTriggers); table.setAlternatingRowColors(True); table.setWordWrap(False); table.horizontalHeader().setStretchLastSection(True)
        ordered=sorted(items,key=lambda x:(0 if str(x.get("status") or "").upper()=="OVERDUE" else 1,str(x.get("dueOn") or ""),str(x.get("id") or "")))
        table.setRowCount(len(ordered))
        labels={"OVERDUE":"逾期未收","OPEN":"未到期未收","SETTLED":"已结清"}
        for r,item in enumerate(ordered):
            status=str(item.get("status") or "").upper()
            allocations=self._m4_receipt_allocations.get(str(item.get("id") or ""),[])
            values=[
                item.get("id") or "—",item.get("customerName") or customer_name,item.get("invoiceNo") or "—",
                item.get("issuedOn") or "—",item.get("dueOn") or "—",f"¥{int(item.get('amountFen') or 0)/100:,.2f}",
                f"¥{int(item.get('settledFen') or 0)/100:,.2f}",f"¥{int(item.get('outstandingFen') or 0)/100:,.2f}",
                labels.get(status,status or "—"),self._m4_invoice_state(item),"是" if allocations or int(item.get("settledFen") or 0)>0 else "否",
            ]
            for c,value in enumerate(values):
                cell=QTableWidgetItem(str(value))
                if c in {0,8}: cell.setForeground(QColor("#c8443c" if status=="OVERDUE" else ("#a66a08" if status=="OPEN" else "#278455")))
                elif c==9: cell.setForeground(QColor("#278455" if self._m4_invoice_state(item)=="已开票" else "#a66a08"))
                elif c==10: cell.setForeground(QColor("#278455" if allocations or int(item.get("settledFen") or 0)>0 else "#a66a08"))
                table.setItem(r,c,cell)
        detail=QTextEdit(); detail.setReadOnly(True); detail.setMaximumHeight(230)
        def show_detail() -> None:
            selected=table.currentRow()
            if selected<0 or selected>=len(ordered): return
            item=ordered[selected]; allocations=self._m4_receipt_allocations.get(str(item.get("id") or ""),[])
            invoice=self._m4_invoice_for_item(item)
            tax_rate=invoice.get("taxRate") or invoice.get("taxRateText") or invoice.get("rate") or "—"
            if isinstance(tax_rate,(int,float)) and tax_rate < 1:
                tax_rate=f"{tax_rate*100:g}%"
            lines=[]
            for entry in allocations:
                receipt=entry["receipt"]; allocation=entry["allocation"]
                amount_fen=int(allocation.get("amountFen") or round(float(allocation.get("amount") or 0)*100))
                lines.append(f"{receipt.get('receiptNo') or receipt.get('id') or '回款'}｜{receipt.get('receivedOn') or '—'}｜{receipt.get('payerName') or '—'}｜¥{amount_fen/100:,.2f}")
            detail.setPlainText(
                f"应收ID：{item.get('id') or '—'}｜客户：{item.get('customerName') or customer_name}\n"
                f"发票：{item.get('invoiceNo') or '—'}｜{self._m4_invoice_state(item)}｜开票日期：{item.get('issuedOn') or '—'}｜到期日：{item.get('dueOn') or '—'}\n"
                f"应收 ¥{int(item.get('amountFen') or 0)/100:,.2f}｜已收 ¥{int(item.get('settledFen') or 0)/100:,.2f}｜待收 ¥{int(item.get('outstandingFen') or 0)/100:,.2f}\n"
                f"发票明细：类型 {invoice.get('type') or invoice.get('invoiceType') or '—'}｜发票代码 {invoice.get('invoiceCode') or item.get('invoiceCode') or '—'}｜发票号码 {invoice.get('invoiceNo') or item.get('invoiceNo') or '—'}\n"
                f"票种 {invoice.get('invoiceKind') or invoice.get('ticketType') or '—'}｜购方 {invoice.get('buyerName') or invoice.get('purchaserName') or '—'}｜销方 {invoice.get('sellerName') or invoice.get('seller') or '—'}\n"
                f"不含税额 ¥{int(invoice.get('amountWithoutTaxFen') or invoice.get('excludingTaxFen') or 0)/100:,.2f}｜税率 {tax_rate}｜税额 ¥{int(invoice.get('taxAmountFen') or 0)/100:,.2f}｜价税合计 ¥{int(invoice.get('totalFen') or invoice.get('amountFen') or 0)/100:,.2f}\n\n"
                f"ERP已登记回款分配：\n{chr(10).join(lines) or '尚无回款分配记录'}"
            )
        table.itemSelectionChanged.connect(show_detail)
        split.addWidget(table); split.addWidget(detail); split.setSizes([500,190]); layout.addWidget(split,1)
        close=QPushButton("关闭"); close.clicked.connect(dialog.accept); buttons=QHBoxLayout(); buttons.addStretch(); buttons.addWidget(close); layout.addLayout(buttons)
        if ordered: table.selectRow(0)
        dialog.exec()

    def render_m4_months(self) -> None:
        if not hasattr(self,"m4_month"):
            return
        grouped: dict[str,list[dict[str,Any]]]={}
        for item in self.receivables:
            month=str(item.get("issuedOn") or "日期缺失")[:7]
            grouped.setdefault(month,[]).append(item)
        rows=[]
        for month,items in grouped.items():
            rows.append((month,items,self._m4_group_values(items),len({str(x.get("customerId") or x.get("customerName") or "") for x in items})))
        rows.sort(key=lambda x:x[0],reverse=True)
        self.m4_month.setRowCount(len(rows))
        for r,(month,items,data,customers) in enumerate(rows):
            values=[month,f"¥{data['total']/100:,.2f}",f"¥{data['settled']/100:,.2f}",f"¥{data['outstanding']/100:,.2f}",f"¥{data['overdue']/100:,.2f}",len(items),customers]
            for c,value in enumerate(values): self.m4_month.setItem(r,c,QTableWidgetItem(str(value)))

    def render_m4_receivables(self) -> None:
        if not hasattr(self, "m4_ar"):
            return
        wanted=self.m4_ar_filter.currentText()
        keyword=self.m4_ar_search.text().strip().lower()
        rows=[]
        for item in self.receivables:
            outstanding=int(item.get("outstandingFen") or 0)
            status=str(item.get("status") or "").upper()
            if wanted=="全部未结应收" and outstanding<=0: continue
            if wanted=="已逾期" and not (outstanding>0 and status=="OVERDUE"): continue
            if wanted=="未到期" and not (outstanding>0 and status=="OPEN"): continue
            haystack=" ".join(str(item.get(key) or "") for key in ("id","invoiceNo","customerName")).lower()
            if keyword and keyword not in haystack: continue
            rows.append(item)
        rows.sort(key=lambda x:(0 if str(x.get("status") or "").upper()=="OVERDUE" else 1, str(x.get("dueOn") or ""), str(x.get("id") or "")))
        self._m4_ar_visible=rows
        self.m4_ar.setRowCount(len(rows))
        labels={"OVERDUE":"逾期未收","OPEN":"未到期未收","SETTLED":"已结清"}
        for r,item in enumerate(rows):
            status=str(item.get("status") or "").upper()
            due_state=labels.get(status, status or "—")
            allocations=self._m4_receipt_allocations.get(str(item.get("id") or ""),[])
            values=[
                due_state, item.get("dueOn") or "—", item.get("id") or "—", item.get("invoiceNo") or "—", self._m4_invoice_state(item),
                item.get("customerName") or "—", f"¥{int(item.get('amountFen') or 0)/100:,.2f}",
                f"¥{int(item.get('settledFen') or 0)/100:,.2f}", f"¥{int(item.get('outstandingFen') or 0)/100:,.2f}",
                f"{len(allocations)} 笔", labels.get(status, status or "—"),
            ]
            for c,value in enumerate(values):
                cell=QTableWidgetItem(str(value))
                if c in {0,4,8,10}:
                    cell.setForeground(QColor("#c8443c" if status=="OVERDUE" else ("#a66a08" if status=="OPEN" else "#278455")))
                self.m4_ar.setItem(r,c,cell)
        if rows and self.m4_ar.currentRow()<0:
            self.m4_ar.selectRow(0)
        elif not rows:
            self.m4_ar_detail.setPlainText("当前筛选条件下没有应收记录。")

    def show_m4_receivable_detail(self) -> None:
        row=self.m4_ar.currentRow()
        if row<0 or row>=len(getattr(self,"_m4_ar_visible",[])):
            return
        item=self._m4_ar_visible[row]
        status=str(item.get("status") or "").upper()
        state={"OVERDUE":"逾期未收，应优先催收并核对是否存在未认领回款。","OPEN":"尚未到期，持续跟踪回款。","SETTLED":"ERP已结清，仅供历史核对。"}.get(status,"请人工核对ERP状态。")
        allocations=self._m4_receipt_allocations.get(str(item.get("id") or ""),[])
        allocation_lines=[]
        for entry in allocations:
            receipt=entry["receipt"]; allocation=entry["allocation"]
            amount_fen=int(allocation.get("amountFen") or round(float(allocation.get("amount") or 0)*100))
            allocation_lines.append(f"- {receipt.get('receiptNo') or receipt.get('id') or '回款'}｜{receipt.get('receivedOn') or '—'}｜{receipt.get('payerName') or '—'}｜¥{amount_fen/100:,.2f}")
        invoice_state=self._m4_invoice_state(item)
        self.m4_ar_detail.setPlainText(
            f"应收ID：{item.get('id') or '—'}\n客户：{item.get('customerName') or '—'}（{item.get('customerId') or '—'}）\n"
            f"关联发票：{item.get('invoiceNo') or '—'}（{item.get('invoiceId') or '—'}）｜{invoice_state}\n开票日期：{item.get('issuedOn') or '—'}\n到期日：{item.get('dueOn') or '—'}\n\n"
            f"应收金额：¥{int(item.get('amountFen') or 0)/100:,.2f}\n已收金额：¥{int(item.get('settledFen') or 0)/100:,.2f}\n"
            f"未收金额：¥{int(item.get('outstandingFen') or 0)/100:,.2f}\nERP状态：{status or '—'}\n\nERP 已登记回款分配：\n{chr(10).join(allocation_lines) or '- 暂无回款分配记录'}\n\n"
            f"处理提示：{state}\n\n数据来源：GET /v1/receivables + GET /v1/receipts + GET /v1/invoices（全部只读）。\n"
            "边界：已结清代表 ERP 应收台账已结，不等于本工作台执行了银行核销；发票记录缺失时需核查同步、历史迁移或红冲/作废状态。"
        )

    def render_m4_receipts(self) -> None:
        if not hasattr(self,"m4_receipt"):
            return
        customers={str(x.get("id")):str(x.get("name") or x.get("shortName") or x.get("id")) for x in self.customers}
        rows=sorted(self.receipts,key=lambda x:(str(x.get("receivedOn") or ""),str(x.get("receiptNo") or "")),reverse=True)
        self.m4_receipt.setRowCount(len(rows))
        for r,item in enumerate(rows):
            allocations=item.get("allocations") or []
            allocated=sum(int(x.get("amountFen") or round(float(x.get("amount") or 0)*100)) for x in allocations)
            amount=int(item.get("amountFen") or round(float(item.get("amount") or 0)*100))
            diff=amount-allocated
            values=[item.get("receiptNo") or item.get("id") or "—",item.get("receivedOn") or "—",item.get("payerName") or "—",customers.get(str(item.get("customerId")),item.get("customerId") or "—"),f"¥{amount/100:,.2f}",len(allocations),f"¥{allocated/100:,.2f}",f"¥{diff/100:,.2f}"]
            for c,value in enumerate(values):
                cell=QTableWidgetItem(str(value))
                if c==7: cell.setForeground(QColor("#278455" if diff==0 else "#c8443c"))
                self.m4_receipt.setItem(r,c,cell)

    def _m4_erp_receipt_match(self, bank: dict[str, Any]) -> dict[str, Any] | None:
        bank_date=str(bank.get("date") or "")[:10].replace("/","-")
        bank_amount=int(bank.get("amountFen") or 0)
        bank_name=normalize_name(bank.get("payer"))
        bank_account="".join(ch for ch in str(bank.get("payerAccount") or "") if ch.isdigit())
        if bank_name:
            match=self._m4_erp_receipt_index.get((bank_date,bank_amount,f"name:{bank_name}"))
            if match:
                return match
        if bank_account:
            return self._m4_erp_receipt_index.get((bank_date,bank_amount,f"account:{bank_account}"))
        return None

    def _m4_bank_key(self, bank: dict[str, Any]) -> str:
        return f"{bank.get('sourceFile') or 'bank'}::{bank.get('id') or ''}"

    def _m4_prefer_erp_allocation(self, bank: dict[str, Any], result: dict[str, Any], erp_receipt: dict[str, Any] | None) -> dict[str, Any]:
        """Promote an ERP receipt allocation as the strongest visible evidence.

        It is still a candidate until a person confirms it; this method never
        changes ERP or the local manual decision.
        """
        if not erp_receipt:
            return result
        allocations = erp_receipt.get("allocations") or []
        ids = [str(item.get("receivableId") or "") for item in allocations if str(item.get("receivableId") or "")]
        if not ids:
            return result
        enriched = dict(result)
        best = dict(result.get("best") or {})
        best.update({
            "kind": "ERP回款分配候选",
            "score": 100,
            "confidence": "高",
            "receivableIds": ids,
            "reasons": ["银行流水与ERP回款同日、同额、同付款方", "ERP回款记录已分配到应收单"],
        })
        enriched["best"] = best
        enriched["candidates"] = [best] + [item for item in (result.get("candidates") or []) if item.get("receivableIds") != ids]
        enriched["classification"] = "ERP回款已关联"
        return enriched

    def render_m4(self) -> None:
        if not hasattr(self,"m4"):
            return
        if len(self.m4_matches) != len(self.bank_rows):
            self.m4_matches=match_all(self.bank_rows, self.receivables, include_settled=True, allow_partial=False)
        rows=[]
        search=self.m4_bank_search.text().strip().lower() if hasattr(self,"m4_bank_search") else ""
        wanted=self.m4_filter.currentText() if hasattr(self,"m4_filter") else "全部到账"
        for bank,result in zip(self.bank_rows,self.m4_matches):
            task=self.store.data["m4Tasks"].get(self._m4_bank_key(bank),self.store.data["m4Tasks"].get(str(bank["id"]),{}))
            erp_receipt=self._m4_erp_receipt_match(bank)
            result=self._m4_prefer_erp_allocation(bank, result, erp_receipt)
            direction=str(bank.get("direction") or "")
            is_credit=direction in {"贷", "收入", "收"}
            if task.get("status")=="已人工确认":
                state="已人工确认"
            elif task.get("status")=="已排除":
                state="已排除"
            elif not is_credit:
                state="非客户回款"
            elif task.get("status")=="待人工核对":
                state="待人工核对"
            elif result.get("classification") in {"高置信候选", "ERP回款已关联"}:
                state="候选可对应"
            elif result.get("classification") in {"待人工复核","疑似重复流水","客户已识别·金额未匹配"}:
                state="待人工核对"
            else:
                state="未匹配"
            best=result.get("best") or {}
            haystack=" ".join([
                str(bank.get("payer") or ""),str(bank.get("id") or ""),
                " ".join(best.get("receivableIds") or [])," ".join(best.get("invoiceNos") or []),
            ]).lower()
            if search and search not in haystack: continue
            if wanted=="贷方到账" and not is_credit: continue
            if wanted=="借方付款" and is_credit: continue
            if wanted=="已人工确认" and state!="已人工确认": continue
            if wanted=="候选可对应" and state!="候选可对应": continue
            if wanted=="待人工核对" and state!="待人工核对": continue
            if wanted=="未匹配" and state!="未匹配": continue
            if wanted=="已排除" and state!="已排除": continue
            rows.append((bank,result,task,state,erp_receipt))
        self._m4_visible=rows
        self.m4.setRowCount(len(rows))
        for r,(bank,result,task,state,erp_receipt) in enumerate(rows):
            direction=str(bank.get("direction") or "")
            is_credit=direction in {"贷", "收入", "收"}
            best=result.get("best") or {}
            candidate="、".join(task.get("receivableIds") or best.get("receivableIds") or []) or "—"
            values=[
                f"尾号{str(bank.get('companyAccountNo') or '')[-4:]}" if bank.get("companyAccountNo") else "—",
                str(bank.get("date") or "—").replace("/","-"),
                "贷方到账" if is_credit else "借方付款", bank.get("payer") or "—",
                f"¥{int(bank.get('amountFen') or 0)/100:,.2f}","已到账" if is_credit else "非回款",
                f"已登记：{erp_receipt.get('receiptNo') or erp_receipt.get('id')}" if erp_receipt else "未找到对应记录",
                result.get("classification") or "未匹配",candidate,state,
            ]
            for c,value in enumerate(values):
                cell=QTableWidgetItem(str(value))
                if c==5: cell.setForeground(QColor("#278455" if is_credit else "#7a8696"))
                elif c==6: cell.setForeground(QColor("#278455" if erp_receipt else "#a66a08"))
                elif c in {7,9}: cell.setForeground(QColor("#278455" if state=="已人工确认" else ("#7a8696" if state in {"非客户回款","已排除"} else ("#c8443c" if state=="未匹配" else "#a66a08"))))
                self.m4.setItem(r,c,cell)
        credit_rows=[x for x in self.bank_rows if str(x.get("direction") or "") in {"贷","收入","收"}]
        total_amount=sum(int(x.get("amountFen") or 0) for x in credit_rows)
        erp_registered=sum(self._m4_erp_receipt_match(x) is not None for x in credit_rows)
        candidate_count=sum(x.get("classification") in {"高置信候选","ERP回款已关联","待人工复核"} for x in self.m4_matches)
        unmatched=sum(x.get("classification")=="未匹配" for x in self.m4_matches)
        debit_count=len(self.bank_rows)-len(credit_rows)
        confirmed=sum(x.get("status")=="已人工确认" for x in self.store.data.get("m4Tasks",{}).values())
        if hasattr(self,"m4_bank_summary"):
            self.m4_bank_summary.setText(
                f"全部流水 {len(self.bank_rows):,} 笔｜贷方到账 {len(credit_rows):,} 笔｜借方付款 {debit_count:,} 笔｜贷方合计 ¥{total_amount/100:,.2f}｜"
                f"ERP找到同日同额同付款方回款 {erp_registered:,} 笔｜应收候选 {candidate_count:,} 笔｜未匹配 {unmatched:,} 笔｜已人工确认 {confirmed:,} 笔"
            )
        if rows and self.m4.currentRow()<0: self.m4.selectRow(0)
        elif not rows: self.m4_detail.setPlainText("当前筛选条件下没有银行到账记录。")

    def show_m4_detail(self) -> None:
        row=self.m4.currentRow()
        if row<0 or row>=len(getattr(self,"_m4_visible",[])): return
        bank,result,task,state,erp_receipt=self._m4_visible[row]
        best=result.get("best") or {}
        candidate_lines=[]
        for index,candidate in enumerate(result.get("candidates",[]),1):
            candidate_lines.append(
                f"{index}. {candidate.get('kind')}｜{candidate.get('confidence')}置信｜{candidate.get('score')}分\n"
                f"   应收ID：{'、'.join(candidate.get('receivableIds',[])) or '—'}｜发票：{'、'.join(candidate.get('invoiceNos',[])) or '—'}\n"
                f"   客户：{candidate.get('customerName') or '—'}｜待收合计：¥{int(candidate.get('outstandingFen') or 0)/100:,.2f}\n"
                f"   依据：{'；'.join(candidate.get('reasons',[])) or '—'}"
            )
        customer_lines=[]
        for index,candidate in enumerate(result.get("customerCandidates",[]),1):
            customer_lines.append(
                f"{index}. 客户名称匹配但金额不一致｜客户：{candidate.get('customerName') or '—'}｜"
                f"未收：¥{int(candidate.get('outstandingFen') or 0)/100:,.2f}｜"
                f"本次：¥{int(candidate.get('amountFen') or 0)/100:,.2f}"
            )
        candidate_text=chr(10).join(candidate_lines) or "未找到足额未收应收候选。"
        if customer_lines and not candidate_lines:
            candidate_text += "\n\n客户线索（不作为收款匹配）：\n" + chr(10).join(customer_lines)
            candidate_text += "\n\n说明：本业务口径只有“已收款/未收款”，不认定部分回款；金额不一致必须转人工核对。"
        company_account=str(bank.get("companyAccountNo") or "")
        payer_account=str(bank.get("payerAccount") or "")
        masked_company=f"****{company_account[-4:]}" if company_account else "—"
        masked_payer=f"****{payer_account[-4:]}" if payer_account else "—"
        erp_text=(
            f"已找到ERP回款：{erp_receipt.get('receiptNo') or erp_receipt.get('id')}｜{erp_receipt.get('receivedOn')}｜¥{int(erp_receipt.get('amountFen') or 0)/100:,.2f}"
            if erp_receipt else "ERP未找到同日、同额且付款方一致的回款记录；这不等于确认漏记，仍需财务核对。"
        )
        self.m4_detail.setPlainText(
            f"银行状态：{'已实际到账' if str(bank.get('direction') or '') in {'贷','收入','收'} else '借方付款，不进入客户应收匹配'}\n方向：{'贷方到账' if str(bank.get('direction') or '') in {'贷','收入','收'} else '借方付款'}\n交易日期：{bank.get('date','—')} {bank.get('time','')}\n付款方：{bank.get('payer','—')}（账号 {masked_payer}）\n"
            f"交易金额：¥{int(bank.get('amountFen') or 0)/100:,.2f}\n收款账户：{bank.get('companyAccountName') or '启衡精密制造有限公司'}（{masked_company}）\n"
            f"银行流水号：{bank.get('id','—')}\n来源文件：{bank.get('sourceFile') or '—'}\n附言：{bank.get('memo') or '—'}\n\n"
            f"ERP回款核对：{erp_text}\n\n应收对应结论：{result.get('classification','未匹配')}\n"
            f"匹配候选：\n{candidate_text}\n\n"
            f"人工状态：{state}\n人工确认应收：{'、'.join(task.get('receivableIds',[])) or '—'}\n备注：{task.get('note') or '—'}\n时间：{task.get('at') or '—'}\n\n"
            "说明：银行“已到账”是事实；应收对应与ERP登记是两项独立核对，系统不会自动核销。"
        )

    def m4_action(self, action: str) -> None:
        row=self.m4.currentRow()
        if row < 0 or row >= len(getattr(self,"_m4_visible",[])):
            QMessageBox.information(self, "M4", "请先选择一条银行流水。\n系统只会记录人工核销建议，不会自动核销或写回 ERP。"); return
        bank,result,old,_state,_erp_receipt=self._m4_visible[row]
        best=result.get("best") or {}
        if action=="已人工确认" and not (old.get("receivableIds") or best.get("receivableIds")):
            QMessageBox.information(self,"M4","当前流水没有候选应收，请先使用“人工选择候选应收”或暂存待复核。"); return
        note, ok=QInputDialog.getMultiLineText(self, "记录人工核销处理", "核对依据（合同号、附言、客户确认等，必填）")
        if not ok or not note.strip():
            QMessageBox.information(self, "M4", "请填写核对依据，才能保存人工处理留痕。"); return
        ids=old.get("receivableIds") or best.get("receivableIds") or []
        self.store.data["m4Tasks"][self._m4_bank_key(bank)]={"status":action,"reviewer":"周晓","note":note.strip(),"at":datetime.now().isoformat(timespec="seconds"),"receivableIds":ids,"matchClass":result.get("classification"),"score":best.get("score")}
        self._audit("M4",action,str(bank["id"]),f"应收：{'、'.join(ids) or '无'}｜{note.strip()}")
        self.store.save(); self.m4_status.setText("已保存本地人工核销处理；未调用 ERP 核销接口。"); self.render_m4(); self.refresh_quality_metrics()

    def m4_manual_link(self) -> None:
        row=self.m4.currentRow()
        if row<0 or row>=len(getattr(self,"_m4_visible",[])):
            QMessageBox.information(self,"M4","请先选择一条银行流水。"); return
        bank,result,task,_state,_erp_receipt=self._m4_visible[row]
        candidates=result.get("candidates",[])
        if not candidates:
            QMessageBox.information(self,"M4","系统没有生成候选。当前版本不允许凭空输入不存在的应收ID，请先核对应收台账。"); return
        labels=[f"{c.get('confidence')}｜{c.get('score')}分｜{'、'.join(c.get('receivableIds',[]))}｜{c.get('customerName')}" for c in candidates]
        selected,ok=QInputDialog.getItem(self,"人工选择候选应收","候选应收",labels,0,False)
        if not ok:return
        candidate=candidates[labels.index(selected)]
        note,ok=QInputDialog.getMultiLineText(self,"人工选择候选应收","选择依据（合同号、附言、客户确认等，必填）")
        if not ok or not note.strip():return
        self.store.data["m4Tasks"][self._m4_bank_key(bank)]={"status":"待人工核对","reviewer":"周晓","note":note.strip(),"at":datetime.now().isoformat(timespec="seconds"),"receivableIds":candidate.get("receivableIds",[]),"manualSelection":True,"matchClass":result.get("classification"),"score":candidate.get("score")}
        self._audit("M4","人工选择候选应收",str(bank["id"]),f"{'、'.join(candidate.get('receivableIds',[]))}｜{note.strip()}")
        self.store.save(); self.render_m4()

    def export_m4_receivables(self) -> None:
        if not self.receivables:
            QMessageBox.information(self,"导出应收台账","尚未读取 ERP 应收台账，请先点击“刷新应收台账（只读）”。"); return
        path,_=QFileDialog.getSaveFileName(self,"导出应收台账",str(Path.home()/"启衡应收台账.csv"),"CSV 文件 (*.csv)")
        if not path:return
        with open(path,"w",encoding="utf-8-sig",newline="") as handle:
            writer=csv.writer(handle); writer.writerow(["到期状态","到期日","应收ID","发票号码","是否开票","客户","应收金额","已收金额","未收金额","回款分配笔数","ERP状态","数据来源"])
            for item in self.receivables:
                status=str(item.get("status") or "").upper()
                writer.writerow([
                    {"OVERDUE":"逾期未收","OPEN":"未到期未收","SETTLED":"已结清"}.get(status,status), item.get("dueOn"), item.get("id"), item.get("invoiceNo"), self._m4_invoice_state(item),
                    item.get("customerName"), f"{int(item.get('amountFen') or 0)/100:.2f}", f"{int(item.get('settledFen') or 0)/100:.2f}",
                    f"{int(item.get('outstandingFen') or 0)/100:.2f}", len(self._m4_receipt_allocations.get(str(item.get("id") or ""),[])), status,
                    "GET /v1/receivables + /v1/receipts + /v1/invoices（只读）",
                ])
        QMessageBox.information(self,"导出完成",f"应收台账已保存：\n{path}")

    def export_m4(self) -> None:
        if not self.bank_rows:
            QMessageBox.information(self,"导出银行到账核对","请先导入银行流水并执行匹配。");return
        path,_=QFileDialog.getSaveFileName(self,"导出银行到账核对",str(Path.home()/"启衡银行到账与应收核对.csv"),"CSV 文件 (*.csv)")
        if not path:return
        with open(path,"w",encoding="utf-8-sig",newline="") as handle:
            writer=csv.writer(handle); writer.writerow(["交易日期","收款账户","银行流水号","借贷方向","对方","交易金额","银行事实","ERP回款登记","应收对应状态","候选应收ID","人工状态","人工备注","处理时间","来源文件","边界说明"])
            for bank,result in zip(self.bank_rows,self.m4_matches):
                task=self.store.data["m4Tasks"].get(self._m4_bank_key(bank),self.store.data["m4Tasks"].get(str(bank["id"]),{})); erp=self._m4_erp_receipt_match(bank); result=self._m4_prefer_erp_allocation(bank,result,erp); best=result.get("best") or {}
                writer.writerow([
                    bank.get("date"),f"尾号{str(bank.get('companyAccountNo') or '')[-4:]}",bank.get("id"),
                    "贷方到账" if str(bank.get("direction") or "") in {"贷","收入","收"} else "借方付款",bank.get("payer"),
                    f"{int(bank.get('amountFen') or 0)/100:.2f}","已到账" if str(bank.get("direction") or "") in {"贷","收入","收"} else "非客户回款",
                    (erp.get("receiptNo") or erp.get("id")) if erp else "未找到同日同额同付款方记录",
                    result.get("classification"),"、".join(task.get("receivableIds") or best.get("receivableIds") or []),
                    task.get("status","待人工核对"),task.get("note",""),task.get("at",""),bank.get("sourceFile"),
                    "银行到账为CSV事实；应收对应仅为候选，未自动核销、未写回ERP",
                ])
        QMessageBox.information(self,"导出完成",f"银行到账核对清单已保存：\n{path}")

    def export_m4_official(self) -> None:
        """Export the exact official M4 fragment after human confirmation."""
        if not self.bank_rows:
            QMessageBox.information(self, "导出官方 M4 JSON", "请先导入银行流水并读取应收台账。")
            return
        matches: list[dict[str, Any]] = []
        unidentified: list[str] = []
        for bank in self.bank_rows:
            if str(bank.get("direction") or "") not in {"贷", "收入", "收"}:
                continue
            key = self._m4_bank_key(bank)
            task = self.store.data.get("m4Tasks", {}).get(key, self.store.data.get("m4Tasks", {}).get(str(bank.get("id") or ""), {}))
            ids = [str(value) for value in (task.get("receivableIds") or []) if str(value)]
            if task.get("status") == "已人工确认" and ids:
                matches.append({"txnId": str(bank.get("id") or ""), "receivableIds": ids})
            else:
                unidentified.append(str(bank.get("id") or ""))
        path, _ = QFileDialog.getSaveFileName(self, "导出官方 M4 JSON", str(Path.home() / "启衡M4官方结果.json"), "JSON 文件 (*.json)")
        if not path:
            return
        payload = {"m4": {"matches": matches, "unidentified": unidentified}}
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._audit("M4", "导出官方M4结果", Path(path).name, f"matches={len(matches)} unidentified={len(unidentified)}")
        self.store.save()
        QMessageBox.information(self, "官方 M4 JSON 已导出", f"匹配 {len(matches)} 笔；未识别 {len(unidentified)} 笔。\n\n说明：只有人工确认的关系进入 matches；其余贷方流水进入 unidentified。\n文件：{path}")

    def _quality_snapshot(self) -> dict[str, Any]:
        try:
            public=json.loads(PUBLIC_REPORT.read_text(encoding="utf-8"))
        except Exception:
            public={"total":0,"verdictMatches":0,"codeMatches":0}
        try:
            report_path=LOCAL_OCR_REPORT if LOCAL_OCR_REPORT.exists() else BUNDLED_OCR_REPORT
            ocr=json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            ocr={"sampleSize":0,"humanConfirmedTickets":0,"unreadableOnOriginal":0,"fields":{}}
        cache=list(self.store.data.get("analysisCache",{}).values())
        decisions=[(item.get("result") or {}).get("result") for item in cache]
        durations=[float(item.get("durationSeconds")) for item in cache if item.get("durationSeconds") is not None]
        manual=list(self.store.data.get("manual",{}).values())
        confirmed=sum(self._workflow_state(key) in {"CONFIRMED","WRITTEN"} for key in self.store.data.get("manual",{}))
        return {
            "generatedAt":datetime.now().isoformat(timespec="seconds"),
            "ruleVersion":RULE_VERSION,
            "public30":public,
            "ocr":ocr,
            "dailyTruthSamples":list(self.store.data.get("truthSamples",[])),
            "m2":{
                "analyzed":len(cache),
                "reject":decisions.count("REJECT"),
                "flag":decisions.count("FLAG"),
                "approve":decisions.count("APPROVE"),
                "manualConfirmed":confirmed,
                "manualReviewRate":round(sum(x=="FLAG" for x in decisions)/len(decisions),4) if decisions else 0,
                "abnormalHitRate":round(sum(x=="REJECT" for x in decisions)/len(decisions),4) if decisions else 0,
                "averageSeconds":round(sum(durations)/len(durations),3) if durations else None,
            },
            "m3":{
                "scanned":(self.m3 or {}).get("summary",{}).get("totalInvoices",0),
                "issues":len((self.m3 or {}).get("details",[])),
                "duplicateGroups":len((self.m3 or {}).get("duplicateInvoices",[])),
                "tasks":len(self.store.data.get("m3Tasks",{})),
            },
            "m4":{
                "receivables":len(self.receivables),
                "receipts":len(self.receipts),
                "salesInvoices":len(self.sales_invoices),
                "customers":len({str(x.get("customerId") or x.get("customerName") or "") for x in self.receivables if x.get("customerId") or x.get("customerName")}),
                "tasks":len(self.store.data.get("m4Tasks",{})),
            },
            "auditEvents":len(self.store.data.get("auditLog",[])),
        }

    def refresh_quality_metrics(self) -> None:
        if not hasattr(self,"quality_badge"): return
        snap=self._quality_snapshot(); ocr=snap["ocr"]; fields=ocr.get("fields",{})
        self.quality_badge.setText(f"OCR人工真值 {ocr.get('humanConfirmedTickets',0)}/{ocr.get('sampleSize',0)}｜新增校准 {len(snap['dailyTruthSamples'])}")
        if not hasattr(self,"quality_cards"): return
        public=snap["public30"]; m2=snap["m2"]; m3=snap["m3"]; m4=snap["m4"]
        self.quality_cards.setText(
            f"公开30单：结论 {public.get('verdictMatches',0)}/{public.get('total',0)} · 代码 {public.get('codeMatches',0)}/{public.get('total',0)}    "
            f"M2：已分析 {m2['analyzed']} · 明确异常 {m2['reject']} · 人工复核 {m2['flag']} · 建议通过 {m2['approve']}    "
            f"M3：台账 {m3['scanned']:,} · 待核查 {m3['issues']} · 重复组 {m3['duplicateGroups']}    "
            f"M4：客户 {m4['customers']} · 应收 {m4['receivables']} · ERP回款 {m4['receipts']} · 销项发票 {m4['salesInvoices']}"
        )
        labels={"invoice_code":"发票代码","invoice_no":"发票号码","issued_on":"开票日期","buyer_name":"购买方名称","buyer_tax_no":"购买方税号","seller_name":"销售方名称","total_fen":"价税合计"}
        self.quality_fields.setRowCount(len(labels))
        for row,(key,label) in enumerate(labels.items()):
            item=fields.get(key,{})
            values=[label,item.get("labeled",0),item.get("correct",0),int(item.get("missing",0))+int(item.get("wrong",0)),f"{float(item.get('accuracy',0))*100:.2f}%"]
            for col,value in enumerate(values):
                cell=QTableWidgetItem(str(value))
                if col==4 and float(item.get("accuracy",0))<0.9: cell.setForeground(QColor("#c8443c"))
                self.quality_fields.setItem(row,col,cell)
        avg="尚无新运行耗时" if m2["averageSeconds"] is None else f"{m2['averageSeconds']:.2f}秒/单"
        self.quality_text.setPlainText(
            f"结论先行\n\n"
            f"1. 公开30单验收：结论与官方违规代码分别为 {public.get('verdictMatches',0)}/{public.get('total',0)}、{public.get('codeMatches',0)}/{public.get('total',0)}。\n"
            f"2. OCR人工真值：已确认 {ocr.get('humanConfirmedTickets',0)}/{ocr.get('sampleSize',0)}，原图不可读 {ocr.get('unreadableOnOriginal',0)}；日常新增人工真值 {len(snap['dailyTruthSamples'])} 条。\n"
            f"3. M2处理：异常命中率 {m2['abnormalHitRate']*100:.2f}%，人工复核率 {m2['manualReviewRate']*100:.2f}%，当前可统计平均处理耗时 {avg}。\n"
            f"4. 规则版本：{RULE_VERSION}。人工校准只影响本单并沉淀真值样本，不会自动改变全局规则。\n"
            f"5. 108类是内部异常诊断体系；官方submission仍只允许10个M2违规代码。\n"
            f"6. M3不改票；M4通过API只读汇总应收、ERP回款和销项发票，不自动核销，也不伪造ERP回写。"
        )

    def export_quality_report(self) -> None:
        path,_=QFileDialog.getSaveFileName(self,"导出验收与运营报告",str(Path.home()/"启衡财务稽核_验收与运营报告.md"),"Markdown 文件 (*.md)")
        if not path:return
        snap=self._quality_snapshot(); m2=snap["m2"]; m3=snap["m3"]; m4=snap["m4"]; ocr=snap["ocr"]; public=snap["public30"]
        text=(
            f"# 启衡财务稽核工作台验收与运营报告\n\n"
            f"- 生成时间：{snap['generatedAt']}\n- 规则版本：{RULE_VERSION}\n\n"
            f"## M2\n\n- 公开30单结论：{public.get('verdictMatches',0)}/{public.get('total',0)}\n- 官方代码一致：{public.get('codeMatches',0)}/{public.get('total',0)}\n- 已分析：{m2['analyzed']}\n- 异常命中率：{m2['abnormalHitRate']*100:.2f}%\n- 人工复核率：{m2['manualReviewRate']*100:.2f}%\n- 平均处理耗时：{m2['averageSeconds'] if m2['averageSeconds'] is not None else '待积累'}\n\n"
            f"## OCR人工真值\n\n- 基线样本：{ocr.get('sampleSize',0)}\n- 已确认：{ocr.get('humanConfirmedTickets',0)}\n- 不可读：{ocr.get('unreadableOnOriginal',0)}\n- 日常新增真值：{len(snap['dailyTruthSamples'])}\n\n"
            f"## M3\n\n- 扫描台账：{m3['scanned']}\n- 待核查：{m3['issues']}\n- 重复组：{m3['duplicateGroups']}\n- 已建任务：{m3['tasks']}\n\n"
            f"## M4 应收台账\n\n- 客户：{m4['customers']}\n- 应收台账：{m4['receivables']}\n- ERP客户回款：{m4['receipts']}\n- 销项发票：{m4['salesInvoices']}\n\n"
            f"## 边界\n\nM2仅写审核意见，不改变单据状态；M3不回写ERP；M4只读展示应收、回款和发票关联，不自动核销。\n"
        )
        Path(path).write_text(text,encoding="utf-8")
        QMessageBox.information(self,"导出完成",f"验收与运营报告已保存：\n{path}")

    def export_108_matrix(self) -> None:
        if not ANOMALY_MATRIX.exists():
            QMessageBox.warning(self,"108类覆盖矩阵","交付包中未找到108类覆盖矩阵。");return
        path,_=QFileDialog.getSaveFileName(self,"导出108类覆盖矩阵",str(Path.home()/"启衡108类异常覆盖矩阵.json"),"JSON 文件 (*.json)")
        if not path:return
        shutil.copy2(ANOMALY_MATRIX,path)
        QMessageBox.information(self,"导出完成",f"108类内部异常覆盖矩阵已保存：\n{path}\n\n官方submission仍只使用10个违规代码。")

    def export_current_audit_package(self) -> None:
        if not self.current_id or self.current_id not in self.outcomes:
            QMessageBox.information(self,"导出单据审计包","请先选择并完成一张报销单的初审。");return
        base=QFileDialog.getExistingDirectory(self,"选择审计包保存位置")
        if not base:return
        packet=self.outcomes[self.current_id]; claim=packet.get("claim") or {}; claim_no=claim.get("claimNo") or self.current_id
        target=Path(base)/f"{claim_no}_审计包_{datetime.now().strftime('%Y%m%d-%H%M%S')}"; attachments_dir=target/"原票附件"; attachments_dir.mkdir(parents=True,exist_ok=True)
        copied=[]
        for item in packet.get("attachments",[]):
            source=Path(str(item.get("image") or ""))
            if source.exists():
                destination=attachments_dir/source.name; shutil.copy2(source,destination); copied.append(str(destination.name))
        manual=self.store.data.get("manual",{}).get(self.current_id,{})
        calibrations=self.store.data.get("calibrations",{}).get(self.current_id,[])
        audit=[x for x in self.store.data.get("auditLog",[]) if x.get("refId")==self.current_id]
        payload={"claim":claim,"result":packet.get("result"),"approvals":packet.get("approvals"),"manualDecision":manual,"calibrations":calibrations,"auditLog":audit,"ruleVersion":RULE_VERSION,"attachments":copied}
        (target/"审计证据.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
        result=packet.get("result") or {}
        md=(
            f"# 报销单 {claim_no} 审计包\n\n- 规则版本：{RULE_VERSION}\n- AI建议：{result_label(result.get('result'))}\n- 财务最终意见：{result_label(manual.get('decision'))}\n- 证据状态：{self._evidence_status({'id':self.current_id})}\n- 原票附件：{len(copied)} 个\n\n"
            f"## AI审核理由\n\n"+"\n".join(f"- {x}" for x in result.get("reasons",[]))+
            f"\n\n## 人工处理\n\n- 操作人：{manual.get('reviewer','—')}\n- 时间：{manual.get('at','—')}\n- 说明：{manual.get('reason','—')}\n\n"
            "## 系统边界\n\n本审计包记录审核意见与证据，不证明ERP单据状态已经改变。\n"
        )
        (target/"审计说明.md").write_text(md,encoding="utf-8")
        QMessageBox.information(self,"导出完成",f"单据审计包已保存：\n{target}")

    def _style(self) -> None:
        self.setStyleSheet("""QMainWindow,QWidget{background:#f3f6fa;font-family:'Microsoft YaHei UI','Microsoft YaHei','SimSun';font-size:13px;color:#1d344a}#side{background:#fff;border-right:1px solid #dbe4ee}#sideBrand{font-size:21px;font-weight:800;color:#123e68;padding:10px 8px 16px}#navGroup{background:#fff;color:#718096;font-size:11px;margin:15px 8px 5px}QPushButton#nav{color:#1d344a;text-align:left;border:0;background:#fff;padding:10px 12px;border-radius:5px;margin:1px 0}QPushButton#nav:hover{color:#164f82;background:#edf4fb}QPushButton#nav:checked{background:#e7f1fa;color:#164f82;font-weight:700;border-left:4px solid #2463a0;padding-left:8px}#heading{font-size:23px;font-weight:800;color:#123e68}#subtitle,#hint,#crumb{color:#6d8195}#crumb{font-size:11px}#pageTitle{font-size:23px;font-weight:800;color:#1d344a}QPushButton#qualityBadge{background:#eaf7ef;color:#278455;padding:6px 11px;border-radius:13px;font-weight:700}QPushButton#qualityBadge:hover{background:#d9f0e2}#cards{background:#fff;border:1px solid #dbe4ee;padding:10px 14px;border-radius:8px;font-size:15px;font-weight:700;color:#163f68}QTableWidget{background:#fff;alternate-background-color:#f8fafc;gridline-color:#e1e8ef;border:1px solid #dbe4ee;border-radius:8px}QHeaderView::section{background:#edf3f8;padding:9px;border:0;border-bottom:1px solid #dbe4ee;font-weight:700;color:#52687d}QTableWidget::item:selected{background:#dcecf9;color:#163f68}QPushButton{background:#2463a0;color:#fff;border:0;border-radius:6px;padding:9px 13px;font-weight:700}QPushButton:hover{background:#1b5488}QPushButton#danger{background:#c8443c}QPushButton#danger:hover{background:#ad3731}QTextEdit{background:#fff;border:1px solid #dbe4ee;border-radius:8px;padding:12px;line-height:1.55}#evidence{background:#fff8e9;border-left:4px solid #b87915;border-radius:5px;padding:12px;line-height:1.55}#panelTitle{font-size:16px;font-weight:800;background:#fff;color:#163f68;padding:2px}#ticketTitle{background:#eef4fa;color:#315a7d;padding:8px;border-radius:5px;font-weight:700}#ticketPreview{background:#fffdf7;border:1px solid #dfd5bd;color:#7b6845;border-radius:5px;padding:8px}#settings{background:#fff;padding:18px;border:1px solid #dbe4ee;border-radius:8px;font-size:15px}#title{font-size:28px;font-weight:800;color:#123e68}""")

    def _theme_changed(self, name: str) -> None:
        """Apply a local stylesheet overlay; never changes data or rules."""
        if not hasattr(self, "_base_style"):
            return
        overlays = {
            "ERP 浅色（推荐）": "",
            "深蓝办公": """QMainWindow,QWidget{background:#edf3f8;color:#17324d}#side{background:#123e68;border-right:1px solid #2b5d86}#sideBrand,#navGroup{color:#dcecff}QPushButton#nav{background:transparent;color:#edf6ff}QPushButton#nav:hover{background:#1f5887;color:#fff}QPushButton#nav:checked{background:#2b6d9f;color:#fff;border-left-color:#f6c453}#heading,#pageTitle{color:#174b78}#cards,QTextEdit,QTableWidget,#settings{background:#fff}QHeaderView::section{background:#dfeaf4;color:#294c6b}QPushButton{background:#1f628f}QPushButton:hover{background:#174b72}""",
            "夜间高对比": """QMainWindow,QWidget{background:#20252b;color:#f2f5f7}#side{background:#15191e;border-right:1px solid #3a444e}#sideBrand,#heading,#pageTitle{color:#f5c95b}#navGroup,#subtitle,#hint,#crumb{color:#b9c7d3}QPushButton#nav{background:transparent;color:#e7edf2}QPushButton#nav:hover{background:#303b46;color:#fff}QPushButton#nav:checked{background:#3d4d5d;color:#fff;border-left-color:#f5c95b}#cards,QTextEdit,#settings{background:#29323b;color:#f2f5f7;border-color:#53616e}QTableWidget{background:#252d35;color:#f2f5f7;alternate-background-color:#2c3741;gridline-color:#53616e;border-color:#53616e}QHeaderView::section{background:#36434f;color:#fff;border-color:#53616e}QTableWidget::item:selected{background:#806b27;color:#fff}QPushButton{background:#2f6f9b;color:#fff}QPushButton:hover{background:#3d87b7}#evidence{background:#3a3321;color:#fff1c2}#ticketTitle{background:#2d3b47;color:#d7edff}#ticketPreview{background:#332e24;color:#ffe6a1}""",
        }
        self.setStyleSheet(self._base_style + overlays.get(name, ""))


def main() -> None:
    app=QApplication(sys.argv); app.setFont(QFont("Microsoft YaHei UI", 10)); login=Login(); login.resize(520,410)
    def enter(client:QihengClient,url:str)->None:
        login.hide(); window=Workbench(client,url); window.show(); app._window=window
    login.connected.connect(enter); login.show(); raise SystemExit(app.exec())

if __name__ == "__main__": main()
