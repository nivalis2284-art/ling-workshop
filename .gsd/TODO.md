# TODO.md — Technical Debt & Follow-up Items

## 待讨论事项 (Phase 3 启动时决策)
- [ ] 决策记忆定期反省思考的具体触发方式（用户显式指令 vs 后台空闲轮数周期 vs 结合模式）。

## 架构演进关注项
- [ ] `activities/service.py` 单文件重构（拆分为 Registry / Scheduler / Notifier）。
- [ ] 评估轻量 Token 预估器以替换过严的 UTF-8 字节计数。
- [ ] 增加 Chrome / Docker 本地环境状态可见性指示。
