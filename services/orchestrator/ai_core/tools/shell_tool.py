"""示例 Shell Tool：受控的只读命令执行器。

为什么不是 ``subprocess(shell=True)``：
  - AI Core 容器挂载了宿主 Docker Socket，任意命令等价于宿主 root；
  - 模型输出不可信，必须假设 Prompt 注入随时会发生。

因此这里采用三层约束：
  1. 禁止 shell，只用 ``shlex.split`` + 参数数组执行；
  2. 命令白名单，只允许只读 / 诊断类命令与有限的 Git 子命令；
  3. 工作目录必须位于配置的 sandbox 目录内，拒绝绝对路径与 ``..``。

该 Tool 默认关闭（TOOL_SHELL_ENABLED=false）；关闭时仍然注册到
LangGraph 工具箱，但调用会返回明确的禁用提示，便于演示与权限编排。
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from ai_core.config import Settings
from ai_core.tools.base import ToolDescriptor, ToolRegistry

logger = logging.getLogger(__name__)

_ALLOWED_COMMANDS = {
    "ls",
    "pwd",
    "cat",
    "head",
    "tail",
    "wc",
    "find",
    "grep",
    "git",
}
_GIT_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "branch",
}
# 明确拒绝任何可能改变系统状态的 Git 子命令，即使误加入白名单也不执行。
_GIT_WRITE_SUBCOMMANDS = {
    "add",
    "commit",
    "push",
    "reset",
    "checkout",
    "clean",
    "rm",
    "mv",
    "merge",
    "rebase",
}


class ShellToolError(ValueError):
    """命令不满足安全约束。"""


def _validate_command(command: str) -> list[str]:
    text = (command or "").strip()
    if not text:
        raise ShellToolError("command 不能为空")
    if len(text) > 2_000:
        raise ShellToolError("command 过长")

    # 先做显式拒绝，再解析。反引号 / $() / 管道 / 重定向在无 shell 模式下
    # 不会被解释，但直接拒绝可以避免调用方误以为它们有效。
    forbidden = ("\n", "\r", "\x00", "`", "$(", ";", "&&", "||", "|", ">", "<")
    if any(token in text for token in forbidden):
        raise ShellToolError("命令包含被禁止的 shell 控制符")

    try:
        parts = shlex.split(text)
    except ValueError as exc:
        raise ShellToolError(f"命令解析失败: {exc}") from exc
    if not parts:
        raise ShellToolError("command 不能为空")

    executable = Path(parts[0]).name
    if executable not in _ALLOWED_COMMANDS:
        raise ShellToolError(f"命令不在只读白名单: {executable}")

    args = parts[1:]
    if executable == "git":
        if not args:
            raise ShellToolError("git 必须指定只读子命令")
        subcommand = args[0].lower()
        if subcommand in _GIT_WRITE_SUBCOMMANDS or subcommand not in _GIT_SUBCOMMANDS:
            raise ShellToolError(f"git 子命令不允许: {subcommand}")

    for arg in args:
        if arg == ".." or arg.startswith("../") or "/../" in arg:
            raise ShellToolError("参数不能跳出工具工作目录")
        if os.path.isabs(arg):
            raise ShellToolError("参数不能是绝对路径")

    return [executable, *args]


def _resolve_workdir(settings: Settings) -> Path:
    root = Path(settings.tool_shell_workdir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def build_shell_tool(settings: Settings) -> BaseTool:
    """构造 LangChain Tool；闭包捕获 Settings，避免全局可变状态。"""

    @tool("shell_tool")
    def shell_tool(command: str) -> str:
        """在受控目录中执行只读系统命令（ls/cat/git status 等）。

        该工具默认禁用；只有服务端显式开启后才返回命令输出。
        """
        if not settings.tool_shell_enabled:
            return "shell_tool 已注册但被服务端禁用（TOOL_SHELL_ENABLED=false）"

        try:
            parts = _validate_command(command)
        except ShellToolError as exc:
            return f"命令被拒绝: {exc}"

        workdir = _resolve_workdir(settings)
        try:
            completed = subprocess.run(  # noqa: S603 - 无 shell + 白名单 + 参数数组
                parts,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=settings.tool_shell_timeout_seconds,
                check=False,
                env={
                    # 不继承任何业务密钥；只保留 PATH 与最小 locale。
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                },
            )
        except subprocess.TimeoutExpired:
            return f"命令超时（>{settings.tool_shell_timeout_seconds}s）: " f"{command}"
        except OSError as exc:
            return f"命令执行失败: {exc}"

        output = (completed.stdout or "") + (completed.stderr or "")
        limit = settings.tool_shell_max_output_chars
        if len(output) > limit:
            output = f"...[truncated]...\n{output[-limit:]}"
        return f"exit={completed.returncode}\n{output}".strip()

    return shell_tool


def register_shell_tool(registry: ToolRegistry, settings: Settings) -> None:
    """把示例 Tool 注册进 LangGraph 工具箱。"""
    registry.register(
        ToolDescriptor(
            name="shell_tool",
            description="在受控目录执行只读命令（默认禁用，启用后仍受白名单约束）。",
            danger_level="read",
            metadata={
                "enabled": settings.tool_shell_enabled,
                "workdir": settings.tool_shell_workdir,
                "allowedCommands": sorted(_ALLOWED_COMMANDS),
            },
        ),
        build_shell_tool(settings),
    )


def build_default_toolbox(
    settings: Settings,
    *,
    include_mcp: bool = True,
) -> ToolRegistry:
    """构建 LangGraph 可用的默认工具箱。

    新增 Tool 时只需在此注册；Agent 的 Manifest 通过 supported_tools
    声明可用子集，运行时由 Skill 决定实际挂载范围。

    MCP Server 发现失败只记录 warning：搜索 / shell 等本地 Tool 必须仍然
    可用，不能让一个挂掉的外部进程拖垮整个 Agent。
    """
    from ai_core.tools.web_search_tool import register_web_search

    registry = ToolRegistry()
    register_shell_tool(registry, settings)
    register_web_search(registry, settings)
    if include_mcp and settings.mcp_enabled:
        try:
            from ai_core.mcp.tools import register_mcp_tools

            register_mcp_tools(registry, settings)
        except Exception as exc:  # noqa: BLE001 - MCP 故障隔离
            logger.warning("MCP tool discovery failed: %s", exc)
    return registry
