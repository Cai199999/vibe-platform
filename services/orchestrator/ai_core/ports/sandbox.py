"""端口：SandboxProvider 沙盒生命周期抽象。

上层 Agent / Run 编排只依赖本接口，不感知底层是本地 Docker、
独立沙盒节点还是 gVisor。具体实现位于
``ai_core.adapters.sandbox``（当前为 LocalDockerSandbox）。
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


class SandboxGitError(SandboxError):
    """沙盒内 Git checkpoint / rollback 失败。"""


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
class SandboxCommit:
    """沙盒 Git 历史中的一条 commit（快照）。

    快照直接复用沙盒内的 Git 仓库，不引入额外数据库：
      - ``commit_hash`` 为完整 40 位 SHA-1，恢复时必须使用完整值；
      - ``short_hash`` 仅用于 UI 展示。
    """

    commit_hash: str
    short_hash: str
    message: str
    author: str
    committed_at: datetime


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

    def create_imported(
        self,
        project_id: str,
        files: dict[str, bytes],
        kind: str,
        start_command: str,
        template: str = "react-vite",
    ) -> SandboxHandle:
        """用用户上传的源码创建沙盒。

        与 ``create`` 的区别：
          - 文件可能是二进制（图片 / 字体），因此使用 bytes；
          - 容器启动命令由导入服务决定（React 先 npm install，HTML 起静态服务）。
        """
        raise SandboxError("当前 Provider 不支持导入项目")

    def get(self, sandbox_id: str) -> SandboxHandle:
        """按 sandbox_id 恢复句柄，供多轮对话复用同一沙盒。

        默认实现抛出 SandboxError；具体 Provider 应覆盖并在容器不存在时
        返回 SandboxNotFoundError，让上层可以回退为创建新沙盒。
        """
        raise SandboxError(f"当前 Provider 不支持恢复沙盒句柄: {sandbox_id}")

    def git_commit(self, sandbox_id: str, message: str) -> str:
        """在沙盒项目内暂存全部改动并提交，返回完整 commit hash。

        默认实现不支持 Git checkpoint；LocalDockerSandbox 覆盖。
        """
        raise SandboxGitError(f"当前 Provider 不支持 Git checkpoint: {sandbox_id}")

    def git_reset_hard(self, sandbox_id: str, commit_hash: str) -> None:
        """把沙盒工作区硬回滚到指定 commit。"""
        raise SandboxGitError(f"当前 Provider 不支持 Git rollback: {sandbox_id}")

    def git_baseline_commit(self, sandbox_id: str) -> str:
        """返回沙盒初始基线 commit（root commit）。"""
        raise SandboxGitError(f"当前 Provider 不支持 Git baseline: {sandbox_id}")

    def git_head(self, sandbox_id: str) -> str:
        """返回当前 HEAD commit。

        Run 开始前记录一次，审批拒绝 / 取消时把工作区恢复到该提交，
        避免未批准的代码残留在多轮会话中。
        """
        raise SandboxGitError(f"当前 Provider 不支持读取 HEAD: {sandbox_id}")

    def git_log(self, sandbox_id: str, limit: int = 50) -> list[SandboxCommit]:
        """按时间倒序返回沙盒的提交历史（即用户可见的快照列表）。"""
        raise SandboxGitError(f"当前 Provider 不支持读取提交历史: {sandbox_id}")

    def git_diff(
        self,
        sandbox_id: str,
        commit_hash: str,
        mode: str = "commit",
    ) -> str:
        """返回快照相关的 unified diff 文本。

        ``mode="commit"``：该快照自身引入的改动（``git show``）；
        ``mode="current"``：当前 HEAD 与该快照之间的差异；
        ``mode="baseline"``：相对沙盒初始版本的累积代码 Diff。
        """
        raise SandboxGitError(f"当前 Provider 不支持生成快照 Diff: {sandbox_id}")

    def restart(self, sandbox_id: str, ready_path: str = "/") -> None:
        """重启沙盒内的开发服务，并等待 ``ready_path`` 可访问。

        默认实现不支持重启；LocalDockerSandbox 通过 ``container.restart``
        重新执行入口命令，从而拉起新生成的 Node 后端与 Vite 双服务。
        FullstackGraph 用 ``ready_path="/api/health"`` 同时验证 Vite 代理
        与后端是否真正就绪。
        """
        raise SandboxError(f"当前 Provider 不支持重启沙盒: {sandbox_id}")

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
    def read_file(self, sandbox_id: str, path: str) -> str:
        """读取沙盒内文件，供 Agent 维护 current_code。"""

    @abstractmethod
    def export_archive(self, sandbox_id: str) -> bytes:
        """把沙盒项目打包为 tar.gz（排除 node_modules / .git / dist）。

        返回的是完整 gzip 字节流，调用方负责直接下发或存储。
        """

    @abstractmethod
    def destroy(self, sandbox_id: str) -> None:
        """销毁容器并释放资源。"""
