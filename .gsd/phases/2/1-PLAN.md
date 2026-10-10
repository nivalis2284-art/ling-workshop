---
phase: 2
plan: 1
wave: 1
depends_on: []
---

# Plan 2.1: 解耦并纯粹化上下文与记忆的级联机制

## Objective
切断对历史消息执行修改或删除时产生的过度级联作废机制（Cascading Invalidation）。确保记忆（知识、事件、技能、人设、长期目标）作为独立思考沉淀，在原始历史变更后保持独立有效，且支持人工优先手动维护。

## Context
- .gsd/SPEC.md
- src/lingnexus/app.py
- src/lingnexus/history/service.py
- src/lingnexus/memory/episodes.py, src/lingnexus/memory/knowledge.py, src/lingnexus/memory/maintenance.py
- src/lingnexus/identity/changes.py, src/lingnexus/skills/service.py, src/lingnexus/development/service.py
- tests/ (relevant integration tests)

## Tasks

<task type="auto">
  <name>移除历史事件监听器与回调广播 (Core History)</name>
  <files>
    - src/lingnexus/app.py
    - src/lingnexus/history/service.py
  </files>
  <action>
    - 修改 `src/lingnexus/app.py`：移除所有形如 `self.history.subscribe_corrections(self.X.invalidate)` 的回调绑定。
    - 修改 `src/lingnexus/history/service.py`：删除 `_on_correct` 列表、`subscribe_corrections` 方法，以及在 `correct` 与 `set_visible` 中迭代 `_on_correct` 执行回调的代码。
    - WHY: 解除原始模块层的强耦合发布/订阅机制。
  </action>
  <verify>pytest tests/history/test_service.py tests/test_chat_conversations.py</verify>
  <done>不再存在 subscribe_corrections 调用且模块可成功导入。</done>
</task>

<task type="auto">
  <name>拔除所有派生服务的 invalidate 方法与读时强校验 (Derived Artifacts)</name>
  <files>
    - src/lingnexus/memory/episodes.py
    - src/lingnexus/memory/knowledge.py
    - src/lingnexus/memory/maintenance.py
    - src/lingnexus/identity/changes.py
    - src/lingnexus/skills/service.py
    - src/lingnexus/development/service.py
  </files>
  <action>
    - 在 `episodes.py`, `knowledge.py`, `changes.py`, `skills/service.py`, `development/service.py` 中删除 `invalidate(self, ref: SourceRef)` 方法。
    - 在 `episodes.py` 与 `knowledge.py` 的 `read()` 方法中，**移除** `self.history.validate_refs(...)` 调用（创建/保存时的校验保留）。
    - 修改 `memory/maintenance.py`：在 `_validate` 中**移除** `history.validate_refs` 校验，使其不再阻碍用户手动修复派生记忆。
    - 在 `identity/changes.py` 的 `decide()` 方法中，移除对 `validate_experience(...)` 的调用。
    - 在 `skills/service.py` 的 `available()`、`decide()` 和 `restore()` 中，移除对 `validate_refs(...)` 的调用。
    - 在 `development/service.py` 的 `decide()` ("activate" 分支) 中，移除对 `validate_refs(...)` 的调用。
    - WHY: 派生记忆与知识具有独立生命周期，仅在首次提取或用户刻意交互时验证依赖，避免历史消息微调导致的全面崩塌。
  </action>
  <verify>pytest tests/ -k "not test_cannot_correct_or_restore_invalid_sources"</verify>
  <done>所有子服务不再存在 invalidate 方法，不再于只读访问时校验失效源。</done>
</task>

<task type="auto">
  <name>重构并绿灯修复集成测试用例 (Test Suite)</name>
  <files>
    - tests/test_memory_maintenance.py
    - tests/test_tasks.py
    - tests/test_skills.py
    - 其他出现失败的 tests/
  </files>
  <action>
    - 检索并修改/移除不再成立的测试断言。特别是针对 `SourceInvalid` / `stale` / `skills_changed` 这种强依赖级联失效机制引发的异常断言。
    - 比如 `test_memory_maintenance.py` 中的 `test_cannot_correct_or_restore_invalid_sources` 可以重命名为 `test_can_correct_or_restore_even_if_sources_invalidated` 并且修改期望行为为成功通过（状态为 active）。
    - 针对 `tests/test_tasks.py` 中检查 `skills_changed` 或 `self_changed` 的测试进行适应性修改或移除。
    - 确保不再测试 `active_only` 传播到下游模块阻断逻辑的功能。
  </action>
  <verify>pytest tests/</verify>
  <done>pytest tests/ 达到 100% 通过（绿灯），所有假想防线与联级废除相关的旧用例已被清理或纠正。</done>
</task>

## Success Criteria
- [ ] 系统中不再有 `subscribe_corrections` 与 `invalidate` 级联方法。
- [ ] 编辑/删除历史消息不再触发技能、情景记忆或自我人设的自动失效。
- [ ] 测试套件重构并实现 100% 回归通过。
