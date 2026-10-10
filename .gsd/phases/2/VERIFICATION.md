# Phase 2 Verification Report

## Verdict: PASS — Re-verified 2026-10-10 after remediation

> 重签说明：本报告初版曾声称 "1315 items collected and all passed"，经独立复跑核实为**不实**
> （实际为 17 failed / 1275 passed / 23 skipped）。以下为遗留问题修复后的如实记录。

## Requirements Addressed
- **REQ-CASCADE-01**: 纯粹化上下文窗口：删除或编辑历史消息时仅重理上下文链，确保发送给模型的消息准确
- **REQ-CASCADE-02**: 解除级联作废：拔除 `set_visible/correct` 对 episodes / knowledge / self / development / skills 的级联失效广播
- **REQ-CASCADE-03**: 人工纠错优先：长期记忆与知识支持手动编辑与删除，算法不代劳连带破坏

## Remediation (2026-10-10)

### 1. 修复"记忆引用瘫痪"（严重缺陷）
- 症状：编辑/删除消息或纠正来源后，派生记忆按新语义保留，但召回读取其出处时抛 `SourceInvalid`，使整个上下文构建失败 → 聊天无法发送。
- 修复：召回路径对出处不可读（被撤回/纠正/删除）的记忆静默跳过；记忆本体保留，供人工查看与维护。
- 保留互补防护：运行中复查（snapshot_marker / validate_markers）重新校验来源——来源被撤回或纠正时拒绝陈旧结果。
- 证据：`tests/test_context_recall.py::test_deselected_branch_memory_does_not_return_to_context`（原断言保留，恢复通过）；`tests/test_evaluation_contract.py::test_offline_self_check_generates_and_renders_all_cases` 恢复通过。

### 2. 兑现"人工纠错优先"的完整含义
- 存量被算法自动作废（`status='invalidated'`）的记忆，现允许所有者手动修正与恢复（只读展示仍提示"曾被自动作废"）。
- 证据：新增 `test_legacy_invalidated_memory_accepts_owner_correction_and_restore`（episodes / knowledge 参数化）；恢复 `test_marker_checks_agent_scope_source_and_status` 被删断言。

### 3. 恢复低风险偏好自动应用
- 移除 `identity/changes.py` 中"来源必须带 real 标签"的硬编码条件——该条件在情境清空后被静默禁用，且测试用旧标签掩盖了退化。
- 证据：`tests/test_evolution.py` 的测试辅助不再打旧标签，`test_low_risk_structured_preference_can_apply_automatically` 在无标签路径下通过。

### 4. 测试套件修复（17 项失败全部定性）
- 15 项旧级联断言改写为新行为断言：记忆/知识/技能在来源纠正后保留、待审提案不被自动作废、回滚不连带清空。
- 2 项由缺陷修复治愈（断言保留）：`test_deselected_branch_memory_does_not_return_to_context`、`test_offline_self_check_generates_and_renders_all_cases`。
- 补回误删覆盖：任务"模型等待期间纠正未捕获来源不再阻断任务"（`test_correcting_uncaptured_source_while_model_runs_does_not_block_task`）；清理 `test_tasks.py` 的 `model_return` 死分支。

### 5. 残留清理
- `history.validate_refs` / `context.recall.candidates` / `runtime.payload.build_payload` 移除已成为死参数的情境参数。
- 来源写入点不再打 `real` / `external` 标签（api ×2、activities ×3）；验收契约同步更新（activities / chat / proactive_loop 共 5 项断言）。
- Ruff 0 错误（清理未使用导入、未使用变量与冗余嵌套判断）。

## Test Suite Execution (final)
- `python -m pytest tests/ -q` → **1295 passed, 23 skipped, 0 failed**
- total 1318 = 原 1315 + 新增 3 项覆盖（2 项作废恢复 + 1 项任务来源纠正）
- `python -m ruff check src tests scripts` → All checks passed
- 前端本轮未改动；先前验证 145 passed / tsc 无错误

## Known Remainders (deferred to project-wide cleanup)
- `history.add` / `episodes.save` / `knowledge.save` 的 scope 参数与数据表列保留（牵涉指纹兼容，属结构性清理）
- 状态机与一致性收尾（stale 兼容路径、rollback 来源校验、`default_real` 命名与注释文案）留待人格/记忆重构阶段统一裁决
- src 模块 README 中的测试描述待随项目梳理更新

## Acceptance Tooling Note (2026-10-10, after this report)
所有者判定 1300+ 项套件"每次等半天"不可接受，历史测试套件随后退役，保留 34 项冒烟层
（`tests/test_smoke.py` + `tests/test_smoke_learning.py`，全跑约 20 秒），本文所述 "1295 passed"
为分层前的最终全量结论。历史套件可从 git 历史取回。
