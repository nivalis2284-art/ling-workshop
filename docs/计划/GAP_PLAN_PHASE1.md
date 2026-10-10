---
gap_closure: true
---

# 阶段一 缺陷修复计划 (GAP PLAN)

## 1. 缺陷概述
在“第一阶段：彻底移除交流情境”的验收过程中，发现以下未达标项：
1. **测试用例恶意删除（偷懒）**：`tests/test_evolution.py` 中的 `test_episode_summary_keeps_original_detail_and_invalidates_on_correction` 测试被整段删除，导致第一阶段意外丢失了对“纠正历史级联作废记忆”功能的保障。
2. **死代码残留**：`frontend/src/features/memory/api.ts` 中残留了不再使用的 `scopeLabels` 常量。

## 2. 修复步骤
- **任务 1**：恢复 `tests/test_evolution.py` 中的 `test_episode_summary_keeps_original_detail_and_invalidates_on_correction` 测试用例。
  - **实现细节**：将被删除的测试完整还原，并仅移除 `app.history.add` 调用中的 `scope="real"` 传参，确保测试能够正常通过。
- **任务 2**：清理前端死代码。
  - **实现细节**：在 `frontend/src/features/memory/api.ts` 中彻底删除 `export const scopeLabels ...` 及其相关的无用类型声明（如果还有的话）。
- **任务 3**：验证回归测试。
  - **实现细节**：执行 `pytest tests/test_evolution.py`，确保恢复后的测试 100% 通过；执行 `npm run build` 确保前端构建正常。
