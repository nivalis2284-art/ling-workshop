# LingNexus 技术栈全貌清单 (Technology Stack Inventory)

> 报告生成时间：2026-10-10  
> 适用版本：LingNexus 0.1.1  
> 基础设施目标平台：Windows 10 / 11 (64 位)

---

## 1. 核心运行时与包管理 (Runtime & Package Managers)

| 运行时 / 工具 | 声明版本要求 | 验证环境版本 | 用途与定位 | 配置文件路径 |
| :--- | :--- | :--- | :--- | :--- |
| **Python** | `>=3.12,<3.13` | `3.12.10` / `3.12.14` | 后端核心服务运行时，单进程异步事件驱动 | [`pyproject.toml:5`](file:///C:/Users/34375/.gemini/antigravity/worktrees/a9a6a57f-1b6b-4d1d-a130-3b0f0fd595d6/subagent-Codebase-Mapper-gsd-researcher-c2769f4b/pyproject.toml#L5) |
| **uv** | `^0.12.x` | `0.12.17` | 高性能依赖同步、虚拟环境管理与哈希锁定 | [`uv.lock`](file:///C:/Users/34375/.gemini/antigravity/worktrees/a9a6a57f-1b6b-4d1d-a130-3b0f0fd595d6/subagent-Codebase-Mapper-gsd-researcher-c2769f4b/uv.lock) |
| **Node.js** | `^20.19.0 \|\| >=22.12.0` | `24.19.0` | 前端开发构建运行时与测试运行宿主 | [`frontend/package.json:14-16`](file:///C:/Users/34375/.gemini/antigravity/worktrees/a9a6a57f-1b6b-4d1d-a130-3b0f0fd595d6/subagent-Codebase-Mapper-gsd-researcher-c2769f4b/frontend/package.json#L14-L16) |
| **npm** | `^10.x` | `10.9.x` | 前端依赖安装与版本固化 | [`frontend/package-lock.json`](file:///C:/Users/34375/.gemini/antigravity/worktrees/a9a6a57f-1b6b-4d1d-a130-3b0f0fd595d6/frontend/package-lock.json) |

---

## 2. 后端生产与核心依赖 (Backend Dependencies)

### 2.1 运行时生产依赖 (Production Dependencies)

| 软件包名 | 版本约束 (`pyproject.toml`) | 核心用途与集成位置 |
| :--- | :--- | :--- |
| `fastapi` | `>=0.115,<1` | 本地 REST API 网关与路由分发 ([`src/lingnexus/api/app.py`](file:///C:/Users/34375/.gemini/antigravity/worktrees/a9a6a57f-1b6b-4d1d-a130-3b0f0fd595d6/subagent-Codebase-Mapper-gsd-researcher-c2769f4b/src/lingnexus/api/app.py)) |
| `uvicorn` | `>=0.30,<1` | 异步 ASGI 服务器，绑定本地环回接口 (`127.0.0.1:8765`) |
| `pydantic` | `>=2.8,<3` | 领域模型、DTO、消息负载与状态快照数据校验与序列化 |
| `httpx` | `>=0.28,<1` | 原生异步 HTTP 客户端，驱动大模型 SSE 长连接流式传输，零三方 SDK |
| `pystray` | `>=0.19,<1` | Windows 系统通知区域托盘图标托管与操作菜单 ([`src/lingnexus/desktop/tray.py`](file:///C:/Users/34375/.gemini/antigravity/worktrees/a9a6a57f-1b6b-4d1d-a130-3b0f0fd595d6/subagent-Codebase-Mapper-gsd-researcher-c2769f4b/src/lingnexus/desktop/tray.py)) |
| `playwright` | `>=1.50,<2` | 受控无头/有头 Chrome 自动化驱动，网页内容提取与受控交互 ([`src/lingnexus/browser/driver.py`](file:///C:/Users/34375/.gemini/antigravity/worktrees/a9a6a57f-1b6b-4d1d-a130-3b0f0fd595d6/subagent-Codebase-Mapper-gsd-researcher-c2769f4b/src/lingnexus/browser/driver.py)) |
| `python-multipart` | `>=0.0.18,<1` | 支持表单文件上传流解析，配合 `UploadBoundary` 防止内存溢出 |
| `pypdf` | `>=5,<7` | PDF 文档解析与文本流提取 ([`src/lingnexus/content/extract.py`](file:///C:/Users/34375/.gemini/antigravity/worktrees/a9a6a57f-1b6b-4d1d-a130-3b0f0fd595d6/subagent-Codebase-Mapper-gsd-researcher-c2769f4b/src/lingnexus/content/extract.py)) |
| `python-docx` | `>=1.1,<2` | Word (.docx) 文档格式解析与段落内容提取 |
| `openpyxl` | `>=3.1,<4` | Excel (.xlsx) 表格结构解析与工作表数据提取 |
| `Pillow` | `>=11,<14` | 图像文件元数据解析、尺寸校验与格式规范化 |

### 2.2 开发、质量与打包依赖 (Dev Dependencies)

| 软件包名 | 版本约束 (`pyproject.toml`) | 核心用途与执行命令 |
| :--- | :--- | :--- |
| `pytest` | `>=8,<9` | 冒烟验收层（34 项、约 20 秒）：`uv run pytest -q -ra`；历史完整套件已于 2026-10-10 退役（可从 git 历史取回） |
| `ruff` | `>=0.6,<1` | 高性能代码格式化与代码静态分析：`uv run ruff check src tests scripts` |
| `pyright` | `>=1.1,<2` | 静态类型安全检查器（标准模式）：`uv run pyright` |
| `httpx2` | `>=2.13,<3` | 测试辅助 HTTP 夹具库 |
| `pyinstaller` | `>=6,<7` | Windows 单文件/目录分发打包：`python scripts/build_windows.py` |

---

## 3. 前端依赖清单 (Frontend Dependencies)

### 3.1 前端生产运行时依赖 (Production)

| 软件包名 | 声明版本 (`package.json`) | 核心用途与集成细节 |
| :--- | :--- | :--- |
| `react` | `^19.3.0` | 前端组件视图框架（采用 React 19 最新特性） |
| `react-dom` | `^19.3.0` | React DOM 渲染引擎与并发根 |
| `lucide-react` | `^1.53.0` | 统一的现代化矢量 SVG 图标库 |

### 3.2 前端构建与测试依赖 (DevDependencies)

| 软件包名 | 声明版本 (`package.json`) | 核心用途与集成细节 |
| :--- | :--- | :--- |
| `vite` | `^8.3.4` | 前端下一代快速打包器与开发热更新服务器 |
| `@vitejs/plugin-react` | `^6.1.2` | Vite React 插件 (Fast Refresh) |
| `typescript` | `^7.0.2` | 前端严格类型校验：`npm run typecheck` (`tsc --noEmit`) |
| `vitest` | `^5.0.3` | 高速单元测试框架（145 项测试通过）：`npm test` |
| `@testing-library/react` | `^16.3.3` | React 组件行为与渲染断言库 |
| `@testing-library/user-event` | `^14.6.7` | 真实用户事件模拟驱动（点击、键盘输入、焦点） |
| `jsdom` | `^30.1.2` | Node.js 端虚拟 DOM 运行环境 |
| `@playwright/test` | `^1.64.0` | 真实浏览器端到端行为验证辅助 |
| `prettier` | `^3.9.9` | 统一前端代码风格格式化 |

---

## 4. 存储与系统基础设施 (Storage & Infrastructure)

| 基础设施组件 | 技术选型 | 配置与实现细节 |
| :--- | :--- | :--- |
| **关系型数据库** | **SQLite 3** | 内置标准库 `sqlite3`；开启 `PRAGMA journal_mode=WAL`，`PRAGMA foreign_keys=ON`，`busy_timeout=10000ms`；支持嵌套 Savepoint 事务；存储于 `%LOCALAPPDATA%/LingNexus/ling.db` |
| **数据库迁移器** | **手写增量 SQL** | `src/lingnexus/storage/migrations/001_history.sql` 至 `024_segments.sql`（共 24 个按序迁移脚本），由 `Database._migrate` 在启动时事务性应用 |
| **凭据加密安全** | **Windows DPAPI** | 通过 `ctypes` 调用 Windows `crypt32.dll` (`CryptProtectData` / `CryptUnprotectData`)，绑定当前 Windows 用户凭据加密，杜绝跨用户或脱机拷贝窃取 |
| **单实例互斥** | **文件锁 (Lock File)** | `InstanceLock` 在数据目录创建会话锁定文件，多开时将新窗口请求无缝转发到已有运行实例并唤起浏览器 |
| **沙箱隔离执行** | **Docker 引擎** | Agent 任务在独立 Docker 容器内执行代码，固定镜像为官方 `python:3.12-slim` 与 `node:24-bookworm-slim`，默认切断容器网络通信 |
| **浏览器环境** | **Google Chrome (CDP)** | 优先调用本地 `C:/Program Files/Google/Chrome/Application/chrome.exe`，通过 Playwright CDP 协议受控操作 |
| **安装包制作** | **Inno Setup 6** | 脚本由 `packaging/installer` 托管，打包生成 `dist/LingNexus-0.1.1-Setup.exe`，支持非管理员按用户独立安装与自启动注册 |

---

## 5. 云端模型端点集成 (LLM Endpoint Integrations)

LingNexus 不使用厂商封装的 SDK，直接与模型提供商的原生端点建立 HTTP 异步长连接：

| API Family (协议族) | 原生端点规范 | 支持能力与协议特征 |
| :--- | :--- | :--- |
| **Anthropic Messages** | `/v1/messages` | 原生 system 提示词通道、原生流式思维链 (`thinking_delta`)、Token 缓存创建与命中计费、`end_turn` 结束标识 |
| **OpenAI Chat Completions** | `/v1/chat/completions` | 兼容 OpenAI、DeepSeek、Moonshot 等标准端点；流式输出、推理内容 (`reasoning_content`) 抓取 |
| **OpenAI Responses** | `/v1/responses` | 原生 `instructions` + `input` 分离格式；新一代流式事件结构解析 |

---

## 6. 系统关键配置与环境变量清单 (Configuration Inventory)

| 变量名 / 配置项 | 默认值 | 作用域与功能描述 |
| :--- | :--- | :--- |
| `LINGNEXUS_DATA_DIR` | `%LOCALAPPDATA%/LingNexus` (Windows) | 全局持久化数据目录覆盖路径（存放数据库、附件、凭据缓存与备份文件） |
| `LINGNEXUS_OWNER_TOKEN` | 动态生成 32 字节安全随机令牌 | 本机所有者身份 Bearer Token，防止本机非授权网页或恶意脚本跨站调用 |
| `LINGNEXUS_DEV_ORIGIN` | 空 (未设置) | 前端开发热更新白名单源（例如 `http://127.0.0.1:5173`），桌面发布包无需此项 |
| `LINGNEXUS_TASK_DOCKER_IMAGE` | `python:3.12-slim` | Agent 编程任务默认的 Python 沙箱镜像 |
| `LINGNEXUS_TASK_NODE_IMAGE` | `node:24-bookworm-slim` | Agent 编程任务默认的 Node.js 沙箱镜像 |
| `LINGNEXUS_DOCKER_SMOKE_IMAGE` | 未设置 (显式跳过) | 自动化冒烟测试所用 Docker 镜像环境 |
| `background_enabled` | `true` (存入 `settings` 表) | 后台 Supervisor 开关状态（控制自动备份、记忆提炼与主动关注） |
| `owner.instructions:<id>` | 默认 5 条安全规则 (存入 `settings`) | 每个个体所有者自定义系统指令清单，优先置于每次对话 Prompt 最前部 |

---

## 7. 构建、测试与运行指令速查 (Commands Cheat Sheet)

```powershell
# 1. 依赖同步 (双端锁定)
uv sync --locked --extra dev
npm --prefix frontend ci

# 2. 本地一键启动 (开发或直接使用)
start.bat
# 或手动运行：
npm --prefix frontend run build
.venv/Scripts/python.exe -m lingnexus.desktop

# 3. 前端热更新开发模式
npm --prefix frontend run dev
# (启动后台时附带环境变量：$env:LINGNEXUS_DEV_ORIGIN="http://127.0.0.1:5173")

# 4. 质量检验（后端冒烟验收约 20 秒）
.venv/Scripts/python.exe -m pytest -q -ra
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe -m pyright
npm --prefix frontend run typecheck
npm --prefix frontend test

# 5. 制作 Windows 交付安装包
.venv/Scripts/python.exe scripts/build_windows.py --smoke --iscc .tools/inno/ISCC.exe
```
