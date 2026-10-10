# REQUIREMENTS.md

## 需求追踪清单 (Requirement Traceability Matrix)

| ID | 需求描述 | 来源 | 状态 | 对应阶段 |
|:---|:---|:---|:---|:---|
| **REQ-SCOPE-01** | 提示词清洗：大模型系统提示词中彻底移除“当前交流情境：real”硬编码 | SPEC Goal 1 | Implemented | Phase 1 |
| **REQ-SCOPE-02** | 会话解耦：移除对话启动与检索时对 scope 的强校验，全局开放检索 | SPEC Goal 1 | Implemented | Phase 1 |
| **REQ-SCOPE-03** | 数据库平滑迁移：通过 025_drop_scope 脚本平滑过渡，无损兼容已有数据 | SPEC Goal 1 | Implemented | Phase 1 |
| **REQ-UI-01** | 前端看板净化：MemoryPage 与 SourceViewer 移除 real/external 标签与选择框 | SPEC Goal 4 | Implemented | Phase 1 & 4 |
| **REQ-CASCADE-01** | 纯粹化上下文窗口：删除或编辑历史消息时仅重理上下文链，确保发送给模型的消息准确 | SPEC Goal 2 | Implemented | Phase 2 |
| **REQ-CASCADE-02** | 解除级联作废：拔除 set_visible 对 episodes / knowledge / self 变更的级联失效广播 | SPEC Goal 2 | Implemented | Phase 2 |
| **REQ-CASCADE-03** | 人工纠错优先：长期记忆与知识支持手动编辑与删除，算法不代劳连带破坏 | SPEC Goal 3 | Implemented | Phase 2 |
| **REQ-EVOLVE-01** | 演化机制重构：移除人格策略中必须来自 real 的硬编码校验，准备定期反省架构 | SPEC Goal 3 | Pending | Phase 3 |
| **REQ-TEST-01** | 测试重构与基准回归：清理假想防御测试，补充上下文精简断言，全量测试绿灯 | SPEC Goal 5 | Superseded（历史套件退役为 34 项冒烟层，2026-10-10 所有者决定） | Phase 5 |
