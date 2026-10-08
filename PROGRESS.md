# 实施恢复点 — 完整 Windows 首版

更新时间2026-10-09。完整首版功能与验收已完成，当前仅剩最终源码提交/主目录快进整合和goal完成记录。以后修改应作为新一轮需求推进，不能再使用旧开发缺口说明。

## 权威资料与环境

上层01/02：H:/文档/LingNexus/docs/。完整计划 docs/计划/2026-10-08-完整Windows首版.md。有效结论见讨论笔记，历史研究在docs/研究，使用说明在docs/使用说明.md，逐项A-G验收在docs/验证/2026-10-09-首版交付验收.md。

开发工作树 H:/文档/LingNexus/Ling/.worktrees/m0-continuity，feature/m0-continuity。主目录 H:/文档/LingNexus/Ling/main将快进到同一源码。工作树保留.venv/node_modules/.tools和测试产物供后续开发，均不进入Git；根目录交付dist安装包/说明。

## 最终验收

- 全量pytest session26542已结束：1305pass/6skip/0fail，1054.78s。实际Docker三镜像optin已启用；跳过1automaticbackup+3workspace+2restore，均Windowssymlink权限。XML .artifacts/final-tests.xml。不要无理由重跑付费或全部测试。
- Frontend21文件121pass，productionbuild/typecheck成功。Ruff src/tests/scripts全通过，Pyright0错误0警告，gitstageddiffcheck通过。
- 三原生云端协议真实流式短答/两轮工具42；真实模型Python编程、DockerPython/Node、依赖联网审批与离线精确版本/真实本地wheel1→2通过。
- 源程序真实GUI10导航/记忆纠正/获批表单1POST/附件模型42/刷新历史/无页面错误；真实任务GUI暂停恢复、exit1/修复/exit0、下载与审批写回一致通过。
- 最终打包exe（非源后台）再次真实云端模型/Chrome GUI同样通过，session69540结束。
- 中文Inno安装、升级、卸载实际验证：已验证任务与成果可用，凭据不落盘，6个DB/附件/任务资料文件hash不变，自启登记不变。测试临时安装已卸载。
- 独立复核四原发现全部关闭，公开接口调整后70专项通过。Task旧记忆认证被阻止、学习纠正fence、完整taskassets备份恢复和retention、pip版本核实均有复现证据。
- 精确密钥检查1457文件0发现，未输出内容。全部开发/验证后台已明确退出；提供密钥仅留NodeREPL内存，最终清除变量即可。
- 固定中文评测114/120记忆vs106/120摘要，238完成2失败；已披露复用摘要来源/评分假阴性/价格缓存未知，不重跑美化数字。

## 交付

主目录dist/LingNexus-0.1.0-Setup.exe，51,111,734bytes，SHA25658a25ae5faecc762b11eeb06be140f6f03c9aa7484dc1d859ff135c29789f036。未做发布者签名。不保存日常用户模型凭据，退出后重新提供。

所有工作代理已关闭。报告代理先前提交01e9983仅新增评估报告；主代理统一提交完整工程，不推送远端。整合后main与feature代码一致，保留worktree不强删环境。

## 后续不能忘记的边界

Windows本机浏览器GUI，Linux/localmodel/multidevice/audio/video/imagegen以后。LLM会错误，词法查档不保证任意指代都命中；近期tail+相关卡+主动原文，没有每条省略raw的滚动摘要。缓存实际行为由渠道决定，不牺牲纠正换命中率。

Task self/memory/source变动后新任务，不偷换旧检查点资料；现实副作用不因回退/恢复撤销，未知行动不自动重放。二进制/删除下载后手动应用，Node类型擦除不代替tsc。Chrome或Playwrightbrowser和DockerLinux镜像为相应功能依赖。

生成预设/默认effort/可选sampling/Anthropiccache和调用时费用估算已实现；价格缺失或缓存费未知保持unknown，partial单列不当完整总额。

Jev为官方契约核验的独立可选shadow源代码适配，90fixturetests通过，没有图形配置、运营路由或实际中文精度结论。默认主模型主动判断完整运行。

备份task资产256MiB/file、2GiB总、100kentry，临时.tmp内容不存；活动任务需停稳，不静默漏资产。浏览器登录资料不便携导出。
