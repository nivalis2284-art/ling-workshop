# GSD Project State

## Current Position
- **Milestone**: v0.2.0 (系统瘦身与架构简化：拆两大部分，第一部分先做合格)
- **Active Phase**: **A–F 六阶段全部完成**（2026-10-11）；仅剩 G（逐条加回第二部分，待所有者梳理）
- **Status**: 🟢 A（边界隔离 `fa556ba`）、B（沙箱分层 `45a3813`）、C（统一循环 `13fd8ce`）、D（任务收尾 `3efc06c`）、E（对账与对齐 `d09ed44`）、F（能力扩展 `33d4ebf`）全部完成并验证
- **Next Step**: 仅剩 G——由所有者梳理顺序后逐条加回第二部分；也可在 F 上加深（给 `mcp__*` 加策略钩子、做技能管理界面）。F 的两条硬约束（可选、不扩权）已在契约 §四 验收对照中留证

## Requirements Progress
- REQ-SCOPE-01: Implemented
- REQ-SCOPE-02: Implemented
- REQ-SCOPE-03: Implemented
- REQ-UI-01: Implemented
- REQ-CASCADE-01: Implemented
- REQ-CASCADE-02: Implemented
- REQ-CASCADE-03: Implemented
- REQ-EVOLVE-01: Pending (Phase 3)
- REQ-TEST-01: Superseded（所有者决定：历史套件退役、冒烟层上线，2026-10-10）
- REQ-LOOP-01: Implemented（C 阶段：对话即循环，多步工具执行 + 中断介入 + 任务合并，2026-10-11）
- REQ-HOOKS-01: Implemented（C 阶段：BEFORE_TOOL/AFTER_TOOL/BEFORE_TURN_END 三事件钩子，策略不进循环体）
- REQ-CONFIRM-01: Implemented（C 阶段：副作用实时确认闸门 + 每步独立记账）
- REQ-ENTRY-01: Implemented（D 阶段：对话是唯一入口，任务路径收进对话为次级「任务工作副本」，`13fd8ce`）
- REQ-SKILLPACK-01: Implemented（F 阶段：技能包三处扫描 + 三级披露 + 受限资源读取，`01d4a84`）
- REQ-MCP-01: Implemented（F 阶段：MCP 最小集，可选且不扩权，远端工具走同一钩子路径，`33d4ebf`）
- REQ-SETTINGS-01: Implemented（E 阶段：设置读写收敛为单一真源 `configuration/settings.py`，6 个调用点复用，`cd6705d`）
- REQ-AUDIT-01: Implemented（E 阶段：常数溯源审计 8 条收口 + 使用说明与实现对齐，`d09ed44`）

## Acceptance Tooling
- 后端验收 = 冒烟层：`tests/test_smoke.py` + `tests/test_smoke_learning.py`（34 项，约 20 秒）。
- 历史 1300+ 项测试套件于 2026-10-10 按所有者决定退役，可从 git 历史取回。
- 更深检查按需临时编写，不再长期维护巨型账本。

## Session Summary
- Project initialized via `/gsd-1-1-project-new`.
- `.gsd/SPEC.md` finalized with 5 core goals.
- `.gsd/ROADMAP.md` established with 5 progressive phases.
- Phase 1 (移除交流情境) 全部代码、迁移与测试已完成原子提交 (commit: f9a053d)。
- Phase 2 (切断过度级联作废机制) 3 个原子任务全部完成并提交 (9e6d422, c284d94, b59bbd9)。
- 2026-10-10 遗留修复轮：
  - 修复"记忆引用瘫痪"缺陷：出处被撤回/纠正的旧记忆不再导致上下文构建崩溃。
  - 兑现"人工纠错优先"：存量被自动作废的记忆可手动修正/恢复；运行中复查恢复来源校验。
  - 恢复低风险偏好自动应用（清除 real 标签硬编码造成的静默失效）。
  - 17 项测试失败全部定性处理（15 改写 + 2 治愈），新增 3 项覆盖；Ruff 0 错误。
  - 全量验收（分层前）：**1295 passed / 0 failed**（详见 `phases/2/VERIFICATION.md` 重签版）。
- 2026-10-10 验收工具分层：历史 60 个测试文件删除、34 项冒烟层上线（约 20 秒）；README / STACK / 账本同步更新。
- 已知遗留（登记待项目梳理阶段）：测试中已随层退役的 scope 死参、`add/save` 的 scope 参数与表列、stale/rollback 一致性收尾、src 模块 README 中过时的测试描述。
