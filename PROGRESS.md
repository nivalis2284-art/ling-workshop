# 实施恢复点 — 完整 Windows 首版

## 架构简化：A/B/C 三阶段完成（2026-10-11，最新）

> 本节为当前工程主线；下方 0.1.x 内容为历史交付记录，仍有效但已非当前进度。

用户方针：**拆成两大部分，第一部分先做合格**。第一部分（基础能力）须可靠、可替换、可审计；第二部分（长期连续与发展）按方案二隔离，日后逐条加回。

| 阶段 | 名称 | 状态 | 提交 |
| :--- | :--- | :--- | :--- |
| A | 边界与隔离 | ✅ 完成 | `fa556ba` |
| B | 沙箱降门槛（执行沙箱分层，容器降为可选） | ✅ 完成 | `45a3813` |
| C | 统一循环（工具原语化 + Hooks 三事件 + 对话内多步循环 + 中断介入 + 任务合并 + 副作用实时确认 + 每步独立记账） | ✅ 完成 | `05f6177` `0d7a996` `a9df398` `f323d7f` `47a0bdd` `13fd8ce` |
| D | 任务收尾（旧任务路径去留、交互统一） | ✅ 完成（交互统一 = 任务收进对话 `13fd8ce`；去留 = 保留为对话内次级入口「任务工作副本」） | `13fd8ce` `3efc06c` |
| E | 对账与对齐（常数溯源、文档对齐） | ⏳ 部分 | — |
| F | 能力扩展（技能包、MCP 最小集） | 待开始（C 已解锁） | — |
| G | 逐条加回第二部分 | 待梳理 | — |

**C 阶段要点**：对话即循环——模型请求工具 → 系统经执行沙箱真执行 → 真实结果回灌 → 继续，直到完成或需介入。策略不进循环体，全部经 `BEFORE_TOOL`/`AFTER_TOOL`/`BEFORE_TURN_END` 钩子注入；`run_command` 有现实副作用，走 `ConfirmationService` 实时确认（`GET/POST /chat/runs/{id}/confirmations`），前端可批准/拒绝；步数预算用尽如实提示（`notice:step_budget_exhausted`）；每步事件 `step_started`/`tool_started`/`tool_result` 前端展示。契约见 `docs/计划/2026-10-11-C阶段-统一循环契约.md`。

**D 阶段（任务收尾）✅ 完成**：**交互统一已落地**——顶层导航去掉「任务」，**对话成为唯一入口**；旧任务路径不删除，收进对话头部 ⋯ 菜单的「任务工作副本」次级入口，承载"先看变更再写回"的审阅流程；`TasksPage` 支持嵌入态（不再重复渲染整页 h1 页头）；概览页去掉「任务」卡片。对应 C 契约 §2.6 与验收 6。**去留结论（决策 13）**：保留为对话内次级入口，不删能力。

**F 阶段（能力扩展）🚧 进行中**：技能包（内置基础技能 + 扫描外部目录，三级渐进式披露、零框架零注册中心）与 MCP 最小集（连服务器/列工具/调工具/版本协商，**必须可选、不成为新前置门槛**）。

**验证**：后端 `pytest tests/` 56 项、前端 `vitest` 134 项全绿；ruff、typecheck、build 通过。新增 `tests/test_chat_loop.py` 9 项、前端循环测试 4 项 + 任务入口 1 项。

---

更新时间2026-10-09。完整首版功能、验收、源码提交与主目录整合已完成。功能提交a1b599b，后续仅文档交付状态更新。以后修改应作为新一轮需求推进，不能再使用旧开发缺口说明。

## 权威资料与环境

上层01/02：H:/文档/LingNexus/docs/。完整计划 docs/计划/2026-10-08-完整Windows首版.md。有效结论见讨论笔记，历史研究在docs/研究，使用说明在docs/使用说明.md，逐项A-G验收在docs/验证/2026-10-09-首版交付验收.md。

开发工作树 H:/文档/LingNexus/Ling/.worktrees/m0-continuity，feature/m0-continuity。主目录 H:/文档/LingNexus/Ling的main已快进到同一源码，工作区干净。工作树保留.venv/node_modules/.tools和测试产物供后续开发，均不进入Git；根目录交付dist安装包/说明。

## 只留一段对话，移除场景交流（2026-10-09，用户要求）

用户拍板：不再需要"新建对话"和"场景交流"，多会话管理以后再说。

- **前端**：删除 `ChatHistory.tsx`（侧栏对话列表）、`NewConversation.tsx`（创建表单）；`Shell` 去掉 `conversationNav` 插槽；`client.createConversation` 删除；场景说明区（`chat-scene-note`/`chat-scene-background`）删除；相关死样式清理（`chat.css` + `layout.css` 的 `.chat-history*`/`.chat-scene*`/`.chat-new-conversation`）。
- **保留**：`useConversations` 仍负责找到当前对话并在首次发送后采纳后端默认；后端 `POST /conversations` 接口保留（`default_real` 自动建"日常交流"依赖它）。
- **后端**：`create()` 只收 `agent_id`+`title`，scope 恒为 `real`（`background` 列保留在表里，`NOT NULL DEFAULT ''`，插入省略即可）；`for_chat` 只放行 real；`payload.build_payload` 删掉 `_scene_background` 注入与虚构场景措辞，只如实标注当前情境。
- **中途返工**：我最初在 `build_payload` 里对非 real 直接抛错，结果挂了 `tests/test_evaluation_contract.py`——离线自检脚本会按用例各自的情境渲染（U04 是场景情境，用来验证"虚构不被当成现实经历"）。那道防线是多余的（`for_chat` 已经只放行 real），去掉后改为如实标注情境。**改共用底层函数前，先 grep 谁在调用它。
- **护栏保留（重要取舍）**：历史层/学习层的 scope 隔离（`test_context`/`test_evolution`/`test_learning`/`test_memory_maintenance`/`test_evaluation_contract` 里那些 scene 来源用例）**没删**——它们防的是"场景记录溜进现实记忆"，是安全属性，不是功能面。功能面（创建场景对话、聊天注入场景背景）已移除。
- **测试**：删除 `tests/test_scene_background.py`（整文件测被移除的注入）；改写 `test_new_real_and_scene_...` → 只测现实交流保留人格；移除附件里的中文场景背景用例；前端删除"创建自然/场景对话"两个用例，"对话切换"类用例改用 sessionStorage 记忆键驱动（列表 UI 已不存在）。
- 验证：前端 145 项、后端受影响 148 项全过；真实浏览器核对侧栏无列表、无新建入口、无场景字段，⋯ 菜单只剩批量删除/分段/模型设置。

## 删除一轮对话（2026-10-09，用户要求）

用户要的不是"删整个对话"，而是**删一轮**（你说的话＋灵的回复），且**删掉后必须从后续发给模型的请求里消失**。

- 后端 `conversations/service.py::delete_turns(anchors)`：
  - 轮次 = 一条用户消息 + 其后直到下一条用户消息之间的助手回复；锚点必须是当前路径上的**用户**消息（助手消息锚点 → `PermissionDenied`，不在路径 → `NotFound`）
  - 保留的消息**重新接线**成一条链（第 3 轮直接接回第 1 轮），更新 `head_id`
  - 被删消息的底稿 `history.set_visible(False)` → 后续上下文不再包含它们 ✓，并触发既有回调作废派生的记忆/知识/身份/成长记录
  - 删除节点及其下方旧分支子树
- **两个外键坑（都是实测撞出来的）**：
  1. `conversation_messages.parent_id` 是自引用外键——必须**按层级从深到浅删**；而且"第 0 层"只能是父节点不在删除集合里的那些节点，否则整批无序删除会撞约束
  2. `chat_runs.user_message_id` 也引用消息——不能直接删消息，改为把该列置 NULL（运行记录作为审计保留）；`conversation_segments` 引用被删消息的行则删掉（它的 `list()` 本来就会过滤掉起点不在路径上的段）
- API：`POST /conversations/{id}/delete-turns`，body `{anchors:[...]}`（`Field(min_length=1)`，重复锚点自动去重）。
- 前端：
  - 每条**你的消息**的悬停操作行加「删除这一轮」（与「编辑消息」同级，红色），助手回复不给删除按钮
  - ⋯ 菜单加「批量删除」→ 进入选择模式：用户消息前出现勾选框（`aria-label="选择这一轮：<文本前 24 字>"`），底部「已选 N 轮 / 取消 / 删除所选」
  - 删除前弹确认框，写明"从对话消失、不再发给模型、派生的记忆与身份变化一并作废、无法撤销"
  - 生成中 / 忙碌 / 历史未就绪时按钮禁用（复用会话层的 `canMutate()`）
- 测试：后端 5 项（单轮、批量不相邻、连带旧分支、清空全部、拒绝助手锚点）；前端 3 项（删一轮连带回复、取消保留、批量一次请求）。后端全量 1316 通过，前端 146 通过；真实浏览器实测删除后消息数与接口调用均正确。

## 聊天区铺满全屏（2026-10-09，用户要求）

用户："还是太宽了，紧凑一点，然后对话界面直接占满全屏而不是悬浮在屏幕中央"。

- **聊天区铺满**：`Shell` 在对话页给 `.workspace` 加 `workspace-chat` 类；`main` 去掉内边距并改纵向 flex，聊天面板去掉 `.panel` 的边框/圆角/底色，撑满整个工作区。内容列（860px）居中，`ChatHeader` 加 `chat-head-inner` 内层使标题居中而分隔线仍通栏。
- **消息宽度收紧**：`.chat-message` 的 `max-width` 从 90% 改为 **640px**（此前灵的气泡 762px）。
- **滚动容器不变**：自动跟随滚动的 `ref` 绑在 `.chat-transcript` 上，所以只改外观，让它 `flex: 1` 填满剩余高度，滚动行为照旧。
- **关键坑（找了三轮）**：`styles.css` 在 `main.tsx` 里排在 `App` 之后导入，导致基础样式在打包顺序里反而最后，页面样式全部被 `.page { max-width: 1120px; margin: 0 auto }` 压住。把 `import "./styles.css"` 挪到最前才生效。**教训：改布局没生效时先查样式表打包顺序。**
- 另一个坑：React 用 `<div key={agent.id}>` 包页面，这层也要 `flex: 1` 拉伸，否则聊天撑不到底。
- 验证（1440×900 实测）：面板 238→1440 全宽、高 793 正好填满 main；内容列 860 居中；气泡上限 640。概览页与设置页截图复核，无样式回归。前端 143 项全过。

## 收紧间距（2026-10-09，用户先看预览图后确认）

用户反馈"行间距太大、看着奇怪"。**先量后改**，实测出两处原因，画了改前后对比图确认后才动手。

根因：① `.chat-message-actions` 用 `opacity: 0` 隐藏按钮，但**仍占布局**，每条消息底部白留 47px（实测"你好"那条气泡高 137px，其中 1/3 是空的）；② 正文吃全局 `p { line-height: 1.85 }`。

- 消息操作改为 `position: absolute` 浮在气泡下方（`bottom: -16px`，胶囊底），不再占位；`opacity: 0` 保留以便键盘可达，`(hover: none)` 下回落为 `position: static` 避免与相邻消息重叠。实测按钮与正文留 2px 间隙、不压字。
- **行高只在聊天气泡内收紧**：`.chat-message p, .chat-message .preserve-text { line-height: 1.55 }`，全局 1.85 不动（身份页、记忆页长文保持原样）。
- 其余：段落间距 20→10、气泡内边距 18→12、消息间距 20→14、说话人标签 12→11px。
- 侧栏收紧：导航项 43→37px、`.nav-caption` 下边距 11→8、`新聊天` 下边距 20→14、对话条目 34→30px。

实测（1440×900 真实浏览器）：用户气泡 **137→73px**、灵的气泡 **205→159px**、行高 **25.9→21.7px**、导航 **43→37px**。前端 143 项全过，typecheck 与 build 通过。

**随后补上（提交 d9999af，用户说"继续"后）**：短消息时气泡被撑满 90% 宽度。改成 `.chat-message { width: fit-content; max-width: 90% }`：短消息贴着字走（实测"你好"那条 **62px** 宽，此前撑满约 712px），长回复仍在 90% 处换行靠左/靠右。父容器 `.chat-turn` 与 `.chat-transcript` 都是普通块级流，不受影响。

## 对话页改版：回归正常聊天界面（2026-10-09）

用户反馈"操作逻辑反人类、看不懂"，要求参照 ChatGPT 重排。

- **对话列表进侧栏**：新增 `ChatHistory` 与 `useConversations`，列在导航下方；`Shell` 新增 `conversationNav` 插槽。列表与「新聊天」替代了原来的「保存的对话 / 新建对话」下拉框。
- **侧栏可折叠**：`Shell` 新增折叠开关，状态存 `localStorage`；收起后成图标栏（保留品牌、导航图标、新聊天按钮）。折叠样式写在 `@media (min-width: 761px)` 内，手机端侧栏本来就是横条，不受影响。
- **侧栏分区滚动**：导航（`flex: 0 1 auto` + 自身滚动）与对话列表（`flex: 1 1 auto; min-height: 180px`）各自滚动，保证对话列表永远在可视区。原先整条侧栏一个滚动条，11 个导航项会把对话列表挤到 881 像素处。
- **顶部收敛**：页头大标题与工具条删除，换成一行「对话名 + ⋯」（`ChatHeader`）。分支切换、交流主题与分段、模型设置、切页提示全部收进 ⋯。
- **消息操作改为悬停出现**：`.chat-message-actions` 默认 `opacity: 0`，`hover` / `focus-within` 时显示；`@media (hover: none)` 下常显。键盘与读屏仍可达（元素留在 DOM 里）。
- **输入框重做**：`.chat-composer-box` 圆角外壳 + 内部 textarea；左侧 `＋` 控制附件面板（`ChatAttachments` 从 `<details>` 改为受控 `open`），右侧模型胶囊 + 圆形发送按钮；「消息」标签改为 `aria-label`，原来的 `<label>` 删除。
- 组件增删：新增 `ChatHeader` / `ChatHistory` / `NewConversation` / `useConversations`；删除被取代的 `ConversationPicker`。`App` 拆出按 agent.id 加 key 的 `AgentWorkspace`，使切换个体时对话选择自动重置。
- 行为变化（有意）：正在生成或上传附件时**允许切换对话**——运行在后台继续，回来即接续（与"切页不中断生成"一致）。原先这两个控件在这两种状态下是禁用的。相应更新了 `test_chat_conversations` / `test_chat_attachments` 里的断言并写明理由。
- 验证：前端 **143 项全过**（新增 `ChatSidebar.test.tsx` 5 项），typecheck 与 build 通过。真实 Chromium 复核：侧栏列表、折叠、⋯ 菜单、＋ 附件面板均正常；实测消息操作按钮悬停前 `opacity: 0`、悬停后 `1`。

## 对话页前端调整（2026-10-09）

用户要求参照 Codex：把模型选择与思考强度移出顶部工具栏，放到输入框右下角。

- 新增 `ChatControls.tsx`：胶囊按钮显示「模型名 · 强度」，点开向上弹出面板，内含模型列表（选中打勾，带刷新按钮）与思考强度档位；点击外部或 Esc 收起，选模型后自动收起。旧的 `#chat-profile` / `#chat-effort` 两个 `<select>` 已移除。
- `ChatComposer` 新增 `controls` 插槽，与发送/停止按钮同排在 `.chat-composer-buttons`；`.chat-composer-actions` 左侧保留提示文字。
- 顶部工具栏只剩「保存的对话 / 新建对话 / 对话分支」；「切页会断开连接」的提示并入该工具栏并单独占一行（删除了旧 `.chat-toolbar > .metadata { flex: 1 1 200px }`，否则会把它拽回同一行）。
- **对齐修复**：`.chat-toolbar` 原为 `align-items: center`，而字段列（标签+控件）约 64 高、按钮仅 42、图标 36，居中后短控件飘在半空。改为 `align-items: flex-end`，浏览器实测三者底边分别为 461/461/461。
- 提示文案修正：未选主模型时原提示写「请在上方选择」，已改为「请在下方输入框右下角选择」。
- 测试：新增 `src/test/ChatControls.test.tsx` 7 项；Chat / test_chat_attachments / test_chat_conversations 里共 9 处旧 `selectOptions(getByLabelText(...))` 改为「点开胶囊再点选项」。前端 138 项全过，typecheck 与 build 通过。真实 Chromium 复核：胶囊、弹层、列表勾选、强度档位与整体对齐均正常。

## 人设透明化与可编辑（2026-10-09）

身份审阅页重排为三层：**系统指令 / 当前身份 / 待审批**，下接版本记录。用户要求"不要黑盒"。

- **系统指令改为可编辑清单**：原先写死在 `context/builder.py` 的 5 条开场白搬进清单，存在 `settings` 表（每个体一个键 `owner.instructions:<agent_id>`，值为 JSON 数组，无需新迁移）。未自定义时用代码里的 `DEFAULT_RULES`，行为与升级前一致；可增删改，也可一键恢复默认。缺省为 5 条而非 6 条，是把原第一句整句保留成一条所致。
- **条目带类别与依据**：新增 `GET /agents/{id}/entries`，经 `self_entry_origins → proposals.kind` 反查每条属于哪一类，并附带来源。
- **新增删除**：`changes.remove` 生成"不含该条"的新版本（reason `owner_remove:<key>`），旧版本与 `self_revision_origins` 保持完整，仍可回退。
- **新增类别 `custom`**：只允许所有者使用（`OWNER_ONLY_KINDS`）；`learning/schema.py` 的 `Change.kind` 仍是 6 个 Literal，自动整理永远写不进这一类。
- **修掉吞名字的缺陷**：`identityFormatting.ts` 原先把认不得的键名一律显示成"自我认识条目"，现在改为"预设中文名 → 去掉前缀的自定义名 → 原样显示键名"。
- **前端**：新增 `SystemInstructions`、`CurrentIdentity`、`PendingReview`、`EntryForm`；删除被替换的 `IdentityEditor`。两栏为默认折叠的七行清单。
- **补样式（用户指出）**：本项目的多行输入框由各页面自管样式（见 `.permission-settings textarea` 等），新组件初版漏写，textarea 露出浏览器默认外观。已在 `features.css` 补 `.rule-editor/.entry-editor textarea`，与设置页写法一致；真实浏览器复核已正常。

验证：后端新增 `tests/test_identity_page.py` 14 项全过；改动相关回归 321 通过 1 跳过。前端新增 `src/test/IdentityPage.test.tsx` 9 项，全套 131 通过 0 失败（**注意需 Node 24**：Node 22 下 `ChatStream`/`ManagementPanels` 的 Blob 断言会失败，与本次改动无关，已用 stash 复现确认）。Ruff、Pyright、前端 typecheck 与构建均通过。

真实实例走查 17/17：默认指令可见 → 加/改/删指令 → 加条目（关系）→ 类别与依据正确 → 自由命名条目 → 删除并确认旧版本仍在 → 过期版本号被 409 拦下 → 候选待审阅并批准 → 恢复默认指令。另用真实 Chromium 打开界面，确认三块渲染、分组可展开、就地加改删可用。走查写入的测试条目已全部清除，当前条目为空、指令为默认。

## 源码运行入口（2026-10-09）

工作目录根 **start.bat** 是唯一的本地运行入口，替代早先的准备环境/启动/停止/开发模式四个脚本与运行说明.md。它自动判断并补全环境与界面构建，然后启动本机后台并打开浏览器；停止走托盘退出。批处理为 GBK 编码并固定 CRLF（cmd 解析 UTF-8 批处理会错行）。.gitattributes 对 *.bat/*.cmd 固定 eol=crlf。

实测：真实启动后 /health 返回 200 且 version=0.1.1，首页 HTML 正常，--quit 退出码 0；首次运行分支用替身副本验证输出无解析错误与乱码。同一提交把 /health 版本号改为读取包版本。

## 0.1.1 更新（最新）

用户要求日常模型密钥改为可清理本地缓存、同时保存多个提供商、EXE头像/网页叶状图标。已实现Windows DPAPI加密文件cache/model-credentials.bin、owner-only清除文件与内存、端点绑定/回滚保护、多提供商独立凭据；备份导出不包含缓存。旧仅内存日常策略已过期，但不主动保存此前对话中的测试密钥。

新版0.1.1后端定向320pass，前端123pass，Ruff/Pyright/build通过。最终exe三个启动周期的真实Chrome验证：两个提供商重启自动恢复、清除文件内存、再重启不恢复、配置主备保留、favicon可用，无页面错误，无真实模型调用。图标7种尺寸/7资源。详见docs/验证/2026-10-09-0.1.1缓存与图标.md。

新版安装包dist/LingNexus-0.1.1-Setup.exe，51614083bytes，SHA2565d73008fc76a6369e969799d1094d8bc8cd3e7a7866bc210f8f86312c9b42b3a。没有停止或覆盖用户运行中的旧版。

## 0.1.0 首版验收（历史）

- 全量pytest session26542已结束：1305pass/6skip/0fail，1054.78s。实际Docker三镜像optin已启用；跳过1automaticbackup+3workspace+2restore，均Windowssymlink权限。XML .artifacts/final-tests.xml。不要无理由重跑付费或全部测试。
- Frontend21文件121pass，productionbuild/typecheck成功。Ruff src/tests/scripts全通过，Pyright0错误0警告，gitstageddiffcheck通过。
- 三原生云端协议真实流式短答/两轮工具42；真实模型Python编程、DockerPython/Node、依赖联网审批与离线精确版本/真实本地wheel1→2通过。
- 源程序真实GUI10导航/记忆纠正/获批表单1POST/附件模型42/刷新历史/无页面错误；真实任务GUI暂停恢复、exit1/修复/exit0、下载与审批写回一致通过。
- 最终打包exe（非源后台）再次真实云端模型/Chrome GUI同样通过，session69540结束。
- 中文Inno安装、升级、卸载实际验证：已验证任务与成果可用，凭据不落盘，6个DB/附件/任务资料文件hash不变，自启登记不变。测试临时安装已卸载。
- 独立复核四原发现全部关闭，公开接口调整后70专项通过。Task旧记忆认证被阻止、学习纠正fence、完整taskassets备份恢复和retention、pip版本核实均有复现证据。
- 精确密钥检查1457文件0发现，未输出内容。全部开发/验证后台已明确退出；提供密钥仅留对话/运行内存，验证后台全部退出，交付前清除验证REPL。
- 固定中文评测114/120记忆vs106/120摘要，238完成2失败；已披露复用摘要来源/评分假阴性/价格缓存未知，不重跑美化数字。

## 交付

主目录dist/LingNexus-0.1.0-Setup.exe，51,111,734bytes，SHA25658a25ae5faecc762b11eeb06be140f6f03c9aa7484dc1d859ff135c29789f036。未做发布者签名。0.1.0仅内存凭据策略已被0.1.1可清理加密缓存替代。

所有工作代理已关闭。报告代理先前提交01e9983仅新增评估报告；主代理统一提交完整工程，不推送远端。整合后main与feature代码一致，保留worktree不强删环境。

## 后续不能忘记的边界

Windows本机浏览器GUI，Linux/localmodel/multidevice/audio/video/imagegen以后。LLM会错误，词法查档不保证任意指代都命中；近期tail+相关卡+主动原文，没有每条省略raw的滚动摘要。缓存实际行为由渠道决定，不牺牲纠正换命中率。

Task self/memory/source变动后新任务，不偷换旧检查点资料；现实副作用不因回退/恢复撤销，未知行动不自动重放。二进制/删除下载后手动应用，Node类型擦除不代替tsc。Chrome或Playwrightbrowser和DockerLinux镜像为相应功能依赖。

生成预设/默认effort/可选sampling/Anthropiccache和调用时费用估算已实现；价格缺失或缓存费未知保持unknown，partial单列不当完整总额。

Jev为官方契约核验的独立可选shadow源代码适配，90fixturetests通过，没有图形配置、运营路由或实际中文精度结论。默认主模型主动判断完整运行。

备份task资产256MiB/file、2GiB总、100kentry，临时.tmp内容不存；活动任务需停稳，不静默漏资产。浏览器登录资料不便携导出。
