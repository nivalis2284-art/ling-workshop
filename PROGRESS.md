# 实施恢复点 — 完整 Windows 首版

更新时间2026-10-09。完整首版功能、验收、源码提交与主目录整合已完成。功能提交a1b599b，后续仅文档交付状态更新。以后修改应作为新一轮需求推进，不能再使用旧开发缺口说明。

## 权威资料与环境

上层01/02：H:/文档/LingNexus/docs/。完整计划 docs/计划/2026-10-08-完整Windows首版.md。有效结论见讨论笔记，历史研究在docs/研究，使用说明在docs/使用说明.md，逐项A-G验收在docs/验证/2026-10-09-首版交付验收.md。

开发工作树 H:/文档/LingNexus/Ling/.worktrees/m0-continuity，feature/m0-continuity。主目录 H:/文档/LingNexus/Ling的main已快进到同一源码，工作区干净。工作树保留.venv/node_modules/.tools和测试产物供后续开发，均不进入Git；根目录交付dist安装包/说明。

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
