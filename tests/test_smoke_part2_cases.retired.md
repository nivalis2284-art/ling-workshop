# 已退役：`test_smoke.py` 中的第二部分用例

`tests/test_smoke.py` 保留了下来，但其中**测试第二部分**的用例已删除，只留第一部分
（独立 Agent 系统）的存活路径。下列用例属于第二部分，按 A 阶段边界契约退役。

## 退役清单

| 原用例 | 依赖的第二部分能力 |
| :--- | :--- |
| `test_reopen_preserves_identity_and_history` | `identity.current`、`history.add`、`memory.read_sources` |
| `test_duplicate_request_has_one_record`（history 版） | `history.add` 幂等 |
| `test_duplicate_request_with_changed_content_is_rejected`（history 版） | `history.add` 冲突 |
| `test_transaction_rolls_back_record_and_job` | `history` / `jobs` |
| `test_export_restores_knowledge_and_goal_history` | `memory.knowledge`、`development.propose_goal` |
| `test_relevant_old_memory_is_recalled_with_precise_source` | `memory.save_episode`、回忆检索 |
| `test_deselected_branch_memory_does_not_return_to_context` | 来源可见性、回忆检索 |
| `test_recall_deduplicates_same_source_and_covers_multiple_events` | `context.recall.candidates` |
| `test_correction_is_versioned_owner_assertion_with_original_refs` | `memory.maintenance` 纠正 |
| `test_episode_fts_replaces_old_content` | 事件卡全文检索 |
| `test_archive_restore_delete_preserves_raw_history_and_audit` | 记忆归档/恢复/删除 |
| `test_legacy_invalidated_memory_accepts_owner_correction_and_restore` | 记忆失效与来源校验 |
| `test_marker_checks_agent_source_and_status` | 记忆来源标记校验 |
| `test_edit_user_forks_and_keeps_derived_memory` | 派生记忆在编辑后存活 |
| `test_delete_turn_removes_round_and_keeps_derived_memory` | 派生记忆在删轮后存活 |
| `test_knowledge_keeps_fact_after_source_correction` | 知识在来源更正后存活 |
| `test_episode_summary_survives_source_correction` | 事件摘要在来源更正后存活 |
| `test_protected_state_waits_for_owner_and_survives_restart` | 自我候选审批 |
| `test_low_risk_structured_preference_can_apply_automatically` | 自我自动应用 |
| `test_pending_proposal_can_apply_after_source_correction` | 自我候选与来源更正 |
| `test_skill_candidate_cannot_be_loaded_as_effective_until_owner_activates` | 技能候选 |
| `test_correcting_uncaptured_source_while_model_runs_does_not_block_task` | 任务对自我版本的校验 |
| `test_owner_can_record_review_and_read_updated_state` | `/sources`、`/proposals`、`/agents/{id}/state` |
| `test_client_cannot_forge_tool_result_kind` | `/sources` 的来源类型 |
| `test_conflicting_source_returns_conflict_without_leaking_internal_error` | `/sources` 冲突 |
| `test_owner_added_entry_carries_category_and_source` | `/agents/{id}/entries`（自我条目） |
| `test_correcting_the_origin_still_keeps_the_entry` | 自我条目与来源更正 |

这些能力在契约里被明确移除：`Application` 不再暴露 `history` / `identity` / `memory` /
`development` / `skills` / `activities`，后台只驱动自动备份，上下文只拼“系统指令 + 名字 +
近期原文 + 附件”。相关路由（`/identity` `/history` `/development` `/skills` `/activities`
`/segments` `/memory` `/sources` `/proposals`）也已不再挂载。

## 第一部分替代覆盖

原清单里的对话行为（重复请求幂等、编辑开分支、删轮、分支切换、长历史截断）已在
`tests/test_smoke.py` 用第一部分自身的能力重写；来源/记忆/自我相关的断言没有等价物，
因为对应能力已被移除。

## 如何取回完整内容

完整原文可从 git tag **`pre-part2-isolation`** 取回（= 隔离前的 `main`）：

```bash
git show pre-part2-isolation:tests/test_smoke.py
```
