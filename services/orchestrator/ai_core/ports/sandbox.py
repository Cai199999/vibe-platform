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
class SandboxDraft:
    """一次 Run 的隔离草稿。

    草稿是主工作树的 ``git worktree`` 影子目录，拥有独立的预览端口。
    模型 / Applier 只能写入 ``workdir``；用户审批通过后，Provider 才把
    草稿变更 promote 回主工作树。
    """

    draft_id: str
    sandbox_id: str
    base_sha: str
    workdir: str
    preview_url: str
    host_port: int
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

    def reap_idle_sandboxes(
        self,
        ttl_seconds: float,
        protected_ids: set[str] | None = None,
    ) -> list[str]:
        """回收空闲超过 TTL 的运行中沙盒；默认 Provider 不支持。"""
        return []

    def restart(
        self,
        sandbox_id: str,
        ready_path: str = "/",
        *,
        require_root: bool = False,
    ) -> None:
        """重启沙盒内的开发服务，并等待 ``ready_path`` 可访问。

        默认实现不支持重启；LocalDockerSandbox 通过 ``container.restart``
        重新执行入口命令，从而拉起新生成的 Node 后端与 Vite 双服务。
        FullstackGraph 用 ``ready_path="/api/health"`` 同时验证 Vite 代理
        与后端是否真正就绪。
        """
        raise SandboxError(f"当前 Provider 不支持重启沙盒: {sandbox_id}")

    # --------------------------------------------------------
    # 草稿隔离（Phase 0）
    # --------------------------------------------------------
    def begin_draft(self, sandbox_id: str) -> SandboxDraft | None:
        """创建隔离草稿 worktree 与预览服务。

        默认返回 ``None``，表示 Provider 不支持草稿隔离，调用方必须回退
        到旧的直写路径（仅用于测试替身 / 未升级的 Provider）。
        """
        return None

    def discard_draft(self, sandbox_id: str, draft_id: str) -> None:
        """丢弃草稿：停止预览、移除 worktree，主工作树保持不变。"""
        raise SandboxError(f"当前 Provider 不支持丢弃草稿: {sandbox_id}")

    def promote_draft(
        self,
        sandbox_id: str,
        draft_id: str,
        expected_base_sha: str,
    ) -> str:
        """把草稿变更合并回主工作树，返回主工作树的新 HEAD。

        调用方必须已获得用户显式审批；Provider 内部仍会校验
        ``expected_base_sha``，防止审批期间主分支被其它 Run 推进。
        """
        raise SandboxError(f"当前 Provider 不支持草稿合并: {sandbox_id}")

    def reap_orphan_drafts(self, ttl_days: int | None = None) -> list[str]:
        """回收超过 TTL 的孤儿草稿，返回被回收的 draft_id 列表。

        孤儿草稿指容器内仍存在 worktree / 预览进程，但当前进程的草稿
        登记表里已查不到（典型场景：python-ai 重启导致内存态丢失）。
        默认实现不做任何事；LocalDockerSandbox 覆盖为真实清理。
        """
        return []

    def reap_stopped_sandboxes(self, ttl_days: int | None = None) -> list[str]:
        """回收超过 TTL 且已停止的沙盒容器，返回被回收的 sandbox_id 列表。

        只处理 ``vibe.managed=true`` 且状态为 exited / created / dead 的
        容器；运行中的沙盒、当前进程登记表中的容器永远不碰。默认实现
        不做任何事；LocalDockerSandbox 覆盖为真实清理。
        """
        return []

    @abstractmethod
    def apply_patch(
        self,
        sandbox_id: str,
        files: dict[str, str] | None = None,
        patch: str | None = None,
        deleted_paths: list[str] | None = None,
    ) -> list[str]:
        """更新沙盒文件，返回变更文件列表。

        ``LocalDockerSandbox`` 额外支持 ``draft_id`` 关键字参数：提供时
        只写草稿 worktree，未提供时维持旧的直写主工作树行为。
        """

    def ast_scope_check(
        self,
        sandbox_id: str,
        files: dict[str, str] | None,
        patch: str | None,
        scopes: list[dict],
        changed_files: list[str],
    ) -> list[dict]:
        """写盘前对公共组件 / 多实例目标做 AST 差分校验。

        默认实现返回空列表（无 AST 能力的 Provider 不做额外拦截）；
        LocalDockerSandbox 会用沙盒内的 Babel 解析 before/after 两棵树，
        拒绝“改基础组件”和“map 里无条件新增 JSX”。
        """
        return []

    # --------------------------------------------------------
    # Phase 1：确定性目标解析 + 唯一写工具
    # --------------------------------------------------------
    def resolve_targets(
        self,
        sandbox_id: str,
        anchors: list[dict],
        draft_id: str | None = None,
    ) -> list[dict] | None:
        """用沙盒内 Babel 把 AnnotationEnvelope 解析为精确 AST 锚点。

        返回 ``None`` 表示当前 Provider 不支持确定性解析，调用方必须
        降级为旧 Coder 路径（测试替身 / 未升级 Provider）。
        返回列表时，每项至少包含 ``anchorId`` / ``status`` / ``file`` /
        ``range``；歧义项包含 ``candidates``。
        """
        return None

    def apply_ast_edit(
        self,
        sandbox_id: str,
        edit: dict,
        draft_id: str | None = None,
    ) -> dict | None:
        """执行一次 Babel 精确切片编辑，返回新文件内容。

        只计算不落盘：调用方（Tool / Applier）负责把 ``content`` 写回
        草稿工作树，从而复用实例作用域门禁与审批隔离。
        返回 ``None`` 表示 Provider 不支持，工具层必须 fail-closed。
        """
        return None

    @abstractmethod
    def verify(
        self,
        sandbox_id: str,
        check: str = "build",
    ) -> VerifyResult:
        """在沙盒内执行 npm run build / npm run test。"""

    def file_exists(
        self,
        sandbox_id: str,
        path: str,
        draft_id: str | None = None,
    ) -> bool:
        """文件是否存在；默认 True 保持旧 Provider 兼容。"""
        return True

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
