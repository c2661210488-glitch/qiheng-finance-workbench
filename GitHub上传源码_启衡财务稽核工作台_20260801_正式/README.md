# 启衡财务稽核工作台（M1-M3）

这是面向财务复核人员的 Windows 工作台：连接 ERP 后，完成费用报销单审核、人工复核与安全回写，以及全量发票台账稽核。它不是自动审批器：AI/OCR 只提供可解释建议，最终决定由人作出。

## 交付范围

- M1：首次连接页仅显示 API 地址、API Key、登录按钮；连接成功后密钥不显示在主页。
- M2：从 API 实时读取待审报销单、原票、审批/特批记录和台账；展示判责依据；支持人工确认、校准后整单重判、写回前预检和安全写回。
- M3：从 API 全量只读扫描发票台账和供应商主数据，形成重复票据、票面待核查、主数据待完善任务；不写回 ERP。
- 不含 M4 应收核销，避免超出本次验收范围。

## 新环境启动

1. 启动课程提供的 Docker ERP：门户通常为 `http://127.0.0.1:8080`，API 通常为 `http://127.0.0.1:8081`。
2. 在 ERP 的“开发者中心”创建最小权限 API Key：
   `expense:read`、`approval:read`、`attachment:read`、`invoice:read`、`master-data:read`；需要验证人工确认后的 M2 写回时再加 `expense:review`。
3. 双击 `启衡财务稽核工作台_M1M2M3_交付版_20260801.exe`，填写 API 地址和刚创建的 Key，点击“登录并进入工作台”。
4. 在“费用管理 / 费用报销单”点击刷新；每次刷新和选单都会读取当前 API 数据，历史本地材料不作为当前结论来源。

详细操作和故障处理见 `docs/操作手册_M1M2M3_20260801.md`，部署边界见 `docs/部署说明_M1M2M3_20260801.md`。

## 关键业务规则

- 证据优先级：原票 > 已确认台账 > ERP 字段 > OCR > 模型建议。
- 火车票、机票等不含购买方 OCR 字段时，不能据此自动通过；必须比对关联发票台账。台账缺失或不一致时进入人工复核。
- OCR 字段纠正会保存为本地人工确认样本，并对整张单据重新判责；不会自动改全局规则、自动训练或直接上线。
- 写回前再次读取 ERP 审核意见。ERP 已有任何意见（相同或冲突）都不覆盖、不追加；只有空意见可写回。写回只创建审核意见，不改变单据状态、不支付、不删除。

## 可复跑验收

所有命令使用 API 实时数据；不要把旧 `runs/`、旧 `formal-m2/` 或旧 `formal-m3/` 结果当作本次验收结果。

```powershell
# 1) 新隔离环境：重新读取并分析恰好 300 张 PENDING 单据（只读）
$env:QIHENG_API_KEY = '仅当前隔离环境的临时 Key'
python tools/run_m2_fresh_batch.py --base-url http://127.0.0.1:18081

# 2) 对同一隔离环境全量只读扫描 M3
python tools/run_m3_scan.py --base-url http://127.0.0.1:18081 --out formal-m3/final-isolated

# 3) 将 300 单证据目录生成 M2 草稿，再与 M3 报告合并为正式 JSON
python tools/build_m2_submission.py --runs-dir formal-m2/fresh-runs/<batch-id> --batch-prefix '' --out formal-m2/final-m2.json --team 启衡财务稽核工作台项目组 --member 林泽锟
python tools/build_final_submission.py --m2 formal-m2/final-m2.json --m3 formal-m3/final-isolated/m3-latest.json --out submission.json --repo-url <GitHub仓库地址> --member 林泽锟

# 4) 官方格式校验器（必须无 error）
node ../validate-submission.mjs submission.json
```

`run_m2_fresh_batch.py` 遇到非 300 张或任一单失败会终止，`build_final_submission.py` 也拒绝生成最终提交；这是为了防止历史缓存或不完整批次混入结果。

## 开发验证

```powershell
python -m unittest tests.test_reviewer_source_conflicts -v
python -m py_compile app.py src/reviewer.py m4-final-20260731/finance_workbench.py
```

测试覆盖车票台账冲突、台账缺失、原票与台账冲突、人工纠正后的整单重判等不可自动通过场景。

## 安全与数据

- API Key 不写入源码、EXE、submission.json 或 Git 仓库；`.env`、`runs/`、正式扫描快照已忽略。
- RapidOCR 在本地运行，不向外部模型服务发送原票。
- 交付演示 HTML 使用明确标识的虚拟数据，与实时 ERP 数据隔离。

ROI 的计算口径、已知边界和后续实测项见 `docs/ROI与价值说明_M1M2M3_20260801.md`。
