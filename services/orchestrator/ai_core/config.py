"""AI Core 配置。

所有配置只从环境变量 / .env 读取，禁止在代码中硬编码宿主机绝对路径。
"""

from __future__ import annotations

from functools import lru_cache

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

    # 单次响应返回的最大日志字符数，避免把超长构建日志塞进 HTTP 响应。
    sandbox_log_max_chars: int = 60_000

    # 单文件 / 单次请求的文件体积上限，防止误传大文件。
    sandbox_max_file_bytes: int = 1_048_576
    sandbox_max_total_bytes: int = 5_242_880

    # 项目导出包上限（已排除 node_modules / .git / dist，正常源码项目远小于该值）。
    sandbox_max_export_bytes: int = 33_554_432

    # ------------------------------------------------------------
    # Agent
    # ------------------------------------------------------------
    # LangGraph 单次执行的最大步数；3 次修复 + 7 个节点远低于该值。
    agent_graph_recursion_limit: int = 60

    # HITL：Planner 置信度低于该阈值时触发澄清；请求可通过
    # options.confidenceThreshold 覆盖。
    agent_clarification_threshold: float = 0.7
    # 澄清最长等待时间；回答接口超时返回 CLARIFICATION_EXPIRED。
    agent_clarification_ttl_hours: int = 24
    # Diff 审批最长等待时间；超时决策返回 APPROVAL_EXPIRED 并回滚沙盒。
    agent_approval_ttl_hours: int = 24
    # 每个 Run 的内存事件缓冲上限；生产切换 Redis Stream 后由 stream TTL 管理。
    agent_event_buffer_size: int = 2000

    # ------------------------------------------------------------
    # Architect（M2.5 全栈架构澄清与 Path 生成）
    # ------------------------------------------------------------
    architect_confidence_threshold: float = 0.85
    architect_max_clarification_rounds: int = 5
    architect_contract_repair_limit: int = 1
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
