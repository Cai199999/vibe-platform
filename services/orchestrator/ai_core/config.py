"""AI Core 配置。

所有配置只从环境变量 / .env 读取，禁止在代码中硬编码宿主机绝对路径。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """服务配置。

    命名规则：环境变量使用大写形式，例如 ``SANDBOX_IMAGE`` 对应 ``sandbox_image``。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    service_name: str = "python-ai"
    service_version: str = "0.1.0"

    # M1 只做占位；M2 由 ModelRouter 统一读取。
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # ------------------------------------------------------------
    # 模型分级路由（ModelRouter）
    # ------------------------------------------------------------
    # 解析优先级：任务级覆盖 > 层级模型 > deepseek_model。
    # 留空表示回退到上一级，避免强制配置。
    model_fast_name: str = ""
    model_strong_name: str = ""
    model_fast_temperature: float = 0.0
    model_strong_temperature: float = 0.2
    # 任务级覆盖：只配置需要偏离层级的节点。
    model_routing_name: str = ""
    model_validation_name: str = ""
    model_summary_name: str = ""
    # 用户消息意图分级（高优：需求变更 / 约束；低优：闲聊 / 重复）。
    model_intent_name: str = ""
    model_planner_name: str = ""
    model_coder_name: str = ""
    model_architect_analyze_name: str = ""
    model_architect_contract_name: str = ""
    model_architect_helper_name: str = ""

    # 复用宿主机 Docker Redis；当前骨架不主动连接。
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379

    # ------------------------------------------------------------
    # Sandbox（LocalDockerSandbox）
    # ------------------------------------------------------------
    # python-ai 容器内通过挂载的 /var/run/docker.sock 访问宿主 Docker。
    docker_host: str = "unix:///var/run/docker.sock"
    docker_api_timeout_seconds: int = 600

    # 预构建的 React + Vite 沙盒镜像。首次使用且镜像不存在时，会基于
    # sandbox_template 目录自动构建。
    sandbox_image: str = "vibe-platform/sandbox-react:dev"
    sandbox_template_dir: str = "/app/sandbox_template"
    sandbox_workdir: str = "/app"
    sandbox_dev_port: int = 5173

    # 浏览器访问预览端口时使用的主机名。M1 为单机开发，默认 127.0.0.1。
    sandbox_preview_host: str = "127.0.0.1"

    sandbox_memory_mb: int = 768
    sandbox_cpu_limit: float = 1.0
    sandbox_pids_limit: int = 256
    sandbox_start_timeout_seconds: int = 90
    sandbox_verify_timeout_seconds: int = 180
    # 导入项目需要先执行 npm install；首次安装依赖远慢于启动预装模板。
    sandbox_import_timeout_seconds: int = 300

    # 单次响应返回的最大日志字符数，避免把超长构建日志塞进 HTTP 响应。
    sandbox_log_max_chars: int = 60_000

    # 单文件 / 单次请求的文件体积上限，防止误传大文件。
    sandbox_max_file_bytes: int = 1_048_576
    sandbox_max_total_bytes: int = 5_242_880

    # 项目导出包上限（已排除 node_modules / .git / dist，正常源码项目远小于该值）。
    sandbox_max_export_bytes: int = 33_554_432

    # ------------------------------------------------------------
    # 项目导入（Zip / 图片）
    # ------------------------------------------------------------
    # 上传包 / 图片硬上限；在 FastAPI 读完请求体后立刻校验，避免 zip 炸弹。
    import_max_archive_bytes: int = 33_554_432
    import_max_image_bytes: int = 12_582_912
    import_max_uncompressed_bytes: int = 134_217_728
    import_max_entries: int = 4_000
    # 返回给前端的文本源码预算；超出部分仍留在沙盒中，只是不进入浏览器状态。
    import_max_text_files: int = 400
    import_max_text_bytes: int = 8_388_608

    # 图片导入视觉模型。协议固定为 OpenAI Chat Completions 兼容的
    # image_url 消息；没有配置密钥时使用确定性 Mock，保证离线演示可用。
    vision_api_key: str = ""
    vision_base_url: str = "https://api.openai.com/v1"
    vision_model: str = "gpt-4o"
    vision_timeout_seconds: int = 90
    vision_max_image_side: int = 1600

    # ------------------------------------------------------------
    # Agent
    # ------------------------------------------------------------
    # LangGraph 单次执行的最大步数；3 次修复 + 7 个节点远低于该值。
    agent_graph_recursion_limit: int = 60

    # Supervisor：企业 CustomAgent Manifest 目录。
    # 只加载 JSON 声明，不执行代码；目录不存在时忽略。
    supervisor_custom_agents_dir: str = "config/custom_agents"
    # 内置 Agent Manifest 根目录；留空则使用 ai_core/ 下各 Agent 目录。
    # 仅允许指向声明文件，部署时应指向只读路径。
    supervisor_agent_manifest_dir: str = ""

    # HITL：Planner 置信度低于该阈值时触发澄清；请求可通过
    # options.confidenceThreshold 覆盖。
    agent_clarification_threshold: float = 0.7
    # 澄清最长等待时间；回答接口超时返回 CLARIFICATION_EXPIRED。
    agent_clarification_ttl_hours: int = 24
    # Diff 审批最长等待时间；超时决策返回 APPROVAL_EXPIRED 并回滚沙盒。
    agent_approval_ttl_hours: int = 24
    # Reflection：同一实现偏好累计多少次成功修改后，才作为默认值注入 Prompt。
    agent_preference_min_evidence: int = 2
    # Planner / Coder 结构化输出校验失败后的自修复重试次数。
    # 每次重试都会把上一次原始输出与校验错误回传模型，要求只修复不合法字段；
    # 0 表示关闭（首次失败直接走节点级重试 / 降级）。
    agent_json_retry_limit: int = 2
    # 每个 Run 的内存事件缓冲上限；生产切换 Redis Stream 后由 stream TTL 管理。
    agent_event_buffer_size: int = 2000

    # ------------------------------------------------------------
    # Token 成本优化
    # ------------------------------------------------------------
    # 对话历史超过阈值才压缩；摘要长度硬上限，防止压缩本身变成新负担。
    history_summary_token_threshold: int = 3000
    history_summary_max_chars: int = 200
    # Token 费用核算：价格单位「货币金额 / 100 万 tokens」。
    # 默认使用 DeepSeek 目录价，可在部署环境按实际账单覆盖，例如：
    # LLM_PRICING={"deepseek-chat":{"input":2,"cached_input":0.5,"output":8}}
    llm_pricing: dict[str, dict[str, float]] = {}
    llm_cost_currency: str = "CNY"
    # 分层压缩：旧消息摘要，最近 N 轮原文保留。1 轮 = user + assistant。
    history_recent_turns: int = 10
    # 每 N 轮生成一次局部摘要，写入 local_summary.md。
    history_local_summary_interval: int = 10
    history_local_summary_max_chars: int = 600
    # tiktoken 编码名；模型不可用时自动回退到字符密度估算。
    context_token_encoding: str = "cl100k_base"
    # 项目上下文记忆目录（容器内 /app/data/project_memory）。
    context_memory_dir: str = "data/project_memory"
    # 工具完整结果落盘；Prompt 只保留预览 + sha256。
    tool_output_preview_chars: int = 2000
    # 全局摘要 / 用户画像长度上限。
    context_global_summary_max_chars: int = 1200
    context_user_profile_max_chars: int = 600
    # 构建失败时回传给模型的代码上下文行数（报错行前后各 N 行）。
    error_context_lines: int = 10
    error_context_max_chars: int = 2000

    # 声明式 Skill 目录：*.skill.json，由 AgentRegistry 加载，并在 UI Agent
    # 运行时把 systemPrompt 追加进模型上下文。
    skills_dir: str = "config/skills"

    # 示例 Shell Tool：默认关闭。开启后也只允许只读命令、限定工作目录，
    # 绝不把宿主 AI Core 的任意命令执行能力直接暴露给模型。
    tool_shell_enabled: bool = False
    tool_shell_workdir: str = "/tmp/vibe-toolbox"
    tool_shell_timeout_seconds: int = 10
    tool_shell_max_output_chars: int = 20_000

    # DuckDuckGo 搜索 Tool：默认开启；端点 / 超时 / 结果数均由服务端控制。
    tool_web_search_enabled: bool = True
    tool_web_search_endpoint: str = "https://html.duckduckgo.com/html/"
    tool_web_search_fallback_endpoint: str = "https://lite.duckduckgo.com/lite/"
    tool_web_search_timeout_seconds: float = 8.0
    tool_web_search_max_results: int = 5
    tool_web_search_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    # ------------------------------------------------------------
    # MCP（Model Context Protocol）
    # ------------------------------------------------------------
    # 标准 MCP stdio Server 列表。每项：
    #   {"name":"vibe_demo","transport":"stdio","command":"python",
    #    "args":["scripts/mcp_server_demo.py"],"enabled":true,
    #    "allowed_tools":["get_current_time"],"timeout_seconds":10}
    # 只允许服务端配置，绝不接受浏览器传入。
    mcp_enabled: bool = True
    mcp_call_timeout_seconds: float = 10.0
    mcp_servers: list[dict[str, Any]] = []

    # ConsistencyVerifier 的真实数据库结构验收：启动内置 SQLite MCP，
    # 把生成 DDL 执行到临时库并查询 sqlite_master / PRAGMA table_info。
    schema_mcp_enabled: bool = True
    # MCP 进程的 Python 解释器；留空使用当前 AI Core 的解释器。
    schema_mcp_python: str = ""
    # MCP Server 脚本；留空使用 ai_core/mcp_servers/sqlite_mcp.py。
    schema_mcp_script: str = ""
    schema_mcp_timeout_seconds: float = 15.0
    # 默认严格：MCP 不可用 / DDL 不可执行都阻断构建。排障环境可设 false。
    schema_mcp_strict: bool = True

    # ------------------------------------------------------------
    # Architect（M2.5 全栈架构澄清与 Path 生成）
    # ------------------------------------------------------------
    architect_confidence_threshold: float = 0.85
    architect_max_clarification_rounds: int = 5
    architect_contract_repair_limit: int = 1
    # JSON 结构化输出：DeepSeek JSON Output + Pydantic 校验失败回传重试。
    architect_structured_output: bool = True
    architect_json_retry_limit: int = 2
    architect_graph_recursion_limit: int = 80
    architect_rag_provider: str = "markdown"
    architect_rag_top_k: int = 5
    architect_knowledge_dir: str = "knowledge/fullstack"
    # 源码上下文预算：防止超大项目把 Prompt 撑爆。
    architect_source_max_bytes: int = 200_000
    architect_source_max_files: int = 80


@lru_cache
def get_settings() -> Settings:
    """进程内单例，避免每次请求重复解析环境变量。"""
    return Settings()
