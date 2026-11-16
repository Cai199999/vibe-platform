"""基于本机 Docker Daemon 的沙盒实现。

python-ai 容器通过挂载的 /var/run/docker.sock 调用宿主机 Docker：
  - 镜像：预装 Node + Vite + React 的 sandbox_template；
  - 容器：非特权、cap_drop=ALL、内存 / CPU / PID 限额；
  - 端口：只绑定 127.0.0.1 的随机宿主端口，返回预览 URL；
  - 生命周期：创建 / 应用补丁 / 构建校验 / 销毁。

安全说明：挂载 Docker Socket 等价于授予容器 root 级宿主权限，
仅适用于本地开发与受信任环境。生产环境由独立沙盒节点 + gVisor 替换。
"""

from __future__ import annotations

import io
import json
import logging
import re
import shlex
import socket
import tarfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from ai_core.config import Settings, get_settings
from ai_core.ports.sandbox import (
    SandboxCommit,
    SandboxDraft,
    SandboxError,
    SandboxGitError,
    SandboxHandle,
    SandboxNotFoundError,
    SandboxPatchError,
    SandboxProvider,
    SandboxStartupError,
    SandboxValidationError,
    VerifyResult,
)

# 容器标签：用于 python-ai 重启后仍能通过标签找回沙盒。
LABEL_SANDBOX_ID = "vibe.sandbox.id"
LABEL_PROJECT_ID = "vibe.project.id"
LABEL_MANAGED = "vibe.managed"

# 沙盒 Git 仓库忽略项：依赖与构建产物重建即可，不需要进入 checkpoint。
# Vite 会在项目根目录瞬时生成 vite.config.js.timestamp-*.mjs 后立即删除；
# 若 git add 恰好扫描到删除瞬间会报 "unable to stat"，因此必须忽略临时文件。
GITIGNORE_CONTENT = (
    # 不带斜杠同时覆盖真实目录与草稿里的 node_modules 软链接。
    "node_modules\n"
    "dist/\n"
    ".vite/\n"
    "*.log\n"
    "vite.config.*.timestamp-*\n"
    "*.timestamp-*.mjs\n"
    # 全栈生成：SQLite 数据库文件属于运行时数据，不进入项目快照。
    "data/\n"
    "*.sqlite\n"
    "*.sqlite3\n"
    "*.db\n"
)
# 写入 .git/info/exclude：兼容本次升级前已创建、且 .gitignore 不含上述
# 临时文件规则的旧沙盒，避免它们在下一次 checkpoint 时偶发失败。
GIT_EXCLUDE_CONTENT = (
    "# vibe sandbox excludes\n"
    "node_modules\n"
    "dist/\n"
    ".vite/\n"
    "*.log\n"
    "vite.config.*.timestamp-*\n"
    "*.timestamp-*.mjs\n"
    "data/\n"
    "*.sqlite\n"
    "*.sqlite3\n"
    "*.db\n"
)
FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# 草稿 ID 由服务端生成，必须严格校验后才能拼进 shell 命令。
DRAFT_ID_RE = re.compile(r"^drf_[0-9a-f]{16,32}$")
DRAFT_GIT_USER = ("Vibe Draft", "draft@vibe.local")
# 创建新草稿前顺手清理同沙盒的残留草稿。60 秒宽限用于避开“另一个线程
# 刚创建 worktree、尚未写入 _drafts 登记表”的窗口，杜绝误删并发草稿。
DRAFT_RECLAIM_GRACE_SECONDS = 60
logger = logging.getLogger(__name__)

# 写盘前静态门禁：语法错误 / 未绑定的 JSX 组件一律拦截，绝不让坏代码
# 触发 Vite HMR。真实事故：`<Plus />` 漏 import，esbuild 构建通过，
# 浏览器运行时报 ReferenceError，整个 React 树被卸载成白屏。
AST_CHECK_SCRIPT = "/opt/vibe-ast-check.mjs"
AST_STAGE_DIR = "/tmp/vibe-ast-stage"
AST_STAGE_PATCH = "/tmp/vibe-ast-stage.patch"
# 实例作用域 AST 门禁：独立脚本与独立 stage，避免与语法门禁互相覆盖。
AST_SCOPE_SCRIPT = "/opt/vibe-ast-scope-check.mjs"
AST_SCOPE_STAGE_DIR = "/tmp/vibe-scope-stage"
AST_SCOPE_STAGE_PATCH = "/tmp/vibe-scope-stage.patch"
# Phase 1：确定性目标解析器 + 唯一写工具执行器。两者共用独立 stage，
# 避免与语法 / 实例门禁的 spec.json 互相覆盖。
AST_RESOLVE_SCRIPT = "/opt/vibe-resolve-target.mjs"
AST_EDIT_SCRIPT = "/opt/vibe-ast-edit.mjs"
AST_TOOL_STAGE_DIR = "/tmp/vibe-ast-tool-stage"
SCRIPT_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
# unified diff 的 ``+++`` 目标行：兼容 ``b/path``、带引号路径与 /dev/null。
PATCH_TARGET_RE = re.compile(r"^\+\+\+\s+(?P<path>.+?)\s*$", re.MULTILINE)


class LocalDockerSandbox(SandboxProvider):
    """单机 Docker 沙盒实现。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.RLock()
        # Git 操作串行化：同一沙盒的 commit / reset 不能交错，否则 HEAD
        # 与 thread checkpoint 可能指向不同的代码快照。
        self._git_lock = threading.RLock()
        # 构造阶段不抛异常：Docker 暂不可用时服务仍应启动，
        # 由具体沙盒请求返回 503，健康检查只展示 UNAVAILABLE。
        self._client: docker.DockerClient | None = None
        self._client_error: str = ""
        try:
            # version="auto" 让 SDK 与宿主 Docker API 协商版本，避免版本漂移。
            self._client = docker.DockerClient(
                base_url=self.settings.docker_host,
                version="auto",
                timeout=self.settings.docker_api_timeout_seconds,
            )
        except DockerException as exc:  # pragma: no cover - 取决于运行环境
            self._client_error = str(exc)
        self._image_lock = threading.Lock()
        # sandbox_id -> container_id。进程重启后通过容器标签恢复。
        self._sandboxes: dict[str, str] = {}
        # 已分配但尚未被 Docker 确认的宿主端口，避免同进程内重复分配。
        self._reserved_ports: set[int] = set()
        # 活跃草稿：draft_id -> 元数据。草稿是内存态，Python 进程重启后
        # 由 /app-drafts 目录与 git worktree 列表负责清理残留。
        self._drafts: dict[str, dict[str, Any]] = {}

    # --------------------------------------------------------
    # 对外接口
    # --------------------------------------------------------
    def create(
        self,
        project_id: str,
        files: dict[str, str],
        template: str = "react-vite",
    ) -> SandboxHandle:
        if template != "react-vite":
            raise SandboxValidationError(f"不支持的沙盒模板: {template}")

        normalized = self._validate_files(files)
        self._ensure_image()

        sandbox_id = f"sbx_{uuid.uuid4().hex[:20]}"
        container_name = f"vibe-sandbox-{sandbox_id}"
        host_port = self._reserve_host_port()
        # 草稿预览端口随沙盒一起发布：运行中无法给已有容器追加端口映射。
        draft_host_port = self._reserve_host_port()

        env = {
            "PROJECT_ID": project_id,
            "SANDBOX_ID": sandbox_id,
            # 宿主端口与容器内 5173 不同，必须显式告诉 Vite HMR 客户端。
            "VITE_HMR_CLIENT_PORT": str(host_port),
        }

        client = self._require_client()
        container = None
        try:
            container = client.containers.create(
                image=self.settings.sandbox_image,
                name=container_name,
                detach=True,
                working_dir=self.settings.sandbox_workdir,
                environment=env,
                # 只暴露 127.0.0.1，避免沙盒预览直接暴露到局域网。
                ports={
                    f"{self.settings.sandbox_dev_port}/tcp": ("127.0.0.1", host_port),
                    f"{self.settings.sandbox_draft_port}/tcp": (
                        "127.0.0.1",
                        draft_host_port,
                    ),
                },
                labels={
                    LABEL_MANAGED: "true",
                    LABEL_SANDBOX_ID: sandbox_id,
                    LABEL_PROJECT_ID: project_id,
                },
                mem_limit=f"{self.settings.sandbox_memory_mb}m",
                nano_cpus=int(self.settings.sandbox_cpu_limit * 1_000_000_000),
                pids_limit=self.settings.sandbox_pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                init=True,
            )

            # 先写入初始文件，再启动开发服务器，避免 Vite 首屏看到半成品。
            if normalized:
                self._put_files(container, normalized)

            container.start()
            # 在 Agent 第一次写入前建立基线 commit：后续每一轮成功 Run 都
            # 在它之上追加 checkpoint，删除标注时可回滚到真正的初始代码。
            self._ensure_git_repo(container)
            self._wait_until_ready(container, sandbox_id)

            with self._lock:
                self._sandboxes[sandbox_id] = container.id
                self._reserved_ports.add(host_port)
                self._reserved_ports.add(draft_host_port)
        except (APIError, DockerException) as exc:
            if container is not None:
                self._safe_remove(container)
            self._release_host_port(host_port)
            self._release_host_port(draft_host_port)
            raise SandboxStartupError(f"创建沙盒失败: {exc}") from exc
        except SandboxError:
            if container is not None:
                self._safe_remove(container)
            self._release_host_port(host_port)
            self._release_host_port(draft_host_port)
            raise

        return SandboxHandle(
            sandbox_id=sandbox_id,
            container_id=container.id,
            container_name=container_name,
            project_id=project_id,
            state="RUNNING",
            preview_url=f"http://{self.settings.sandbox_preview_host}:{host_port}",
            host_port=host_port,
            internal_port=self.settings.sandbox_dev_port,
            template=template,
            created_at=datetime.now(timezone.utc),
        )

    def create_imported(
        self,
        project_id: str,
        files: dict[str, bytes],
        kind: str,
        start_command: str,
        template: str = "react-vite",
    ) -> SandboxHandle:
        """用上传的 zip / 图片生成源码创建沙盒。

        文件通过 Docker put_archive 以 tar 形式写入容器（等价于在容器内解压），
        然后执行导入服务生成的启动命令：
          - React：``npm install && npm run dev``；
          - HTML ：``node /opt/vibe-static-server.mjs /app 5173``。
        """
        if not files:
            raise SandboxValidationError("导入项目没有可写入的文件")
        self._ensure_image()

        sandbox_id = f"sbx_{uuid.uuid4().hex[:20]}"
        container_name = f"vibe-sandbox-{sandbox_id}"
        host_port = self._reserve_host_port()
        draft_host_port = self._reserve_host_port()
        env = {
            "PROJECT_ID": project_id,
            "SANDBOX_ID": sandbox_id,
            "VITE_HMR_CLIENT_PORT": str(host_port),
            # CRA / 旧项目可能读取 PORT；Vite 由命令行参数覆盖。
            "PORT": str(self.settings.sandbox_dev_port),
            "HOST": "0.0.0.0",
        }

        client = self._require_client()
        container = None
        try:
            container = client.containers.create(
                image=self.settings.sandbox_image,
                name=container_name,
                detach=True,
                working_dir=self.settings.sandbox_workdir,
                environment=env,
                command=["sh", "-lc", start_command],
                ports={
                    f"{self.settings.sandbox_dev_port}/tcp": ("127.0.0.1", host_port),
                    f"{self.settings.sandbox_draft_port}/tcp": (
                        "127.0.0.1",
                        draft_host_port,
                    ),
                },
                labels={
                    LABEL_MANAGED: "true",
                    LABEL_SANDBOX_ID: sandbox_id,
                    LABEL_PROJECT_ID: project_id,
                },
                mem_limit=f"{self.settings.sandbox_memory_mb}m",
                nano_cpus=int(self.settings.sandbox_cpu_limit * 1_000_000_000),
                pids_limit=self.settings.sandbox_pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                init=True,
            )

            # 先写文件再启动：npm install 与 Vite 启动必须看到完整源码。
            self._put_archive_bytes(container, files)
            container.start()
            self._ensure_git_repo(container)
            self._wait_until_ready(
                container,
                sandbox_id,
                timeout=self.settings.sandbox_import_timeout_seconds,
            )

            with self._lock:
                self._sandboxes[sandbox_id] = container.id
                self._reserved_ports.add(host_port)
                self._reserved_ports.add(draft_host_port)
        except (APIError, DockerException) as exc:
            if container is not None:
                self._safe_remove(container)
            self._release_host_port(host_port)
            self._release_host_port(draft_host_port)
            raise SandboxStartupError(f"创建导入沙盒失败: {exc}") from exc
        except SandboxError:
            if container is not None:
                self._safe_remove(container)
            self._release_host_port(host_port)
            self._release_host_port(draft_host_port)
            raise

        return SandboxHandle(
            sandbox_id=sandbox_id,
            container_id=container.id,
            container_name=container_name,
            project_id=project_id,
            state="RUNNING",
            preview_url=f"http://{self.settings.sandbox_preview_host}:{host_port}",
            host_port=host_port,
            internal_port=self.settings.sandbox_dev_port,
            template=template,
            created_at=datetime.now(timezone.utc),
        )

    def get(self, sandbox_id: str) -> SandboxHandle:
        """恢复已存在沙盒的句柄，供同一标注的多轮增量修改复用。"""
        container = self._get_container(sandbox_id)
        try:
            container.reload()
            attrs = container.attrs
        except DockerException as exc:
            raise SandboxError(f"读取沙盒状态失败 {sandbox_id}: {exc}") from exc

        ports = (attrs.get("NetworkSettings") or {}).get("Ports") or {}
        bindings = ports.get(f"{self.settings.sandbox_dev_port}/tcp") or []
        if not bindings:
            raise SandboxNotFoundError(f"沙盒没有可用的预览端口: {sandbox_id}")
        host_port = int(bindings[0]["HostPort"])

        labels = attrs.get("Config", {}).get("Labels") or {}
        created_raw = str(attrs.get("Created") or "")
        try:
            created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except ValueError:
            created_at = datetime.now(timezone.utc)

        return SandboxHandle(
            sandbox_id=sandbox_id,
            container_id=container.id,
            container_name=container.name,
            project_id=str(labels.get(LABEL_PROJECT_ID) or ""),
            state=str(container.status),
            preview_url=f"http://{self.settings.sandbox_preview_host}:{host_port}",
            host_port=host_port,
            internal_port=self.settings.sandbox_dev_port,
            template="react-vite",
            created_at=created_at,
        )

    def git_commit(self, sandbox_id: str, message: str) -> str:
        """暂存全部源码并提交，返回 40 位 commit hash。

        ``--allow-empty`` 保证“模型没有实际改动但构建通过”的轮次也能形成
        可回滚 checkpoint，使前端每一轮助手消息都有稳定锚点。
        """
        container = self._get_container(sandbox_id)
        with self._git_lock:
            self._ensure_git_repo(container)
            self._git_add_all(container, "git add 失败")

            code, output = self._exec(
                container,
                ["git", "commit", "--allow-empty", "-m", message],
                workdir=self.settings.sandbox_workdir,
            )
            if code != 0:
                raise SandboxGitError(f"git commit 失败: {self._decode(output)}")

            code, output = self._exec(
                container,
                ["git", "rev-parse", "HEAD"],
                workdir=self.settings.sandbox_workdir,
            )
            commit_hash = self._extract_commit_hash(self._decode(output))
            if code != 0 or commit_hash is None:
                raise SandboxGitError(f"读取 commit hash 失败: {self._decode(output)}")
            return commit_hash

    def git_reset_hard(self, sandbox_id: str, commit_hash: str) -> None:
        """恢复工作区到指定 commit。

        commit hash 必须通过 40 位十六进制白名单校验，且用参数数组传给
        ``git``，不做 shell 字符串拼接，避免注入。
        """
        normalized = commit_hash.strip().lower()
        if not FULL_COMMIT_RE.fullmatch(normalized):
            raise SandboxValidationError("非法 commit hash")

        container = self._get_container(sandbox_id)
        with self._git_lock:
            self._ensure_git_repo(container)
            code, output = self._exec(
                container,
                ["git", "cat-file", "-e", f"{normalized}^{{commit}}"],
                workdir=self.settings.sandbox_workdir,
            )
            if code != 0:
                raise SandboxGitError(
                    f"commit 不存在或不是提交对象 {normalized}: "
                    f"{self._decode(output)}"
                )

            code, output = self._exec(
                container,
                ["git", "reset", "--hard", normalized],
                workdir=self.settings.sandbox_workdir,
            )
            if code != 0:
                raise SandboxGitError(f"git reset --hard 失败: {self._decode(output)}")

    def git_head(self, sandbox_id: str) -> str:
        """读取当前 HEAD，作为本轮 Run 的撤销/拒绝回滚锚点。"""
        container = self._get_container(sandbox_id)
        with self._git_lock:
            self._ensure_git_repo(container)
            code, output = self._exec(
                container,
                ["git", "rev-parse", "HEAD"],
                workdir=self.settings.sandbox_workdir,
            )
            value = self._extract_commit_hash(self._decode(output))
            if code != 0 or value is None:
                raise SandboxGitError(f"读取 HEAD 失败: {self._decode(output)}")
            return value

    def git_baseline_commit(self, sandbox_id: str) -> str:
        """读取沙盒根提交（创建容器时建立的初始代码快照）。"""
        container = self._get_container(sandbox_id)
        with self._git_lock:
            self._ensure_git_repo(container)
            code, output = self._exec(
                container,
                ["git", "rev-list", "--max-parents=0", "HEAD"],
                workdir=self.settings.sandbox_workdir,
            )
            value = self._extract_commit_hash(self._decode(output))
            if code != 0 or value is None:
                raise SandboxGitError(
                    f"读取 baseline commit 失败: {self._decode(output)}"
                )
            return value

    def git_log(self, sandbox_id: str, limit: int = 50) -> list[SandboxCommit]:
        """读取沙盒提交历史，按时间倒序返回。

        使用 ``%x1f``（字段分隔）与 ``%x1e``（记录分隔）作为不可见分隔符，
        避免 commit message 中的空格 / 换行破坏解析；不依赖语言环境日期。
        """
        safe_limit = max(1, min(int(limit or 50), 200))
        container = self._get_container(sandbox_id)
        with self._git_lock:
            self._ensure_git_repo(container)
            code, output = self._exec(
                container,
                [
                    "git",
                    "log",
                    f"-n{safe_limit}",
                    "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1e",
                    "HEAD",
                    "--",
                ],
                workdir=self.settings.sandbox_workdir,
            )
            if code != 0:
                raise SandboxGitError(f"git log 失败: {self._decode(output)}")
            return self._parse_git_log(self._decode(output))

    def git_diff(
        self,
        sandbox_id: str,
        commit_hash: str,
        mode: str = "commit",
    ) -> str:
        """生成快照 Diff。

        只以参数数组调用 git，且对 hash 做 40 位十六进制白名单校验，
        不存在 shell 注入面。
        """
        normalized = commit_hash.strip().lower()
        if not FULL_COMMIT_RE.fullmatch(normalized):
            raise SandboxValidationError("非法 commit hash")
        if mode not in {"commit", "current", "baseline"}:
            raise SandboxValidationError("mode 只能是 commit / current / baseline")

        container = self._get_container(sandbox_id)
        with self._git_lock:
            self._ensure_git_repo(container)
            code, output = self._exec(
                container,
                ["git", "cat-file", "-e", f"{normalized}^{{commit}}"],
                workdir=self.settings.sandbox_workdir,
            )
            if code != 0:
                raise SandboxGitError(f"commit 不存在: {normalized}")

            if mode == "commit":
                # 该快照自身引入的改动（根提交会展示完整文件新增）。
                command = [
                    "git",
                    "show",
                    "--format=",
                    "--no-color",
                    "--find-renames",
                    normalized,
                    "--",
                ]
            elif mode == "baseline":
                # 快照标签的默认视图：该版本相对初始基线的累积代码，
                # 即使快照本身是空提交（仅打标记）也能看到完整实现。
                code, root_output = self._exec(
                    container,
                    ["git", "rev-list", "--max-parents=0", "HEAD"],
                    workdir=self.settings.sandbox_workdir,
                )
                root_commit = self._extract_commit_hash(self._decode(root_output))
                if code != 0 or root_commit is None:
                    raise SandboxGitError(
                        f"读取 baseline commit 失败: {self._decode(root_output)}"
                    )
                command = [
                    "git",
                    "diff",
                    "--no-color",
                    "--find-renames",
                    root_commit,
                    normalized,
                    "--",
                ]
            else:
                # 当前工作区 HEAD 与目标快照之间的差异。
                command = [
                    "git",
                    "diff",
                    "--no-color",
                    "--find-renames",
                    normalized,
                    "HEAD",
                    "--",
                ]

            code, output = self._exec(
                container,
                command,
                workdir=self.settings.sandbox_workdir,
            )
            if code != 0:
                raise SandboxGitError(f"生成快照 Diff 失败: {self._decode(output)}")
            return self._decode(output)

    @staticmethod
    def _parse_git_log(output: str) -> list[SandboxCommit]:
        """把自定义格式的 git log 输出解析为 SandboxCommit 列表。"""
        commits: list[SandboxCommit] = []
        for record in output.split("\x1e"):
            record = record.strip()
            if not record:
                continue
            parts = record.split("\x1f", 4)
            if len(parts) < 5:
                continue
            full_hash, short_hash, author, committed_iso, message = parts
            full_hash = full_hash.strip().lower()
            if not FULL_COMMIT_RE.fullmatch(full_hash):
                continue
            try:
                committed_at = datetime.fromisoformat(committed_iso.strip())
            except ValueError:
                committed_at = datetime.now(timezone.utc)
            commits.append(
                SandboxCommit(
                    commit_hash=full_hash,
                    short_hash=short_hash.strip() or full_hash[:7],
                    message=message.strip(),
                    author=author.strip() or "vibe",
                    committed_at=committed_at,
                )
            )
        return commits

    def _git_add_all(self, container: Any, context: str) -> None:
        """``git add -A``，并对 Vite 临时文件竞态做有限重试。

        Vite 在启动 / 配置变更时会瞬时创建 ``vite.config.js.timestamp-*.mjs``
        再删除；如果 git 扫描恰好落在删除瞬间，会返回
        ``unable to stat ... No such file or directory``。这类错误是瞬时的，
        忽略规则无法 100% 消除竞态，因此保留 3 次短重试，仍失败才上抛。
        """
        workdir = self.settings.sandbox_workdir
        last_output = ""
        for attempt in range(3):
            code, output = self._exec(
                container,
                ["git", "add", "-A"],
                workdir=workdir,
            )
            if code == 0:
                return
            last_output = self._decode(output)
            transient = "unable to stat" in last_output or (
                "No such file or directory" in last_output
            )
            if not transient or attempt == 2:
                break
            time.sleep(0.2 * (attempt + 1))
        raise SandboxGitError(f"{context}: {last_output}")

    def _ensure_git_repo(self, container: Any) -> None:
        """确保工作区是带初始提交的 Git 仓库（幂等）。

        兼容本次升级前已创建的沙盒：第一次 commit / rollback 时即时初始化，
        以当时代码作为 baseline。
        """
        workdir = self.settings.sandbox_workdir
        code, _ = self._exec(
            container,
            ["git", "rev-parse", "--is-inside-work-tree"],
            workdir=workdir,
        )
        if code != 0:
            code, output = self._exec(
                container,
                ["git", "init", "-q"],
                workdir=workdir,
            )
            if code != 0:
                raise SandboxGitError(f"git init 失败: {self._decode(output)}")

        # 不覆盖项目自带 .gitignore；仅在新沙盒里补默认忽略项。
        code, _ = self._exec(container, ["test", "-f", ".gitignore"], workdir=workdir)
        if code != 0:
            self._put_files(container, {".gitignore": GITIGNORE_CONTENT})

        # 旧沙盒的 .gitignore 可能还没有 Vite 临时文件规则。.git/info/exclude
        # 只影响当前仓库、不会进入源码导出，用它补齐忽略项最安全。
        code, _ = self._exec(
            container,
            ["grep", "-qF", "vibe sandbox excludes", ".git/info/exclude"],
            workdir=workdir,
        )
        if code != 0:
            self._put_files(container, {".git/info/exclude": GIT_EXCLUDE_CONTENT})

        self._exec(
            container,
            ["git", "config", "user.name", "Vibe Agent"],
            workdir=workdir,
        )
        self._exec(
            container,
            ["git", "config", "user.email", "agent@vibe.local"],
            workdir=workdir,
        )

        code, _ = self._exec(
            container,
            ["git", "rev-parse", "--verify", "HEAD"],
            workdir=workdir,
        )
        if code != 0:
            self._git_add_all(container, "初始化 git add 失败")
            code, output = self._exec(
                container,
                ["git", "commit", "--allow-empty", "-m", "baseline: sandbox init"],
                workdir=workdir,
            )
            if code != 0:
                raise SandboxGitError(f"初始化 commit 失败: {self._decode(output)}")

            self._exec(
                container,
                ["git", "tag", "-f", "vibe-baseline", "HEAD"],
                workdir=workdir,
            )

    def restart(self, sandbox_id: str, ready_path: str = "/") -> None:
        """重启容器并等待开发服务 / 反向代理就绪。

        注意：容器入口为 ``npm run dev``。全栈生成会把该脚本改写为
        ``node scripts/dev.mjs``，由它同时拉起 Node 后端与 Vite；
        因此重启后不能只探测 ``/``，还要探测 ``/api/health`` 才能确认
        Vite 的 ``/api`` 代理和沙盒后端都已可用。
        """
        container = self._get_container(sandbox_id)
        try:
            container.restart(timeout=20)
        except DockerException as exc:
            raise SandboxStartupError(f"重启沙盒失败: {exc}") from exc
        self._wait_until_ready(container, sandbox_id, ready_path=ready_path)

    def apply_patch(
        self,
        sandbox_id: str,
        files: dict[str, str] | None = None,
        patch: str | None = None,
        deleted_paths: list[str] | None = None,
        draft_id: str | None = None,
    ) -> list[str]:
        container = self._get_container(sandbox_id)
        workdir = self._resolve_workdir(sandbox_id, draft_id)
        changed: list[str] = []

        normalized = self._validate_files(files or {})
        # 关键顺序：先在临时副本里预演全部变更并跑 AST 门禁，通过后才
        # 触碰工作目录。这样非法代码不会触发 HMR，沙盒始终停留在上一个
        # 可运行的 commit，不需要额外 reset。
        if normalized or patch:
            self._preflight_code_check(container, normalized, patch, workdir)
        if normalized:
            self._put_files(container, normalized, workdir)
            changed.extend(sorted(normalized))

        for raw_path in deleted_paths or []:
            rel_path = self._normalize_path(raw_path)
            code, output = self._exec(
                container,
                ["sh", "-lc", f"rm -rf -- {shlex.quote(rel_path)}"],
                workdir=workdir,
            )
            if code != 0:
                raise SandboxPatchError(
                    f"删除文件失败 {rel_path}: {self._decode(output)}"
                )
            changed.append(rel_path)

        if patch:
            self._apply_unified_diff(container, patch, workdir)
            changed.append("<unified-diff>")

        # 给 Vite 文件监听器一点时间捕获变更；即使尚未触发 HMR，
        # 下一次请求时 Vite 也会按最新文件重新 transform。
        time.sleep(0.2)
        return sorted(set(changed))

    def verify(
        self,
        sandbox_id: str,
        check: str = "build",
        draft_id: str | None = None,
    ) -> VerifyResult:
        if check not in {"build", "test", "install"}:
            raise SandboxValidationError(f"不支持的校验类型: {check}")

        container = self._get_container(sandbox_id)
        workdir = self._resolve_workdir(sandbox_id, draft_id)
        timeout = self.settings.sandbox_verify_timeout_seconds
        # busybox timeout：超时返回 124，避免 exec_run 无限阻塞。
        if check == "install":
            # 全栈生成会新增后端依赖（例如 multer）：先装根项目，再装
            # server/，否则容器重启后 Node 会因 ERR_MODULE_NOT_FOUND 起不来。
            install_command = (
                "npm install --no-audit --no-fund && "
                "if [ -f server/package.json ]; then "
                "(cd server && npm install --no-audit --no-fund); fi"
            )
            command = f"timeout {timeout}s sh -c {shlex.quote(install_command)}"
        else:
            command = f"timeout {timeout}s npm run {check}"

        started = time.monotonic()
        try:
            exit_code, output = self._exec(
                container,
                ["sh", "-lc", command],
                workdir=workdir,
                demux=True,
            )
        except DockerException as exc:
            raise SandboxError(f"执行 {check} 失败: {exc}") from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout, stderr = output if isinstance(output, tuple) else (output or b"", b"")
        raw_logs = self._decode(stdout)
        if isinstance(stderr, bytes) and stderr:
            raw_logs = f"{raw_logs}\n{self._decode(stderr)}"

        logs, truncated = self._truncate_logs(raw_logs)
        return VerifyResult(
            success=exit_code == 0,
            exit_code=exit_code,
            timed_out=exit_code == 124,
            duration_ms=duration_ms,
            logs=logs,
            logs_truncated=truncated,
        )

    def read_file(
        self,
        sandbox_id: str,
        path: str,
        draft_id: str | None = None,
    ) -> str:
        """读取沙盒内文本文件。

        Agent 在每次 apply 之后重新读取目标文件，确保 current_code 与沙盒
        真实状态一致（尤其适用于 unified diff 场景）。传入 ``draft_id``
        时读取草稿 worktree，审批前绝不触碰主工作树。
        """
        container = self._get_container(sandbox_id)
        workdir = self._resolve_workdir(sandbox_id, draft_id)
        rel_path = self._normalize_path(path)
        code, output = self._exec(
            container,
            ["sh", "-lc", f"cat {shlex.quote(rel_path)}"],
            workdir=workdir,
        )
        if code != 0:
            raise SandboxValidationError(
                f"读取沙盒文件失败 {rel_path}: {self._decode(output)}"
            )
        return self._decode(output)

    def export_archive(self, sandbox_id: str) -> bytes:
        """在容器内把当前项目打成 tar.gz，并把字节流返回给上层。

        - 用 `-czf -` 直接写到 stdout，不在沙盒工作目录留下临时压缩包；
        - 排除 node_modules / .git / dist：依赖与构建产物可通过
          `npm install && npm run dev` 重建，导出包保持可移植的最小体积；
        - 保留 package-lock.json，保证解压后依赖版本与沙盒一致。
        """
        container = self._get_container(sandbox_id)
        command = (
            "tar -czf - " "--exclude=node_modules " "--exclude=.git " "--exclude=dist ."
        )
        try:
            exit_code, output = self._exec(
                container,
                ["sh", "-lc", command],
                workdir=self.settings.sandbox_workdir,
            )
        except DockerException as exc:
            raise SandboxError(f"导出项目失败: {exc}") from exc

        if exit_code != 0:
            raise SandboxError(f"导出项目失败: {self._decode(output)}")

        data = output if isinstance(output, bytes) else self._decode(output).encode()
        if not data:
            raise SandboxError("导出项目失败: 压缩包为空")

        limit = self.settings.sandbox_max_export_bytes
        if len(data) > limit:
            raise SandboxValidationError(
                f"导出包超过大小限制（{len(data)} > {limit} 字节）"
            )
        return data

    def destroy(self, sandbox_id: str) -> None:
        # 先回收草稿：worktree 的 git 元数据与预览进程都在容器内，
        # 容器删除后无法再清理，会在宿主留下失联的 worktree 记录。
        with self._lock:
            draft_ids = [
                draft_id
                for draft_id, meta in self._drafts.items()
                if meta.get("sandbox_id") == sandbox_id
            ]
        for draft_id in draft_ids:
            try:
                self.discard_draft(sandbox_id, draft_id)
            except SandboxError:
                logger.warning(
                    "discard draft before destroy failed sandbox=%s draft=%s",
                    sandbox_id,
                    draft_id,
                    exc_info=True,
                )

        container = self._get_container(sandbox_id)
        try:
            container.remove(force=True)
        except NotFound:
            pass
        except DockerException as exc:
            raise SandboxError(f"销毁沙盒失败: {exc}") from exc

        with self._lock:
            self._sandboxes.pop(sandbox_id, None)

    # --------------------------------------------------------
    # 草稿隔离（Phase 0）
    # --------------------------------------------------------
    def begin_draft(self, sandbox_id: str) -> SandboxDraft:
        """从当前 HEAD 创建 worktree 影子目录，并启动第二预览服务。

        主工作树在此方法返回后不再接受任何写入：Applier 必须携带
        ``draft_id`` 才能落到草稿目录。草稿预览使用容器内 5174 端口，
        与主预览 5173 完全独立。
        """
        container = self._get_container(sandbox_id)
        workdir = self.settings.sandbox_workdir
        self._ensure_git_repo(container)

        # 进程重启后草稿登记表为空，旧 worktree 会继续占用 5174 草稿预览
        # 端口，导致新 Run 的预览起不来。创建新草稿前主动回收同沙盒的
        # 残留草稿（不区分重启前后，仅保留 60 秒并发宽限）。
        try:
            with self._git_lock:
                self._reclaim_drafts(
                    container,
                    sandbox_id,
                    cutoff=time.time() - DRAFT_RECLAIM_GRACE_SECONDS,
                )
        except (DockerException, SandboxError):
            logger.warning(
                "reclaim stale drafts before begin failed sandbox=%s",
                sandbox_id,
                exc_info=True,
            )

        # 同一沙盒只有一个草稿预览端口；存在未完成草稿时再开新草稿会
        # 抢 5174 并让前一个审批的 iframe 失效。必须显式收敛，不能静默覆盖。
        with self._lock:
            active = [
                draft_id
                for draft_id, meta in self._drafts.items()
                if meta.get("sandbox_id") == sandbox_id
            ]
        if active:
            raise SandboxError(
                "沙盒存在未完成的隔离草稿，请先完成或取消该审批: "
                + ", ".join(sorted(active))
            )

        with self._git_lock:
            code, output = self._exec(
                container,
                ["git", "rev-parse", "HEAD"],
                workdir=workdir,
            )
            if code != 0:
                raise SandboxError(f"读取基准提交失败: {self._decode(output)}")
            base_sha = self._extract_commit_hash(self._decode(output))
            if not base_sha:
                raise SandboxError("读取基准提交失败: 未返回合法 SHA")

            # 未提交的已跟踪文件不会进入 worktree；若存在，说明上一轮
            # 没有正常收口，继续创建草稿会让审批结果与用户所见不一致。
            code, output = self._exec(
                container,
                ["git", "status", "--porcelain", "--untracked-files=no"],
                workdir=workdir,
            )
            if code != 0:
                raise SandboxError(f"检查主工作树状态失败: {self._decode(output)}")
            dirty = self._decode(output).strip()
            if dirty:
                raise SandboxError(
                    "主工作树存在未提交修改，无法创建隔离草稿。"
                    f"请先处理以下文件:\n{dirty[:1000]}"
                )

            draft_id = f"drf_{uuid.uuid4().hex[:20]}"
            draft_path = self._draft_workdir(draft_id)

            # --detach：草稿不占用分支名，promote 时以 commit SHA 快进主分支。
            command = (
                f"rm -rf -- {shlex.quote(draft_path)} && "
                f"mkdir -p -- {shlex.quote(self.settings.sandbox_draft_root)} && "
                f"git worktree add --detach {shlex.quote(draft_path)} "
                f"{shlex.quote(base_sha)}"
            )
            code, output = self._exec(
                container, ["sh", "-lc", command], workdir=workdir
            )
            if code != 0:
                raise SandboxError(f"创建草稿 worktree 失败: {self._decode(output)}")

            # 依赖目录不复制：软链回主工作树，节省空间并保持版本一致。
            if (
                self._exec(
                    container,
                    [
                        "sh",
                        "-lc",
                        f"[ -d {shlex.quote(workdir + '/node_modules')} ] && "
                        f"ln -sfn {shlex.quote(workdir + '/node_modules')} "
                        f"{shlex.quote(draft_path + '/node_modules')} || true",
                    ],
                )[0]
                != 0
            ):
                logger.warning("link node_modules for draft failed")

        try:
            draft_host_port = self._published_port(
                container, self.settings.sandbox_draft_port
            )
            self._start_draft_preview(
                container,
                draft_id,
                draft_path,
                draft_host_port,
            )
        except SandboxError:
            # 预览起不来时回滚 worktree，避免留下无法审批的“孤儿草稿”。
            self._remove_worktree(container, draft_path)
            raise

        draft = SandboxDraft(
            draft_id=draft_id,
            sandbox_id=sandbox_id,
            base_sha=base_sha,
            workdir=draft_path,
            preview_url=(
                f"http://{self.settings.sandbox_preview_host}:{draft_host_port}"
                f"/?draft={draft_id}"
            ),
            host_port=draft_host_port,
            created_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._drafts[draft_id] = {
                "sandbox_id": sandbox_id,
                "workdir": draft_path,
                "base_sha": base_sha,
                "host_port": draft_host_port,
                "preview_url": draft.preview_url,
            }
        logger.info(
            "draft created sandbox=%s draft=%s base=%s preview=%s",
            sandbox_id,
            draft_id,
            base_sha[:12],
            draft.preview_url,
        )
        return draft

    def discard_draft(self, sandbox_id: str, draft_id: str) -> None:
        """停止草稿预览并移除 worktree；主工作树不做任何修改。"""
        with self._lock:
            meta = self._drafts.get(draft_id)
        if meta is None or meta.get("sandbox_id") != sandbox_id:
            # 幂等：审批取消 / 失败清理可能重复调用。
            logger.info("draft already gone sandbox=%s draft=%s", sandbox_id, draft_id)
            return

        container = self._get_container(sandbox_id)
        self._stop_draft_preview(container, draft_id)
        self._remove_worktree(container, str(meta["workdir"]))
        with self._lock:
            self._drafts.pop(draft_id, None)
        logger.info("draft discarded sandbox=%s draft=%s", sandbox_id, draft_id)

    def promote_draft(
        self,
        sandbox_id: str,
        draft_id: str,
        expected_base_sha: str,
    ) -> str:
        """审批通过后把草稿快进合并到主工作树。

        安全边界：
          - 主 HEAD 必须仍是草稿创建时的 ``expected_base_sha``；
          - 草稿必须仍以该提交为祖先；
          - 只允许 ``--ff-only`` 合并，绝不产生冲突合并提交。
        """
        with self._lock:
            meta = self._drafts.get(draft_id)
        if meta is None or meta.get("sandbox_id") != sandbox_id:
            raise SandboxError(f"草稿不存在或已失效: {draft_id}")

        container = self._get_container(sandbox_id)
        workdir = self.settings.sandbox_workdir
        draft_path = str(meta["workdir"])

        with self._git_lock:
            code, output = self._exec(
                container, ["git", "rev-parse", "HEAD"], workdir=workdir
            )
            current_sha = self._extract_commit_hash(self._decode(output))
            if code != 0 or not current_sha:
                raise SandboxError(f"读取主工作树 HEAD 失败: {self._decode(output)}")
            if current_sha != expected_base_sha:
                raise SandboxError(
                    "主工作树在审批期间已变化，拒绝合并草稿。"
                    f"expected={expected_base_sha[:12]} actual={current_sha[:12]}"
                )

            # 先停预览，避免合并瞬间 Vite 正在读取草稿文件。
            self._stop_draft_preview(container, draft_id)

            name, email = DRAFT_GIT_USER
            commit_command = (
                "git add -A && "
                f"git -c user.name={shlex.quote(name)} "
                f"-c user.email={shlex.quote(email)} "
                f"commit --allow-empty -m {shlex.quote('draft: ' + draft_id)} && "
                "git rev-parse HEAD"
            )
            code, output = self._exec(
                container, ["sh", "-lc", commit_command], workdir=draft_path
            )
            draft_sha = self._extract_commit_hash(self._decode(output))
            if code != 0 or not draft_sha:
                raise SandboxError(f"提交草稿失败: {self._decode(output)}")

            # 草稿必须仍以基准提交为祖先，防止 worktree 被外部改写。
            code, _ = self._exec(
                container,
                ["git", "merge-base", "--is-ancestor", expected_base_sha, draft_sha],
                workdir=draft_path,
            )
            if code != 0:
                raise SandboxError("草稿历史与基准提交不一致，拒绝合并")

            code, output = self._exec(
                container,
                ["git", "merge", "--ff-only", draft_sha],
                workdir=workdir,
            )
            if code != 0:
                raise SandboxError(f"草稿合并失败: {self._decode(output)}")

            code, output = self._exec(
                container, ["git", "rev-parse", "HEAD"], workdir=workdir
            )
            new_head = self._extract_commit_hash(self._decode(output))
            if code != 0 or not new_head:
                raise SandboxError(f"读取合并后 HEAD 失败: {self._decode(output)}")

            self._remove_worktree(container, draft_path, remove_dir=False)
            with self._lock:
                self._drafts.pop(draft_id, None)

        logger.info(
            "draft promoted sandbox=%s draft=%s base=%s head=%s",
            sandbox_id,
            draft_id,
            expected_base_sha[:12],
            new_head[:12],
        )
        return new_head

    def reap_orphan_drafts(self, ttl_days: int | None = None) -> list[str]:
        """回收超过 TTL 的孤儿草稿（worktree + 草稿预览进程）。

        判定标准（三者必须同时满足）：
          - 容器内 ``/app-drafts`` 存在合法 draft 目录；
          - 当前进程 ``_drafts`` 未登记（重启后登记表为空，即孤儿）；
          - 目录 mtime 早于 TTL（给人工排障保留现场）。

        任何单个沙盒 / 草稿失败都不能中断整轮回收；异常降级为日志，
        下次周期继续尝试。
        """
        ttl = (
            self.settings.sandbox_draft_orphan_ttl_days
            if ttl_days is None
            else ttl_days
        )
        if ttl <= 0:
            logger.info("orphan draft gc skipped: ttl_days=%s", ttl)
            return []

        now = time.time()
        cutoff = now - ttl * 86400
        reclaimed: list[str] = []

        try:
            containers = self._require_client().containers.list(
                filters={"label": f"{LABEL_MANAGED}=true"}
            )
        except DockerException:
            logger.warning("orphan draft gc skipped: docker unavailable", exc_info=True)
            return []

        for container in containers:
            labels = getattr(container, "labels", None) or {}
            sandbox_id = labels.get(LABEL_SANDBOX_ID, "")
            if not sandbox_id:
                continue

            try:
                reclaimed.extend(
                    self._reclaim_drafts(
                        container,
                        sandbox_id,
                        cutoff=cutoff,
                    )
                )
            except (DockerException, SandboxError):
                logger.warning(
                    "orphan draft gc failed sandbox=%s",
                    sandbox_id,
                    exc_info=True,
                )

        return reclaimed

    def _reclaim_drafts(
        self,
        container: Any,
        sandbox_id: str,
        *,
        cutoff: float | None,
    ) -> list[str]:
        """回收指定沙盒中未被当前进程登记的草稿。

        ``cutoff`` 为 None 表示不看年龄（调用方已确认这些草稿不可能
        再被任何活跃 Run 引用）；否则只回收 mtime 早于 cutoff 的目录。
        """
        candidates = self._list_draft_dirs(container)
        with self._lock:
            active = {
                draft_id
                for draft_id, meta in self._drafts.items()
                if meta.get("sandbox_id") == sandbox_id
            }

        reclaimed: list[str] = []
        for draft_id, mtime in candidates:
            # 活跃审批优先级最高：即使超过 TTL 也绝不清理。
            if draft_id in active:
                continue
            if cutoff is not None and mtime > cutoff:
                continue
            draft_path = f"{self.settings.sandbox_draft_root}/{draft_id}"
            try:
                self._stop_draft_preview(container, draft_id)
                self._remove_worktree(container, draft_path)
            except (DockerException, SandboxError):
                logger.warning(
                    "orphan draft reclaim failed sandbox=%s draft=%s",
                    sandbox_id,
                    draft_id,
                    exc_info=True,
                )
                continue
            with self._lock:
                self._drafts.pop(draft_id, None)
            reclaimed.append(draft_id)
            logger.info(
                "orphan draft reclaimed sandbox=%s draft=%s age_days=%.1f",
                sandbox_id,
                draft_id,
                (time.time() - mtime) / 86400,
            )

        return reclaimed

    def _list_draft_dirs(self, container: Any) -> list[tuple[str, float]]:
        """列出容器内合法草稿目录及其 mtime 时间戳。

        使用目录 mtime 而不是任何外部记录：草稿元数据是进程内存态，
        重启后唯一可信的“年龄”来源就是文件系统本身。命令只拼接配置中
        的根目录（shlex 转义），draft_id 在 Python 侧用正则二次校验。
        """
        root = shlex.quote(self.settings.sandbox_draft_root)
        command = (
            f"for d in {root}/drf_*; do "
            '[ -d "$d" ] || continue; '
            "n=${d##*/}; "
            't=$(stat -c %Y "$d" 2>/dev/null || echo 0); '
            'echo "$n $t"; '
            "done"
        )
        code, output = self._exec(
            container,
            ["sh", "-lc", command],
            workdir=self.settings.sandbox_workdir,
        )
        if code != 0:
            raise SandboxError(f"列出草稿目录失败: {self._decode(output)}")

        entries: list[tuple[str, float]] = []
        for line in self._decode(output).splitlines():
            name, _, raw_mtime = line.strip().partition(" ")
            if not DRAFT_ID_RE.fullmatch(name) or not raw_mtime.isdigit():
                continue
            entries.append((name, float(raw_mtime)))
        return entries

    def _draft_workdir(self, draft_id: str) -> str:
        """草稿目录必须由服务端 ID 推导，禁止调用方传入任意路径。"""
        if not DRAFT_ID_RE.fullmatch(draft_id):
            raise SandboxValidationError(f"非法草稿 ID: {draft_id}")
        return f"{self.settings.sandbox_draft_root}/{draft_id}"

    def _resolve_workdir(self, sandbox_id: str, draft_id: str | None) -> str:
        """把可选的草稿 ID 解析为真实工作目录。

        未传 draft_id 时保持旧行为（主工作树）；传入时必须命中本 Provider
        登记的活跃草稿，防止任意路径写入。
        """
        if not draft_id:
            return self.settings.sandbox_workdir
        with self._lock:
            meta = self._drafts.get(draft_id)
        if meta is None or meta.get("sandbox_id") != sandbox_id:
            raise SandboxError(f"草稿不存在或已失效: {draft_id}")
        return str(meta["workdir"])

    def _published_port(self, container: Any, internal_port: int) -> int:
        """读取容器启动时发布的宿主端口；旧沙盒没有草稿端口时给出明确错误。"""
        try:
            container.reload()
            attrs = container.attrs
        except DockerException as exc:
            raise SandboxError(f"读取沙盒端口失败: {exc}") from exc
        ports = (attrs.get("NetworkSettings") or {}).get("Ports") or {}
        bindings = ports.get(f"{internal_port}/tcp") or []
        if not bindings:
            raise SandboxError(
                f"沙盒未发布草稿预览端口 {internal_port}；"
                "该沙盒创建于草稿隔离上线前，请重新创建沙盒后重试。"
            )
        try:
            return int(bindings[0]["HostPort"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SandboxError(f"草稿预览端口映射异常: {bindings}") from exc

    def _start_draft_preview(
        self,
        container: Any,
        draft_id: str,
        draft_path: str,
        draft_host_port: int,
    ) -> None:
        """在草稿目录启动第二个预览进程，并等待端口就绪。"""
        vite_bin = f"{draft_path}/node_modules/.bin/vite"
        draft_port = self.settings.sandbox_draft_port
        log_path = f"/tmp/vibe-draft-{draft_id}.log"
        pid_path = f"/tmp/vibe-draft-{draft_id}.pid"
        # Vite 项目优先；纯静态导入项目回退到镜像内置静态服务器。
        command = (
            f"cd {shlex.quote(draft_path)} && "
            f"if [ -x {shlex.quote(vite_bin)} ]; then "
            f"nohup env VITE_HMR_CLIENT_PORT={draft_host_port} "
            f"{shlex.quote(vite_bin)} --host 0.0.0.0 --port {draft_port} "
            f"--strictPort </dev/null >{shlex.quote(log_path)} 2>&1 & "
            "else "
            "nohup node /opt/vibe-static-server.mjs "
            f"{shlex.quote(draft_path)} {draft_port} "
            f"</dev/null >{shlex.quote(log_path)} 2>&1 & "
            "fi; "
            f"echo $! > {shlex.quote(pid_path)}"
        )
        code, output = self._exec(container, ["sh", "-lc", command], workdir=draft_path)
        if code != 0:
            raise SandboxError(f"启动草稿预览失败: {self._decode(output)}")

        try:
            self._wait_port_ready(
                container,
                draft_port,
                timeout=self.settings.sandbox_draft_start_timeout_seconds,
                log_path=log_path,
                label=f"草稿 {draft_id}",
            )
        except SandboxStartupError as exc:
            raise SandboxError(str(exc)) from exc

    def _stop_draft_preview(self, container: Any, draft_id: str) -> None:
        if not DRAFT_ID_RE.fullmatch(draft_id):
            return
        pid_path = f"/tmp/vibe-draft-{draft_id}.pid"
        command = (
            f"if [ -f {shlex.quote(pid_path)} ]; then "
            f"kill $(cat {shlex.quote(pid_path)}) 2>/dev/null || true; "
            f"rm -f {shlex.quote(pid_path)}; fi"
        )
        try:
            self._exec(container, ["sh", "-lc", command])
        except DockerException:
            logger.warning(
                "stop draft preview failed draft=%s", draft_id, exc_info=True
            )

    def _remove_worktree(
        self,
        container: Any,
        draft_path: str,
        *,
        remove_dir: bool = True,
    ) -> None:
        workdir = self.settings.sandbox_workdir
        # worktree remove 会先检查未提交修改；--force 保证失败 Run 也能回收。
        command = (
            f"git worktree remove --force {shlex.quote(draft_path)} "
            "2>/dev/null || true; "
            f"git worktree prune; "
            + (f"rm -rf -- {shlex.quote(draft_path)}; " if remove_dir else "")
            + "true"
        )
        try:
            self._exec(container, ["sh", "-lc", command], workdir=workdir)
        except DockerException:
            logger.warning("remove worktree failed path=%s", draft_path, exc_info=True)

    def _wait_port_ready(
        self,
        container: Any,
        port: int,
        *,
        timeout: int,
        log_path: str = "",
        label: str = "",
    ) -> None:
        """通用端口探活；日志只在该端口启动失败时读取。"""
        deadline = time.monotonic() + timeout
        probe = f"wget -q -T 2 -O /dev/null http://127.0.0.1:{port}/"
        while time.monotonic() < deadline:
            container.reload()
            if container.status != "running":
                break
            code, _ = self._exec(container, ["sh", "-lc", probe])
            if code == 0:
                return
            time.sleep(0.4)

        logs = ""
        if log_path:
            try:
                code, output = self._exec(
                    container, ["sh", "-lc", f"tail -40 {shlex.quote(log_path)}"]
                )
                if code == 0:
                    logs = self._decode(output)
            except DockerException:
                pass
        raise SandboxStartupError(
            f"{label or '草稿'}预览启动超时（端口 {port}）。日志:\n{logs[-3000:]}"
        )

    # --------------------------------------------------------
    # Docker 状态（供健康检查展示，不参与 UP/DEGRADED 判定）
    # --------------------------------------------------------
    def docker_version(self) -> str:
        if self._client is None:
            return "UNAVAILABLE"
        try:
            self._client.ping()
            return str(self._client.version().get("Version", "unknown"))
        except DockerException:
            return "UNAVAILABLE"

    def image_state(self) -> str:
        if self._client is None:
            return "UNAVAILABLE"
        try:
            self._client.images.get(self.settings.sandbox_image)
            return "PRESENT"
        except ImageNotFound:
            return "MISSING"
        except DockerException:
            return "UNAVAILABLE"

    def _require_client(self) -> docker.DockerClient:
        if self._client is None:
            raise SandboxStartupError(
                f"Docker 不可用，请检查 {self.settings.docker_host} 挂载与权限: "
                f"{self._client_error}"
            )
        return self._client

    # --------------------------------------------------------
    # 镜像
    # --------------------------------------------------------
    def _ensure_image(self) -> None:
        """镜像不存在时，基于 sandbox_template 自动构建。

        构建上下文位于 python-ai 容器内的 /app/sandbox_template，
        由 Docker SDK 打包后发送给宿主 Daemon，无需挂载宿主路径。
        """
        client = self._require_client()
        try:
            client.images.get(self.settings.sandbox_image)
            return
        except ImageNotFound:
            pass
        except DockerException as exc:
            raise SandboxStartupError(f"Docker 不可用: {exc}") from exc

        with self._image_lock:
            # 双检：等待锁期间其他请求可能已完成构建。
            try:
                client.images.get(self.settings.sandbox_image)
                return
            except ImageNotFound:
                pass

            context_dir = Path(self.settings.sandbox_template_dir)
            if not context_dir.is_dir():
                raise SandboxStartupError(
                    f"沙盒模板目录不存在: {context_dir}；请检查镜像构建上下文"
                )

            try:
                for chunk in client.api.build(
                    path=str(context_dir),
                    tag=self.settings.sandbox_image,
                    rm=True,
                    decode=True,
                ):
                    if "error" in chunk:
                        raise SandboxStartupError(f"构建沙盒镜像失败: {chunk['error']}")
            except DockerException as exc:
                raise SandboxStartupError(f"构建沙盒镜像失败: {exc}") from exc

    # --------------------------------------------------------
    # 容器查找
    # --------------------------------------------------------
    def _get_container(self, sandbox_id: str):
        client = self._require_client()
        with self._lock:
            container_id = self._sandboxes.get(sandbox_id)

        if container_id:
            try:
                container = client.containers.get(container_id)
                if container.status not in {"running", "created"}:
                    raise SandboxNotFoundError(f"沙盒未运行: {sandbox_id}")
                return container
            except NotFound:
                with self._lock:
                    self._sandboxes.pop(sandbox_id, None)
                raise SandboxNotFoundError(f"沙盒不存在: {sandbox_id}")

        # 进程重启后的恢复路径：通过标签查找。
        containers = client.containers.list(
            all=True,
            filters={"label": f"{LABEL_SANDBOX_ID}={sandbox_id}"},
        )
        if not containers:
            raise SandboxNotFoundError(f"沙盒不存在: {sandbox_id}")

        container = containers[0]
        if container.status not in {"running", "created"}:
            raise SandboxNotFoundError(f"沙盒未运行: {sandbox_id}")
        with self._lock:
            self._sandboxes[sandbox_id] = container.id
        return container

    # --------------------------------------------------------
    # 文件写入
    # --------------------------------------------------------
    def _put_files(
        self,
        container: Any,
        files: dict[str, str],
        workdir: str | None = None,
    ) -> None:
        archive = self._build_tar(files)
        target = workdir or self.settings.sandbox_workdir
        try:
            ok = container.put_archive(target, archive)
        except DockerException as exc:
            raise SandboxError(f"写入沙盒文件失败: {exc}") from exc
        if not ok:
            raise SandboxError("写入沙盒文件失败: put_archive 返回 False")

    def _put_archive_bytes(self, container: Any, files: dict[str, bytes]) -> None:
        """写入二进制安全的导入文件；只允许沙盒工作目录内的相对路径。"""
        normalized: dict[str, bytes] = {}
        for raw_path, content in files.items():
            rel_path = self._normalize_path(raw_path)
            if not isinstance(content, (bytes, bytearray)):
                raise SandboxValidationError(f"导入文件必须是 bytes: {rel_path}")
            normalized[rel_path] = bytes(content)
        archive = self._build_tar_bytes(normalized)
        try:
            ok = container.put_archive(self.settings.sandbox_workdir, archive)
        except DockerException as exc:
            raise SandboxError(f"写入导入文件失败: {exc}") from exc
        if not ok:
            raise SandboxError("写入导入文件失败: put_archive 返回 False")

    def _apply_unified_diff(
        self,
        container: Any,
        patch: str,
        workdir: str | None = None,
    ) -> None:
        """应用 LLM 生成的 unified diff。

        ``--recount`` 是关键：模型经常把 hunk 头的行数写错（例如插入 35 行
        却声明 ``@@ -893,6 +893,24 @@``），严格的 ``git apply`` 会直接报
        ``corrupt patch``，导致代码生成明明正确却写不进沙盒。--recount 让
        Git 忽略 hunk 头里的计数、按实际 +/-/空格 行重新统计；行号偏移
        （模型从裁剪窗口的行号起算）仍由 git apply 的上下文搜索自动容忍。
        """
        patch_archive = self._build_tar({"vibe.patch.diff": patch})
        try:
            container.put_archive("/tmp", patch_archive)
            code, output = self._exec(
                container,
                [
                    "sh",
                    "-lc",
                    "git apply --recount --whitespace=nowarn /tmp/vibe.patch.diff",
                ],
                workdir=workdir or self.settings.sandbox_workdir,
            )
        except DockerException as exc:
            raise SandboxPatchError(f"应用补丁失败: {exc}") from exc

        if code != 0:
            raise SandboxPatchError(f"应用补丁失败: {self._decode(output)}")

    # --------------------------------------------------------
    # 写盘前 AST 门禁
    # --------------------------------------------------------
    def _preflight_code_check(
        self,
        container: Any,
        files: dict[str, str] | None,
        patch: str | None,
        workdir: str | None = None,
    ) -> None:
        """在临时副本中预演变更并执行 AST 检查。

        关键约束：本方法只能读 /app，绝不能写 /app。Vite 监听的是真实
        工作目录，只要坏代码没有落到那里，HMR 就不会被触发，页面也不会
        白屏；失败时沙盒自然停留在上一个可运行的 commit。
        """
        if not files and not patch:
            return
        if not self._ensure_ast_checker(container):
            # 检查器缺失属于环境异常（例如极老的导入镜像）。此时放行并
            # 记录告警，避免整个平台因为缺少检查器而无法写代码。
            logger.warning("AST checker missing, skip preflight scan")
            return

        stage = AST_STAGE_DIR
        workdir = workdir or self.settings.sandbox_workdir
        try:
            # 1. 准备临时工作目录。
            if patch:
                # 补丁上下文来自线上工作树，不能 checkout HEAD 预演：
                # 失败重试时线上可能存在尚未 commit 的代码。这里复制
                # 源码快照（排除依赖 / 构建产物），保证预演结果与真实
                # 应用完全一致。
                code, output = self._exec(
                    container,
                    [
                        "sh",
                        "-lc",
                        f"rm -rf {stage} && mkdir -p {stage} && "
                        f"tar -C {shlex.quote(workdir)} "
                        "--exclude=./node_modules --exclude=./dist "
                        "--exclude=./.git --exclude=./.vite -cf - . "
                        f"| tar -C {stage} -xf -",
                    ],
                )
                if code != 0:
                    raise SandboxPatchError(f"代码预检快照失败: {self._decode(output)}")
            else:
                code, output = self._exec(
                    container, ["sh", "-lc", f"rm -rf {stage} && mkdir -p {stage}"]
                )
                if code != 0:
                    raise SandboxPatchError(
                        f"代码预检目录创建失败: {self._decode(output)}"
                    )

            # 2. files 模式：把待写内容放进副本；patch 模式：先落 files
            #    再打补丁，和真实 apply_patch 的顺序保持一致。
            if files:
                container.put_archive(stage, self._build_tar(files))
            if patch:
                container.put_archive(
                    "/tmp", self._build_tar({"vibe-ast-stage.patch": patch})
                )
                code, output = self._exec(
                    container,
                    [
                        "sh",
                        "-lc",
                        f"git apply --recount --whitespace=nowarn {AST_STAGE_PATCH}",
                    ],
                    workdir=stage,
                )
                if code != 0:
                    # 补丁本身无法应用也在这里拦截：绝不把半份补丁写进
                    # 线上工作树。
                    raise SandboxPatchError(
                        "补丁预检失败（沙盒未写盘，仍停留在上一 commit）: "
                        f"{self._decode(output)}"
                    )

            # 3. 只检查本次真正变更的脚本文件，避免历史文件里的既有
            #    问题阻塞一次无关的写盘。
            script_paths: list[str] = []
            for rel_path in sorted(files or {}):
                if rel_path.lower().endswith(SCRIPT_SUFFIXES):
                    script_paths.append(f"{stage}/{rel_path}")
            for rel_path in self._patch_target_paths(patch or ""):
                if rel_path.lower().endswith(SCRIPT_SUFFIXES):
                    script_paths.append(f"{stage}/{rel_path}")
            script_paths = list(dict.fromkeys(script_paths))
            if not script_paths:
                return

            code, output = self._exec(
                container, ["node", AST_CHECK_SCRIPT, *script_paths]
            )
            logs = self._decode(output).strip()
            if code == 2:
                # 退出码 2 = 检查器自身无法加载 Babel，属于环境问题；
                # 与“代码不合法”（退出码 1）严格区分。
                logger.warning("AST checker unavailable: %s", logs[:500])
                return
            if code != 0:
                raise SandboxPatchError(
                    "代码静态检查未通过，已拦截写入（未触发 HMR，"
                    "沙盒保持在上一 commit）:\n" + logs
                )
        finally:
            # 清理由容器内 rm 完成，失败也不影响调用方。
            try:
                self._exec(
                    container, ["sh", "-lc", f"rm -rf {stage} {AST_STAGE_PATCH}"]
                )
            except DockerException:  # pragma: no cover - 容器已经消失
                pass

    def _ensure_ast_checker(self, container: Any) -> bool:
        """把仓库里的检查器推送到容器 /opt，兼容升级前创建的旧沙盒。"""
        candidates = [
            Path(self.settings.sandbox_template_dir) / "ast_check.mjs",
            Path(__file__).resolve().parents[3] / "sandbox_template" / "ast_check.mjs",
        ]
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            return False
        try:
            container.put_archive(
                "/opt",
                self._build_tar(
                    {"vibe-ast-check.mjs": source.read_text(encoding="utf-8")}
                ),
            )
        except DockerException:
            logger.warning("failed to inject AST checker", exc_info=True)
            return False
        return True

    # --------------------------------------------------------
    # 写盘前的实例作用域 AST 门禁
    # --------------------------------------------------------
    def ast_scope_check(
        self,
        sandbox_id: str,
        files: dict[str, str] | None,
        patch: str | None,
        scopes: list[dict],
        changed_files: list[str],
        draft_id: str | None = None,
    ) -> list[dict]:
        """用沙盒内的 Babel 对 before / after 做结构差分。

        只复制本次涉及的文件（变更文件 + scope 的源码 / callSite 文件），
        不复制 node_modules；before 目录取自线上工作树，因此能覆盖
        “上一轮尚未 commit 的代码”，与真实写盘结果一致。
        """
        if not scopes:
            return []
        container = self._get_container(sandbox_id)
        if not self._ensure_ast_scope_checker(container):
            raise SandboxError("实例 AST 检查器缺失，拒绝写入公共组件")

        workdir = self._resolve_workdir(sandbox_id, draft_id)
        stage = AST_SCOPE_STAGE_DIR
        before_root = f"{stage}/before"
        after_root = f"{stage}/after"
        paths: list[str] = []
        for raw in list(changed_files or []):
            try:
                normalized = self._normalize_path(raw)
            except SandboxValidationError:
                continue
            if normalized not in paths:
                paths.append(normalized)
        for scope in scopes:
            for key in ("file", "callSiteFile"):
                raw = str(scope.get(key) or "").strip()
                if not raw:
                    continue
                try:
                    normalized = self._normalize_path(raw)
                except SandboxValidationError:
                    continue
                if normalized not in paths:
                    paths.append(normalized)

        try:
            copy_commands = (
                " && ".join(
                    f"([ -f {shlex.quote(rel)} ] && "
                    f"cp --parents {shlex.quote(rel)} {shlex.quote(before_root)}/ || true)"
                    for rel in paths
                )
                or "true"
            )
            command = (
                f"rm -rf {shlex.quote(stage)} && "
                f"mkdir -p {shlex.quote(before_root)} {shlex.quote(after_root)} && "
                f"cd {shlex.quote(workdir)} && {copy_commands} && "
                f"cp -a {shlex.quote(before_root)}/. {shlex.quote(after_root)}/"
            )
            code, output = self._exec(container, ["sh", "-lc", command])
            if code != 0:
                raise SandboxError(f"实例 AST 预检快照失败: {self._decode(output)}")

            if files:
                container.put_archive(after_root, self._build_tar(files))
            if patch:
                container.put_archive(
                    "/tmp", self._build_tar({"vibe-scope-stage.patch": patch})
                )
                code, output = self._exec(
                    container,
                    [
                        "sh",
                        "-lc",
                        "git apply --recount --whitespace=nowarn "
                        f"{AST_SCOPE_STAGE_PATCH}",
                    ],
                    workdir=after_root,
                )
                if code != 0:
                    raise SandboxError(
                        "实例 AST 预检补丁应用失败: " + self._decode(output)
                    )

            spec = {
                "workdir": workdir,
                "beforeRoot": before_root,
                "afterRoot": after_root,
                "changedFiles": paths,
                "scopes": scopes,
            }
            container.put_archive(
                stage,
                self._build_tar({"spec.json": json.dumps(spec, ensure_ascii=False)}),
            )
            code, output = self._exec(
                container, ["node", AST_SCOPE_SCRIPT, f"{stage}/spec.json"]
            )
            logs = self._decode(output).strip()
            if code == 2:
                # 检查器自身异常：硬门禁必须 fail-closed。
                raise SandboxError(f"实例 AST 检查器异常: {logs[:1000]}")
            if code not in (0, 1):
                raise SandboxError(f"实例 AST 检查器返回未知状态 {code}: {logs[:1000]}")
            try:
                payload = json.loads(logs)
            except json.JSONDecodeError as exc:
                raise SandboxError(
                    f"实例 AST 检查器输出不是 JSON: {logs[:1000]}"
                ) from exc
            return list(payload.get("violations") or [])
        finally:
            try:
                self._exec(
                    container,
                    [
                        "sh",
                        "-lc",
                        f"rm -rf {shlex.quote(stage)} {AST_SCOPE_STAGE_PATCH}",
                    ],
                )
            except DockerException:  # pragma: no cover - 容器已经消失
                pass

    # --------------------------------------------------------
    # Phase 1：确定性目标解析 / 唯一写工具
    # --------------------------------------------------------
    def resolve_targets(
        self,
        sandbox_id: str,
        anchors: list[dict],
        draft_id: str | None = None,
    ) -> list[dict] | None:
        """调用沙盒内 Babel 解析器，把标注解析为精确 AST 锚点。

        Babel 只能在容器内运行（依赖来自沙盒 node_modules），因此这里
        沿用 AST 门禁的脚本注入 + 临时 stage 模式。脚本异常直接抛出，
        由 Resolver 节点决定降级或 fail-closed。
        """
        items = [item for item in anchors or [] if isinstance(item, dict)]
        if not items:
            return []
        container = self._get_container(sandbox_id)
        if not self._ensure_ast_script(
            container,
            "resolve_target.mjs",
            AST_RESOLVE_SCRIPT,
            "target resolver",
        ):
            raise SandboxError("目标解析器缺失，拒绝让模型猜测目标节点")
        workdir = self._resolve_workdir(sandbox_id, draft_id)
        payload = self._run_ast_tool(
            container,
            script=AST_RESOLVE_SCRIPT,
            spec={"workdir": workdir, "anchors": items},
            label="目标解析",
        )
        anchors_out = payload.get("anchors")
        if not isinstance(anchors_out, list):
            raise SandboxError("目标解析器返回格式非法: anchors 缺失")
        return [item for item in anchors_out if isinstance(item, dict)]

    def apply_ast_edit(
        self,
        sandbox_id: str,
        edit: dict,
        draft_id: str | None = None,
    ) -> dict | None:
        """执行一次 Babel 精确切片编辑，返回 ``{"status": ..., "content": ...}``。

        注意：本方法只计算不落盘。新增 / 修改 / 删除后的完整文件内容由
        Tool 返回给 Graph，再经 Applier 统一写入草稿，继续复用实例作用域
        门禁与审批隔离。
        """
        if not isinstance(edit, dict):
            raise SandboxError("AST 编辑请求必须是对象")
        try:
            file = self._normalize_path(str(edit.get("file") or ""))
        except SandboxValidationError as exc:
            raise SandboxError(f"AST 编辑目标文件非法: {exc}") from exc
        operation = str(edit.get("operation") or "").lower()
        if operation not in {"insert", "delete", "modify"}:
            raise SandboxError(f"不支持的 AST 编辑操作: {operation or '(空)'}")

        container = self._get_container(sandbox_id)
        if not self._ensure_ast_script(
            container,
            "apply_ast_edit.mjs",
            AST_EDIT_SCRIPT,
            "ast edit tool",
        ):
            raise SandboxError("AST 编辑工具缺失，拒绝写入")
        workdir = self._resolve_workdir(sandbox_id, draft_id)
        spec = {
            "workdir": workdir,
            "file": file,
            "operation": operation,
            "anchorRange": edit.get("anchorRange") or {},
            "targetRange": edit.get("targetRange") or {},
            "insertPosition": edit.get("insertPosition") or "after",
            # inside_index 的子节点序号；None 表示不适用。
            "insertIndex": edit.get("insertIndex"),
            "jsxCode": str(edit.get("jsxCode") or ""),
        }
        # 同文件多编辑：服务端把前一条结果作为下一条输入，绝不接受模型传入。
        override = edit.get("contentOverride")
        if isinstance(override, str):
            spec["contentOverride"] = override
        return self._run_ast_tool(
            container,
            script=AST_EDIT_SCRIPT,
            spec=spec,
            label="AST 编辑",
        )

    def _run_ast_tool(
        self,
        container: Any,
        *,
        script: str,
        spec: dict[str, Any],
        label: str,
    ) -> dict[str, Any]:
        """把 spec.json 推入容器并执行 Node 脚本，返回解析后的 JSON。

        错误约定与 AST 门禁一致：退出码 2 = 脚本异常（调用方 fail-closed）；
        其它非 0/2 视为未知错误。业务层错误放在 JSON 的 ``status`` 字段。
        """
        stage = AST_TOOL_STAGE_DIR
        try:
            code, output = self._exec(
                container,
                [
                    "sh",
                    "-lc",
                    f"rm -rf {shlex.quote(stage)} && mkdir -p {shlex.quote(stage)}",
                ],
            )
            if code != 0:
                raise SandboxError(f"{label}临时目录创建失败: {self._decode(output)}")
            container.put_archive(
                stage,
                self._build_tar(
                    {"spec.json": json.dumps(spec, ensure_ascii=False, default=str)}
                ),
            )
            code, output = self._exec(container, ["node", script, f"{stage}/spec.json"])
            logs = self._decode(output).strip()
            if code == 2:
                raise SandboxError(f"{label}执行器异常: {logs[:1000]}")
            if code != 0:
                raise SandboxError(f"{label}执行器返回未知状态 {code}: {logs[:1000]}")
            try:
                payload = json.loads(logs)
            except json.JSONDecodeError as exc:
                raise SandboxError(f"{label}输出不是 JSON: {logs[:1000]}") from exc
            if not isinstance(payload, dict):
                raise SandboxError(f"{label}输出结构非法")
            return payload
        finally:
            try:
                self._exec(
                    container,
                    ["sh", "-lc", f"rm -rf {shlex.quote(stage)}"],
                )
            except DockerException:  # pragma: no cover - 容器已经消失
                pass

    def _ensure_ast_script(
        self,
        container: Any,
        filename: str,
        target: str,
        label: str,
    ) -> bool:
        """把 sandbox_template 下的 Node 脚本注入容器 /opt。

        镜像可能早于脚本发布（升级前创建的沙盒），因此每次调用都重新
        推送；put_archive 覆盖同名文件是幂等的。
        """
        candidates = [
            Path(self.settings.sandbox_template_dir) / filename,
            Path(__file__).resolve().parents[3] / "sandbox_template" / filename,
        ]
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            logger.warning("%s script not found: %s", label, filename)
            return False
        try:
            container.put_archive(
                "/opt",
                self._build_tar(
                    {Path(target).name: source.read_text(encoding="utf-8")}
                ),
            )
        except DockerException:
            logger.warning("failed to inject %s", label, exc_info=True)
            return False
        return True

    def _ensure_ast_scope_checker(self, container: Any) -> bool:
        """把实例作用域检查器推送到容器 /opt，兼容旧沙盒。"""
        candidates = [
            Path(self.settings.sandbox_template_dir) / "ast_scope_check.mjs",
            Path(__file__).resolve().parents[3]
            / "sandbox_template"
            / "ast_scope_check.mjs",
        ]
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            return False
        try:
            container.put_archive(
                "/opt",
                self._build_tar(
                    {"vibe-ast-scope-check.mjs": source.read_text(encoding="utf-8")}
                ),
            )
        except DockerException:
            logger.warning("failed to inject AST scope checker", exc_info=True)
            return False
        return True

    @staticmethod
    def _patch_target_paths(patch: str) -> list[str]:
        """从 unified diff 提取受影响的目标文件（删除文件跳过）。"""
        paths: list[str] = []
        for raw in PATCH_TARGET_RE.findall(patch):
            value = raw.split("\t", 1)[0].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            if not value or value == "/dev/null":
                continue
            if value.startswith("b/"):
                value = value[2:]
            pure = PurePosixPath(value)
            # 防御路径穿越：预检副本的路径必须收敛在 stage 内。
            if pure.is_absolute() or ".." in pure.parts:
                continue
            paths.append(str(pure))
        return paths

    def _build_tar(self, files: dict[str, str]) -> bytes:
        """把相对路径文本文件打成 Docker put_archive 需要的 tar。"""
        return self._build_tar_bytes(
            {path: content.encode("utf-8") for path, content in files.items()}
        )

    def _build_tar_bytes(self, files: dict[str, bytes]) -> bytes:
        """二进制安全 tar；导入项目可能包含图片 / 字体等非 UTF-8 文件。"""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for rel_path, data in files.items():
                self._add_parent_dirs(tar, rel_path)
                info = tarfile.TarInfo(name=rel_path)
                info.size = len(data)
                info.mode = 0o644
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    @staticmethod
    def _add_parent_dirs(tar: tarfile.TarFile, rel_path: str) -> None:
        parts = PurePosixPath(rel_path).parts[:-1]
        for index in range(1, len(parts) + 1):
            dir_name = "/".join(parts[:index])
            info = tarfile.TarInfo(name=dir_name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = int(time.time())
            tar.addfile(info)

    # --------------------------------------------------------
    # 校验
    # --------------------------------------------------------
    def _validate_files(self, files: dict[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        total = 0
        for raw_path, content in files.items():
            rel_path = self._normalize_path(raw_path)
            if not isinstance(content, str):
                raise SandboxValidationError(f"文件内容必须是字符串: {rel_path}")
            size = len(content.encode("utf-8"))
            if size > self.settings.sandbox_max_file_bytes:
                raise SandboxValidationError(f"单文件超过大小限制: {rel_path}")
            total += size
            result[rel_path] = content

        if total > self.settings.sandbox_max_total_bytes:
            raise SandboxValidationError("单次请求文件总体积超过限制")
        return result

    @staticmethod
    def _normalize_path(raw_path: str) -> str:
        if not raw_path or "\x00" in raw_path or len(raw_path) > 512:
            raise SandboxValidationError("非法文件路径")
        path = PurePosixPath(raw_path)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise SandboxValidationError(f"文件路径必须位于沙盒工作目录内: {raw_path}")
        return str(path)

    # --------------------------------------------------------
    # 进程 / 端口 / 日志工具
    # --------------------------------------------------------
    def _wait_until_ready(
        self,
        container: Any,
        sandbox_id: str,
        timeout: int | None = None,
        ready_path: str = "/",
    ) -> None:
        """轮询容器内开发服务器，确认目标路径已真正可用。

        ``ready_path="/api/health"`` 时，请求会经过 Vite 的 ``/api`` 代理，
        同时验证前端、代理与 Node 后端三者都就绪。
        """
        deadline = time.monotonic() + (
            timeout or self.settings.sandbox_start_timeout_seconds
        )
        path = ready_path if ready_path.startswith("/") else f"/{ready_path}"
        probe = (
            "wget -q -T 2 -O /dev/null "
            f"http://127.0.0.1:{self.settings.sandbox_dev_port}{path}"
        )
        while time.monotonic() < deadline:
            container.reload()
            if container.status != "running":
                break
            code, _ = self._exec(container, ["sh", "-lc", probe])
            if code == 0:
                return
            time.sleep(0.4)

        logs = ""
        try:
            logs = self._decode(container.logs(tail=80))
        except DockerException:
            pass
        raise SandboxStartupError(
            f"沙盒 {sandbox_id} 开发服务器启动超时。容器日志:\n{logs}"
        )

    def _reserve_host_port(self) -> int:
        """选一个宿主端口并绑定到 127.0.0.1。

        注意：python-ai 在容器内选端口，只作为候选；真正的冲突由
        Docker 在 create 时检测。端口冲突会由 create 的 APIError 捕获。
        """
        for _ in range(20):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            with self._lock:
                if port not in self._reserved_ports:
                    return port
        raise SandboxStartupError("无法分配可用的宿主端口")

    def _release_host_port(self, port: int) -> None:
        with self._lock:
            self._reserved_ports.discard(port)

    @staticmethod
    def _exec(
        container: Any,
        command: list[str],
        workdir: str | None = None,
        demux: bool = False,
    ) -> tuple[int, Any]:
        result = container.exec_run(
            cmd=command,
            workdir=workdir,
            demux=demux,
        )
        return int(result.exit_code), result.output

    @staticmethod
    def _extract_commit_hash(output: str) -> str | None:
        """从可能有告警行的 Git stdout 中提取最后一个合法 SHA-1。"""
        for line in reversed(output.strip().splitlines()):
            candidate = line.strip()
            if FULL_COMMIT_RE.fullmatch(candidate):
                return candidate
        return None

    @staticmethod
    def _decode(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, tuple):
            return "\n".join(
                (
                    part.decode("utf-8", errors="replace")
                    if isinstance(part, bytes)
                    else str(part)
                )
                for part in value
                if part
            )
        return str(value)

    def _truncate_logs(self, logs: str) -> tuple[str, bool]:
        limit = self.settings.sandbox_log_max_chars
        if len(logs) <= limit:
            return logs, False
        # 构建错误通常在末尾，保留尾部更利于定位。
        return f"...[truncated {len(logs) - limit} chars]...\n{logs[-limit:]}", True

    @staticmethod
    def _safe_remove(container: Any) -> None:
        try:
            container.remove(force=True)
        except DockerException:
            pass
