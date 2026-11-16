"""Architect LLM 适配层。

与局部 UI Run 的 ``AgentLLM`` 完全分离：
  - ``MockArchitectLLM`` 确定性推进应用形态 -> 动态槽位 -> 分级契约；
  - ``DeepSeekArchitectLLM`` 使用 OpenAI 兼容接口输出结构化 JSON。

LLM 只负责“分析 + 起草”，应用形态约束、槽位校验、置信度封顶、契约
分级校验全部由确定性代码完成，避免模型过度设计或虚报置信度。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from ai_core.adapters.model.deepseek import DeepSeekClient
from ai_core.agent.memory import fallback_summary
from ai_core.architect.confidence import is_known_shape, missing_slots_for
from ai_core.architect.slots import merge_inferred_slots
from ai_core.architect.source import format_source
from ai_core.config import Settings, get_settings
from ai_core.domain.architect import (
    APP_SHAPE_LABELS,
    ArchitectAnalysis,
    ArchitectApiRoute,
    ArchitectComponentInteraction,
    ArchitectImplementationPhase,
    ArchitectQuestion,
    ArchitectTableRelation,
    normalize_database_engine,
)
from ai_core.ports.model import ModelClient, ModelClientError

logger = logging.getLogger(__name__)


class ArchitectLLMError(Exception):
    """Architect LLM 输出不可用。"""


class ArchitectLLMConfigurationError(ArchitectLLMError):
    """缺少模型密钥或配置。"""


@dataclass(frozen=True)
class ArchitectContext:
    """分析与起草共享的只读上下文。"""

    project_id: str
    project_name: str
    source_files: dict[str, str]
    ui_history: list[dict[str, Any]]
    slots: dict[str, Any]
    app_shape: str | None
    round: int
    threshold: float
    retrieved_chunks: list[dict[str, Any]] = field(default_factory=list)
    source_hash: str = ""
    # 快照保存时生成的项目摘要 + 本次改动说明；两者可为空（首次启动）。
    project_summary: str = ""
    latest_changes: str = ""
    # 用户最近通过 Direct Ops 手工修改的摘要；由 ChangeLogStore 注入。
    direct_changelog: list[dict[str, Any]] = field(default_factory=list)
    # 影子 LLM 专用的只读架构快照（包含当前澄清问题 / 契约摘要）。
    # 主流程的 analyze / draft 不读取该字段。
    architecture_snapshot: str = ""


@dataclass(frozen=True)
class ContractDraft:
    """LLM 起草的分级契约，尚未写入 runId / contractId。"""

    contract_markdown: str
    tech_stack: dict[str, Any] = field(default_factory=dict)
    api_routes: list[ArchitectApiRoute] = field(default_factory=list)
    database_ddl: str = ""
    # 主从表 / 关联表定义：Users 1 ── N Resumes 等业务关联。
    # LLM 漏写时由 DDL 的 <parent>_id 列确定性推导，不会阻断生成。
    relations: list[ArchitectTableRelation] = field(default_factory=list)
    component_interactions: list[ArchitectComponentInteraction] = field(
        default_factory=list
    )
    # 前端实施结构：前端路由、页面布局与组件清单。
    # FrontendCoder 只依赖这三项生成初始 React 骨架。
    frontend_routes: list[dict[str, Any]] = field(default_factory=list)
    page_layout: list[dict[str, Any]] = field(default_factory=list)
    components: list[dict[str, Any]] = field(default_factory=list)
    ui_logic: list[dict[str, Any]] = field(default_factory=list)
    frontend_data_model: list[dict[str, Any]] = field(default_factory=list)
    script_spec: dict[str, Any] = field(default_factory=dict)
    implementation_path: list[ArchitectImplementationPhase] = field(
        default_factory=list
    )
    assumptions: list[str] = field(default_factory=list)
    # AI-Native 应用：生成真实 openai SDK 调用与对话/上传界面。
    include_ai_capability: bool = False
    # 用户只给出抽象目标时，契约只描述空白骨架；FrontendCoder 据此
    # 渲染欢迎页与占位组件，不得生成任何猜测出来的业务 UI。
    skeleton_mode: bool = False


class ArchitectLLM:
    """Architect 模型接口。"""

    def analyze(self, context: ArchitectContext) -> ArchitectAnalysis:
        raise NotImplementedError

    def draft_contract(self, context: ArchitectContext) -> ContractDraft:
        raise NotImplementedError

    def revise_contract(
        self,
        context: ArchitectContext,
        previous: ContractDraft,
        feedback: str,
    ) -> ContractDraft:
        """根据校验反馈修复契约；默认重新起草。"""
        return self.draft_contract(context)

    def summarize_history(self, text: str, max_chars: int = 200) -> str:
        """把长对话压缩为核心需求与约束。子类必须实现。"""
        raise NotImplementedError

    def answer_helper(
        self,
        context: ArchitectContext,
        question: str,
        history: list[dict[str, Any]] | None = None,
    ) -> str:
        """影子助手：基于只读架构上下文回答问题。

        实现方不得写入 slots / contract，也不得触发任何状态推进。
        """
        raise NotImplementedError


# ------------------------------------------------------------
# Mock
# ------------------------------------------------------------
class MockArchitectLLM(ArchitectLLM):
    """无密钥、确定性的架构澄清流程。

    对话前置模式：先从用户自然语言目标中推断应用形态，再补齐该形态的
    动态槽位；推断不出时用自由文本询问用户群体与核心功能，绝不展示
    “全栈 / 轻量 / 脚本”三选一。
    """

    def analyze(self, context: ArchitectContext) -> ArchitectAnalysis:
        slots = dict(context.slots)
        # Mock 也必须“听懂”用户自由输入：从历史对话中确定性回填框架、
        # 数据库、实体与业务流程。否则用户每轮补充都会被无视。
        for message in context.ui_history:
            content = str((message or {}).get("content") or "")
            slots = merge_inferred_slots(slots, content)
        shape = slots.get("app.shape")
        if not is_known_shape(shape):
            return ArchitectAnalysis(
                app_shape=None,
                confidence_score=0.30,
                missing_slots=["app.shape"],
                slots=slots,
                reasoning=(
                    "源码与历史对话不足以判断应该生成轻量网页、自动化脚本"
                    "还是全栈系统，必须先询问应用形态。"
                ),
            )

        missing = missing_slots_for(shape, slots)
        if missing:
            return ArchitectAnalysis(
                app_shape=shape,
                confidence_score=0.90,
                missing_slots=missing,
                slots=slots,
                reasoning=(
                    f"已确认应用形态为 {APP_SHAPE_LABELS.get(shape, shape)}，"
                    f"还需要补齐动态槽位：{', '.join(missing)}。"
                ),
            )

        return ArchitectAnalysis(
            app_shape=shape,
            confidence_score=0.94,
            missing_slots=[],
            slots=slots,
            tech_stack=self._tech_stack(shape, slots),
            business_flows=self._business_flows(shape, slots),
            entities=self._entities(shape, slots),
            assumptions=[],
            reasoning="应用形态与关键槽位均已明确，可以生成分级实施契约。",
        )

    def draft_contract(self, context: ArchitectContext) -> ContractDraft:
        shape = context.app_shape or context.slots.get("app.shape")
        if shape == "lightweight_web":
            return self._lightweight_contract(context)
        if shape == "script_automation":
            return self._script_contract(context)
        if shape == "full_stack_system":
            return self._full_stack_contract(context)
        raise ArchitectLLMError(f"无法为未知应用形态生成契约: {shape}")

    def revise_contract(
        self,
        context: ArchitectContext,
        previous: ContractDraft,
        feedback: str,
    ) -> ContractDraft:
        # Mock 的契约是确定性生成的；反馈只影响修订说明，不改变结构。
        draft = self.draft_contract(context)
        note = f"\n\n## 修订记录\n\n- 根据反馈调整：{feedback.strip()}\n"
        return ContractDraft(
            contract_markdown=draft.contract_markdown + note,
            tech_stack=draft.tech_stack,
            api_routes=draft.api_routes,
            database_ddl=draft.database_ddl,
            component_interactions=draft.component_interactions,
            frontend_routes=draft.frontend_routes,
            page_layout=draft.page_layout,
            components=draft.components,
            ui_logic=draft.ui_logic,
            frontend_data_model=draft.frontend_data_model,
            script_spec=draft.script_spec,
            implementation_path=draft.implementation_path,
            assumptions=draft.assumptions,
            include_ai_capability=draft.include_ai_capability,
            skeleton_mode=draft.skeleton_mode,
        )

    # --------------------------------------------------------
    # Mock 分级契约
    # --------------------------------------------------------
    def summarize_history(self, text: str, max_chars: int = 200) -> str:
        """Mock 用确定性截取，保证离线链路可验证。"""
        return fallback_summary(text, max_chars)

    def answer_helper(
        self,
        context: ArchitectContext,
        question: str,
        history: list[dict[str, Any]] | None = None,
    ) -> str:
        """离线影子助手：基于当前槽位做确定性解释。

        只读：不修改 context，也不触碰主 Architect 状态机。答案明确标注
        “只读”，避免用户误以为在这里提问会推进架构流程。
        """
        shape = context.app_shape or context.slots.get("app.shape")
        shape_label = APP_SHAPE_LABELS.get(shape, shape or "尚未确定")
        backend = str(
            context.slots.get("backend.framework")
            or context.slots.get("script.runtime")
            or "尚未确定"
        )
        database = (
            normalize_database_engine(context.slots.get("database.engine"))
            or "尚未确定"
        )
        prompt = str(question or "").strip()
        parts: list[str] = [f"当前架构形态：{shape_label}。"]

        if (
            "数据库" in prompt
            or "mysql" in prompt.lower()
            or "sqlite" in prompt.lower()
        ):
            parts.append(
                f"当前数据库选型是 {database}。MySQL / PostgreSQL 用于生产，"
                "SQLite 只用于本地与沙盒预览；这样能保证预览与部署环境尽量"
                "一致，同时避免把测试数据库带上线。"
            )
        if "node" in prompt.lower() or "后端" in prompt or "java" in prompt.lower():
            parts.append(
                f"当前记录的后端 / 运行时选型是 {backend}。平台的核心业务更"
                "看重类型安全、事务边界与长期可维护性；如果你希望换成 "
                "Node.js，需要在【架构确认】里提出修订，影子助手不会直接改。"
            )
        if not parts[1:]:
            stack = "、".join(
                f"{key}={value}"
                for key, value in context.slots.items()
                if value not in (None, "", [])
            )
            parts.append(
                "当前已确认的架构信息："
                + (stack or "尚无槽位，仍需在架构确认中补齐关键问题。")
            )

        if context.retrieved_chunks:
            titles = "；".join(
                str(chunk.get("title") or chunk.get("docId") or "知识库")
                for chunk in context.retrieved_chunks[:3]
            )
            parts.append(f"知识库参考：{titles}。")
        parts.append("以上为只读解释，不会修改或推进主架构状态机。")
        return "\n\n".join(parts)

    def _lightweight_contract(self, context: ArchitectContext) -> ContractDraft:
        flow = str(context.slots.get("ui.primary_flow") or "展示内容并响应交互").strip()
        storage = str(context.slots.get("ui.persistence") or "localStorage")
        interactions = [
            ArchitectComponentInteraction(
                component="App",
                event="mount",
                api_route="",
                logic="从浏览器存储读取数据并初始化页面状态",
            ),
            ArchitectComponentInteraction(
                component="PrimaryAction",
                event="click",
                api_route="",
                logic=f"执行核心流程：{flow}；成功后立即更新本地状态",
            ),
        ]
        data_model = [
            {
                "name": "appState",
                "storage": storage,
                "version": 1,
                "fields": ["items", "updatedAt"],
            }
        ]
        markdown = f"""# 轻量网页工具实施契约

## 1. 项目目标

**{context.project_name}** 是一个纯前端轻量工具，不引入独立后端与数据库。

## 2. 核心操作流程

{flow}

## 3. UI 交互

| 组件 | 事件 | 行为 | 成功反馈 | 失败反馈 |
|---|---|---|---|---|
| App | mount | 读取 {storage} 并恢复状态 | 展示已有数据 | 回退为空状态并提示 |
| PrimaryAction | click | 执行核心流程 | 更新界面并持久化 | 保留旧状态并显示错误 |

必须实现空状态、加载状态与错误状态。

## 4. 状态管理

- 使用 React `useState` / `useReducer` 管理页面状态；
- 所有写操作先更新内存状态，再持久化到 `{storage}`；
- 持久化失败时回滚内存状态，避免界面与存储不一致。

## 5. 前端数据模型

```json
{{
  "version": 1,
  "items": [],
  "updatedAt": "ISO-8601"
}}
```

## 6. 实施路径

1. 梳理现有页面与组件；
2. 实现核心交互与本地状态；
3. 接入 `{storage}` 持久化与版本迁移；
4. 补齐空 / 错 / 加载状态；
5. 执行构建与交互验收。
"""
        return ContractDraft(
            contract_markdown=markdown,
            tech_stack={
                "appShape": "lightweight_web",
                "frontend": "React 18 + Vite",
                "persistence": storage,
                "backend": None,
                "database": None,
            },
            api_routes=[],
            database_ddl="",
            component_interactions=interactions,
            frontend_routes=[
                {
                    "path": "/",
                    "name": "主页面",
                    "description": f"承载核心流程：{flow}",
                }
            ],
            page_layout=[
                {"region": "header", "title": "应用标题", "description": "品牌与状态"},
                {
                    "region": "main",
                    "title": "核心工作区",
                    "description": flow,
                },
            ],
            components=[
                {
                    "name": "PrimaryAction",
                    "role": "action",
                    "section": "核心工作区",
                    "description": "触发核心流程并展示成功 / 失败反馈",
                },
                {
                    "name": "ItemList",
                    "role": "list",
                    "section": "核心工作区",
                    "description": f"展示并持久化到 {storage} 的数据",
                },
            ],
            ui_logic=[
                {
                    "flow": flow,
                    "states": ["empty", "ready", "error"],
                    "persistence": storage,
                }
            ],
            frontend_data_model=data_model,
            script_spec={},
            implementation_path=[
                ArchitectImplementationPhase(
                    phase="frontend",
                    title="轻量前端实现",
                    tasks=["核心交互", "本地状态", "浏览器持久化", "构建验收"],
                )
            ],
            assumptions=[],
        )

    def _script_contract(self, context: ArchitectContext) -> ContractDraft:
        runtime = str(context.slots.get("script.runtime") or "python")
        script_input = str(context.slots.get("script.input") or "命令行参数")
        output = str(context.slots.get("script.output") or "标准输出")
        logic = str(context.slots.get("script.logic") or "执行核心数据处理")
        dependencies = (
            ["requests>=2.32", "python-dotenv>=1.0"]
            if runtime == "python"
            else ["axios>=1.7", "dotenv>=16.4"]
        )
        entry = "main.py" if runtime == "python" else "src/index.js"
        spec = {
            "runtime": runtime,
            "entry": entry,
            "dependencies": dependencies,
            "input": script_input,
            "output": output,
            "exitCodes": {"0": "成功", "1": "输入或处理失败", "2": "外部依赖失败"},
            "timeoutSeconds": 30,
            "retries": 2,
        }
        markdown = f"""# 自动化脚本实施契约

## 1. 项目目标

**{context.project_name}** 是一个无 UI 的后台自动化脚本。

## 2. 运行环境

- 运行时：`{runtime}`
- 入口：`{entry}`
- 依赖：{", ".join(f"`{item}`" for item in dependencies)}

## 3. 输入与输出

- **输入**：{script_input}
- **输出**：{output}
- 所有文件路径必须通过参数或环境变量传入，禁止写死本机路径。

## 4. 核心处理逻辑

{logic}

## 5. 异常与可观测性

- 外部调用设置 30 秒超时，最多重试 2 次；
- 失败时输出可诊断错误并返回非零退出码；
- 禁止在日志中打印密钥、Token 或完整个人数据。

## 6. 验收标准

1. 使用示例输入可生成约定格式的输出；
2. 输入缺失时返回退出码 1，并给出明确提示；
3. 外部接口超时不会无限阻塞；
4. 重复执行不会产生未声明的副作用。

## 7. 实施路径

1. 建立 `{entry}` 与依赖清单；
2. 实现输入解析与校验；
3. 实现核心处理与输出序列化；
4. 补充超时、重试与错误码；
5. 用样例数据执行端到端验收。
"""
        return ContractDraft(
            contract_markdown=markdown,
            tech_stack={
                "appShape": "script_automation",
                "runtime": runtime,
                "dependencies": dependencies,
            },
            api_routes=[],
            database_ddl="",
            component_interactions=[],
            ui_logic=[],
            frontend_data_model=[],
            script_spec=spec,
            implementation_path=[
                ArchitectImplementationPhase(
                    phase="script",
                    title="脚本实现",
                    tasks=[
                        "入口与依赖",
                        "输入校验",
                        "核心逻辑",
                        "异常处理",
                        "样例验收",
                    ],
                )
            ],
            assumptions=[],
        )

    def _full_stack_contract(self, context: ArchitectContext) -> ContractDraft:
        backend = str(context.slots.get("backend.framework") or "java_spring_boot")
        database = (
            normalize_database_engine(context.slots.get("database.engine")) or "mysql"
        )
        flows = str(context.slots.get("business.key_flows") or "创建并查询业务记录")
        entities = str(context.slots.get("data.entities") or "业务记录")
        table = _table_name(entities)
        ai_enabled = _wants_ai_capability(context)
        api_routes = [
            ArchitectApiRoute(
                method="GET",
                path=f"/api/v1/{table}",
                request="查询参数：page, size",
                response="分页记录列表",
                description="查询业务记录",
            ),
            ArchitectApiRoute(
                method="POST",
                path=f"/api/v1/{table}",
                request="业务字段 + Idempotency-Key",
                response="创建后的记录",
                description="创建业务记录",
            ),
        ]
        if ai_enabled:
            api_routes.extend(
                [
                    ArchitectApiRoute(
                        method="POST",
                        path="/api/v1/ai/chat",
                        request="对话消息 messages",
                        response="模型回复内容",
                        description="AI 对话：调用 OpenAI 生成回复",
                    ),
                    ArchitectApiRoute(
                        method="POST",
                        path="/api/v1/ai/analyze",
                        request="multipart/form-data：file + prompt",
                        response="结构化分析结果",
                        description="AI 分析：上传简历/文档并调用 OpenAI",
                    ),
                ]
            )
        ddl = _mock_ddl(table, database)
        interactions = [
            ArchitectComponentInteraction(
                component="RecordList",
                event="mount",
                api_route=f"GET /api/v1/{table}",
                logic="加载分页数据并渲染空 / 错 / 加载状态",
            ),
            ArchitectComponentInteraction(
                component="SubmitButton",
                event="click",
                api_route=f"POST /api/v1/{table}",
                logic="校验表单、防重复提交、成功后刷新列表",
            ),
        ]
        if ai_enabled:
            interactions.extend(
                [
                    ArchitectComponentInteraction(
                        component="AIChatPanel",
                        event="submit",
                        api_route="POST /api/v1/ai/chat",
                        logic="发送消息时展示 Loading，成功追加回复，失败显示可重试错误",
                    ),
                    ArchitectComponentInteraction(
                        component="AIUploadPanel",
                        event="submit",
                        api_route="POST /api/v1/ai/analyze",
                        logic="上传文件后调用分析接口，展示结构化结果与错误状态",
                    ),
                ]
            )
        ui_logic = [
            {
                "component": "SubmitButton",
                "event": "click",
                "logic": "前端校验 -> POST 创建 -> 成功后刷新列表",
            }
        ]
        data_model = [
            {"name": "RecordsPage", "fields": ["items", "page", "total", "loading"]}
        ]
        if ai_enabled:
            data_model.append(
                {
                    "name": "AIWorkspace",
                    "fields": ["messages", "analysis", "loading", "error"],
                }
            )
        markdown = f"""# 全栈系统实施契约

## 1. 项目目标

**{context.project_name}** 需要前端、后端与持久化数据库协同工作。

## 2. 技术栈

- 前端：React 18 + Vite
- 后端：`{backend}`
- 数据库：`{database}`
- ORM：Sequelize（生产通过 `DATABASE_URL` 连接；沙盒预览自动回退 SQLite）
- 通信：RESTful JSON API

> 数据库选型说明：`sqlite` 只用于本地测试与沙盒预览；生产部署必须使用
> `mysql` 或 `postgresql`，并通过环境变量 `DATABASE_URL` 注入连接串。

## 3. 关键业务流程

{flows}

## 4. API 契约

### GET `/api/v1/{table}`

- 请求：`page`、`size`
- 响应：`{{ items: Record[], total: number }}`
- 语义：只返回当前租户数据

### POST `/api/v1/{table}`

- 请求头：`Idempotency-Key`
- 请求体：业务字段
- 成功：201 + 创建后的记录
- 失败：400 参数错误 / 409 重复提交 / 500 服务异常

## 5. 数据库 DDL

```sql
{ddl}
```

约束：

- 禁止物理外键，关联字段由业务代码保证一致性；
- 金额使用 `DECIMAL(18,2)`；
- 列表查询必须带租户条件与分页。

## 6. 组件交互

| 组件 | 事件 | API | 逻辑 |
|---|---|---|---|
| RecordList | mount | GET `/api/v1/{table}` | 加载并渲染列表 |
| SubmitButton | click | POST `/api/v1/{table}` | 防重复提交，成功后刷新 |

## 7. 实施路径

1. 建立数据库表与迁移；
2. 实现后端领域模型、校验与 REST API；
3. 实现前端 API client 与页面状态；
4. 联调错误码、幂等与分页；
5. 执行端到端验收。
"""
        if ai_enabled:
            markdown += (
                "\n## 8. AI 能力（AI-Native）\n\n"
                "- 后端引入官方 `openai` SDK，`OPENAI_API_KEY` 只从环境变量读取；\n"
                "- `POST /api/v1/ai/chat` 处理多轮对话，`POST /api/v1/ai/analyze` "
                "接收上传文件并返回结构化分析；\n"
                "- 缺少密钥时返回 503 `AI_NOT_CONFIGURED`，禁止伪造模型结果；\n"
                "- 前端提供对话区、文件上传、加载态与失败重试。\n"
            )
        return ContractDraft(
            contract_markdown=markdown,
            tech_stack={
                "appShape": "full_stack_system",
                "frontend": "React 18 + Vite",
                "backend": backend,
                "database": database,
                "databaseEngine": database,
                "orm": "sequelize",
                "databaseUrlEnv": "DATABASE_URL",
                "apiStyle": "RESTful JSON",
            },
            api_routes=api_routes,
            database_ddl=ddl,
            component_interactions=interactions,
            frontend_routes=[
                {
                    "path": "/",
                    "name": "总览",
                    "description": f"{context.project_name} 的核心业务首页",
                },
                {
                    "path": f"/{table}",
                    "name": "数据管理",
                    "description": "创建、查询与管理业务记录",
                },
            ],
            page_layout=[
                {
                    "region": "header",
                    "title": "顶部导航",
                    "description": "品牌、页面切换与账号入口",
                },
                {"region": "main", "title": "业务工作区", "description": flows},
            ],
            components=[
                {
                    "name": "RecordList",
                    "role": "list",
                    "section": "业务工作区",
                    "description": "分页展示记录，覆盖加载 / 空 / 错误状态",
                },
                {
                    "name": "SubmitForm",
                    "role": "form",
                    "section": "业务工作区",
                    "description": "创建记录，包含前端校验与防重复提交",
                },
                {
                    "name": "StatusSummary",
                    "role": "summary",
                    "section": "业务工作区",
                    "description": "展示数量、状态等关键指标",
                },
                *(
                    [
                        {
                            "name": "AIWorkspace",
                            "role": "ai",
                            "section": "业务工作区",
                            "description": "AI 对话与文件分析，含加载 / 错误 / 重试状态",
                        }
                    ]
                    if ai_enabled
                    else []
                ),
            ],
            ui_logic=ui_logic,
            frontend_data_model=data_model,
            script_spec={},
            implementation_path=[
                ArchitectImplementationPhase(
                    phase="database",
                    title="数据库",
                    tasks=["建表", "索引", "迁移脚本"],
                ),
                ArchitectImplementationPhase(
                    phase="backend",
                    title="后端 API",
                    tasks=["领域模型", "校验", "REST 接口", "幂等"],
                ),
                ArchitectImplementationPhase(
                    phase="frontend",
                    title="前端联调",
                    tasks=["API client", "页面状态", "错误提示", "验收"],
                ),
            ],
            assumptions=(
                ["AI 能力使用 OpenAI API；未配置 OPENAI_API_KEY 时返回 503"]
                if ai_enabled
                else []
            ),
            include_ai_capability=ai_enabled,
        )

    @staticmethod
    def _tech_stack(shape: str, slots: dict[str, Any]) -> dict[str, Any]:
        if shape == "lightweight_web":
            return {
                "appShape": shape,
                "frontend": "React 18 + Vite",
                "persistence": slots.get("ui.persistence") or "localStorage",
                "backend": None,
                "database": None,
            }
        if shape == "script_automation":
            return {
                "appShape": shape,
                "runtime": slots.get("script.runtime") or "python",
                "ui": None,
            }
        engine = normalize_database_engine(slots.get("database.engine")) or "mysql"
        return {
            "appShape": shape,
            "frontend": "React 18 + Vite",
            "backend": slots.get("backend.framework"),
            "database": engine,
            "databaseEngine": engine,
            "orm": "sequelize",
            "databaseUrlEnv": "DATABASE_URL",
            "apiStyle": "RESTful JSON",
        }

    @staticmethod
    def _business_flows(shape: str, slots: dict[str, Any]) -> list[dict[str, Any]]:
        if shape == "lightweight_web":
            return [{"name": "前端核心流程", "detail": slots.get("ui.primary_flow")}]
        if shape == "script_automation":
            return [
                {
                    "name": "脚本输入输出",
                    "detail": (
                        f"{slots.get('script.input')} -> "
                        f"{slots.get('script.logic')} -> {slots.get('script.output')}"
                    ),
                }
            ]
        return [{"name": "核心业务", "detail": slots.get("business.key_flows")}]

    @staticmethod
    def _entities(shape: str, slots: dict[str, Any]) -> list[dict[str, Any]]:
        if shape == "full_stack_system":
            return [{"name": slots.get("data.entities") or "业务记录"}]
        if shape == "lightweight_web":
            return [{"name": "appState", "storage": slots.get("ui.persistence")}]
        return []


# ------------------------------------------------------------
# DeepSeek
# ------------------------------------------------------------
class DeepSeekArchitectLLM(ArchitectLLM):
    """DeepSeek Chat 实现。"""

    def __init__(
        self,
        settings: Settings | None = None,
        client: ModelClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.deepseek_api_key:
            raise ArchitectLLMConfigurationError(
                "DEEPSEEK_API_KEY 未配置；请设置密钥或使用 llm_provider=mock"
            )
        # 应用层只依赖 ModelClient 端口，模型分级与 SDK 由适配器负责。
        self._client = client or DeepSeekClient(self.settings)

    def analyze(self, context: ArchitectContext) -> ArchitectAnalysis:
        system = (
            "你是平台级软件架构师，处于“架构师对话前置”模式。用户会先用自然"
            "语言描述目标，你必须主动从目标、用户群体与核心功能中判断应用形态，"
            "禁止把“全栈 / 轻量 / 脚本”做成三选一问题丢回给用户。\n\n"
            "【防脑补铁律 · 最高优先级】\n"
            "- 在需求不明确时，绝对禁止自行脑补具体业务组件。用户没提登录，"
            "绝对不能生成 LoginForm；没提简历，绝对不能生成 ResumeUploader；"
            "没提职位，绝对不能生成 JDdetail。\n"
            "- 用户只输入“工作台 / 仪表盘 / 管理后台 / 系统”等抽象词，且没有"
            "说明业务对象、核心操作或数据字段时，视为“空白骨架”需求：不得列出"
            "任何业务实体、API、表单或业务组件，只能确认 Header + 侧边栏 + "
            "空白主内容区。\n"
            "- business_flows / entities / slots 只能来自用户明确说出的名词与"
            "动作；不确定时宁可留空，禁止猜测。\n"
            "- **提问模板铁律（最高优先级）**：每次追问最多抛出 2 个具体"
            "问题；每个问题必须给出 A/B/C 三个可执行选项（single_choice，"
            "或带 3 个选项的 multi_choice），选项 label 以“A. / B. / C.”"
            "开头。禁止发散性、开放式、问卷式提问，禁止一次抛出整份需求问卷。\n"
            "- 每个问题必须指向一个可拍板的技术或业务分支；禁止问“你还想要"
            "什么”“请详细描述”这类无法直接执行的问题。\n\n"
            "【交互行为澄清 · 强制规则】\n"
            "- 需求澄清阶段不仅要问“有什么组件”，还必须主动询问关键组件的"
            "交互行为：点击提交 / 保存 / 删除 / 搜索 / 导出 / 登录之后，到底"
            "是写入数据库、跳转页面、弹出提示，还是只做本地状态切换？\n"
            "- 交互问题必须绑定到用户已明确提到的组件与动作；最多占用 2 个"
            "追问名额中的 1 个，用 type=single_choice 或 multi_choice 的 "
            "slot=ui.interactions 承载，并提供三个明确选项，不要为每个按钮"
            "单独提问。\n"
            "- 用户回答“只要展示 / 没有交互 / 先做静态页”时，必须在 slots 写入 "
            'ui.interactions="none"，并在 assumptions 中记录“静态展示，无点击'
            "行为”；后续契约只能生成静态占位符，禁止编造 onClick 与网络请求。\n"
            "- 用户没有说明交互且当前也不便追问时，不得自行补全业务交互；"
            "把 ui.interactions 写入 missing_slots，让契约以 is_placeholder=true "
            "的静态组件交付。\n\n"
            "应用形态只有三种：\n"
            "- lightweight_web：纯前端网页工具，使用 LocalStorage / IndexedDB "
            "或 Serverless；不需要独立后端与数据库；\n"
            "- script_automation：无 UI 的 Python / Node 自动化脚本，输入 -> "
            "处理 -> 输出；不需要前端框架；\n"
            "- full_stack_system：前端 + 后端 + 数据库，需要持久化/多端协作。\n\n"
            "动态槽位规则（必须严格遵守）：\n"
            "- lightweight_web：只要求 ui.primary_flow；绝对不要追问 backend."
            "framework 或 database.engine；\n"
            "- script_automation：只要求 script.runtime、script.input、script."
            "output、script.logic；绝对不要追问前端框架；\n"
            "- full_stack_system：要求 backend.framework、database.engine、"
            "business.key_flows、data.entities；\n"
            "- full_stack_system 的 database.engine 必须区分使用场景：sqlite "
            "仅供本地测试；用户要部署上线时必须选 mysql 或 postgresql。"
            "用户没有明确指定时默认 mysql，禁止默认 sqlite；\n"
            "- 优先根据用户目标推断形态：出现团队 / 协作 / 登录 / 权限 / "
            "数据库 / 持久化等信号时按 full_stack_system 处理；明确“纯前端 / "
            "小工具 / 浏览器存储”时按 lightweight_web；明确“脚本 / 定时 / "
            "命令行”时按 script_automation；\n"
            "- 只有在目标完全无法判断时 app_shape 才为 null，且不得让用户做"
            "形态选择题；应询问用户群体与核心功能，例如："
            f"“{_shape_question()}”\n\n"
            "项目摘要规则（最高优先级）：\n"
            "- 输入中的 Project Summary 是已经确认的长期上下文；其中的技术栈、"
            "架构、实体与约束必须直接视为已确认，禁止重复询问；\n"
            "- latestChanges 描述本次快照实际改了什么。你的问题必须结合本次"
            "改动，确认“这些改动对应的全栈需求”（接口 / 数据 / 鉴权等），"
            "而不是重新做一遍技术选型问卷；\n"
            "- 如果摘要与用户最新回答冲突，以用户最新回答为准，并更新 slots。\n\n"
            "槽位填充与完成度规则（最高优先级）：\n"
            "- 用户用自然语言给出技术栈、数据库、业务动作或实体名时，必须"
            "写入对应 slots；不要因为字段不完整就拒绝或重复追问；\n"
            "- data.entities：用户列出实体名（如“订单和商品表”）即视为已填，"
            "缺失字段写入 assumptions 并使用合理默认值（id、createdAt 等）；\n"
            "- business.key_flows：用户描述“点击 -> 扣库存 -> 跳转”等动作即"
            "视为已填，不要要求用户再写成正式流程文档；\n"
            "- auth.required / api.style 是可选槽位：用户明确“不需要登录”时"
            "写入 auth.required=no，之后不得再作为缺失项或追问理由；\n"
            "- 不要重复追问已出现在 slots 或 ui_history 中的槽位；每轮问题必须"
            "优先覆盖仍缺失的必需槽位，最多 2 个；\n"
            "- 当所有必需槽位都有合理值或可采用默认值时，confidence_score 必须"
            "不低于 0.86；不要为了追求完美细节而压低分数；\n"
            "- 可采用合理默认值（Spring Boot / MySQL / RESTful 等），但必须写入"
            " assumptions，禁止编造用户未提供的业务实体。\n\n"
            "只输出一个 JSON 对象，不要 Markdown 围栏。格式：\n"
            '{"app_shape":null或"lightweight_web|script_automation|'
            'full_stack_system","confidence_score":0.0到1.0,'
            '"missing_slots":["槽位名"],"slots":{"槽位名":"值"},'
            '"questions":[{"question_id":"q1","slot":"槽位名",'
            '"type":"single_choice|multi_choice|free_text|entity_fields|'
            'schema_fields","prompt":"...","required":true,'
            '"options":[{"value":"...","label":"..."}],'
            '"default_value":null}],"tech_stack":{},'
            '"business_flows":[],"entities":[],"assumptions":[],'
            '"reasoning":"..."}\n\n'
            "问题最多 2 个，每个问题必须带 A/B/C 三个选项；不得重复已问过"
            "且已回答的槽位；已确认的槽位必须写入 slots。不要把“怕不确定”"
            "当作全栈的理由。"
        )
        system += _changelog_block(context)
        human = _analysis_human(context)

        def parse(raw: str) -> ArchitectAnalysis:
            # 先做兼容性归一化，再交给 Pydantic 做严格校验；校验错误会
            # 原样回传模型，让它只修复不合法字段。
            payload = _normalize_analysis_payload(_extract_json(raw))
            return ArchitectAnalysis.model_validate(payload)

        return self._generate_with_repair(
            task="architect_analyze",
            system=system,
            human=human,
            json_schema=ArchitectAnalysis.model_json_schema(),
            parse=parse,
            label="分析",
        )

    def draft_contract(self, context: ArchitectContext) -> ContractDraft:
        system = (
            "你是软件交付架构师。根据已确认的应用形态生成“分级实施契约”，"
            "只输出该形态真正需要的章节，禁止过度设计。\n\n"
            "【质量分级铁律 · 最高优先级】契约确认前，平台必须向用户抛出"
            "不可跳过的单选问题，由用户决定本次生成的质量要求：\n"
            "请选择您对本次生成的质量要求：\n"
            "- A. L1 敏捷模式（极速生成，跳过严格校验，适合做 Demo）\n"
            "- B. L2 标准模式（带 AST 代码校验与基础数据库，适合内部工具）\n"
            "- C. L3 生产级（全量一致性校验与真实数据库迁移验证，适合上线交付）\n"
            "该选择由平台 UI 收集并写入契约 quality_tier，你绝对不能替用户"
            "猜测或在契约 JSON 中输出该字段；但契约内容必须与用户选择的质量"
            "等级相称：L1 允许最小可用结构，L3 必须给出完整 API、DDL、关系与"
            "可验证验收标准。\n\n"
            "只输出一个 JSON 对象，不要 Markdown 围栏。格式：\n"
            '{"contract_markdown":"完整 Markdown 契约","tech_stack":{},'
            '"api_routes":[{"method":"GET","path":"/api/v1/...",'
            '"request":"","response":"","description":"","auth_required":true}],'
            '"database_ddl":"SQL 字符串","relations":[{"parent_table":"users",'
            '"child_table":"resumes","foreign_key":"user_id",'
            '"type":"one_to_many","alias":""}],"component_interactions":'
            '[{"component":"","event":"","api_route":"","logic":""}],'
            '"frontend_routes":[{"path":"/","name":"页面名","description":""}],'
            '"page_layout":[{"region":"header|main|sidebar","title":"",'
            '"description":""}],"components":[{"name":"","role":"",'
            '"section":"","description":"","is_placeholder":false}],'
            '"ui_logic":[{}],"frontend_data_model":[{}],"script_spec":{},'
            '"implementation_path":[{"phase":"","title":"","tasks":[]}],'
            '"assumptions":[],"include_ai_capability":false,'
            '"skeleton_mode":false}\n\n'
            "【防脑补铁律 · 最高优先级】\n"
            "- components 只能来自历史对话或 slots 中用户明确说出的业务名词与"
            "动作；禁止自行补出 LoginForm、ResumeUploader、JDdetail、数据表格等"
            "用户没有要求的组件或表单；\n"
            "- 如果当前需求只是“工作台 / 仪表盘 / 后台 / 系统”等抽象目标，"
            "skeleton_mode 必须为 true：page_layout 只给 header + sidebar + "
            'main，components 只给一个 {"name":"待定区域",'
            '"role":"placeholder","is_placeholder":true}，禁止生成任何业务卡片；\n'
            "- 某个区域的需求还没聊透时，只生成一个占位组件："
            '{"name":"待定区域","role":"placeholder","section":"主内容区",'
            '"description":"需求未明确，等待补充","is_placeholder":true}，'
            "禁止猜测里面该放什么表单；\n\n"
            "【组件交互契约 · 强制规则】\n"
            "- component_interactions 只能描述用户在澄清阶段明确确认过的交互；"
            "每一条必须写清 component / event / api_route / logic 四要素：\n"
            "  · event：触发的用户动作（如 click、submit、change）；\n"
            "  · api_route：必须与 api_routes 中某条 path 逐字符一致；纯前端"
            "本地交互（弹窗、切换 Tab、本地校验）留空字符串；\n"
            "  · logic：请求前 Loading、成功后的数据刷新 / 跳转 / 提示、失败后"
            "的错误展示与重试，三者缺一不可。\n"
            "- 【主从表关联铁律】只要一次需求包含“主表 + 明细 / 子记录”"
            "（如 Users 对 Resumes、Orders 对 OrderItems），必须：\n"
            "  · DDL 同时创建父表与子表，子表包含 `<父表单数>_id` 外键列"
            "（禁止物理 FOREIGN KEY 约束）；\n"
            "  · relations 显式声明 {parent_table, child_table, foreign_key,"
            " type: one_to_many}，字段必须与 DDL 逐字一致；\n"
            "  · 子表 API 路由挂在父资源下（如 GET/POST "
            "/api/v1/users/{user_id}/resumes），或至少提供父表详情关联查询；\n"
            "  · 不得把主从关系塞进一个 JSON 字段，也不得只生成父表 CRUD。\n"
            "- 【子资源契约铁律 · 强制显式声明】如果存在 "
            "`/users/{user_id}/profile`、`/orders/{order_id}/items` 这类子资源"
            "路由，必须在 databaseDdl 中显式创建对应子表（如 profiles / "
            "order_items）并在 relations 中声明外键；禁止依赖“代码里大概能"
            "推断出外键”的隐式做法，禁止让 Coder 猜表名或外键列。若子资源"
            "没有独立表，就改成父资源详情查询，不得生成悬空嵌套路由。\n"
            "- 如果用户明确表示“只展示 / 没有交互”，或某区域仍是待定占位："
            "该组件必须 is_placeholder=true，且不得出现在 component_interactions "
            "中；前端 Coder 会据此生成静态占位符，禁止伪造 onClick。\n"
            "- 交互问题尚未确认时，宁可少写交互，也不得猜测“提交后保存到数据库”"
            "这类业务行为；把不确定项写入 assumptions。\n"
            "- 前端 Coder 会严格按 component_interactions 生成真实事件与网络"
            "请求，因此 api_route 写错会直接导致联调失败。\n\n"
            "分级规则：\n"
            "- lightweight_web：api_routes 必须为空、database_ddl 必须为空；"
            "契约只包含 UI 交互、状态管理、前端数据模型、验收标准；\n"
            "- script_automation：api_routes / database_ddl / component_"
            "interactions / ui_logic 必须为空；契约只包含脚本逻辑、依赖、"
            "运行环境、输入输出、错误码；\n"
            "- full_stack_system：必须给出 REST API、数据库 DDL（禁止物理"
            "外键）、组件交互、前后端联调；tech_stack.database 必须是 "
            "mysql / postgresql / sqlite 之一；选择 mysql/postgresql 时必须"
            "在契约中写明后端通过 Sequelize ORM 与环境变量 DATABASE_URL "
            "连接云数据库，禁止把 SQLite 描述为生产数据库；\n"
            "- 所有 Web 形态（lightweight_web / full_stack_system）都必须给出 "
            "frontend_routes、page_layout 与 components：前端工程师将直接据此"
            "生成 React 骨架，不能省略；脚本形态这三项为空；\n"
            "- 所有形态都必须给 implementation_path 与可验证验收标准；\n"
            "- 必须复用 Project Summary 中已确认的技术栈与架构决策，不要另起"
            "一套；latestChanges 涉及的前后端能力必须进入契约；\n"
            "- 【API 路径一致性铁律】full_stack_system 的 api_routes 必须为"
            "每个资源先声明一条静态集合路由（例如 GET/POST "
            "/api/v1/resumes），再声明详情路由（/api/v1/resumes/{resume_id}）；"
            "路径参数只能使用 {name} 形式，禁止使用冒号；除用户明确要求实时"
            "进度外，不要生成 /stream、/events 等 SSE 路由。前端数据面板只会"
            "请求集合根路径，后端也必须注册完全相同的路径，禁止一边写 "
            "/api/v1/tasks、另一边写 /api/v1/tasks/stream；\n"
            "- 【资源与动作分离铁律】必须区分实体资源（Resource）与动作"
            "端点（Action）。注册 / 登录 / 登出 / 刷新令牌 / 重置密码都是"
            "users 表上的动作，不是独立资源：集合根只能是 /api/v1/users，"
            "动作写成 /api/v1/users/login、/api/v1/users/register、"
            "/api/v1/users/{user_id}/password 等；禁止把 /auth、/login、"
            "/session、/account 声明为静态集合根路径（Collection Root），"
            "也不得为 auth 单独建表。\n"
            "- 【集合根必须落地铁律】每条集合根路径必须在 databaseDdl 中找到"
            "同名（允许单数 / 复数差异）的真实表；找不到时，要么新增该业务"
            "表，要么把集合根改成已存在的真实表。禁止声明悬空集合，禁止让"
            "前端数据面板请求一个 DDL 中不存在的资源。\n"
            "- 不要编造用户未提供的业务实体；不确定处写入 assumptions。\n"
            "- 【AI-Native 判定】需求包含 AI 对话、内容生成、简历筛选/分析、"
            "图片理解等能力时，include_ai_capability 必须为 true，并在契约中"
            "明确 AI 输入、模型处理与输出结构；纯 CRUD / 展示型应用必须为 false。"
        )
        system += _changelog_block(context)
        human = _contract_human(context)
        return self._generate_with_repair(
            task="architect_contract",
            system=system,
            human=human,
            json_schema=None,
            parse=lambda raw: _parse_contract_draft(_extract_json(raw)),
            label="契约",
        )

    def revise_contract(
        self,
        context: ArchitectContext,
        previous: ContractDraft,
        feedback: str,
    ) -> ContractDraft:
        system = (
            "你是软件交付架构师。上一版实施契约未通过确定性校验。"
            "请严格根据校验反馈修复，不要扩写无关内容；仍然只输出契约 JSON。\n"
            "质量分级 quality_tier 由用户在契约确认时显式选择，你不得在 JSON "
            "中输出或修改；修订时保持与用户所选等级相称的契约完整度。\n"
            "必须遵守应用形态分级规则：lightweight_web 不含 API/DDL；"
            "script_automation 不含 UI/API/DDL；full_stack_system 必须包含 "
            "API/DDL/交互。\n"
            "防脑补铁律：不得新增用户未明确要求的业务组件或表单；需求未聊透"
            "的区域只保留一个 is_placeholder:true 的占位组件；如果原契约是抽象"
            "骨架，除非用户反馈中明确给出业务对象，否则继续只输出空白骨架。"
            "API 路径必须前后端逐字符一致：每个资源先有静态集合路由，再有"
            "详情路由；路径参数用 {name}，不要新增 /stream、/events 端点。"
            "必须区分资源与动作：注册 / 登录 / 登出 / 刷新令牌属于 users 表"
            "上的动作，禁止把 /auth、/login、/session、/account 作为集合根，"
            "也不得为 auth 单独建表；所有集合根都必须在 databaseDdl 中有"
            "同名（允许单复数差异）真实表。"
            "子资源契约必须显式声明：存在 /users/{user_id}/profile 这类嵌套"
            "路由时，databaseDdl 必须创建 profile 对应子表，relations 必须"
            "声明父表、子表与外键；禁止依赖隐式推断，没有独立子表就改成"
            "父资源详情查询。"
            "component_interactions 只能写用户已确认的交互；api_route 必须与 "
            "api_routes 中的 path 逐字符一致，纯前端交互留空；用户明确不要"
            "交互或区域待定时，组件必须 is_placeholder:true 且不写交互，"
            "禁止编造 onClick 与请求。include_ai_capability 必须与用户需求一致："
            "AI 对话 / 生成 / 筛选分析为 true，纯 CRUD 为 false。"
        )
        system += _changelog_block(context)
        human = (
            f"{_contract_human(context)}\n\n"
            f"上一版契约:\n{json.dumps(_draft_to_payload(previous), ensure_ascii=False, default=str)}\n\n"
            f"校验反馈:\n{feedback[:3000]}"
        )
        raw = self._invoke("architect_contract", system, human)
        return _parse_contract_draft(_extract_json(raw))

    def summarize_history(self, text: str, max_chars: int = 200) -> str:
        """用廉价模型把长对话压缩为核心需求与约束。"""
        if not text.strip():
            return ""
        system = (
            "你是对话压缩器。把给定对话压缩为 200 字以内的“核心需求与约束”，"
            "只保留：用户明确要求、已确认的技术约束、禁止事项、当前待办。"
            "丢弃寒暄、中间代码和重复解释。直接输出摘要，不要前缀。"
        )
        human = f"对话内容:\n{text[:12000]}"
        summary = self._invoke("summary", system, human).strip()
        if not summary:
            return fallback_summary(text, max_chars)
        if len(summary) > max_chars:
            summary = summary[:max_chars]
        return summary

    def answer_helper(
        self,
        context: ArchitectContext,
        question: str,
        history: list[dict[str, Any]] | None = None,
    ) -> str:
        """影子助手：只读问答，不参与主图状态推进。

        使用 fast 档模型即可；答案要求直接、简短，并明确区分“已确认”
        与“尚未确认”，禁止替主流程做决定。
        """
        system = (
            "你是 Vibe 平台的“影子架构助手（Helper Agent）”。你的唯一职责是"
            "解释当前架构方案、回答用户疑问、引用知识库片段。\n\n"
            "【只读铁律 · 最高优先级】\n"
            "- 你没有任何写入权限：不能修改 slots、契约、源码，也不能推进或"
            "结束主架构流程；\n"
            "- 用户让你“直接改成 Node.js / 直接开始生成”时，明确说明这必须"
            "在“架构确认”页签中操作，你只提供分析与建议；\n"
            "- 只依据提供的架构快照、项目摘要与知识库回答；信息不足时直接说"
            "“当前方案尚未确认这一点”，禁止编造技术选型、实体或接口；\n"
            "- 不要重复整份契约，不要输出 JSON，不要使用 Markdown 代码围栏。\n\n"
            "回答风格：先给结论，再给 1-3 条理由；总长度控制在 300 字以内；"
            "如果引用了知识库，在句末标注 [文档标题]。"
        )
        system += _changelog_block(context)
        human = _helper_human(context, question, history or [])
        answer = self._invoke("architect_helper", system, human).strip()
        if not answer:
            raise ArchitectLLMError("Helper Agent 未返回有效回答")
        return answer

    def _invoke(self, task: str, system: str, human: str) -> str:
        try:
            return self._client.generate(task, system, human)
        except ModelClientError as exc:
            raise ArchitectLLMError(f"DeepSeek Architect 调用失败: {exc}") from exc

    def _invoke_json(
        self,
        task: str,
        system: str,
        human: str,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        """调用强制 JSON 输出的模型；端口默认实现会退化为 generate。"""
        try:
            return self._client.generate_json(
                task,
                system,
                human,
                json_schema=json_schema,
            )
        except ModelClientError as exc:
            raise ArchitectLLMError(f"DeepSeek Architect 调用失败: {exc}") from exc

    def _generate_with_repair(
        self,
        *,
        task: str,
        system: str,
        human: str,
        json_schema: dict[str, Any] | None,
        parse: Callable[[str], Any],
        label: str,
    ) -> Any:
        """生成 JSON 并在校验失败时把错误回传模型重试。

        最多重试 ``ARCHITECT_JSON_RETRY_LIMIT`` 次（默认 2）。每次重试都
        携带上一轮原始输出与 Pydantic 错误摘要，要求模型只修复不合法字段，
        避免因单个字段类型漂移直接抛 500。
        """
        attempts = max(0, int(self.settings.architect_json_retry_limit))
        current_human = human
        last_error = "未知错误"
        for attempt in range(attempts + 1):
            raw = self._invoke_json(task, system, current_human, json_schema)
            try:
                return parse(raw)
            except (
                ValidationError,
                ArchitectLLMError,
                ValueError,
                TypeError,
                KeyError,
            ) as exc:
                last_error = str(exc)
                logger.warning(
                    "architect %s JSON 校验失败 attempt=%s/%s: %s",
                    label,
                    attempt + 1,
                    attempts + 1,
                    last_error,
                )
                if attempt >= attempts:
                    break
                current_human = (
                    f"{human}\n\n"
                    f"【上一次输出】\n{_truncate_text(raw, 6000)}\n\n"
                    f"【校验错误】\n{_truncate_text(last_error, 2000)}\n\n"
                    "请只输出修正后的完整 JSON 对象：保留已经正确的字段，"
                    "修复校验错误指出的字段，不要输出 Markdown 围栏或解释。"
                )
        raise ArchitectLLMError(
            f"Architect {label} JSON 连续 {attempts + 1} 次不合法: {last_error}"
        )


def build_architect_llm(
    provider: str = "auto",
    settings: Settings | None = None,
) -> ArchitectLLM:
    resolved = settings or get_settings()
    name = (provider or "auto").strip().lower()
    if name == "mock":
        return MockArchitectLLM()
    if name == "deepseek":
        return DeepSeekArchitectLLM(resolved)
    if name == "auto":
        if resolved.deepseek_api_key:
            return DeepSeekArchitectLLM(resolved)
        return MockArchitectLLM()
    raise ArchitectLLMConfigurationError(f"未知 llm_provider: {provider}")


# ------------------------------------------------------------
# Prompt / JSON 工具
# ------------------------------------------------------------
def _shape_question() -> str:
    from ai_core.architect.confidence import APP_SHAPE_PROMPT

    return APP_SHAPE_PROMPT


def _changelog_block(context: ArchitectContext) -> str:
    """把最近 Direct Ops 变更注入 System Prompt。

    只保留最近 10 条、总长 1200 字；文本做换行压缩与截断，避免用户
    输入内容演变成 Prompt 注入。该块是上下文，不是新需求。
    """
    entries = list(context.direct_changelog or [])[-10:]
    if not entries:
        return ""
    lines: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip()
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        component = str(target.get("component") or target.get("file") or "组件")
        summary = str(item.get("summary") or "").replace("\n", " ").strip()
        created = str(item.get("createdAt") or item.get("created_at") or "")
        time_label = created[11:16] if len(created) >= 16 else created[:16]
        line = f"- {time_label} {action} {component}：{summary}"
        lines.append(line[:240])
    if not lines:
        return ""
    body = "\n".join(lines)
    body = body[:1200]
    return (
        "\n\n## 用户最近直接操作（仅作上下文，不要当作新需求）\n"
        f"{body}\n"
        "如果与当前需求相关，请主动提及用户刚刚的直接修改；"
        "禁止把用户手改的文案 / 位置回滚或重复生成。"
    )


def _summary_block(context: ArchitectContext) -> str:
    """项目摘要与本次改动：已确认的技术选型必须复用，不得重复追问。"""
    summary = str(context.project_summary or "").strip() or "（暂无项目摘要）"
    latest = str(context.latest_changes or "").strip() or "（暂无快照改动说明）"
    return (
        "## 项目全局摘要（Project Summary，已确认的长期上下文）\n"
        f"{summary}\n\n"
        "## 本次快照改动（latestChanges）\n"
        f"{latest}\n"
    )


def _analysis_human(context: ArchitectContext) -> str:
    return (
        f"项目：{context.project_name}（{context.project_id}）\n"
        f"当前澄清轮次：{context.round}\n"
        f"已确认槽位：{json.dumps(context.slots, ensure_ascii=False, default=str)}\n"
        "用户历史对话（结构化回答与“补充说明”同等有效，已提供的信息不得"
        f"再次追问）：{json.dumps(context.ui_history[-12:], ensure_ascii=False, default=str)}\n\n"
        f"{_summary_block(context)}\n"
        f"RAG 规范片段：\n{_format_chunks(context.retrieved_chunks)}\n\n"
        f"当前源码：\n{format_source(context.source_files, 42000)}"
    )


def _helper_human(
    context: ArchitectContext,
    question: str,
    history: list[dict[str, Any]],
) -> str:
    """影子助手的 Human Prompt：只读快照 + 知识库 + 最近问答。"""
    snapshot = str(context.architecture_snapshot or "").strip() or (
        f"应用形态：{context.app_shape or '尚未确定'}\n"
        f"已确认槽位：{json.dumps(context.slots, ensure_ascii=False, default=str)}"
    )
    recent = history[-8:]
    history_text = (
        "\n".join(
            f"{'用户' if item.get('role') == 'user' else '助手'}："
            f"{str(item.get('content') or '')[:600]}"
            for item in recent
        )
        or "（无）"
    )
    return (
        f"项目：{context.project_name}（{context.project_id}）\n\n"
        f"## 当前架构快照（只读）\n{snapshot}\n\n"
        f"## 项目摘要\n{str(context.project_summary or '（无）')[:4000]}\n\n"
        f"## 本次改动\n{str(context.latest_changes or '（无）')[:2000]}\n\n"
        f"## 知识库片段\n{_format_chunks(context.retrieved_chunks)}\n\n"
        f"## 最近问答\n{history_text}\n\n"
        f"## 用户问题\n{question.strip()}"
    )


def _contract_human(context: ArchitectContext) -> str:
    return (
        f"项目：{context.project_name}（{context.project_id}）\n"
        f"应用形态：{context.app_shape}\n"
        f"置信度阈值：{context.threshold}\n"
        f"已确认槽位：{json.dumps(context.slots, ensure_ascii=False, default=str)}\n"
        f"历史对话：{json.dumps(context.ui_history[-12:], ensure_ascii=False, default=str)}\n\n"
        f"{_summary_block(context)}\n"
        f"RAG 规范片段：\n{_format_chunks(context.retrieved_chunks)}\n\n"
        f"当前源码：\n{format_source(context.source_files, 36000)}"
    )


def _format_chunks(chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return "（无）"
    parts = []
    for chunk in chunks[:5]:
        parts.append(
            f"[{chunk.get('docId')}] {chunk.get('title')} / "
            f"{chunk.get('section')}\n{chunk.get('content', '')}"
        )
    return "\n\n".join(parts)


def _truncate_text(text: str, limit: int) -> str:
    """截断错误反馈，避免重试 Prompt 自己把上下文撑爆。"""
    value = str(text or "")
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n...[已截断，共 {len(value)} 字符]"


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ArchitectLLMError("模型未返回 JSON 对象")
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ArchitectLLMError(f"模型 JSON 解析失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArchitectLLMError("模型 JSON 顶层必须是对象")
    return payload


def _normalize_analysis_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把模型常见的 camelCase / 非严格字段统一成 Pydantic 输入。"""
    normalized = dict(payload)
    shape = normalized.pop("appShape", None) or normalized.get("app_shape")
    if shape not in {
        "lightweight_web",
        "script_automation",
        "full_stack_system",
    }:
        shape = None
    normalized["app_shape"] = shape

    missing = normalized.pop("missingSlots", normalized.get("missing_slots"))
    normalized["missing_slots"] = [
        str(item) for item in missing or [] if str(item).strip()
    ][:12]

    slots = normalized.get("slots")
    normalized["slots"] = slots if isinstance(slots, dict) else {}

    questions = normalized.get("questions")
    if not isinstance(questions, list):
        normalized["questions"] = []
    else:
        normalized["questions"] = [
            item
            for item in questions[:2]
            if isinstance(item, dict) and item.get("prompt")
        ]

    raw_confidence = normalized.pop(
        "confidenceScore", normalized.get("confidence_score", 0.0)
    )
    try:
        normalized["confidence_score"] = max(0.0, min(1.0, float(raw_confidence)))
    except (TypeError, ValueError):
        normalized["confidence_score"] = 0.0

    for key in ("tech_stack", "business_flows", "entities", "assumptions"):
        camel = _to_camel(key)
        value = normalized.pop(camel, normalized.get(key))
        if key == "tech_stack":
            normalized[key] = value if isinstance(value, dict) else {}
        elif key == "assumptions":
            normalized[key] = [str(item) for item in value or [] if str(item).strip()][
                :12
            ]
        elif key in {"business_flows", "entities"}:
            # 保留 str / dict / 混合数组；字段级 field_validator 会做最终
            # 归一化。不能在这里把字符串直接丢掉，否则模型只给了字符串
            # 流程时会被静默变成空数组。
            normalized[key] = value if isinstance(value, (list, dict, str)) else []
        else:
            normalized[key] = value if isinstance(value, list) else []

    normalized.setdefault("reasoning", "")
    return normalized


def _parse_contract_draft(payload: dict[str, Any]) -> ContractDraft:
    markdown = str(
        payload.get("contract_markdown") or payload.get("contractMarkdown") or ""
    ).strip()
    if not markdown:
        raise ArchitectLLMError("契约缺少 contract_markdown")

    raw_routes = payload.get("api_routes") or payload.get("apiRoutes") or []
    routes: list[ArchitectApiRoute] = []
    for item in raw_routes if isinstance(raw_routes, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            routes.append(ArchitectApiRoute.model_validate(item))
        except Exception:  # noqa: BLE001 - 单条非法路由直接跳过
            continue

    raw_relations = (
        payload.get("relations")
        or payload.get("table_relations")
        or payload.get("tableRelations")
        or []
    )
    relations: list[ArchitectTableRelation] = []
    for item in raw_relations if isinstance(raw_relations, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            relations.append(ArchitectTableRelation.model_validate(item))
        except Exception:  # noqa: BLE001 - 单条非法关联交给契约校验报告
            continue

    raw_interactions = (
        payload.get("component_interactions")
        or payload.get("componentInteractions")
        or []
    )
    interactions: list[ArchitectComponentInteraction] = []
    for item in raw_interactions if isinstance(raw_interactions, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            interactions.append(ArchitectComponentInteraction.model_validate(item))
        except Exception:  # noqa: BLE001
            continue

    raw_path = (
        payload.get("implementation_path") or payload.get("implementationPath") or []
    )
    phases: list[ArchitectImplementationPhase] = []
    for item in raw_path if isinstance(raw_path, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            phases.append(ArchitectImplementationPhase.model_validate(item))
        except Exception:  # noqa: BLE001
            continue

    return ContractDraft(
        contract_markdown=markdown,
        tech_stack=_as_dict(payload.get("tech_stack") or payload.get("techStack")),
        api_routes=routes,
        database_ddl=str(
            payload.get("database_ddl") or payload.get("databaseDdl") or ""
        ).strip(),
        relations=relations,
        component_interactions=interactions,
        frontend_routes=_as_list(
            payload.get("frontend_routes") or payload.get("frontendRoutes")
        ),
        page_layout=_as_list(payload.get("page_layout") or payload.get("pageLayout")),
        components=_as_list(payload.get("components")),
        ui_logic=_as_list(payload.get("ui_logic") or payload.get("uiLogic")),
        frontend_data_model=_as_list(
            payload.get("frontend_data_model") or payload.get("frontendDataModel")
        ),
        script_spec=_as_dict(payload.get("script_spec") or payload.get("scriptSpec")),
        implementation_path=phases,
        assumptions=[
            str(item)
            for item in (
                payload.get("assumptions")
                if isinstance(payload.get("assumptions"), list)
                else []
            )
            if str(item).strip()
        ][:12],
        include_ai_capability=bool(
            payload.get("include_ai_capability")
            if "include_ai_capability" in payload
            else payload.get("includeAiCapability")
        ),
        skeleton_mode=bool(payload.get("skeleton_mode") or payload.get("skeletonMode")),
    )


def _draft_to_payload(draft: ContractDraft) -> dict[str, Any]:
    return {
        "contractMarkdown": draft.contract_markdown,
        "techStack": draft.tech_stack,
        "apiRoutes": [item.model_dump(by_alias=True) for item in draft.api_routes],
        "databaseDdl": draft.database_ddl,
        "relations": [item.model_dump(by_alias=True) for item in draft.relations],
        "componentInteractions": [
            item.model_dump(by_alias=True) for item in draft.component_interactions
        ],
        "frontendRoutes": draft.frontend_routes,
        "pageLayout": draft.page_layout,
        "components": draft.components,
        "uiLogic": draft.ui_logic,
        "frontendDataModel": draft.frontend_data_model,
        "scriptSpec": draft.script_spec,
        "implementationPath": [
            item.model_dump(by_alias=True) for item in draft.implementation_path
        ],
        "assumptions": draft.assumptions,
        "includeAiCapability": draft.include_ai_capability,
        "skeletonMode": draft.skeleton_mode,
    }


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


_AI_INTENT = re.compile(
    r"(\bAI\b|人工智能|大模型|LLM|OpenAI|智能(?:筛选|分析|生成|问答)|"
    r"简历(?:筛选|分析)|内容生成|对话(?:机器人|助手)|图片理解|OCR)",
    re.IGNORECASE,
)


def _wants_ai_capability(context: ArchitectContext) -> bool:
    """确定性判断需求是否需要真实 AI 能力。"""
    if context.app_shape != "full_stack_system" and (
        context.slots.get("app.shape") != "full_stack_system"
    ):
        return False
    parts = [
        context.project_name,
        json.dumps(context.slots, ensure_ascii=False, default=str),
        context.project_summary,
        context.latest_changes,
        "\n".join(
            str(item.get("content") or "")
            for item in context.ui_history
            if isinstance(item, dict)
        ),
    ]
    return bool(_AI_INTENT.search("\n".join(parts)))


def _to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _table_name(raw: str) -> str:
    """从实体描述中提取稳定的表名，避免把中文整段拼进 SQL。"""
    candidates = re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,31}", raw or "")
    stop = {"the", "and", "with", "table", "entity", "field", "fields"}
    for candidate in candidates:
        lowered = candidate.lower()
        if lowered not in stop:
            return _snake(lowered)
    return "app_records"


def _snake(value: str) -> str:
    value = re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()
    value = re.sub(r"[^a-z0-9_]", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "app_records"


def _mock_ddl(table: str, engine: str = "mysql") -> str:
    """按目标数据库生成对应方言的 Mock DDL。

    sqlite 仅用于本地测试；mysql / postgresql 是生产部署的推荐选项。
    字段集合保持一致，Coder 只需理解列名与类型亲和性，不依赖方言细节。
    """
    if engine == "postgresql":
        return f"""CREATE TABLE {table} (
  id BIGSERIAL PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  name VARCHAR(255) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
  payload JSONB NULL,
  created_at TIMESTAMP(3) NOT NULL,
  updated_at TIMESTAMP(3) NOT NULL,
  deleted_at TIMESTAMP(3) NULL
);
CREATE INDEX idx_{table}_tenant_updated ON {table} (tenant_id, updated_at);
CREATE INDEX idx_{table}_tenant_status ON {table} (tenant_id, status);"""
    if engine == "sqlite":
        return f"""CREATE TABLE {table} (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  payload TEXT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT NULL
);"""
    return f"""CREATE TABLE {table} (
  id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  tenant_id VARCHAR(64) NOT NULL,
  name VARCHAR(255) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
  payload JSON NULL,
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  deleted_at DATETIME(3) NULL,
  KEY idx_{table}_tenant_updated (tenant_id, updated_at),
  KEY idx_{table}_tenant_status (tenant_id, status)
);"""
