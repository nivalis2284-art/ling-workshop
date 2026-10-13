# 已退役：`test_smoke_learning.py`（第二部分）

## 为什么退役

该文件整体属于**第二部分（长期连续与发展）**：它驱动 `learning.ConsolidationWorker`，
让后台整理把一条原始经历变成 `episodes` / `knowledge` 记忆，并验证来源更正后记忆仍在。

在 A 阶段边界契约（`docs/09-归档/首版与第一部分改造/计划/2026-10-11-A阶段-边界契约.md`）下，第二部分**整体不再装配**：

- `Application` 不再暴露 `learning` / `memory` / `history`；
- 后台 `Supervisor` 只驱动自动备份，不再有整理 worker；
- `app.history.add(...)` 等调用点已不存在。

因此该用例无法在“只装第一部分”的运行时里运行，按契约整体退役。

## 如何取回完整内容

完整原文可从 git tag **`pre-part2-isolation`** 取回（= 隔离前的 `main`）：

```bash
git show pre-part2-isolation:tests/test_smoke_learning.py
```

将来若把第二部分重新装配回来，可从该 tag 恢复此文件并重新接线。
