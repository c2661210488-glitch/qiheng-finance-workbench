from __future__ import annotations

"""Build human-readable, structured evidence from deterministic review output."""

from typing import Any


RULE_TITLES = {
    "OVER_STANDARD_HOTEL": "住宿费超标",
    "OVER_STANDARD_MEAL": "伙食补助超标",
    "OVER_STANDARD_CITY_TRANSPORT": "市内交通超标",
    "OVER_STANDARD_TRANSPORT_CLASS": "长途交通舱位超标",
    "INVOICE_TITLE_MISMATCH": "发票抬头错误",
    "INVOICE_TAXNO_MISMATCH": "发票税号错误",
    "MISSING_APPROVAL_OVERTIME_TAXI": "加班打车缺事前审批",
    "MISSING_ATTACHMENT": "缺少票据附件",
    "DUPLICATE_INVOICE": "重复发票",
    "AMOUNT_MISMATCH": "报销金额与票面金额不一致",
}

# Every card names its rule source and the concrete action a finance reviewer
# can take.  The exact clause numbers are kept conservative until the policy
# owner supplies a clause-indexed version of the source documents.
RULE_GUIDANCE = {
    "OVER_STANDARD_HOTEL": ("《费用报销管理办法 V3.2》差旅住宿标准", "核对职级、城市档次、住宿晚数及财务总监事前特批；无有效特批则按超标金额退回。"),
    "OVER_STANDARD_MEAL": ("《费用报销管理办法 V3.2》差旅伙食补助标准", "核对行程天数和有效特批；无有效特批则按超标金额退回。"),
    "OVER_STANDARD_CITY_TRANSPORT": ("《费用报销管理办法 V3.2》差旅市内交通标准", "核对行程天数和有效特批；无有效特批则按超标金额退回。"),
    "OVER_STANDARD_TRANSPORT_CLASS": ("《费用报销管理办法 V3.2》长途交通席别标准", "核对原票席别、员工职级及事前特批；无有效特批则退回。"),
    "INVOICE_TITLE_MISMATCH": ("《发票合规指引（2025-06）》购买方名称要求", "请更换购买方名称为“启衡精密制造有限公司”的合规发票。"),
    "INVOICE_TAXNO_MISMATCH": ("《发票合规指引（2025-06）》购买方税号要求", "请更换购买方税号为公司登记税号的合规发票。"),
    "MISSING_APPROVAL_OVERTIME_TAXI": ("《费用报销管理办法 V3.2》加班用车事前审批要求", "补充费用发生前由直属主管作出的、覆盖本次加班用车的审批；事后审批不替代事前审批。"),
    "MISSING_ATTACHMENT": ("《费用报销管理办法 V3.2》票据附件要求", "补齐与该费用行对应的原始票据附件。"),
    "DUPLICATE_INVOICE": ("《发票合规指引（2025-06）》重复报销禁止要求", "核对匹配的历史报销单；确认重复后删除本次重复费用行或提供可区分的证据。"),
    "AMOUNT_MISMATCH": ("《费用报销管理办法 V3.2》票面金额与申报金额一致性要求", "按票面价税合计更正申报金额，或补充可核验的金额差异说明。"),
}


def yuan(fen: int | None) -> str:
    return "—" if fen is None else f"¥{fen / 100:,.2f}"


def _line_map(claim: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {line["id"]: line for line in claim.get("lines", [])}


def _diagnostic_values(code: str, source: dict[str, Any], lines: dict[str, dict[str, Any]], actual: int | None, limit: int | None) -> tuple[str | None, str | None, str | None]:
    """Return human-readable actual / correct value / correction message."""
    line = lines.get(source.get("lineId", ""), {})
    if code == "INVOICE_TITLE_MISMATCH":
        return source.get("actual"), source.get("expected"), "购买方名称须与公司全称完全一致。"
    if code == "INVOICE_TAXNO_MISMATCH":
        return source.get("actual"), source.get("expected"), "购买方税号须与公司登记税号完全一致。"
    if code == "AMOUNT_MISMATCH":
        claimed, ticket = source.get("claimedFen"), source.get("invoiceTotalFen")
        return f"申报 {yuan(claimed)}；票面价税合计 {yuan(ticket)}", f"应申报 {yuan(ticket)}", "更正申报金额或补充可核验的差异说明。"
    if code == "DUPLICATE_INVOICE":
        invoice = line.get("invoice") or {}
        return f"发票代码/号码：{invoice.get('invoiceCode', '—')} / {invoice.get('invoiceNo', '—')}", "同一代码 + 号码只能关联一张报销单", "核对历史关联单据，确认重复后删除本次重复费用行。"
    if code == "MISSING_APPROVAL_OVERTIME_TAXI":
        return "未找到费用发生前的直属主管审批", "费用发生前的直属主管审批", "补充事前审批；事后审批不替代事前审批。"
    if code == "MISSING_ATTACHMENT":
        return "该费用行未关联附件", "与费用行对应的原始票据附件", "补齐原始票据附件。"
    if actual is not None and limit is not None:
        return yuan(actual), yuan(limit), f"实际值比制度标准高 {yuan(actual - limit)}。"
    return None, None, None


def build_evidence_chain(
    claim: dict[str, Any],
    review: dict[str, Any],
    approvals: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create UI-ready cards: rule -> calculation -> source -> next action."""
    lines = _line_map(claim)
    paths = {item.get("lineId"): item.get("image") for item in attachments if item.get("image")}
    policy = {item.get("rule"): item for item in review.get("evidence", []) if item.get("type") == "policy_cap"}
    cards: list[dict[str, Any]] = []

    for violation in review.get("violations", []):
        code = violation["code"]
        source = violation.get("evidence") or {}
        line_ids = source.get("lineIds") or ([source["lineId"]] if source.get("lineId") else [])
        cap = policy.get(code, {})
        actual = source.get("actualFen", cap.get("actualFen"))
        limit = source.get("capFen", cap.get("capFen"))
        line_no = [lines[item].get("lineNo") for item in line_ids if item in lines]
        actual_text, correct_text, correction = _diagnostic_values(code, source, lines, actual, limit)
        policy_source, next_action = RULE_GUIDANCE.get(code, ("现行审核规则矩阵", "请财务核对原票和相关记录后处理。"))
        card = {
            "ruleCode": code,
            "title": RULE_TITLES.get(code, code),
            "status": "REJECT",
            "summary": violation["reason"],
            "lineIds": line_ids,
            "lineNos": line_no,
            "actual": actual_text or (yuan(actual) if actual is not None else None),
            "limit": correct_text or (yuan(limit) if limit is not None else None),
            "difference": yuan(actual - limit) if isinstance(actual, int) and isinstance(limit, int) else None,
            "attachments": [paths[item] for item in line_ids if paths.get(item)],
            "approvals": approvals,
            "policy": policy_source,
            "source": source.get("source") or "原票 / ERP 单据字段 / 发票台账交叉核验",
            "correction": correction,
            "nextAction": next_action,
        }
        cards.append(card)

    for reason in review.get("flags", []):
        cards.append({
            "ruleCode": "MANUAL_REVIEW",
            "title": "需要人工复核",
            "status": "FLAG",
            "summary": reason,
            "lineIds": [],
            "lineNos": [],
            "actual": None,
            "limit": None,
            "difference": None,
            "attachments": [],
            "approvals": approvals,
            "policy": "《启衡精密报销审核工作台完整约束任务书 v2.0》：证据不足不得判定员工违规",
            "source": "OCR / 原票 / ERP 发票台账存在缺失或冲突",
            "correction": "这不是员工违规结论；补齐或人工核对缺失证据后再判断。",
            "nextAction": "请打开原始票据，补齐或确认缺失证据后再作决定。",
        })

    if not cards:
        cards.append({
            "ruleCode": "ALL_CHECKS_PASSED",
            "title": "已通过当前固定规则",
            "status": "APPROVE",
            "summary": "票据、申报金额、制度标准和审批记录已通过当前适用规则检查。",
            "lineIds": [],
            "lineNos": [],
            "actual": None,
            "limit": None,
            "difference": None,
            "attachments": [],
            "approvals": approvals,
            "policy": "当前固定规则矩阵",
            "source": "原票、ERP 单据字段、发票台账、审批记录和差旅标准的当前适用检查",
            "correction": "未发现需纠正的确定性问题。",
            "nextAction": "仍请财务人员结合业务真实性作最终复核。",
        })
    return cards
