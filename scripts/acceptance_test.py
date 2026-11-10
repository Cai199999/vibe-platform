#!/usr/bin/env python3
"""全栈产物验收：生成 -> 导出 -> 解压检查 -> 纯净容器 SQLite 自检。

用法::

    python3 scripts/acceptance_test.py
    AI_BASE_URL=http://127.0.0.1:8100 python3 scripts/acceptance_test.py

验收链路：

  1. 通过 Architect（mock）驱动到全栈契约并确认；
  2. 调用全栈生成 SSE，拿到 sandboxId / runId；
  3. 调用导出接口下载 ``project-export.tar.gz``；
  4. 安全解压，检查 DEPLOY.md / Dockerfile / render.yaml；
  5. 文本比对前端 fetch 路径与后端实际路由，提示 404 风险；
  6. 在 node:22-alpine 纯净容器内执行 ``npm install`` / ``npm run build``；
  7. ``docker build`` 生成镜像，运行时不注入任何数据库环境变量，
     验证 SQLite 回退可自动建表、健康检查与 CRUD 均正常。

全程通过打印::

    🎉 全栈产物验收通过，已具备上云条件

任何一步失败都会列出缺失文件、路由不匹配或编译/运行报错，并以 1 退出。

环境变量：

  - ``AI_BASE_URL``：默认 Java BFF ``http://127.0.0.1:8080``；
  - ``ACCEPTANCE_KEEP=1``：保留临时目录用于排查；
  - ``ACCEPTANCE_NPM_TIMEOUT``：npm 阶段超时秒数，默认 900；
  - ``ACCEPTANCE_BUILD_TIMEOUT``：docker build 超时秒数，默认 900。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# 复用既有 E2E 的真实生成链路，避免验收脚本与主流程出现两份实现。
import fullstack_generation_e2e_test as generation  # noqa: E402

BASE_URL = generation.BASE_URL
VIA_BFF = generation.VIA_BFF
NPM_TIMEOUT = float(os.environ.get("ACCEPTANCE_NPM_TIMEOUT", "900"))
BUILD_TIMEOUT = float(os.environ.get("ACCEPTANCE_BUILD_TIMEOUT", "900"))
KEEP_WORKDIR = os.environ.get("ACCEPTANCE_KEEP") == "1"
VERBOSE = os.environ.get("ACCEPTANCE_VERBOSE") == "1"

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
REQUIRED_FILES = ("DEPLOY.md", "Dockerfile", "render.yaml")
FRONTEND_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}
FORBIDDEN_DB_ENV = re.compile(
    r"^(DATABASE_URL|DB_HOST|DB_PORT|DB_USER|DB_PASSWORD|DB_NAME|"
    r"MYSQL_|POSTGRES_|PGHOST|PGPORT|PGUSER|PGPASSWORD|PGDATABASE|"
    r"REDIS_|MINIO_)",
    re.IGNORECASE,
)


class AcceptanceFailure(AssertionError):
    """验收过程中的可预期失败。"""


class Report:
    """收集全部问题，最后统一输出。"""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, message: str) -> None:
        print(f"   ✓ {message}")

    def fail(self, message: str) -> None:
        self.failures.append(message)
        print(f"   ✗ {message}")

    def finish(self) -> int:
        if not self.failures:
            print("\n🎉 全栈产物验收通过，已具备上云条件")
            return 0
        print(f"\n❌ 全栈产物验收失败（{len(self.failures)} 项）")
        for index, failure in enumerate(self.failures, start=1):
            print(f"  {index}. {failure}")
        return 1


def stage(title: str) -> None:
    print(f"\n[{title}]")


def run_command(
    command: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """执行外部命令，统一 UTF-8 解码，不因非零退出码抛异常。"""
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def tail(text: str, lines: int = 40) -> str:
    content = (text or "").strip()
    if not content:
        return "<无输出>"
    return "\n".join(content.splitlines()[-lines:])


def format_command_failure(
    label: str,
    proc: subprocess.CompletedProcess[str],
) -> str:
    detail = tail(proc.stderr) if proc.stderr.strip() else tail(proc.stdout)
    return f"{label}失败（exit={proc.returncode}）:\n{detail}"


# ------------------------------------------------------------
# 1. 生成与导出
# ------------------------------------------------------------
def generate_fullstack() -> tuple[str, str, dict[str, Any]]:
    """跑完整 Architect -> CONFIRM -> 全栈生成，返回 runId/sandboxId/payload。"""
    run_id, contract, contract_seq, _ = generation.drive_to_fullstack_contract()
    confirmed = generation.confirm_contract(run_id, contract, contract_seq)
    events, terminal = generation.start_fullstack(f"fsk_{run_id}", confirmed)
    if terminal.get("type") != "fullstack.completed":
        raise AcceptanceFailure(
            "全栈生成未成功结束: "
            f"{terminal.get('type')} -> {json.dumps(terminal.get('payload'), ensure_ascii=False)}"
        )
    payload = terminal.get("payload") or {}
    if payload.get("status") != "SUCCEEDED":
        raise AcceptanceFailure(f"全栈终态不是 SUCCEEDED: {payload}")
    sandbox_id = str(payload.get("sandboxId") or "")
    if not sandbox_id:
        raise AcceptanceFailure(f"全栈终态缺少 sandboxId: {payload}")
    # 生成事件里必须真的出现部署文件，否则后续解压检查会给出更具体原因。
    generated_paths = {
        event["payload"]["path"]
        for event in events
        if event.get("type") == "fullstack.node.output"
    }
    for required in REQUIRED_FILES:
        if required not in generated_paths:
            raise AcceptanceFailure(f"生成事件中没有出现 {required}")
    return run_id, sandbox_id, payload


def download_export(run_id: str, sandbox_id: str, archive_path: Path) -> int:
    """通过 BFF 或 AI Core 下载 tar.gz，返回字节数。"""
    if VIA_BFF:
        query = urllib_parse.urlencode({"sandboxId": sandbox_id})
        url = f"{BASE_URL}/api/v1/runs/{urllib_parse.quote(run_id)}/export?{query}"
    else:
        url = (
            f"{BASE_URL}/api/v1/internal/sandbox/"
            f"{urllib_parse.quote(sandbox_id)}/export"
        )
    request = urllib_request.Request(url, headers={"Accept": "application/gzip"})
    try:
        with urllib_request.urlopen(request, timeout=120) as response:
            body = response.read()
            status = response.status
            content_type = response.headers.get("Content-Type", "")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AcceptanceFailure(f"导出接口 HTTP {exc.code}: {detail[:800]}") from exc
    except urllib_error.URLError as exc:
        raise AcceptanceFailure(f"导出接口不可达: {exc}") from exc

    if status != 200:
        raise AcceptanceFailure(f"导出接口返回 HTTP {status}")
    if not body.startswith(b"\x1f\x8b"):
        raise AcceptanceFailure(
            f"导出内容不是 gzip（Content-Type={content_type}, {len(body)} bytes）"
        )
    if len(body) < 1024:
        raise AcceptanceFailure(f"导出包异常小: {len(body)} bytes")
    archive_path.write_bytes(body)
    return len(body)


def extract_archive(archive_path: Path, package_dir: Path) -> None:
    """安全解压：拒绝绝对路径、路径穿越与符号链接/设备文件。"""
    package_dir.mkdir(parents=True, exist_ok=False)
    max_files = 5000
    max_bytes = 200 * 1024 * 1024
    total_bytes = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > max_files:
            raise AcceptanceFailure(f"压缩包文件数异常: {len(members)} > {max_files}")
        for member in members:
            name = member.name
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise AcceptanceFailure(f"压缩包包含非法路径: {name}")
            if member.issym() or member.islnk():
                raise AcceptanceFailure(f"压缩包包含链接文件，拒绝解压: {name}")
            if member.isdev():
                raise AcceptanceFailure(f"压缩包包含设备文件，拒绝解压: {name}")
            if member.isfile():
                total_bytes += member.size
                if total_bytes > max_bytes:
                    raise AcceptanceFailure("压缩包解压后体积超过 200MB，拒绝继续")
        # filter="data" 由 Python 3.12 提供，会再次清洗权限与路径。
        archive.extractall(package_dir, filter="data")


def resolve_package_root(extracted_dir: Path) -> Path:
    """兼容压缩包自带单一顶层目录的导出实现。"""
    entries = [entry for entry in extracted_dir.iterdir() if entry.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        candidate = entries[0]
        if (candidate / "Dockerfile").exists():
            return candidate
    return extracted_dir


# ------------------------------------------------------------
# 2. 文件与路由静态检查
# ------------------------------------------------------------
def check_required_files(package_root: Path) -> list[str]:
    missing = [name for name in REQUIRED_FILES if not (package_root / name).is_file()]
    return missing


def normalize_route(path: str) -> str:
    """把 Express / 前端动态参数统一成 :param，便于文本层面比对。"""
    value = (path or "").strip()
    value = value.split("?", 1)[0].split("#", 1)[0]
    value = re.sub(r"\$\{[^}]*\}", ":param", value)
    value = re.sub(r"\{[^}]*\}", ":param", value)
    value = re.sub(r"<[^>]*>", ":param", value)
    segments: list[str] = []
    for segment in value.split("/"):
        if not segment:
            continue
        if segment.startswith(":"):
            segment = ":param"
        elif re.fullmatch(r"\d+", segment):
            segment = ":param"
        elif re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            segment,
        ):
            segment = ":param"
        segments.append(segment)
    return "/" + "/".join(segments)


def collect_backend_routes(package_root: Path) -> dict[str, set[str]]:
    """从 api-routes.json 与 index.js 收集后端实际注册的路由。"""
    routes: dict[str, set[str]] = {}

    def add(method: str | None, path: str | None, source: str) -> None:
        if not method or not path:
            return
        method = method.upper()
        if method not in HTTP_METHODS:
            return
        if not path.startswith("/api"):
            return
        normalized = normalize_route(path)
        if normalized in {"/api"}:
            return
        routes.setdefault(normalized, set()).add(method)

    api_file = package_root / "server" / "api-routes.json"
    if api_file.is_file():
        try:
            payload = json.loads(api_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure(
                f"server/api-routes.json 不是合法 JSON: {exc}"
            ) from exc
        for key in ("routes", "registeredRoutes"):
            for item in payload.get(key) or []:
                if isinstance(item, dict):
                    add(item.get("method"), item.get("path"), f"api-routes.json.{key}")

    index_file = package_root / "server" / "index.js"
    if index_file.is_file():
        text = index_file.read_text(encoding="utf-8")
        pattern = re.compile(
            r"(?:app|router)\.(get|post|put|patch|delete)\(\s*" r"(['\"`])([^'\"`]+)\2",
            re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            add(match.group(1), match.group(3), "server/index.js")

    if not routes:
        raise AcceptanceFailure(
            "无法解析后端路由：server/api-routes.json 与 server/index.js 均无 /api 路由"
        )
    return routes


def collect_frontend_requests(
    package_root: Path,
) -> list[dict[str, Any]]:
    """扫描 src 下所有前端源码，提取 fetch 发起的方法与路径。"""
    src_dir = package_root / "src"
    if not src_dir.is_dir():
        raise AcceptanceFailure("缺少 src/ 目录，无法检查前端 API 调用")

    found: dict[tuple[str | None, str], dict[str, Any]] = {}

    def add(
        method: str | None,
        route: str,
        file: str,
        line: int,
    ) -> None:
        if not route or not route.startswith("/api"):
            return
        if normalize_route(route) == "/api":
            return
        method = method.upper() if method else None
        if method and method not in HTTP_METHODS:
            return
        key = (method, normalize_route(route))
        found.setdefault(
            key,
            {
                "method": method,
                "route": route,
                "normalized": normalize_route(route),
                "file": file,
                "line": line,
            },
        )

    for path in sorted(src_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in FRONTEND_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(package_root).as_posix()

        def line_of(offset: int) -> int:
            return text.count("\n", 0, offset) + 1

        # 2.1 契约交互数组：优先把 INTERACTIONS 当 JSON 整体解析，
        # 避免 logic 字符串里的 "{task_id}" 干扰花括号匹配。
        interactions_match = re.search(
            r"(?:const|let|var)\s+INTERACTIONS\s*=\s*(\[[\s\S]*?\n\])",
            text,
        )
        if interactions_match:
            try:
                interactions = json.loads(interactions_match.group(1))
            except json.JSONDecodeError:
                interactions = []
            for item in interactions:
                if not isinstance(item, dict):
                    continue
                route = item.get("apiRoute")
                method = item.get("method")
                if not isinstance(route, str):
                    continue
                offset = text.find(route, interactions_match.start())
                add(
                    method if isinstance(method, str) else None,
                    route,
                    relative,
                    line_of(offset if offset >= 0 else interactions_match.start()),
                )

        # 2.2 其他写法：{"method": "POST", "apiRoute": "/api/..."}
        for obj in re.finditer(r"\{[^{}]*\}", text, re.DOTALL):
            chunk = obj.group(0)
            if '"apiRoute"' not in chunk:
                continue
            method_match = re.search(
                r'"method"\s*:\s*"(GET|POST|PUT|PATCH|DELETE|LOCAL)"',
                chunk,
                re.IGNORECASE,
            )
            route_match = re.search(r'"apiRoute"\s*:\s*"([^"]+)"', chunk)
            if not route_match:
                continue
            method = method_match.group(1) if method_match else None
            if method and method.upper() == "LOCAL":
                continue
            add(method, route_match.group(1), relative, line_of(obj.start()))

        # 2.3 路径常量 + fetch(apiUrl(CONST))，从调用点后 600 字符找 method。
        constants = {
            match.group(1): match.group(3)
            for match in re.finditer(
                r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*(['\"])(/api/[^'\"]*)\2",
                text,
            )
        }
        for name, route in constants.items():
            references = list(
                re.finditer(
                    rf"fetch\(\s*apiUrl\(\s*{re.escape(name)}\s*\)",
                    text,
                )
            )
            if not references:
                add(None, route, relative, line_of(text.find(route)))
                continue
            for reference in references:
                window = text[reference.start() : reference.start() + 600]
                method_match = re.search(r"method\s*:\s*['\"](\w+)['\"]", window)
                method = method_match.group(1) if method_match else "GET"
                add(method, route, relative, line_of(reference.start()))

        # 2.4 直接 fetch('/api/...')。
        for match in re.finditer(r"fetch\(\s*(['\"])(/api/[^'\"]*)\1", text):
            window = text[match.start() : match.start() + 500]
            method_match = re.search(r"method\s*:\s*['\"](\w+)['\"]", window)
            method = method_match.group(1) if method_match else "GET"
            add(method, match.group(2), relative, line_of(match.start()))

        # 2.5 兜底：任何以 /api/ 开头的字符串常量都视为被引用。
        for match in re.finditer(r"(['\"])(/api/[^'\"\s`]*)\1", text):
            add(None, match.group(2), relative, line_of(match.start()))

    return list(found.values())


def check_route_consistency(package_root: Path) -> tuple[int, list[str]]:
    backend = collect_backend_routes(package_root)
    frontend = collect_frontend_requests(package_root)
    if not frontend:
        return len(backend), ["前端源码中未发现任何 /api 请求，无法证明接口连通"]

    issues: list[str] = []
    missing_paths: set[str] = set()
    ordered = sorted(
        frontend,
        key=lambda entry: (entry["normalized"], entry["method"] or ""),
    )

    # 先检查带方法的调用：能精确指出方法不匹配还是完全未注册。
    for item in ordered:
        method = item["method"]
        if not method:
            continue
        path = item["normalized"]
        location = f"{item['file']}:{item['line']}"
        if method in backend.get(path, set()):
            continue
        missing_paths.add(path)
        if path in backend:
            issues.append(
                f"方法不匹配: 前端 {method} {item['route']}（{location}），"
                f"后端只有 {', '.join(sorted(backend[path]))}"
            )
        else:
            issues.append(
                f"404 风险: 前端 {method} {item['route']}（{location}）在后端未注册"
            )

    # 再检查仅有路径引用的调用；同一路径已有精确问题时不重复报。
    for item in ordered:
        if item["method"]:
            continue
        path = item["normalized"]
        if path in backend or path in missing_paths:
            continue
        location = f"{item['file']}:{item['line']}"
        issues.append(f"404 风险: 前端引用 {item['route']}（{location}）在后端未注册")
    return len(backend), issues


# ------------------------------------------------------------
# 3. 纯净容器验证
# ------------------------------------------------------------
def ensure_docker(report: Report) -> bool:
    if shutil.which("docker") is None:
        report.fail("未安装 docker CLI，无法执行容器验收")
        return False
    try:
        proc = run_command(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        report.fail("docker daemon 响应超时")
        return False
    if proc.returncode != 0:
        report.fail(f"docker daemon 不可用: {tail(proc.stderr)}")
        return False
    image = run_command(["docker", "image", "inspect", "node:22-alpine"], timeout=20)
    if image.returncode != 0:
        pull = run_command(["docker", "pull", "node:22-alpine"], timeout=BUILD_TIMEOUT)
        if pull.returncode != 0:
            report.fail("拉取 node:22-alpine 失败: " + tail(pull.stderr))
            return False
    return True


def npm_install_and_build(
    package_root: Path,
    report: Report,
) -> None:
    """在纯净 node 容器内安装依赖并执行前端构建。"""
    package_json = package_root / "package.json"
    if not package_json.is_file():
        report.fail("缺少 package.json，无法执行 npm install")
        return
    try:
        package_data = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.fail(f"package.json 不是合法 JSON: {exc}")
        return

    install = (
        "npm ci --no-audit --no-fund"
        if (package_root / "package-lock.json").is_file()
        else "npm install --no-audit --no-fund"
    )
    scripts = package_data.get("scripts") or {}
    build = " && npm run build" if "build" in scripts else ""
    command = f"{install}{build}"
    name = f"vibe-acceptance-npm-{uuid.uuid4().hex[:8]}"
    proc = run_command(
        [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--label",
            "vibe.acceptance=1",
            "-v",
            f"{package_root}:/app",
            "-w",
            "/app",
            "node:22-alpine",
            "sh",
            "-lc",
            command,
        ],
        timeout=NPM_TIMEOUT,
    )
    if VERBOSE:
        print(tail(proc.stdout, 20))
    if proc.returncode != 0:
        report.fail(format_command_failure("npm install/build ", proc))
        return
    report.ok(f"npm 依赖安装{(' + 前端构建' if build else '')}成功")


def docker_build(package_root: Path, report: Report) -> str | None:
    tag = f"vibe-acceptance:{uuid.uuid4().hex[:8]}"
    try:
        proc = run_command(
            [
                "docker",
                "build",
                "--label",
                "vibe.acceptance=1",
                "-t",
                tag,
                str(package_root),
            ],
            timeout=BUILD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        report.fail(f"docker build 超时（>{BUILD_TIMEOUT:.0f}s）")
        return None
    if VERBOSE:
        print(tail(proc.stdout, 30))
    if proc.returncode != 0:
        report.fail(format_command_failure("docker build ", proc))
        return None
    report.ok(f"docker build 成功（{tag}）")
    return tag


def container_env(container: str) -> list[str]:
    proc = run_command(
        ["docker", "inspect", "--format", "{{json .Config.Env}}", container],
        timeout=20,
    )
    if proc.returncode != 0:
        return []
    try:
        return list(json.loads(proc.stdout.strip() or "[]"))
    except json.JSONDecodeError:
        return []


def http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib_request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body) if body else None
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def wait_health(base_url: str, container: str, timeout: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            status, payload = http_json(f"{base_url}/api/health")
            if status == 200 and isinstance(payload, dict):
                return payload
            last_error = f"HTTP {status}: {payload}"
        except Exception as exc:  # noqa: BLE001 - 轮询阶段保留最后错误
            last_error = str(exc)
        time.sleep(1)
    logs = run_command(["docker", "logs", "--tail", "50", container], timeout=20)
    raise AcceptanceFailure(
        f"/api/health 在 {timeout:.0f}s 内未就绪: {last_error}\n"
        f"容器日志:\n{tail(logs.stdout + logs.stderr)}"
    )


def docker_run_and_verify(
    tag: str,
    collection_path: str | None,
    report: Report,
) -> tuple[str, str] | None:
    """启动无数据库配置的容器，验证 SQLite 自动建表与 CRUD。"""
    container = f"vibe-acceptance-run-{uuid.uuid4().hex[:8]}"
    keep_container = False
    try:
        proc = run_command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--label",
                "vibe.acceptance=1",
                # 只映射端口，不传任何 DATABASE_URL / DB_* 环境变量。
                "-p",
                "127.0.0.1::3000",
                tag,
            ],
            timeout=60,
        )
        if proc.returncode != 0:
            report.fail(format_command_failure("docker run ", proc))
            return None

        # 确认镜像/容器没有夹带外部数据库配置。
        forbidden = [
            item
            for item in container_env(container)
            if FORBIDDEN_DB_ENV.match(item.split("=", 1)[0])
        ]
        if forbidden:
            report.fail(f"容器携带数据库环境变量: {', '.join(forbidden)}")
            return None

        port = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not port:
            port_proc = run_command(
                ["docker", "port", container, "3000/tcp"],
                timeout=10,
            )
            output = (port_proc.stdout or "").strip().splitlines()
            if output:
                port = output[0].rsplit(":", 1)[-1]
            else:
                time.sleep(0.5)
        if not port:
            report.fail("docker port 未返回 3000 映射")
            return None

        base_url = f"http://127.0.0.1:{port}"
        health = wait_health(base_url, container)
        if health.get("status") != "ok":
            report.fail(f"健康检查 status 异常: {health}")
            return None
        if health.get("storage") != "sqlite":
            report.fail(f"未回退到 SQLite: {health}")
            return None
        if health.get("runtime") != "express-sqlite":
            report.fail(f"运行时不是 express-sqlite: {health}")
            return None
        report.ok(
            "无数据库配置启动成功："
            f"storage={health.get('storage')} runtime={health.get('runtime')} "
            f"db={health.get('dbPath')}"
        )

        collection = collection_path or "/api/data"
        status, listing = http_json(f"{base_url}{collection}")
        if status != 200:
            report.fail(f"GET {collection} -> HTTP {status}: {listing}")
            return None
        name = f"acceptance-{uuid.uuid4().hex[:8]}"
        status, created = http_json(
            f"{base_url}{collection}",
            method="POST",
            payload={"name": name, "title": name, "status": "todo"},
        )
        if status not in (200, 201):
            report.fail(f"POST {collection} -> HTTP {status}: {created}")
            return None
        record_id = created.get("id") if isinstance(created, dict) else None
        status, listing = http_json(f"{base_url}{collection}")
        items = listing.get("items") if isinstance(listing, dict) else listing
        if status != 200 or not isinstance(items, list):
            report.fail(f"再次 GET {collection} 失败: HTTP {status} {listing}")
            return None
        if record_id is not None and not any(
            isinstance(item, dict) and item.get("id") == record_id for item in items
        ):
            report.fail(f"SQLite 写入后未查到记录 id={record_id}")
            return None
        report.ok(f"SQLite CRUD 正常：{collection} GET/POST 已落库")
        keep_container = True
        return container, tag
    except AcceptanceFailure as exc:
        report.fail(str(exc))
        return None
    finally:
        if not keep_container:
            run_command(["docker", "rm", "-f", container], timeout=30)


# ------------------------------------------------------------
# 4. 清理
# ------------------------------------------------------------
def cleanup_sandbox(sandbox_id: str) -> None:
    if not sandbox_id:
        return
    try:
        if VIA_BFF:
            generation.cleanup(sandbox_id)
        else:
            request = urllib_request.Request(
                f"{BASE_URL}/api/v1/internal/sandbox/"
                f"{urllib_parse.quote(sandbox_id)}",
                method="DELETE",
            )
            urllib_request.urlopen(request, timeout=20).close()
    except Exception:  # noqa: BLE001 - 清理失败不影响验收结论
        print(f"   ! 清理沙盒失败（可手动删除）: {sandbox_id}")


def cleanup_docker(container: str | None, tag: str | None) -> None:
    if container:
        run_command(["docker", "rm", "-f", container], timeout=30)
    if tag:
        run_command(["docker", "image", "rm", "-f", tag], timeout=60)


def main() -> int:
    print(f"🔎 全栈产物验收  BASE_URL={BASE_URL} via_bff={VIA_BFF}")
    report = Report()
    work_dir = Path(tempfile.mkdtemp(prefix="vibe-acceptance-"))
    archive_path = work_dir / "project-export.tar.gz"
    package_dir = work_dir / "package"
    sandbox_id = ""
    container = ""
    image_tag = ""
    docker_ready = ensure_docker(report)

    try:
        stage("1/6 生成全栈代码")
        try:
            run_id, sandbox_id, payload = generate_fullstack()
            print(
                f"   ✓ runId={run_id} sandboxId={sandbox_id} "
                f"collection={payload.get('collectionPath')}"
            )
        except Exception as exc:  # noqa: BLE001 - 统一转为验收失败
            report.fail(f"全栈代码生成失败: {exc}")
            return report.finish()

        stage("2/6 下载导出包")
        try:
            size = download_export(run_id, sandbox_id, archive_path)
            report.ok(f"已下载 project-export.tar.gz（{size / 1024:.1f} KB）")
        except Exception as exc:  # noqa: BLE001
            report.fail(f"导出包下载失败: {exc}")
            return report.finish()

        stage("3/6 解压与必需文件")
        try:
            extract_archive(archive_path, package_dir)
            package_root = resolve_package_root(package_dir)
            report.ok(f"已安全解压到临时目录（{package_root.name}）")
        except Exception as exc:  # noqa: BLE001
            report.fail(f"解压失败: {exc}")
            return report.finish()

        missing = check_required_files(package_root)
        if missing:
            report.fail("缺少必需部署文件: " + ", ".join(missing))
        else:
            report.ok("DEPLOY.md / Dockerfile / render.yaml 均存在")

        stage("4/6 前后端路由一致性")
        try:
            backend_count, route_issues = check_route_consistency(package_root)
            if route_issues:
                for issue in route_issues:
                    report.fail(issue)
            else:
                report.ok(f"前端请求与后端 {backend_count} 条路由文本匹配，无 404 风险")
        except Exception as exc:  # noqa: BLE001
            report.fail(f"路由检查失败: {exc}")

        stage("5/6 纯净容器 npm install / build")
        if not docker_ready:
            report.fail("跳过容器内 npm 与 docker build（docker 不可用）")
        elif not (package_root / "Dockerfile").is_file():
            report.fail("缺少 Dockerfile，跳过 npm / docker build")
        else:
            npm_install_and_build(package_root, report)
            image_tag = docker_build(package_root, report) or ""

            stage("6/6 无数据库配置运行 + SQLite 自检")
            if image_tag:
                result = docker_run_and_verify(
                    image_tag,
                    payload.get("collectionPath"),
                    report,
                )
                if result:
                    container = result[0]
            else:
                report.fail("docker build 未产出镜像，跳过运行验收")

        return report.finish()
    finally:
        cleanup_docker(container, image_tag)
        cleanup_sandbox(sandbox_id)
        if KEEP_WORKDIR:
            print(f"\n📁 临时目录已保留: {work_dir}")
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        raise SystemExit(130)
