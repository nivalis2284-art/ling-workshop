# LingNexus · 灵

LingNexus 是由所有者掌握发展方向的长期个人 AI 系统。当前首版运行在 Windows，本机后台和中文图形界面管理云端模型、交流、连续经历、可演化的自我状态、任务与主动关注。角色扮演是可以进入和退出的一种交流情境。当前自我不会因重启、换模型或退出场景恢复成初始设定。

长期目标与抽象需求分别在上一级的 [01](../docs/01-概念与原则.md) 和 [02](../docs/02-抽象需求.md)。技术选择允许替换。执行范围是 [完整 Windows 首版计划](docs/计划/2026-10-08-完整Windows首版.md)，详细状态在 [PROGRESS](PROGRESS.md)。

## 安装与日常使用

本次交付安装包在 dist/LingNexus-0.1.1-Setup.exe；构建原件在工作树 packaging/installer。安装器按当前 Windows 用户安装，无需管理员权限，默认不自启。首版验证见 [交付验收记录](docs/验证/2026-10-09-首版交付验收.md)，本次更新见 [0.1.1更新记录](docs/验证/2026-10-09-0.1.1缓存与图标.md)。

启动后打开本机图形页面，并在通知区域显示托盘图标。关闭页面后后台继续运行，从托盘选择退出才能停止整个程序。默认资料目录是 %LOCALAPPDATA%/LingNexus，程序目录与资料目录分开。

首次使用先创建灵，再在设置添加模型连接并选主模型。支持 Anthropic Messages、OpenAI Chat Completions、OpenAI Responses 三种原生端点，能力按渠道实际支持设置。模型连接允许同时保存多个提供商，各自配置协议、地址和密钥。密钥保存到当前 Windows 用户加密的本地缓存，重启自动读取；可在设置清除缓存，前端不读取已有密钥。

详细步骤、编程所需 Docker 环境、审批、备份与限制见 [使用说明](docs/使用说明.md)。

## 当前功能

- 交流：流式文字和服务实际返回的可见思考、附件、临时场景、修改自己的消息、重新生成回复、主题和时间分段、旧分支回看。
- 连续发展：有来源的事件卡和知识、自动整理、记忆纠正和归档、自我变化候选审批与回退、长期目标、版本化技能。
- 行动：多步 Agent 任务、中途指导、暂停恢复、隔离 Python/Node/TypeScript 编程、指定项目副本、真实测试、成果下载与审阅后写回。
- 主动关注：提醒、指定文件/网页/RSS 变化、任务结果、关注事项回顾，在预先授权范围观察与提出下一步。
- 管理：单实例托盘、可选登录启动、目录和网页授权、后台暂停、用量、渠道价格估算、可读导出、带附件和任务副本的备份、恢复前保护。

人格不是不可变角色卡。普通经验和低风险调整可自动形成；身份、关系、价值、重大人格或重要能力变化需要所有者确认。候选、网页和技能都不能授予权限。

## 开发环境

### 不打包，直接双击运行

开发迭代时不需要打包：双击根目录的 **start.bat** 即可。第一次会自动创建虚拟环境、安装依赖并生成界面（需要联网，几分钟），之后双击直接启动。启动后浏览器自动打开 LingNexus 页面，通知区域出现托盘图标。

那个黑色窗口就是正在运行的程序，可以最小化，但不要直接关闭；要退出请右键托盘里的 LingNexus 图标选择退出。资料保存在根目录 `.ling-data`，与源码分开，均不进入 Git。重复双击 start.bat 不会启动第二个程序，只会把已有界面重新打开。

### 手动命令

Python 3.12、uv、Node.js ^20.19.0 或 >=22.12.0。依赖分别由 uv.lock 和 frontend/package-lock.json 固定。当前验证使用 Python 3.12.10、Node 24.19。

~~~powershell
uv sync --locked --extra dev
npm --prefix frontend ci
npm --prefix frontend run build
.venv/Scripts/python.exe -m lingnexus.desktop
~~~

热更新前端使用 npm --prefix frontend run dev，后台设置 LINGNEXUS_DEV_ORIGIN=http://127.0.0.1:5173。普通桌面启动不需要此变量。不要把模型密钥写入环境文件或命令行。

~~~powershell
.venv/Scripts/python.exe -m pytest -q -ra
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe -m pyright
npm --prefix frontend test
npm --prefix frontend run build
~~~

真实 Docker 用例需要预先准备镜像，并设置 LINGNEXUS_DOCKER_SMOKE_IMAGE=python:3.12-slim、LINGNEXUS_TASK_DOCKER_IMAGE=python:3.12-slim、LINGNEXUS_TASK_NODE_IMAGE=node:24-bookworm-slim。未设置时明确跳过。编程默认不联网，安装精确版本依赖需要单次任务审批。

Windows 打包在锁定环境运行：

~~~powershell
.venv/Scripts/python.exe scripts/build_windows.py --smoke --iscc .tools/inno/ISCC.exe
~~~

## 模块关系与数据

采用模块化单体：一套后台进程中有分清职责的模块，通过公开服务交互。Application 只组装服务。common/storage 提供小型公共基础，历史、自我、记忆、模型、任务、活动、网页、维护等各自实现，外部模型和执行工具可替换。

原始经历、派生记忆、当前自我、待审变化和执行进度分别保存。修正来源会隔离失效派生资料；引用旧记忆的任务被阻止认证，需要新任务按有效资料重新处理。恢复不能撤销已发生的现实操作。

备份包含数据库、附件和任务工作副本/基线/安装依赖，临时目录内容不作为长期资产。恢复先验证，在下次桌面启动独占资料目录时应用，保留恢复前备份。自动备份默认保留 14 份，只清理自身创建且校验完整的旧备份。

## 能力边界与证据

当前 LLM 仍可能错误，不能承诺永久记忆、零误判或成功完成任意编程任务。上下文采用稳定当前状态、有限近期历史、相关事件线索和按需查原文，不声称消除长上下文偏移。缓存由渠道决定，开启选项不代表实际命中。

本轮真实模型、Docker、Chrome 和界面证据见 [编程与记忆验证](docs/验证/2026-10-09-真实编程与记忆.md)、[模型评估报告](docs/验证/2026-10-09-模型评估报告.md) 和交付验收记录。评测保留原始严格分数及评分/来源限制，不能解释为通用最优证明。

Linux、本地模型、多设备、音频视频、图片生成与任意本机软件操作属于后续拓展。可选 Jev 需独立配置和评估，未配置时主模型关注判断仍可运行。
