# 第二部分 · 已归档的 HTTP 接口层

这里存放**第二部分**（长期连续与发展）的 HTTP 路由文件。它们于 2026-10-11 从 `api/` 移到这里，因为第一部分已经不再提供这些能力。

## 为什么放在这里而不是删除

隔离不是移除。第二部分将来会**逐条加回来**，这些接口层是现成的草稿。同时，第一部分要求「任何文件都不引用第二部分模块」，把它们留在 `api/` 会让这条边界形同虚设——所以移出第一部分的范围，但保留在磁盘上。

## 归档清单

| 文件 | 原属能力 |
| :--- | :--- |
| `identity.py` | 自我状态、候选审批、版本回退、系统指令（指令部分已迁到 `api/agents.py`） |
| `history.py` | 原始经历库 |
| `memory.py` | 事件卡、知识、记忆维护 |
| `skills.py` | 技能积累 |
| `development.py` | 长期目标 |
| `activities.py` | 主动关注 |
| `segments.py` | 主题分段 |

## 恢复方式

这些文件**不在任何装配路径上**，不会被加载，也不会被打包。要恢复某一条能力时：

1. 从 `git show pre-part2-isolation:src/lingnexus/api/<文件名>` 取回原位置，或直接从这里移回 `api/`；
2. 在 `api/app.py` 里重新注册路由；
3. 同时把对应的第二部分模块重新装配进 `Application`。

## 边界约定

`src/lingnexus/` 下：
- **第一部分** = 除本目录与第二部分模块目录之外的全部内容
- **第二部分** = `history/` `memory/` `identity/` `learning/` `skills/` `development/` `activities/` `conversations/segments.py` `context/recall.py` 以及本目录

「第一部分零反向依赖」的静态检查应当**排除本目录与第二部分模块目录**。
