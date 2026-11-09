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
    "node_modules/\n"
    "dist/\n"
    ".vite/\n"
    "*.log\n"
    "vite.config.*.timestamp-*\n"
    "*.timestamp-*.mjs\n"
)
# 写入 .git/info/exclude：兼容本次升级前已创建、且 .gitignore 不含上述
# 临时文件规则的旧沙盒，避免它们在下一次 checkpoint 时偶发失败。
GIT_EXCLUDE_CONTENT = (
    "# vibe sandbox excludes\n"
    "node_modules/\n"
    "dist/\n"
    ".vite/\n"
    "*.log\n"
    "vite.config.*.timestamp-*\n"
    "*.timestamp-*.mjs\n"
)
FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


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
                    f"{self.settings.sandbox_dev_port}/tcp": ("127.0.0.1", host_port)
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
        except (APIError, DockerException) as exc:
            if container is not None:
                self._safe_remove(container)
            self._release_host_port(host_port)
            raise SandboxStartupError(f"创建沙盒失败: {exc}") from exc
        except SandboxError:
            if container is not None:
                self._safe_remove(container)
            self._release_host_port(host_port)
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
                    f"{self.settings.sandbox_dev_port}/tcp": ("127.0.0.1", host_port)
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
        except (APIError, DockerException) as exc:
            if container is not None:
                self._safe_remove(container)
            self._release_host_port(host_port)
            raise SandboxStartupError(f"创建导入沙盒失败: {exc}") from exc
        except SandboxError:
            if container is not None:
                self._safe_remove(container)
            self._release_host_port(host_port)
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
    ) -> list[str]:
        container = self._get_container(sandbox_id)
        changed: list[str] = []

        normalized = self._validate_files(files or {})
        if normalized:
            self._put_files(container, normalized)
            changed.extend(sorted(normalized))

        for raw_path in deleted_paths or []:
            rel_path = self._normalize_path(raw_path)
            code, output = self._exec(
                container,
                ["sh", "-lc", f"rm -rf -- {shlex.quote(rel_path)}"],
                workdir=self.settings.sandbox_workdir,
            )
            if code != 0:
                raise SandboxPatchError(
                    f"删除文件失败 {rel_path}: {self._decode(output)}"
                )
            changed.append(rel_path)

        if patch:
            self._apply_unified_diff(container, patch)
            changed.append("<unified-diff>")

        # 给 Vite 文件监听器一点时间捕获变更；即使尚未触发 HMR，
        # 下一次请求时 Vite 也会按最新文件重新 transform。
        time.sleep(0.2)
        return sorted(set(changed))

    def verify(self, sandbox_id: str, check: str = "build") -> VerifyResult:
        if check not in {"build", "test"}:
            raise SandboxValidationError(f"不支持的校验类型: {check}")

        container = self._get_container(sandbox_id)
        timeout = self.settings.sandbox_verify_timeout_seconds
        # busybox timeout：超时返回 124，避免 exec_run 无限阻塞。
        command = f"timeout {timeout}s npm run {check}"

        started = time.monotonic()
        try:
            exit_code, output = self._exec(
                container,
                ["sh", "-lc", command],
                workdir=self.settings.sandbox_workdir,
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

    def read_file(self, sandbox_id: str, path: str) -> str:
        """读取沙盒内文本文件。

        Agent 在每次 apply 之后重新读取目标文件，确保 current_code 与沙盒
        真实状态一致（尤其适用于 unified diff 场景）。
        """
        container = self._get_container(sandbox_id)
        rel_path = self._normalize_path(path)
        code, output = self._exec(
            container,
            ["sh", "-lc", f"cat {shlex.quote(rel_path)}"],
            workdir=self.settings.sandbox_workdir,
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
    def _put_files(self, container: Any, files: dict[str, str]) -> None:
        archive = self._build_tar(files)
        try:
            ok = container.put_archive(self.settings.sandbox_workdir, archive)
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

    def _apply_unified_diff(self, container: Any, patch: str) -> None:
        patch_archive = self._build_tar({"vibe.patch.diff": patch})
        try:
            container.put_archive("/tmp", patch_archive)
            code, output = self._exec(
                container,
                [
                    "sh",
                    "-lc",
                    "git apply --whitespace=nowarn /tmp/vibe.patch.diff",
                ],
                workdir=self.settings.sandbox_workdir,
            )
        except DockerException as exc:
            raise SandboxPatchError(f"应用补丁失败: {exc}") from exc

        if code != 0:
            raise SandboxPatchError(f"应用补丁失败: {self._decode(output)}")

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
