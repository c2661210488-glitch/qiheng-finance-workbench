from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication


ROOT = Path(__file__).resolve().parents[1]
# Test the same desktop entry point that is packaged into the formal M1–M3 EXE.
# Keeping this path aligned with the delivery source prevents a green test run
# against an older prototype that is not included in the submission archive.
MODULE_PATH = ROOT / "m4-final-20260731" / "finance_workbench.py"
SPEC = importlib.util.spec_from_file_location("finance_workbench_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class FakeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.existing_reviews: list[dict] = []

    def iter_expense_claims(self, _status: str):
        return iter(())

    def request(self, path: str, method: str = "GET", body=None):
        if method == "POST":
            self.posts.append((path, body or {}))
            return {"ok": True}
        if path.endswith("/reviews"):
            return {"data": list(self.existing_reviews)}
        return {"data": []}

    def me(self):
        """Mirror the least-privilege capability check used before write-back."""
        return {"scopes": ["expense:review"]}

    def get_claim(self, claim_id: str):
        return {"id": claim_id, "claimNo": "BX-TEST-001", "lines": [], "totalFen": 10000}

    def approvals_for_claim(self, _claim_id: str):
        return {"data": []}


class FinanceWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        module.DATA_DIR = Path(self.tmp.name)
        module.STATE = module.DATA_DIR / "manual-state.json"
        module.Workbench._setup_tray = lambda _self: None
        module.Workbench.load_claims = lambda _self: None
        module.QMessageBox.information = lambda *_args, **_kwargs: module.QMessageBox.Ok
        module.QMessageBox.warning = lambda *_args, **_kwargs: module.QMessageBox.Ok
        module.QMessageBox.question = lambda *_args, **_kwargs: module.QMessageBox.Yes
        self.client = FakeClient()
        self.window = module.Workbench(self.client, "http://127.0.0.1:8081")
        self.app.processEvents()

        self.claim = {
            "id": "claim-1",
            "claimNo": "BX-TEST-001",
            "employeeName": "周晓",
            "departmentName": "财务部",
            "totalFen": 10000,
        }
        self.window.claims = [self.claim]
        self.window.outcomes["claim-1"] = {
            "result": {"result": "REJECT", "reasons": ["票面金额与申报金额不一致"]},
            "run_dir": "run-test",
        }
        self.window.render_queue()
        self.window.current_id = "claim-1"

    def tearDown(self) -> None:
        self.window._allow_exit = True
        self.window.close()
        self.tmp.cleanup()

    def test_confirm_ai_moves_claim_to_write_queue_without_post(self) -> None:
        self.window.confirm_ai_decision()
        item = self.window.store.data["manual"]["claim-1"]
        self.assertEqual(item["decision"], "REJECT")
        self.assertEqual(item["workflowState"], "CONFIRMED")
        self.assertEqual(item["writeState"], "待预检")
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.window.queue.rowCount(), 0)
        self.assertEqual(self.window.write_table.rowCount(), 1)
        self.assertEqual(
            self.window.pages.currentIndex(),
            self.window.order["m2"],
            "确认后应留在 M2 连续处理下一张，只更新批量写回队列数量",
        )

    def test_explicit_finance_approve_overrides_ai_and_moves_to_write_queue(self) -> None:
        self.window.finalize_current_claim("APPROVE")
        item = self.window.store.data["manual"]["claim-1"]
        self.assertEqual(item["decision"], "APPROVE")
        self.assertEqual(item["ai"], "REJECT")
        self.assertEqual(item["decisionSource"], "HUMAN_FINAL")
        self.assertEqual(item["workflowState"], "CONFIRMED")
        self.assertEqual(self.window.queue.rowCount(), 0)
        self.assertEqual(self.window.write_table.rowCount(), 1)
        self.assertEqual(self.client.posts, [])

    def test_explicit_finance_reject_keeps_audit_trail_without_post(self) -> None:
        self.window.finalize_current_claim("REJECT")
        item = self.window.store.data["manual"]["claim-1"]
        self.assertEqual(item["decision"], "REJECT")
        self.assertTrue(any(log["action"].startswith("财务") for log in self.window.store.data["auditLog"]))
        self.assertEqual(self.client.posts, [])

    def test_confirm_precheck_and_writeback_are_linked(self) -> None:
        self.window._run = lambda fn, done: done(fn())
        self.window.finalize_current_claim("REJECT")
        self.window.write_table.item(0, 0).setCheckState(module.Qt.Checked)
        self.window.precheck()
        item = self.window.store.data["manual"]["claim-1"]
        self.assertEqual(item["writeState"], "可写回")
        self.window.writeback()
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.posts[0][1]["result"], "REJECT")
        self.assertEqual(item["workflowState"], "WRITTEN")

    def test_same_erp_verdict_is_not_mislabeled_as_conflict(self) -> None:
        self.window._run = lambda fn, done: done(fn())
        self.client.existing_reviews = [{"result": "REJECT", "reasons": ["历史原因"], "createdAt": "2026-07-30"}]
        self.window.finalize_current_claim("REJECT")
        self.window.write_table.item(0, 0).setCheckState(module.Qt.Checked)
        self.window.precheck()
        item = self.window.store.data["manual"]["claim-1"]
        self.assertEqual(item["writeState"], "结论一致")
        self.assertIn("均为建议驳回", item["conflictReason"])

    def test_return_from_write_queue_restores_m2_and_preserves_history(self) -> None:
        self.window.confirm_ai_decision()
        item = self.window.store.data["manual"]["claim-1"]
        item["workflowState"] = "WRITTEN"
        item["writeState"] = "已写回"
        item["writeHistory"] = [{"at": "2026-07-30T10:00:00", "decision": "REJECT"}]
        self.window.render_writeback()
        self.window.write_table.item(0, 0).setCheckState(module.Qt.Checked)
        self.window.current_id = None
        self.window.return_selected_to_review()

        restored = self.window.store.data["manual"]["claim-1"]
        self.assertEqual(restored["workflowState"], "RETURNED")
        self.assertTrue(restored["returnedAfterWrite"])
        self.assertEqual(len(restored["writeHistory"]), 1)
        self.assertEqual(self.window.write_table.rowCount(), 0)
        self.assertEqual(self.window.queue.rowCount(), 1)

    def test_calibration_state_requires_second_confirmation(self) -> None:
        self.window._merge_manual(
            "claim-1",
            decision=None,
            overrideDecision=None,
            ai="REJECT",
            workflowState="RECONFIRM",
            writeState="校准后待二次确认",
            writeHistory=[{"at": "2026-07-30T10:00:00", "decision": "APPROVE"}],
        )
        self.window.store.save()
        self.window.current_id = None
        self.window.render_queue()
        self.window.render_writeback()
        self.assertEqual(self.window.queue.rowCount(), 1)
        self.assertEqual(self.window.write_table.rowCount(), 0)

        self.window.current_id = "claim-1"
        self.window.confirm_ai_decision()
        item = self.window.store.data["manual"]["claim-1"]
        self.assertEqual(item["workflowState"], "CONFIRMED")
        self.assertEqual(len(item["writeHistory"]), 1)
        self.assertEqual(self.client.posts, [])

    def test_reconfirmed_claim_is_labeled_as_second_review(self) -> None:
        self.window._merge_manual(
            "claim-1",
            decision=None,
            ai="REJECT",
            workflowState="RECONFIRM",
            writeState="校准后待二次确认",
        )
        self.window._show_outcome(self.window.outcomes["claim-1"])
        text = self.window.evidence.toPlainText()
        self.assertIn("二审建议：建议驳回", text)
        self.assertIn("人工真值已保存并完成整单重判", text)


if __name__ == "__main__":
    unittest.main()
