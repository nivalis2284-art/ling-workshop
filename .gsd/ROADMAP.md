# ROADMAP.md

> **Current Phase**: Phase 3
> **Milestone**: v0.2.0 (系统瘦身与架构简化：彻底移除情境与解耦记忆级联)

## Must-Haves (from SPEC)
- [x] 彻底移除交流情境（Scope / Real）
- [x] 切断编辑与删除历史时的过度级联作废机制
- [x] 长期记忆与知识人工手动纠错优先
- [x] 前端界面冗余情境标签与选择器清空
- [x] 验收工具分层（2026-10-10，所有者决定）：历史 1300+ 项套件退役，34 项冒烟层上线（约 20 秒全跑）

---

## Phases

### Phase 1: 交流情境（Scope / Scene / Real）彻底除名与基准收敛
**Status**: ✅ Complete
**Objective**: 从 Prompt、会话管理、记忆检索及数据库中全面剥离情境属性，完成 025_drop_scope 迁移与前端死代码清理。
**Requirements**: REQ-SCOPE-01, REQ-SCOPE-02, REQ-SCOPE-03, REQ-UI-01

### Phase 2: 切断编辑与删除时的过度级联作废机制
**Status**: ✅ Complete (re-verified 2026-10-10, 遗留修复与账本重签)
**Objective**: 纯粹化上下文窗口维护逻辑，解除历史修改对派生记忆、知识与人格提案的级联失效广播，确立人工手动纠错优先原则。
**Requirements**: REQ-CASCADE-01, REQ-CASCADE-02, REQ-CASCADE-03

### Phase 3: 重构记忆与人格演化架构（定期反省思考准备）
**Status**: ⬜ Not Started
**Objective**: 消除人格策略中代码层硬编码防线，解绑单消息即时提取强耦合，奠定轻量定期反省演化基础。
**Requirements**: REQ-EVOLVE-01

### Phase 4: 前端界面与看板精简
**Status**: ⬜ Not Started
**Objective**: 深度净化前端记忆管理与设置看板，清理残留的僵尸样式与配置项，优化直观编辑交互。
**Requirements**: REQ-UI-01

### Phase 5: 测试用例重构与工程基准验收
**Status**: ⬜ Not Started
**Objective**: 清理陈旧假想防御测试，补全精简架构核心断言，执行全栈端到端测试与质量基准回归。
**Requirements**: REQ-TEST-01
