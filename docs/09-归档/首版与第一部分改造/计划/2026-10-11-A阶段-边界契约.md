# A 阶段 · 边界契约与分工（冻结）

> 创建日期：2026-10-11
> 状态：**契约冻结**，并行实施依据
> 恢复点：`tag pre-part2-isolation` = `main` = `isolate-part2` = `68519ba`
> 验收基线：`pytest tests/` = 34 passed（改动前实测）

---

## 一、目标

按**方案二（模块边界 + 独立装配）**把「第二部分」整体从运行中摘掉，让第一部分成为一个**能独立运行且完整**的 Agent 系统。

- **代码不删**：第二部分模块（`history/` `memory/` `identity/` `learning/` `skills/` `development/` `activities/` `conversations/segments.py` `context/recall.py`）全部保留在仓库里，只是**不再被装配、不再被引用**。
- **数据不删**：数据库表与数据一条不动。
- **方向单向**：第一部分不得引用第二部分。

---

## 二、冻结契约（所有并行工作必须遵守）

### 2.1 装配清单

**第一部分装配后暴露**（`Application` 的属性）：

```
database, instructions, profiles, attachments, conversations,
permissions, actions, browser, maintenance, dependencies, automatic_backups
```

**不再创建**（保持不装配即可，模块文件保留）：

```
history, identity, memory, development, segments, skills, activities
```

### 2.2 构造函数签名（冻结）

| 类 | 新签名 | 说明 |
| :--- | :--- | :--- |
| `Conversations` | `Conversations(database)` | **不再接收 history** |
| `ContextBuilder` | `ContextBuilder(database, instructions)` | **不再接收 identity / history** |
| `Application.create_agent(name)` | 签名不变，内部实现改为直接读写 `agents` 表 | 不再经由 identity |

**第一部分需要的最小个体登记**：只需要 `id + 名字`。可以直接读写 `agents` 表，也可以新建一个极小的登记服务；**不得依赖 `identity` 模块**。

### 2.3 对话消息自持文本

- `conversation_messages` 增加 `text` 列；消息文本由对话自己保存，**不再经过 sources / 经历库**。
- 不再有 `source_id` / `source_revision` / 可见性 / 修订号。
- **分支选择 = `head_id` 指向的路径**。原来的"未选中分支标记不可见"机制**整体取消**——它的存在只是为了让第二部分知道哪些经历是"选中的"。
- 迁移要求：新增一个迁移，把现有 `conversation_messages` 的文本从 `source_revisions` 回填到新列（**老数据必须可读**）。

### 2.4 对话运行记录简化

`chat_runs` 去掉：`user_source_id`、`user_source_revision`、`context_sources`、`self_revision`。
保留：`user_message_id`、`profile_id`、状态与时间戳、`context_manifest`（简化为只描述本轮实际发送的附件与截断情况）。

### 2.5 运行时

| 位置 | 要求 |
| :--- | :--- |
| 上下文组装 | 只拼：**系统指令 + 名字 + 近期原文 + 附件**。不再有当前自我状态、不再有回忆检索 |
| 主备与重试 | **保留不变**（模型选择、一次重试、用量记账） |
| 落库前校验 | **去掉**自我版本与记忆版本校验 |
| 附件摘要校验 | **去掉**（属假想敌） |
| 上下文超预算 | 改为**截断 + 明确提示**，不再整条失败 |
| 生成中禁用编辑/删除/重新生成 | 改为**允许**，但明确提示后果 |

### 2.6 后台

`Supervisor` 只驱动**自动备份**。不再有整理 worker、不再有主动关注。

### 2.7 路由清单

**移除**：`identity`、`history`、`development`、`skills`、`activities`、`segments`、`memory`
**保留**：`models`、`content`、`chat`、`usage`、`conversations`、`permissions`、`background`、`browser`、`tasks`、`dependencies`、`system`、`maintenance`

> 若 `maintenance` 的导出/统计引用了被隔离的服务，**改为直接查表或去掉该字段**，不要为了它把第二部分装回来。

### 2.8 任务

- 去掉对记忆的校验与记忆类工具（`find_memories` 等）。
- 去掉对整理（learning）的引用。
- 任务本身的多步循环、工具、沙箱、审批**全部保留**。

### 2.9 前端

**移除页面与入口**：`identity`、`memory`、`skills`、`development`、`activities`、`sources`
**保留并简化**：`chat`、`content`、`onboarding`、`overview`、`settings`、`tasks`、`browser`、`background`

> `background` 页只保留自动备份状态，去掉整理队列相关内容。
> `onboarding` 保留"创建个体 + 添加模型连接"，但创建个体不再走身份体系。

---

## 三、分工（文件所有权，互不越界）

| 工作流 | 独占文件范围 |
| :--- | :--- |
| **WS-核心** | `src/lingnexus/conversations/`、`src/lingnexus/runtime/`、`src/lingnexus/context/`、`src/lingnexus/app.py`、`src/lingnexus/storage/migrations/` |
| **WS-外围** | `src/lingnexus/api/`、`src/lingnexus/background/`、`src/lingnexus/tasks/`、`src/lingnexus/maintenance/` |
| **WS-前端** | `frontend/src/` |
| **WS-测试**（第三轮） | `tests/` |

**硬性禁止**：任何工作流都不得修改第二部分模块自身的文件（`history/` `memory/` `identity/` `learning/` `skills/` `development/` `activities/`），也不得删除它们。

---

## 四、验收标准

1. `pytest tests/` 全绿（测试在第三轮重写后）。
2. **边界测试**：在第二部分完全不装配的情况下，以下链路可用——
   - 启动后台
   - 创建个体
   - 发起一次对话（含附件）
   - 编辑消息、重新生成、删除一轮、分支切换
   - 建一个任务并执行（沙箱默认档）
   - 备份与恢复
3. **零反向依赖**：第一部分任何文件都不 import 第二部分模块。
   **校验口径**：从装配根（`lingnexus.app.Application` 与 `lingnexus.api.app`）出发的**可达性传递闭包**。
   第二部分整体排除在校验范围外，包括 `history/` `memory/` `identity/` `learning/` `skills/` `development/` `activities/` `conversations/segments.py` `context/recall.py`，以及归档目录 `part2_retired/`。
4. 前端能启动、能聊天、无残留的失效入口。

---

## 五、禁令

- **不删除**第二部分模块的任何文件与任何数据库表/数据。
- **不修改**第二部分模块内部逻辑（保持原样，以便将来加回）。
- 不顺手做计划外的重构（例如沙箱分层、MCP、技能包——那些属于 B/C 阶段）。
- 不把 `scope` 相关的死参数清理与本次混在一起（登记为后续）。

---

## 六、A 阶段完成记录（2026-10-11）

**分支** `isolate-part2`，恢复点 tag `pre-part2-isolation`。

### 做了什么

| 范围 | 内容 |
| :--- | :--- |
| 对话与运行时 | 消息文本自持（迁移 `026_conversation_message_text.sql`）；分支选择改为 `head_id` 路径；上下文组装只剩「系统指令 + 名字 + 近期原文 + 附件」；落库前版本校验与附件摘要复校移除；超预算改为截断 + 提示 |
| 装配与接口 | 只装配第一部分；第二部分路由全部取消注册；后台协调器只驱动自动备份 |
| 任务 | 去掉记忆类工具与记忆/自我校验；多步循环、工具、沙箱、审批全部保留 |
| 动作 | 去掉对经历/自我/记忆的写入，效果跟踪保留 |
| 前端 | 移除六个第二部分功能目录与全部入口；后台页只留自动备份 |
| 接口层归档 | 第二部分路由文件从 `api/` 移到 `part2_retired/`，见该目录 README |
| 测试 | 第二部分用例退役（留说明文件，可从 tag 取回）；新增边界测试 |

### 验收结果

| 项 | 结果 |
| :--- | :--- |
| 后端测试 | `20 passed`（改动前 34 passed，减少的 14 项经独立核对全部属第二部分） |
| 前端类型检查 / 构建 / 测试 | 通过 / 通过 / `124 passed` |
| 边界链路（第二部分不装配） | 通过：启动 → 建个体 → 含附件对话 → 重新生成 → 编辑 → 删轮 → 分支切换 → 任务执行 → 导出恢复 |
| 零反向依赖（可达性口径） | 通过 |
| 迁移对老库 | 通过：自造老库验证文本正确回填，无表/数据丢失，外键与完整性检查通过 |
| 第二部分模块被改动 | **零改动** |

### 独立验收裁决

**有条件接受。** 四条条件中：

- ✅ 条件 1（系统指令编辑入口缺失）—— **已修复**：入口加回设置页，支持逐条增删改与一键恢复默认。
- ✅ 条件 2（残留死路由引用第二部分）—— **已处理**：7 个文件归档到 `part2_retired/`。
- ✅ 条件 3（登记已知耦合）—— 见第七节。
- ⚪ 条件 4（前端测试基建 `setup.ts` 的 Blob 改动）—— 保留，属测试专用、低风险。

### 独立验收未能覆盖的部分（诚实登记）

- 未用**真实模型 API** 跑通（全部为模拟端点）。
- 未在**真实容器**中验证任务执行。
- 未验证浏览器自动化、Windows 启动器、打包。
- 前端只做了静态验证（类型检查 / 构建 / 测试），未做人工界面巡检。

---

## 七、已知耦合登记

这些是第一部分与第二部分之间**仍然存在的硬依赖**。它们不影响运行，但将来处理第二部分时必须一并考虑：

| # | 位置 | 性质 | 处理建议 |
| :-- | :--- | :--- | :--- |
| 1 | 对话删除逻辑中直接引用了第二部分的表 `conversation_segments` | 表级硬依赖。**本次未新增**（既有实现）。因为外键约束打开，删除消息前必须清掉引用它的分段行，否则会失败 | 第二部分回归时一并处理；若该表被移除，需同步改这里 |
| 2 | 数据保管模块的表清单里硬编码了全部第二部分表名 | **本次未改**。作用是让备份/恢复连带搬运第二部分数据，符合"数据不删" | 保留即可，但需知道它随第二部分表结构变化而需要同步 |
| 3 | 上下文预算的预留常数、附件上限、任务步数上限、备份保留份数等 | 数值来历未溯源 | 属 E 阶段（常数对账），与本次无关 |
