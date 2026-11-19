# vibe-platform · 项目规则

## 0. 上下文铁律（最高优先级）

从今往后，每次开启新对话，请优先读取 `CURRENT_CONTEXT.md` 获取上下文，**严禁默认全量读取 `docs/design` 目录下的长篇历史文档**。除非我明确要求你查阅某个具体文档，否则只依据上下文简报进行开发。

补充约束：

- 不主动读取 `docs/archive/**`；需要历史细节时必须等我明确指令。
- `TECH_STACK.md`、`TECH_STACK_DELTA_*.md`、`PROJECT_STATUS.md` 仅按需检索局部章节，禁止整文件加载。
- 每次完成里程碑或事实变化后，同步更新 `CURRENT_CONTEXT.md`，保持不超过 1000 字。
- 归档历史文档使用 `python3 scripts/archive_docs.py --apply`；默认不带参数为 dry-run，恢复使用 `--restore <timestamp>`。

## 1. 开发约定

- 全栈：React 18 + Java 21 / Spring Boot 3.3.5 BFF + Python 3.12 / FastAPI / LangGraph。
- 中间件复用 `~/Workspace/infra`，禁止为本项目重复部署 MySQL / Redis / MinIO。
- 后端 API 交付前必须本地跑通测试；Python 代码写完后执行 `black`。
- 只做本地 commit，不自动 `git push`。
- 全局规范见 `~/AGENTS.md`，与本文件不冲突时同时生效。

## 2. 开发会话上下文预算

目标是避免“同一批大文件 / 大日志被反复带进上下文”，不是限制深入排查。

- **读文件**：先用 `rg` / `grep -n` 定位，再用 `Read offset/limit` 读目标函数；
  禁止整文件加载超过约 200 行的文件（`WorkspacePage.jsx`、`inspect_sdk.js`、
  `agent/graph.py`、`coders.py`、`test_fullstack.py` 等）。
- **命令输出**：禁止 `cat` 大文件或直接打印完整 E2E 日志 / JSON；
  统一用 `head`、`tail`、`rg`、`jq` 摘要。
- **E2E 日志**：默认只输出 `httpStatus / action / result / errorCode /
  parentOid / siblingIndex`；完整响应体、DOM tree、placement 仅在
  `LIVE_DEBUG=1` 或 `LIVE_DIAGNOSE=1` 时展开。
- **失败信息**：失败时只展开关键诊断字段并截断到约 600 字符，不整段回灌。
- **重复读取**：同一会话内不重复读取同一文件；编辑后不重读验证，依赖
  `Edit/Write` 成功、定向测试和 `node --check` / `git diff --check`。
- **测试节奏**：日常只跑定向测试；`scripts/ci_local.sh` 仅在里程碑或交付前跑。
- **跨文件排查**：优先派子 Agent 做只读搜索，只回收结论与文件位置，不把
  全量搜索结果带回主会话。
