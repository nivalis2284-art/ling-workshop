# GSD Project State

## Current Position
- **Milestone**: v0.2.0 (系统瘦身与架构简化：彻底移除情境与解耦记忆级联)
- **Active Phase**: Phase 3 (重构记忆与人格演化架构：定期反省思考准备)
- **Status**: 🟢 Phase 1 & 2 遗留修复完成；验收工具已分层（2026-10-10）
- **Next Step**: 用户主导的项目级梳理（削减复杂规则与无关业务）→ 之后规划 Phase 3

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
