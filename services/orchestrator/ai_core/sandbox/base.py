"""SandboxProvider 抽象。

上层 Agent / Run 编排只依赖本接口，不感知底层是本地 Docker、
独立沙盒节点还是 gVisor。M1 提供 LocalDockerSandbox 实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


class SandboxError(Exception):
    """Sandbox 基础异常。"""


class SandboxNotFoundError(SandboxError):
    """沙盒不存在或已销毁。"""


class SandboxValidationError(SandboxError):
    """文件路径 / 内容不合法。"""


class SandboxStartupError(SandboxError):
    """容器启动失败或开发服务器未就绪。"""


class SandboxPatchError(SandboxError):
    """unified diff 应用失败。"""


@dataclass(frozen=True)
class SandboxHandle:
    """Provider 无关的沙盒句柄。"""

    sandbox_id: str
    container_id: str
    container_name: str
    project_id: str
    state: str
    preview_url: str
    host_port: int
    internal_port: int
    template: str
    created_at: datetime


@dataclass(frozen=True)
class VerifyResult:
    """构建 / 测试结果。"""

    success: bool
    exit_code: int
    timed_out: bool
    duration_ms: int
    logs: str
    logs_truncated: bool


class SandboxProvider(ABC):
    """沙盒生命周期抽象。"""

    @abstractmethod
    def create(
        self,
        project_id: str,
        files: dict[str, str],
        template: str = "react-vite",
    ) -> SandboxHandle:
        """创建并启动沙盒，返回可访问的预览地址。"""

    @abstractmethod
    def apply_patch(
        self,
        sandbox_id: str,
        files: dict[str, str] | None = None,
        patch: str | None = None,
        deleted_paths: list[str] | None = None,
    ) -> list[str]:
        """更新沙盒文件，返回变更文件列表。"""

    @abstractmethod
    def verify(
        self,
        sandbox_id: str,
        check: str = "build",
    ) -> VerifyResult:
        """在沙盒内执行 npm run build / npm run test。"""

    @abstractmethod
    def destroy(self, sandbox_id: str) -> None:
        """销毁容器并释放资源。"""
