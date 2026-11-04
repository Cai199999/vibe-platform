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
from ai_core.sandbox.base import (
    SandboxError,
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


class LocalDockerSandbox(SandboxProvider):
    """单机 Docker 沙盒实现。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.RLock()
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
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for rel_path, content in files.items():
                self._add_parent_dirs(tar, rel_path)
                data = content.encode("utf-8")
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
    def _wait_until_ready(self, container: Any, sandbox_id: str) -> None:
        """轮询容器内 Vite 开发服务器，确认端口已真正可用。"""
        deadline = time.monotonic() + self.settings.sandbox_start_timeout_seconds
        probe = (
            "wget -q -T 2 -O /dev/null "
            f"http://127.0.0.1:{self.settings.sandbox_dev_port}/"
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
