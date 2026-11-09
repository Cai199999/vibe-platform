"""MCP stdio 客户端。

一次连接 = 一个子进程 + 一条 stdin/stdout 管道。为了不让某个挂死的
MCP Server 拖垮 Agent Run，所有读写都有硬超时：
  - ``select`` 等待 stdout 可读；
  - ``timeout_seconds`` 覆盖 initialize 与每次 tools/call；
  - 进程退出 / EOF / JSON 解析失败统一转成领域异常。

安全边界：
  - command / args / env 只来自服务端 Settings，绝不接受浏览器输入；
  - 子进程不继承完整 ``os.environ``，避免把 DeepSeek / 数据库密钥泄漏给
    第三方 MCP Server；只保留 PATH / HOME / LANG 与配置显式声明的变量。
"""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PROTOCOL_VERSION = "2024-11-05"
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class McpError(RuntimeError):
    """MCP 连接或协议错误。"""

    code = "MCP_ERROR"


class McpTimeoutError(McpError):
    code = "MCP_TIMEOUT"


class McpProtocolError(McpError):
    code = "MCP_PROTOCOL_ERROR"


@dataclass(frozen=True)
class McpServerConfig:
    """一个 stdio MCP Server 的启动配置。"""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    timeout_seconds: float = 10.0
    allowed_tools: tuple[str, ...] = ()

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        default_timeout: float = 10.0,
    ) -> "McpServerConfig":
        name = str(payload.get("name") or "").strip()
        command = str(payload.get("command") or "").strip()
        if not name:
            raise McpError("MCP Server 缺少 name")
        if not command:
            raise McpError(f"MCP Server {name} 缺少 command")
        args = payload.get("args") or []
        if not isinstance(args, (list, tuple)):
            raise McpError(f"MCP Server {name} 的 args 必须是数组")
        env = payload.get("env") or {}
        if not isinstance(env, dict):
            raise McpError(f"MCP Server {name} 的 env 必须是对象")
        allowed = payload.get("allowed_tools") or payload.get("allowedTools") or []
        if not isinstance(allowed, (list, tuple)):
            raise McpError(f"MCP Server {name} 的 allowed_tools 必须是数组")
        timeout = payload.get("timeout_seconds") or payload.get("timeoutSeconds")
        return cls(
            name=name,
            command=command,
            args=tuple(str(item) for item in args),
            env={str(key): str(value) for key, value in env.items()},
            cwd=str(payload["cwd"]) if payload.get("cwd") else None,
            timeout_seconds=float(timeout or default_timeout),
            allowed_tools=tuple(str(item) for item in allowed),
        )


class McpStdioClient:
    """同步 MCP stdio 客户端，适用于 FastAPI 线程池与 LangChain Tool。"""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._buffer = b""
        self._next_id = 0
        self._lock = threading.RLock()
        self._closed = True

    # --------------------------------------------------------
    # 生命周期
    # --------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", "/tmp"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            env.update(self.config.env)
            command = [self.config.command, *self.config.args]
            try:
                self._process = subprocess.Popen(  # noqa: S603 - 命令来自服务端配置
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=self.config.cwd,
                    env=env,
                    bufsize=0,
                )
            except OSError as exc:
                raise McpError(
                    f"MCP Server {self.config.name} 启动失败: {exc}"
                ) from exc
            self._buffer = b""
            self._next_id = 0
            self._closed = False
            try:
                self._initialize()
            except Exception:
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            self._closed = True
            self._buffer = b""
            if process is None:
                return
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
            except OSError:
                pass
            finally:
                for stream in (process.stdin, process.stdout):
                    try:
                        stream and stream.close()
                    except OSError:
                        pass

    def __enter__(self) -> "McpStdioClient":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # --------------------------------------------------------
    # MCP 方法
    # --------------------------------------------------------
    def _initialize(self) -> None:
        result = self._request(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "vibe-platform-ai-core",
                    "version": "0.1.0",
                },
            },
        )
        version = result.get("protocolVersion")
        if version and version != DEFAULT_PROTOCOL_VERSION:
            logger.info(
                "MCP server %s negotiated protocol %s",
                self.config.name,
                version,
            )
        self._notify("notifications/initialized", {})

    def ping(self) -> dict[str, Any]:
        return self._request("ping", {})

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {})
        tools = result.get("tools") or []
        if not isinstance(tools, list):
            raise McpProtocolError("tools/list 返回的 tools 不是数组")
        return [item for item in tools if isinstance(item, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments or {})},
        )
        texts: list[str] = []
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
            elif item.get("type") == "json":
                texts.append(json.dumps(item.get("json"), ensure_ascii=False))
        structured = result.get("structuredContent")
        text = "\n".join(part for part in texts if part).strip()
        if not text and structured is not None:
            text = json.dumps(structured, ensure_ascii=False)
        if not text:
            text = json.dumps(result, ensure_ascii=False)
        return {
            "text": text,
            "isError": bool(result.get("isError")),
            "structured": structured,
        }

    # --------------------------------------------------------
    # JSON-RPC 底层
    # --------------------------------------------------------
    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                raise McpError(f"MCP Server {self.config.name} 未启动或已退出")
            self._next_id += 1
            request_id = self._next_id
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            deadline = time.monotonic() + self.config.timeout_seconds
            while True:
                message = self._read_message(deadline)
                # 跳过服务器主动推送的 notification（没有 id）。
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    error = message.get("error") or {}
                    raise McpProtocolError(
                        f"MCP {self.config.name} {method} 失败: "
                        f"{error.get('message') or error} "
                        f"(code={error.get('code')})"
                    )
                result = message.get("result")
                if not isinstance(result, dict):
                    raise McpProtocolError(
                        f"MCP {self.config.name} {method} 返回 result 不是对象"
                    )
                return result

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpError("MCP stdin 不可用")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(body) > MAX_MESSAGE_BYTES:
            raise McpError("MCP 请求超过大小限制")
        try:
            process.stdin.write(body + b"\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError(f"MCP 写入失败: {exc}") from exc

    def _read_message(self, deadline: float) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise McpError("MCP stdout 不可用")
        fd = process.stdout.fileno()
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpTimeoutError(
                    f"MCP {self.config.name} 响应超时"
                    f"（>{self.config.timeout_seconds}s）"
                )
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise McpTimeoutError(
                    f"MCP {self.config.name} 响应超时"
                    f"（>{self.config.timeout_seconds}s）"
                )
            try:
                chunk = os.read(fd, 65536)
            except OSError as exc:
                raise McpError(f"MCP 读取失败: {exc}") from exc
            if not chunk:
                raise McpError(
                    f"MCP {self.config.name} stdout 已关闭" f"（exit={process.poll()}）"
                )
            self._buffer += chunk
            if len(self._buffer) > MAX_MESSAGE_BYTES:
                raise McpProtocolError("MCP 响应超过大小限制")
        line, _, self._buffer = self._buffer.partition(b"\n")
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpProtocolError(f"MCP 返回非法 JSON: {exc}") from exc
        if not isinstance(message, dict):
            raise McpProtocolError("MCP 返回的消息不是对象")
        return message
