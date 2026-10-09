# 实施恢复点 — 完整 Windows 首版

更新时间2026-10-09。完整首版功能、验收、源码提交与主目录整合已完成。功能提交a1b599b，后续仅文档交付状态更新。以后修改应作为新一轮需求推进，不能再使用旧开发缺口说明。

## 权威资料与环境

上层01/02：H:/文档/LingNexus/docs/。完整计划 docs/计划/2026-10-08-完整Windows首版.md。有效结论见讨论笔记，历史研究在docs/研究，使用说明在docs/使用说明.md，逐项A-G验收在docs/验证/2026-10-09-首版交付验收.md。

开发工作树 H:/文档/LingNexus/Ling/.worktrees/m0-continuity，feature/m0-continuity。主目录 H:/文档/LingNexus/Ling的main已快进到同一源码，工作区干净。工作树保留.venv/node_modules/.tools和测试产物供后续开发，均不进入Git；根目录交付dist安装包/说明。

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
