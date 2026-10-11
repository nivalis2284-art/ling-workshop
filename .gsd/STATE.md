# GSD Project State

## Current Position
- **Milestone**: v0.2.0 (系统瘦身与架构简化：拆两大部分，第一部分先做合格)
- **Active Phase**: **F（能力扩展）进行中**——技能包 + MCP 最小集（D 已于 2026-10-11 完成）
- **Status**: 🟢 A（边界隔离 `fa556ba`）、B（沙箱分层 `45a3813`）、C（统一循环 `13fd8ce`）、D（任务收尾 `3efc06c`）全部完成并验证；🚧 F 进行中
- **Next Step**: 推进 F——先冻结契约，再做技能包（内置基础技能 + 扫描外部目录）与 MCP 最小集（连服务器/列工具/调工具/版本协商，必须可选、不成为前置门槛）；之后可选 E（常数对账与文档对齐）；G 待梳理

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
