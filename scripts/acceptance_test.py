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
     验证 SQLite 回退可自动建表、健康检查与 CRUD 均正常；
     若契约要求上传，再发送真实 ``multipart/form-data`` 请求，
     校验文件落盘、静态 URL 可回读且内容逐字节一致；
  8. 模拟真实云端部署：把带 MySQL 连接串的 ``.env`` 注入镜像，
     启动容器 → 验证未迁移时 ``/api/health`` 返回 503 →
     以独立发布步骤执行 ``node server/migrate.mjs`` →
     验证 ``_schema_migrations`` 与真实表结构 → 健康检查恢复 200 →
     CRUD 后用宿主机连接直接查库，证明数据确实写入 MySQL；
     最后重复执行迁移，验证幂等。

全程通过打印::

    🎉 生产级产物验收通过，可直接交付云端部署

任何一步失败都会列出缺失文件、路由不匹配或编译/运行报错，并以 1 退出。

环境变量：

  - ``AI_BASE_URL``：默认 Java BFF ``http://127.0.0.1:8080``；
  - ``ACCEPTANCE_KEEP=1``：保留临时目录用于排查；
  - ``ACCEPTANCE_NPM_TIMEOUT``：npm 阶段超时秒数，默认 900；
  - ``ACCEPTANCE_BUILD_TIMEOUT``：docker build 超时秒数，默认 900；
  - ``ACCEPTANCE_MYSQL=0``：跳过真实 MySQL 验收（默认执行）；
  - ``ACCEPTANCE_MYSQL_HOST/PORT/USER/PASSWORD``：宿主机 MySQL 连接信息，
    默认 ``127.0.0.1:3306`` / ``root`` / ``root``；
  - ``ACCEPTANCE_MYSQL_CONTAINER_HOST``：容器内访问宿主机的地址，
    默认 ``host.docker.internal``（通过 ``--add-host`` 注入）。
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
MYSQL_ENABLED = os.environ.get("ACCEPTANCE_MYSQL", "1") != "0"
MYSQL_HOST = os.environ.get("ACCEPTANCE_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("ACCEPTANCE_MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("ACCEPTANCE_MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("ACCEPTANCE_MYSQL_PASSWORD", "root")
# 容器访问宿主机 MySQL 的地址；Linux 下由 --add-host host-gateway 提供。
MYSQL_CONTAINER_HOST = os.environ.get(
    "ACCEPTANCE_MYSQL_CONTAINER_HOST", "host.docker.internal"
)
MYSQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
REQUIRED_FILES = (
    "DEPLOY.md",
    "Dockerfile",
    "render.yaml",
    ".env.example",
    "server/migrations.mjs",
    "server/migrations/001_init.sql",
)
REQUIRED_ENV_KEYS = (
    "NODE_ENV",
    "DATABASE_URL",
    "DB_ENGINE",
    "DB_SSL",
    "PORT",
    "CORS_ORIGINS",
)
HARD_CODED_DATABASE_URL = re.compile(
    r"[\"'](?:mysql|postgres(?:ql)?)://[^\"'\s]+[\"']", re.IGNORECASE
)
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
            print("\n🎉 生产级产物验收通过，可直接交付云端部署")
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
def generate_fullstack() -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """跑完整 Architect -> CONFIRM -> 全栈生成，返回 runId/sandboxId/payload/契约。"""
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
    return run_id, sandbox_id, payload, confirmed


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


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def check_production_contract(package_root: Path) -> list[str]:
    """静态验证导出包是否具备生产部署契约。

    验收不能只看“容器能跑”：必须在代码层面确认连接串来自环境变量、
    迁移文件存在、CORS 不是隐式通配、健康检查会真实返回 503、
    Dockerfile 采用多阶段构建且以非 root 用户运行。
    """
    issues: list[str] = []

    env_example = package_root / ".env.example"
    if not env_example.is_file():
        issues.append("缺少 .env.example，云端无法按模板注入生产配置")
    else:
        env_text = _read_text(env_example)
        for key in REQUIRED_ENV_KEYS:
            if not re.search(rf"^\s*{re.escape(key)}=", env_text, re.MULTILINE):
                issues.append(f".env.example 缺少 {key} 配置项")
        if "DB_AUTO_MIGRATE" in env_text:
            issues.append(".env.example 仍包含已废弃的 DB_AUTO_MIGRATE")

    server_dir = package_root / "server"
    if not server_dir.is_dir():
        return issues + ["缺少 server/ 目录，无法验证生产后端契约"]

    server_code = "\n".join(
        _read_text(path)
        for path in sorted(server_dir.rglob("*"))
        if path.is_file() and path.suffix in {".js", ".mjs", ".cjs"}
    )
    if "DB_AUTO_MIGRATE" in server_code:
        issues.append("后端代码仍引用 DB_AUTO_MIGRATE，生产建表必须走迁移")
    if re.search(r"await\s+\w+\.sync\s*\(", server_code):
        issues.append("后端代码仍在启动时调用 model.sync() 自动建表")
    if "process.env.DATABASE_URL" not in server_code:
        issues.append("后端未通过 process.env.DATABASE_URL 读取数据库连接串")
    if HARD_CODED_DATABASE_URL.search(server_code):
        issues.append("后端代码硬编码了数据库连接串，违反环境变量隔离")
    if "process.env.CORS_ORIGINS" not in server_code:
        issues.append("后端未从 process.env.CORS_ORIGINS 读取跨域白名单")
    if re.search(r"cors\(\s*\{[^}]*origin\s*:\s*['\"]\*['\"]", server_code, re.DOTALL):
        issues.append("后端 CORS 使用通配符 origin: '*'，生产环境禁止")
    if "tableReady" not in server_code or "status(503)" not in server_code:
        issues.append("健康检查未真实探测表结构（缺少 tableReady / HTTP 503）")
    if not (server_dir / "migrations.mjs").is_file():
        issues.append("缺少 server/migrations.mjs，生产无法执行迁移")
    elif "_schema_migrations" not in _read_text(server_dir / "migrations.mjs"):
        issues.append("迁移执行器未记录 _schema_migrations，无法保证幂等")
    migration_sql = server_dir / "migrations" / "001_init.sql"
    if not migration_sql.is_file():
        issues.append("缺少 server/migrations/001_init.sql")
    elif not re.search(r"CREATE\s+TABLE", _read_text(migration_sql), re.IGNORECASE):
        issues.append("001_init.sql 未包含 CREATE TABLE")

    dockerfile = _read_text(package_root / "Dockerfile")
    if dockerfile:
        if not re.search(r"FROM\s+\S+\s+AS\s+deps", dockerfile, re.IGNORECASE):
            issues.append("Dockerfile 不是多阶段构建（缺少 deps 阶段）")
        if re.search(r"^\s*USER\s+root\s*$", dockerfile, re.IGNORECASE | re.MULTILINE):
            issues.append("Dockerfile 以 root 用户运行")
        if "npm install" not in dockerfile:
            issues.append("Dockerfile 未在 deps 阶段安装依赖")

    render = _read_text(package_root / "render.yaml")
    if render:
        if "preDeployCommand" not in render:
            issues.append("render.yaml 未配置 preDeployCommand 执行迁移")
        if "migrate.mjs" not in render:
            issues.append("render.yaml 未在发布阶段调用 node server/migrate.mjs")

    deploy_doc = _read_text(package_root / "DEPLOY.md")
    if deploy_doc and "migrate.mjs" not in deploy_doc:
        issues.append("DEPLOY.md 未写明生产迁移执行步骤")

    return issues


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


IDENTIFIER = r"[A-Za-z_$][\w$]*"
ROUTE_CALL_PATTERN = re.compile(
    rf"\b({IDENTIFIER})\.(get|post|put|patch|delete)\(\s*" r"(['\"`])([^'\"`]+)\3",
    re.IGNORECASE,
)
ROUTE_CHAIN_PATTERN = re.compile(
    rf"\b({IDENTIFIER})\.route\(\s*(['\"`])([^'\"`]+)\2\s*\)"
    r"((?:\s*\.(?:get|post|put|patch|delete)\((?:[^()]|\([^()]*\))*\))+)",
    re.IGNORECASE,
)
USE_PATTERN = re.compile(
    rf"\b({IDENTIFIER})\.use\(\s*"
    rf"(?:(['\"`])([^'\"`]*)\2\s*,\s*)?({IDENTIFIER})\s*\)",
    re.IGNORECASE,
)


def _join_route(prefix: str, path: str) -> str:
    """模拟 Express 的挂载拼接：prefix + child path。"""
    if not prefix:
        return path
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def parse_express_routes(text: str) -> list[tuple[str, str]]:
    """解析单文件内 Express 路由，支持 app.use 二级挂载。

    覆盖：

      - ``app.get('/api/...')`` / ``router.post('/...')``
      - ``router.use('/v1', childRouter)`` 多级前缀
      - ``app.route('/api/...').get(...).post(...)`` 链式注册

    返回 ``(method, full_path)``，不包含未挂载到 /api 的路径。
    """
    routes_by_object: dict[str, list[tuple[str, str]]] = {}
    for match in ROUTE_CALL_PATTERN.finditer(text):
        routes_by_object.setdefault(match.group(1), []).append(
            (match.group(2).upper(), match.group(4))
        )
    for match in ROUTE_CHAIN_PATTERN.finditer(text):
        obj = match.group(1)
        path = match.group(3)
        for method in re.findall(
            r"\.(get|post|put|patch|delete)\(",
            match.group(4),
            re.IGNORECASE,
        ):
            routes_by_object.setdefault(obj, []).append((method.upper(), path))

    edges: dict[str, list[tuple[str, str]]] = {}
    children: set[str] = set()
    for match in USE_PATTERN.finditer(text):
        parent = match.group(1)
        prefix = match.group(3) or ""
        child = match.group(4)
        edges.setdefault(parent, []).append((prefix, child))
        children.add(child)

    # 从 app 出发计算每个 router 对象的完整挂载前缀；同一 router 可以被
    # 挂载多次，因此保留集合而不是单值。
    prefixes: dict[str, set[str]] = {}
    queue: list[tuple[str, str]] = [("app", "")]
    # 没有挂载边的 router 视为根路由器（常见于 router.get('/api/...')）。
    for obj in routes_by_object:
        if obj != "app" and obj not in children:
            queue.append((obj, ""))
    visited: set[tuple[str, str]] = set()
    while queue:
        obj, prefix = queue.pop(0)
        if (obj, prefix) in visited:
            continue
        visited.add((obj, prefix))
        prefixes.setdefault(obj, set()).add(prefix)
        for child_prefix, child in edges.get(obj, []):
            queue.append((child, _join_route(prefix, child_prefix)))

    collected: list[tuple[str, str]] = []
    for obj, registrations in routes_by_object.items():
        for prefix in prefixes.get(obj, {""}):
            for method, path in registrations:
                full_path = _join_route(prefix, path)
                if full_path.startswith("/api"):
                    collected.append((method, full_path))
    return collected


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
        for method, path in parse_express_routes(text):
            add(method, path, "server/index.js")

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

        # 2.3 路径常量 + fetch / axios / 自定义 client 引用。
        constants = {
            match.group(1): match.group(3)
            for match in re.finditer(
                r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
                r"(['\"`])(/api/[^'\"`]*)\2",
                text,
            )
        }
        for name, route in constants.items():
            quoted = re.escape(name)
            references = list(
                re.finditer(
                    rf"(?:\bfetch\(\s*(?:apiUrl\(\s*)?{quoted}\s*\)?"
                    rf"|\b{IDENTIFIER}\.(get|post|put|patch|delete)\(\s*"
                    rf"(?:apiUrl\(\s*)?{quoted}\s*\)?)",
                    text,
                    re.IGNORECASE,
                )
            )
            if not references:
                add(None, route, relative, line_of(text.find(route)))
                continue
            for reference in references:
                explicit_method = reference.group(1) if reference.lastindex else None
                if explicit_method:
                    add(explicit_method, route, relative, line_of(reference.start()))
                    continue
                window = text[reference.start() : reference.start() + 600]
                method_match = re.search(r"method\s*:\s*['\"](\w+)['\"]", window)
                method = method_match.group(1) if method_match else "GET"
                add(method, route, relative, line_of(reference.start()))

        # 2.4 fetch('/api/...') / fetch(`/api/.../${id}`)。
        for match in re.finditer(
            r"fetch\(\s*(?:(['\"`])(/api/[^'\"`]*)\1|apiUrl\(\s*(['\"`])(/api/[^'\"`]*)\3)",
            text,
        ):
            route = match.group(2) or match.group(4)
            window = text[match.start() : match.start() + 500]
            method_match = re.search(r"method\s*:\s*['\"](\w+)['\"]", window)
            method = method_match.group(1) if method_match else "GET"
            add(method, route, relative, line_of(match.start()))

        # 2.5 axios / 自定义 HTTP client 的显式方法：
        #     axios.get('/api/...')、client.post(apiUrl('/api/...'))。
        for match in re.finditer(
            rf"\b({IDENTIFIER})\.(get|post|put|patch|delete)\(\s*"
            r"(?:apiUrl\(\s*)?(['\"`])(/api/[^'\"`]*)\3",
            text,
            re.IGNORECASE,
        ):
            add(match.group(2), match.group(4), relative, line_of(match.start()))

        # 2.6 axios({ method: 'post', url: '/api/...' }) 配置对象写法。
        for match in re.finditer(
            r"\baxios(?:\.request)?\(\s*\{([^{}]*)\}",
            text,
            re.DOTALL,
        ):
            chunk = match.group(1)
            method_match = re.search(r"method\s*:\s*['\"](\w+)['\"]", chunk)
            url_match = re.search(r"url\s*:\s*['\"`](/api/[^'\"`]*)['\"`]", chunk)
            if not url_match:
                continue
            method = method_match.group(1) if method_match else "GET"
            add(method, url_match.group(1), relative, line_of(match.start()))

        # 2.7 兜底：任何以 /api/ 开头的字符串常量都视为被引用。
        for match in re.finditer(r"(['\"`])(/api/[^'\"`\s]*)\1", text):
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
# 2.6 业务语义断言：契约要求的能力必须在生成代码中可见
# ------------------------------------------------------------
UPLOAD_INTENT_PATTERN = re.compile(
    r"(上传|附件|文件上传|upload|multipart|form-data|file\s*upload)",
    re.IGNORECASE,
)
UPLOAD_MIDDLEWARE_PATTERN = re.compile(
    r"\b(multer|formidable|busboy|multiparty|express-fileupload)\b",
    re.IGNORECASE,
)
SUCCESS_STATE_PATTERN = re.compile(
    r"(set(?:Status|State)\(\s*['\"]success|setSuccess\s*\(|成功|alert\s*\()",
    re.IGNORECASE,
)


def _contract_value(contract: dict[str, Any], *keys: str) -> Any:
    """兼容 camelCase / snake_case 两种契约字段名。"""
    for key in keys:
        if key in contract:
            return contract[key]
    return None


def _contract_text(contract: dict[str, Any]) -> str:
    return json.dumps(contract, ensure_ascii=False, default=str)


SQL_COLUMN_STOPWORDS = {
    "PRIMARY",
    "FOREIGN",
    "UNIQUE",
    "CONSTRAINT",
    "KEY",
    "INDEX",
    "FULLTEXT",
    "SPATIAL",
    "CHECK",
}


def _split_sql_definitions(body: str) -> list[str]:
    """按顶层逗号切分字段定义；``DECIMAL(10,2)`` 不能被拆开。"""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in body:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def parse_ddl_schema(ddl: str) -> dict[str, list[str]]:
    """解析 CREATE TABLE，返回 ``{表名: [字段名]}``。

    按括号配对截取字段定义，兼容 ``DECIMAL(10,2)`` 这类嵌套括号；
    同时跳过 PRIMARY / FOREIGN / KEY 等表级约束行。
    """
    schema: dict[str, list[str]] = {}
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"[\"'`\[]?([A-Za-z_][\w]*)[\"'`\]]?\s*\(",
        re.IGNORECASE,
    )
    for match in pattern.finditer(ddl or ""):
        table = match.group(1)
        depth = 1
        index = match.end()
        while index < len(ddl) and depth:
            char = ddl[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        if depth != 0:
            continue
        body = ddl[match.end() : index - 1]
        columns: list[str] = []
        for line in _split_sql_definitions(body):
            stripped = line.strip()
            if not stripped:
                continue
            name = stripped.split()[0].strip("\"'`[]")
            if name.upper() in SQL_COLUMN_STOPWORDS:
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                continue
            columns.append(name)
        schema[table] = columns
    return schema


def _ddl_tables(ddl: str) -> list[str]:
    return list(parse_ddl_schema(ddl))


def _ddl_columns(ddl: str) -> list[str]:
    columns: list[str] = []
    for table_columns in parse_ddl_schema(ddl).values():
        columns.extend(table_columns)
    return columns


# ------------------------------------------------------------
# ORM 模型 vs 迁移 DDL：Unknown column 的静态门禁
# ------------------------------------------------------------
SYSTEM_TABLE_NAMES = {
    "user",
    "users",
    "account",
    "accounts",
    "session",
    "sessions",
    "role",
    "roles",
    "permission",
    "permissions",
    "user_role",
    "user_roles",
    "refresh_token",
    "refresh_tokens",
    "audit_log",
    "audit_logs",
    "_schema_migrations",
    "schema_migrations",
    "migrations",
}
SYSTEM_TABLE_PREFIXES = ("auth_", "oauth_", "session_")
VERSION_SEGMENTS = {"api", "v1", "v2", "v3", "v4"}
TABLE_CONSTANT_PATTERN = re.compile(r"\bconst\s+TABLE\s*=\s*(['\"])([^'\"]+)\1")
COLLECTION_CONSTANT_PATTERN = re.compile(
    r"\bconst\s+COLLECTION_PATH\s*=\s*(['\"])([^'\"]+)\1"
)


def _normalize_word(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _singular_word(value: str) -> str:
    word = _normalize_word(value)
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "zes", "ches", "shes")) and len(word) > 3:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 2:
        return word[:-1]
    return word


def _collection_resource(path: str) -> str:
    segments = [
        segment
        for segment in str(path or "").split("?", 1)[0].split("/")
        if segment and not segment.startswith(("{", ":"))
    ]
    for segment in reversed(segments):
        normalized = _normalize_word(segment)
        if normalized and normalized not in VERSION_SEGMENTS:
            return normalized
    return ""


def _manifest_payload(package_root: Path) -> dict[str, Any]:
    content = _read_text(package_root / "server" / "api-routes.json")
    try:
        payload = json.loads(content) if content else {}
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def package_collection_path(package_root: Path) -> str:
    manifest = _manifest_payload(package_root)
    collection = str(manifest.get("collection") or "").strip()
    if collection:
        return collection
    frontend = _read_text(package_root / "src" / "fullstack-api.jsx")
    match = COLLECTION_CONSTANT_PATTERN.search(frontend)
    return match.group(2).strip() if match else ""


def core_business_table(
    package_root: Path,
    contract: dict[str, Any] | None = None,
) -> str:
    """确定导出包的核心业务表。

    顺序：集合路由资源名 → 清单 table → schema.core.json → 第一张非系统表。
    集合路由必须优先：它代表用户真正访问的 CRUD 端点，ORM 指向 users
    而集合是 /api/v1/resumes 时，健康检查会“假通过”，CRUD 却 500。
    """
    migration = _read_text(package_root / "server" / "migrations" / "001_init.sql")
    ddl = migration
    if contract is not None:
        ddl = str(_contract_value(contract, "databaseDdl", "database_ddl") or migration)
    tables = list(parse_ddl_schema(ddl))
    resource = _collection_resource(package_collection_path(package_root))
    if resource:
        for name in tables:
            if _normalize_word(name) == resource:
                return name
        for name in tables:
            if _singular_word(name) == _singular_word(resource):
                return name
    manifest_table = str(_manifest_payload(package_root).get("table") or "").strip()
    if manifest_table:
        return manifest_table
    core_json = _read_text(package_root / "server" / "schema.core.json")
    if core_json:
        try:
            payload = json.loads(core_json)
        except json.JSONDecodeError:
            payload = {}
        table = (
            str(payload.get("table") or "").strip() if isinstance(payload, dict) else ""
        )
        if table:
            return table
    for name in tables:
        lowered = name.lower()
        if lowered not in SYSTEM_TABLE_NAMES and not lowered.startswith(
            SYSTEM_TABLE_PREFIXES
        ):
            return name
    return tables[0] if tables else ""


def _js_balanced_literal(content: str, marker: str) -> str:
    index = content.find(marker)
    if index < 0:
        return ""
    equals = content.find("=", index + len(marker))
    if equals < 0:
        return ""
    starts = [
        position
        for position in (content.find("[", equals), content.find("{", equals))
        if position >= 0
    ]
    if not starts:
        return ""
    start = min(starts)
    opening = content[start]
    closing = "]" if opening == "[" else "}"
    depth = 1
    quote: str | None = None
    position = start + 1
    while position < len(content) and depth:
        char = content[position]
        if quote:
            if char == "\\":
                position += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
        position += 1
    return content[start:position] if depth == 0 else ""


def _js_orm_columns(db_code: str) -> list[str]:
    literal = _js_balanced_literal(db_code, "const COLUMN_DEFS")
    columns: list[str] = []
    if literal:
        try:
            payload = json.loads(literal)
        except json.JSONDecodeError:
            payload = []
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, (list, tuple)) and item:
                    columns.append(str(item[0]).strip())
    if not columns:
        for match in re.finditer(
            r"\battributes(?:\[\s*['\"]([^'\"]+)['\"]\s*\]|\.([A-Za-z_]\w*))\s*=",
            db_code,
        ):
            name = (match.group(1) or match.group(2) or "").strip()
            if name:
                columns.append(name)
    return [column for column in columns if column]


def check_orm_ddl_consistency(
    package_root: Path,
    contract: dict[str, Any] | None = None,
) -> list[str]:
    """静态确认 ``server/db.js`` 的 ORM 模型没有引用迁移中不存在的字段。

    真实事故：多表 DDL 第一条是 users，ORM 生成
    ``TABLE='users' / COLUMN_DEFS=[id,name,status]``，生产迁移创建的是
    resumes(file_name, content, ...)。服务启动、健康检查都正常，
    但 GET/POST /api/v1/resumes 直接 500 Unknown column 'name'。
    """
    issues: list[str] = []
    db_code = _read_text(package_root / "server" / "db.js")
    if not db_code:
        return ["缺少 server/db.js，无法校验 ORM 模型与迁移 DDL 一致性"]

    table = core_business_table(package_root, contract)
    migration = _read_text(package_root / "server" / "migrations" / "001_init.sql")
    schema = parse_ddl_schema(migration)
    if not table:
        issues.append("无法确定核心业务表，ORM/DDL 一致性校验失败")
        expected: list[str] = []
    elif table not in schema:
        issues.append(
            f"迁移 001_init.sql 未创建核心业务表 {table}，部署后 CRUD 必然失败"
        )
        expected = []
    else:
        expected = schema[table]

    orm_match = TABLE_CONSTANT_PATTERN.search(db_code)
    orm_table = orm_match.group(2).strip() if orm_match else ""
    if not orm_table:
        issues.append("server/db.js 未声明 const TABLE")
    elif table and orm_table.lower() != table.lower():
        issues.append(
            f"ORM 目标表与业务路由不一致：db.js TABLE={orm_table}，"
            f"业务路由对应 {table}（Unknown column 的直接来源）"
        )

    index_code = _read_text(package_root / "server" / "index.js")
    index_match = TABLE_CONSTANT_PATTERN.search(index_code)
    if index_match and table and index_match.group(2).lower() != table.lower():
        issues.append(
            f"Express 会话层表名与业务路由不一致：index.js TABLE="
            f"{index_match.group(2)}，业务路由对应 {table}"
        )

    orm_columns = _js_orm_columns(db_code)
    if not orm_columns:
        issues.append("server/db.js 未解析出 COLUMN_DEFS，ORM 字段不可验证")
    elif expected:
        allowed = {column.lower() for column in expected}
        unknown = [column for column in orm_columns if column.lower() not in allowed]
        if unknown:
            issues.append(
                "ORM 使用了迁移 DDL 中不存在的字段（运行时 500 Unknown column）："
                + "、".join(unknown)
                + f"；{table} 真实字段："
                + "、".join(expected)
            )
    return issues


def check_business_semantics(
    package_root: Path,
    contract: dict[str, Any],
    backend_routes: dict[str, set[str]] | None = None,
    *,
    require_upload: bool | None = None,
    require_success_state: bool | None = None,
) -> list[str]:
    """检查“契约要求的业务能力”是否真的落到生成代码里。

    这里不追求运行所有业务逻辑，而是抓住最容易“文档有、代码没有”的断点：

      1. 契约声明的每个 API 路由必须出现在后端路由表；
      2. 契约 DDL 中每张表必须出现在 ``server/schema.sql``；
      3. 契约提到上传/附件时，后端必须引入上传中间件，
         且 DDL 中存在 file/upload/attachment/resume 类字段；
      4. 契约存在真实 API 交互时，前端必须包含成功态提示或状态切换。

    这些断言由契约驱动：纯展示/轻量应用不会被强行要求 multer。
    """
    issues: list[str] = []
    shape = str(_contract_value(contract, "appShape", "app_shape") or "")
    if shape != "full_stack_system":
        return issues

    routes = backend_routes or collect_backend_routes(package_root)
    contract_routes = _contract_value(contract, "apiRoutes", "api_routes") or []
    for route in contract_routes:
        if not isinstance(route, dict):
            continue
        method = str(route.get("method") or "").upper()
        path = str(route.get("path") or "")
        if not method or not path:
            continue
        normalized = normalize_route(path)
        if method not in routes.get(normalized, set()):
            issues.append(f"契约路由未落到后端: {method} {path}")

    ddl = str(_contract_value(contract, "databaseDdl", "database_ddl") or "").strip()
    if not ddl:
        issues.append("全栈契约缺少 databaseDdl，无法验证数据库表")
        return issues

    schema_file = package_root / "server" / "schema.sql"
    if not schema_file.is_file():
        issues.append("缺少 server/schema.sql，数据库表未落盘")
        schema = ""
    else:
        schema = schema_file.read_text(encoding="utf-8", errors="replace")

    tables = _ddl_tables(ddl)
    if not tables:
        issues.append("契约 DDL 中未解析出任何 CREATE TABLE")
    # 生产迁移必须覆盖契约的每一张表；SQLite 预览 schema.sql 只需覆盖
    # 前端真正 CRUD 的核心业务表（ORM 也只建模这一张）。
    migration = _read_text(package_root / "server" / "migrations" / "001_init.sql")
    for table in tables:
        target = migration or schema
        if not re.search(rf"\b{re.escape(table)}\b", target, re.IGNORECASE):
            issues.append(f"契约表未写入生产迁移 001_init.sql: {table}")
    core_table = core_business_table(package_root, contract)
    if core_table and not re.search(
        rf"\b{re.escape(core_table)}\b", schema, re.IGNORECASE
    ):
        issues.append(f"核心业务表未写入 server/schema.sql: {core_table}")

    contract_blob = _contract_text(contract)
    upload_required = (
        require_upload
        if require_upload is not None
        else bool(UPLOAD_INTENT_PATTERN.search(contract_blob))
    )
    if upload_required:
        index_file = package_root / "server" / "index.js"
        index_code = (
            index_file.read_text(encoding="utf-8", errors="replace")
            if index_file.is_file()
            else ""
        )
        middleware = UPLOAD_MIDDLEWARE_PATTERN.search(index_code)
        if not middleware:
            issues.append(
                "契约要求文件上传，但 server/index.js 未引入 "
                "multer/formidable/busboy 等上传中间件"
            )
        else:
            package_file = package_root / "server" / "package.json"
            dependencies: dict[str, Any] = {}
            if package_file.is_file():
                try:
                    dependencies = (
                        json.loads(package_file.read_text(encoding="utf-8")).get(
                            "dependencies"
                        )
                        or {}
                    )
                except json.JSONDecodeError:
                    dependencies = {}
            expected_dep = middleware.group(1).lower()
            if not any(expected_dep in str(name).lower() for name in dependencies):
                issues.append(
                    f"server/index.js 使用了 {middleware.group(1)}，"
                    "但 server/package.json 未声明该依赖"
                )
        upload_columns = [
            column
            for column in _ddl_columns(ddl)
            if re.search(r"(file|upload|attachment|resume)", column, re.IGNORECASE)
        ]
        if not upload_columns:
            issues.append(
                "契约要求文件上传，但 DDL 中没有 file/upload/attachment/resume 类字段"
            )

    interactions = (
        _contract_value(contract, "componentInteractions", "component_interactions")
        or []
    )
    has_api_interaction = any(
        isinstance(item, dict)
        and str(item.get("apiRoute") or item.get("api_route") or "").strip()
        for item in interactions
    )
    success_required = (
        require_success_state
        if require_success_state is not None
        else has_api_interaction
    )
    if success_required:
        frontend_code = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted((package_root / "src").rglob("*"))
            if path.is_file() and path.suffix.lower() in FRONTEND_SUFFIXES
        )
        if not SUCCESS_STATE_PATTERN.search(frontend_code):
            issues.append(
                "契约存在 API 交互，但前端未发现成功态提示"
                "（setStatus('success') / setSuccess / alert / 成功文案）"
            )

    return issues


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


def wait_health_status(
    base_url: str,
    container: str,
    *,
    expect_status: int,
    timeout: float = 90,
) -> dict[str, Any]:
    """等待健康检查返回指定状态码；用于验证“未迁移 = 503”这一契约。"""
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            status, payload = http_json(f"{base_url}/api/health")
            if status == expect_status and isinstance(payload, dict):
                return payload
            last_error = f"HTTP {status}: {payload}"
        except Exception as exc:  # noqa: BLE001 - 启动阶段连接拒绝属于正常现象
            last_error = str(exc)
        time.sleep(1)
    logs = run_command(["docker", "logs", "--tail", "50", container], timeout=20)
    raise AcceptanceFailure(
        f"/api/health 在 {timeout:.0f}s 内未返回 HTTP {expect_status}: "
        f"{last_error}\n容器日志:\n{tail(logs.stdout + logs.stderr)}"
    )


def start_container(
    tag: str,
    report: Report,
    *,
    env: dict[str, str] | None = None,
    env_file: Path | None = None,
    extra_args: list[str] | None = None,
) -> tuple[str, str] | None:
    """启动验收容器并等待端口映射，返回 ``(container, base_url)``。"""
    container = f"vibe-acceptance-run-{uuid.uuid4().hex[:8]}"
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        container,
        "--label",
        "vibe.acceptance=1",
        "-p",
        "127.0.0.1::3000",
    ]
    command.extend(extra_args or [])
    if env_file is not None:
        # 模拟云端平台注入 Secret：连接串不出现在 docker run 参数中。
        command.extend(["--env-file", str(env_file)])
    for key, value in (env or {}).items():
        command.extend(["-e", f"{key}={value}"])
    command.append(tag)

    proc = run_command(command, timeout=60)
    if proc.returncode != 0:
        report.fail(format_command_failure("docker run ", proc))
        return None

    port = ""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not port:
        port_proc = run_command(["docker", "port", container, "3000/tcp"], timeout=10)
        output = (port_proc.stdout or "").strip().splitlines()
        if output:
            port = output[0].rsplit(":", 1)[-1]
        else:
            time.sleep(0.5)
    if not port:
        report.fail("docker port 未返回 3000 映射")
        run_command(["docker", "rm", "-f", container], timeout=30)
        return None
    return container, f"http://127.0.0.1:{port}"


def check_forbidden_db_env(container: str, report: Report) -> bool:
    """确认容器没有夹带外部数据库配置；返回 True 表示检查通过。"""
    forbidden = [
        item
        for item in container_env(container)
        if FORBIDDEN_DB_ENV.match(item.split("=", 1)[0])
    ]
    if forbidden:
        report.fail(f"容器携带数据库环境变量: {', '.join(forbidden)}")
        return False
    return True


def verify_health(
    base_url: str,
    container: str,
    report: Report,
    *,
    storage: str,
    runtime: str,
) -> dict[str, Any] | None:
    health = wait_health(base_url, container)
    if health.get("status") != "ok":
        report.fail(f"健康检查 status 异常: {health}")
        return None
    if health.get("storage") != storage:
        report.fail(f"期望 storage={storage}，实际 {health}")
        return None
    if health.get("runtime") != runtime:
        report.fail(f"期望 runtime={runtime}，实际 {health}")
        return None
    return health


SCHEMA_500_PATTERN = re.compile(
    r"unknown column|doesn't have a default value|cannot be null|"
    r"no such column|unknown field|field list",
    re.IGNORECASE,
)


def _describe_schema_500(action: str, collection: str, status: int, body: Any) -> str:
    detail = json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body
    if status == 500 and SCHEMA_500_PATTERN.search(detail):
        return (
            f"{action} {collection} 因表结构/字段缺失返回 500 "
            f"（Unknown column 类致命错误）: {body}"
        )
    return f"{action} {collection} -> HTTP {status}: {body}"


def verify_crud(
    base_url: str,
    collection: str,
    report: Report,
    expected_table: str | None = None,
) -> dict[str, Any] | None:
    """GET 列表 + POST 新增 + 再 GET 校验，返回新增记录。

    ``expected_table`` 来自导出包的核心业务表（清单 / 集合路由）。
    运行期必须确认服务真正读写的是这张表：如果 ORM 漂移到 users，
    健康检查可能仍 200，但业务 CRUD 会 500 Unknown column。
    """
    status, listing = http_json(f"{base_url}{collection}")
    if status != 200:
        report.fail(_describe_schema_500("GET", collection, status, listing))
        return None
    if (
        expected_table
        and isinstance(listing, dict)
        and str(listing.get("table") or "").strip()
        and str(listing["table"]).lower() != expected_table.lower()
    ):
        report.fail(
            f"GET {collection} 命中了错误的数据表："
            f"响应 table={listing.get('table')}，核心业务表={expected_table}"
            "（ORM 与迁移 DDL 已错位）"
        )
        return None
    name = f"acceptance-{uuid.uuid4().hex[:8]}"
    status, created = http_json(
        f"{base_url}{collection}",
        method="POST",
        payload={"name": name, "title": name, "status": "todo"},
    )
    if status not in (200, 201) or not isinstance(created, dict):
        report.fail(_describe_schema_500("POST", collection, status, created))
        return None
    record_id = created.get("id")
    status, listing = http_json(f"{base_url}{collection}")
    items = listing.get("items") if isinstance(listing, dict) else listing
    if status != 200 or not isinstance(items, list):
        report.fail(f"再次 GET {collection} 失败: HTTP {status} {listing}")
        return None
    if record_id is not None and not any(
        isinstance(item, dict) and item.get("id") == record_id for item in items
    ):
        report.fail(f"写入后未在列表查到记录 id={record_id}")
        return None
    report.ok(f"CRUD 正常：{collection} GET/POST 已落库（id={record_id}）")
    return created


# ------------------------------------------------------------
# 3.1 真实 multipart 上传
# ------------------------------------------------------------
UPLOAD_ROUTE_PATTERN = re.compile(
    r"app\.(post|put|patch)\(\s*"
    r"(?:(?P<quote>['\"`])(?P<literal>[^'\"`]+)(?P=quote)"
    r"|(?P<identifier>[A-Za-z_$][\w$]*))"
    r"\s*,\s*upload\.single\(\s*"
    r"(?P<fieldquote>['\"`])(?P<field>[^'\"`]+)(?P=fieldquote)",
    re.IGNORECASE,
)
STRING_CONSTANT_PATTERN = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*" r"(['\"`])([^'\"`]+)\2"
)


def find_upload_route(
    package_root: Path,
    preferred_path: str | None = None,
) -> tuple[str, str, str] | None:
    """从生成的 ``server/index.js`` 提取 multer 挂载的 ``(method, path, field)``。

    契约上传路由有时就是集合路径（POST /api/v1/resumes 同时接收文件与字段），
    有时是独立动作路径；优先返回集合路径，保证拿到真正落库的那条。

    模板里的路由也可能写成 ``app.post(COLLECTION_PATH, upload.single('file'))``，
    因此需要把字符串常量解析回真实路径，不能只匹配字面量。
    """
    index_file = package_root / "server" / "index.js"
    if not index_file.is_file():
        return None
    text = index_file.read_text(encoding="utf-8", errors="replace")
    constants = {
        match.group(1): match.group(3)
        for match in STRING_CONSTANT_PATTERN.finditer(text)
    }
    matches: list[tuple[str, str, str]] = []
    for match in UPLOAD_ROUTE_PATTERN.finditer(text):
        path = match.group("literal")
        if path is None:
            path = constants.get(match.group("identifier") or "")
        if not path:
            continue
        matches.append((match.group(1).upper(), path, match.group("field")))
    if not matches:
        return None
    if preferred_path:
        for item in matches:
            if normalize_route(item[1]) == normalize_route(preferred_path):
                return item
    return matches[0]


def iter_strings(value: Any) -> list[str]:
    """递归收集 JSON 中的所有字符串，用于定位上传后的文件 URL。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in iter_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in iter_strings(child)]
    return []


def http_multipart(
    url: str,
    *,
    field: str,
    filename: str,
    content: bytes,
    fields: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """构造并发送真实 multipart/form-data 请求。"""
    boundary = f"----vibeaccept{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in (fields or {}).items():
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                f"\r\n\r\n{value}\r\n"
            ).encode("utf-8")
        )
    chunks.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream'
            "\r\n\r\n"
        ).encode("utf-8")
        + content
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    request = urllib_request.Request(
        url,
        data=b"".join(chunks),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body) if body else None
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body
    except urllib_error.URLError as exc:
        return 0, str(exc)


def contract_requires_upload(contract: dict[str, Any]) -> bool:
    """与 ``check_business_semantics`` 保持一致的上传意图判断。"""
    return bool(UPLOAD_INTENT_PATTERN.search(_contract_text(contract)))


def verify_upload(
    base_url: str,
    package_root: Path,
    report: Report,
    *,
    preferred_path: str | None = None,
) -> None:
    """发送真实 multipart 请求，并回读静态文件校验内容。"""
    route = find_upload_route(package_root, preferred_path)
    if not route:
        report.fail(
            "契约要求文件上传，但生成代码中未找到 "
            "upload.single(...) 挂载的 POST/PUT/PATCH 路由"
        )
        return
    method, path, field = route
    if method != "POST":
        report.fail(f"上传路由方法异常: {method} {path}")
        return

    filename = f"acceptance-{uuid.uuid4().hex[:8]}.pdf"
    content = b"%PDF-1.4\n% vibe acceptance upload\n"
    status, payload = http_multipart(
        f"{base_url}{path}",
        field=field,
        filename=filename,
        content=content,
        fields={
            "name": "验收用户",
            "email": "acceptance@example.com",
            "position": "QA",
        },
    )
    if status not in (200, 201):
        report.fail(f"multipart POST {path} -> HTTP {status}: {payload}")
        return

    upload_urls = [value for value in iter_strings(payload) if "/uploads/" in value]
    if not upload_urls:
        report.fail(f"上传响应未返回 /uploads/ 文件地址: {payload}")
        return
    upload_url = upload_urls[0]
    try:
        with urllib_request.urlopen(f"{base_url}{upload_url}", timeout=15) as response:
            stored = response.read()
    except urllib_error.URLError as exc:
        report.fail(f"上传文件静态回读失败 {upload_url}: {exc}")
        return
    if stored != content:
        report.fail(
            f"上传文件内容不一致: 期望 {len(content)} 字节，实际 {len(stored)} 字节"
        )
        return
    report.ok(f"multipart 上传真实落盘：POST {path} -> {upload_url}，回读一致")


# ------------------------------------------------------------
# 3.2 真实 MySQL 持久化
# ------------------------------------------------------------
def _pymysql() -> Any:
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指令
        raise AcceptanceFailure(
            "缺少 pymysql，无法执行真实 MySQL 验收（pip install pymysql）"
        ) from exc
    return pymysql


def mysql_provision(report: Report) -> str | None:
    """在宿主机 MySQL 创建一次性验收库，返回库名。"""
    pymysql = _pymysql()
    database = f"vibe_acc_{uuid.uuid4().hex[:8]}"
    try:
        connection = pymysql.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            connect_timeout=5,
            autocommit=True,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_unicode_ci"
            )
        connection.close()
    except Exception as exc:  # noqa: BLE001 - 连接失败统一转为验收结论
        report.fail(
            f"连接宿主机 MySQL 失败（{MYSQL_HOST}:{MYSQL_PORT}）: {exc}；"
            "确认 ~/Workspace/infra 已启动，或设置 ACCEPTANCE_MYSQL=0 跳过"
        )
        return None
    return database


def mysql_drop(database: str) -> None:
    if not MYSQL_IDENTIFIER.match(database):
        return
    try:
        pymysql = _pymysql()
        connection = pymysql.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            connect_timeout=5,
            autocommit=True,
        )
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        connection.close()
    except Exception:  # noqa: BLE001 - 清理失败不影响主结论
        print(f"   ! 清理 MySQL 临时库失败（可手动删除）: {database}")


def mysql_fetch_row(database: str, table: str, record_id: Any) -> dict[str, Any] | None:
    """用宿主机连接直接查库，证明 API 写的是真 MySQL。"""
    if not MYSQL_IDENTIFIER.match(table):
        raise AcceptanceFailure(f"非法表名，拒绝查询: {table}")
    pymysql = _pymysql()
    connection = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=database,
        connect_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM `{table}` WHERE `id` = %s", (record_id,))
            return cursor.fetchone()
    finally:
        connection.close()


def docker_run_and_verify_sqlite(
    tag: str,
    collection_path: str | None,
    package_root: Path,
    contract: dict[str, Any],
    report: Report,
) -> str | None:
    """无数据库配置启动，验证 SQLite 回退 + 真实 multipart 上传。"""
    started = start_container(tag, report)
    if not started:
        return None
    container, base_url = started
    keep_container = False
    try:
        if not check_forbidden_db_env(container, report):
            return None
        health = verify_health(
            base_url,
            container,
            report,
            storage="sqlite",
            runtime="express-sqlite",
        )
        if health is None:
            return None
        expected_table = core_business_table(package_root, contract)
        if (
            expected_table
            and str(health.get("table") or "").strip()
            and str(health["table"]).lower() != expected_table.lower()
        ):
            report.fail(
                "SQLite 预览命中了错误的数据表："
                f"health.table={health.get('table')}，核心业务表={expected_table}"
            )
            return None
        report.ok(
            "无数据库配置启动成功："
            f"storage={health.get('storage')} runtime={health.get('runtime')} "
            f"db={health.get('dbPath')}"
        )
        if (
            verify_crud(
                base_url,
                collection_path or "/api/data",
                report,
                expected_table=expected_table,
            )
            is None
        ):
            return None
        if contract_requires_upload(contract):
            verify_upload(
                base_url,
                package_root,
                report,
                preferred_path=collection_path,
            )
        keep_container = True
        return container
    except AcceptanceFailure as exc:
        report.fail(str(exc))
        return None
    finally:
        if not keep_container:
            run_command(["docker", "rm", "-f", container], timeout=30)


def write_production_env(database_url: str) -> tuple[Path, Path]:
    """写一份模拟云端 Secret 注入的 .env，返回 ``(env_file, temp_dir)``。"""
    directory = Path(tempfile.mkdtemp(prefix="vibe-prod-env-"))
    env_file = directory / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NODE_ENV=production",
                "PORT=3000",
                f"DATABASE_URL={database_url}",
                "DB_ENGINE=mysql",
                "DB_SSL=false",
                "DB_POOL_MAX=5",
                "CORS_ORIGINS=https://console.example.com",
                "FULLSTACK_DATA_DIR=/app/data",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return env_file, directory


def run_migration_container(
    tag: str,
    env_file: Path,
    report: Report,
    *,
    label: str,
    expect_applied: bool,
) -> bool:
    """以独立发布步骤执行 ``node server/migrate.mjs``。

    与业务容器分离，模拟 Render preDeployCommand / 阿里云 Job 的真实发布顺序。
    """
    command = [
        "docker",
        "run",
        "--rm",
        "--label",
        "vibe.acceptance=1",
        "--add-host",
        f"{MYSQL_CONTAINER_HOST}:host-gateway",
        "--env-file",
        str(env_file),
        tag,
        "node",
        "server/migrate.mjs",
    ]
    proc = run_command(command, timeout=180)
    if VERBOSE:
        print(tail(proc.stdout + "\n" + proc.stderr, 20))
    if proc.returncode != 0:
        report.fail(format_command_failure(f"{label} ", proc))
        return False
    output = proc.stdout + proc.stderr
    if expect_applied:
        if not re.search(r"applied\s+001_init\.sql", output):
            report.fail(f"{label} 未应用 001_init.sql，输出:\n{tail(output)}")
            return False
    else:
        if "alreadyApplied" not in output or "001_init.sql" not in output:
            report.fail(f"{label} 未确认迁移幂等，输出:\n{tail(output)}")
            return False
    report.ok(f"{label}成功（exit=0）")
    return True


def mysql_migration_rows(database: str) -> list[dict[str, Any]]:
    """读取迁移记录表；不存在时返回空列表。"""
    pymysql = _pymysql()
    connection = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=database,
        connect_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            try:
                cursor.execute(
                    "SELECT filename, checksum, applied_at "
                    "FROM `_schema_migrations` ORDER BY filename"
                )
            except pymysql.err.ProgrammingError:
                return []
            return list(cursor.fetchall())
    finally:
        connection.close()


def mysql_table_columns(database: str, table: str) -> list[str]:
    """直接查询 information_schema，确认真实表与字段已由迁移创建。"""
    if not MYSQL_IDENTIFIER.match(table):
        raise AcceptanceFailure(f"非法表名，拒绝查询: {table}")
    pymysql = _pymysql()
    connection = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=database,
        connect_timeout=5,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (database, table),
            )
            return [str(row[0]) for row in cursor.fetchall()]
    finally:
        connection.close()


def docker_run_and_verify_mysql(
    tag: str,
    collection_path: str | None,
    package_root: Path,
    contract: dict[str, Any],
    report: Report,
) -> str | None:
    """模拟真实云端部署：env 注入 → 未迁移 503 → 显式迁移 → 200 + CRUD。

    与旧流程的关键差异：不再通过 ``DB_AUTO_MIGRATE`` 让 ORM 隐式建表。
    验收严格复刻生产发布顺序，未执行迁移时服务必须不可用（503）。
    """
    expected_table = core_business_table(package_root, contract)
    if not expected_table:
        report.fail("无法从导出包确定核心业务表，MySQL 部署验收无从比对")
        return None
    report.ok(
        f"核心业务表={expected_table}，集合路由="
        f"{package_collection_path(package_root) or '/api/data'}"
    )
    database = mysql_provision(report)
    if not database:
        return None
    env_file: Path | None = None
    env_dir: Path | None = None
    container = ""
    keep_container = False
    try:
        credentials = (
            f"{urllib_parse.quote(MYSQL_USER, safe='')}:"
            f"{urllib_parse.quote(MYSQL_PASSWORD, safe='')}"
        )
        database_url = (
            f"mysql://{credentials}@{MYSQL_CONTAINER_HOST}:{MYSQL_PORT}/"
            f"{database}?charset=utf8mb4"
        )
        env_file, env_dir = write_production_env(database_url)
        report.ok("已生成生产 .env 并通过 --env-file 注入镜像（连接串不落命令行）")

        started = start_container(
            tag,
            report,
            env_file=env_file,
            extra_args=[
                "--add-host",
                f"{MYSQL_CONTAINER_HOST}:host-gateway",
            ],
        )
        if not started:
            return None
        container, base_url = started

        # 1) 空库 + 未迁移：健康检查必须失败，绝不能被 ORM 悄悄建表。
        degraded = wait_health_status(base_url, container, expect_status=503)
        if (
            degraded.get("storage") != "mysql"
            or degraded.get("tableReady") is not False
        ):
            report.fail(f"未迁移时健康检查语义异常: {degraded}")
            return None
        report.ok(
            "未迁移数据库健康检查正确返回 503："
            f"status={degraded.get('status')} tableReady=false"
        )
        table = str(degraded.get("table") or "")
        if not table:
            report.fail(f"健康检查未返回表名，无法继续验收: {degraded}")
            return None
        if table.lower() != expected_table.lower():
            report.fail(
                "健康检查使用的表与集合路由不一致："
                f"health.table={table}，集合资源对应 {expected_table}；"
                "ORM 与迁移 DDL 已错位，CRUD 必然 Unknown column"
            )
            return None

        # 2) 独立发布步骤执行迁移，失败必须非 0 退出。
        if not run_migration_container(
            tag, env_file, report, label="生产迁移首次执行", expect_applied=True
        ):
            return None

        rows = mysql_migration_rows(database)
        matched = [row for row in rows if row.get("filename") == "001_init.sql"]
        if not matched:
            report.fail(f"_schema_migrations 未记录 001_init.sql: {rows}")
            return None
        checksum = str(matched[0].get("checksum") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            report.fail(f"迁移 checksum 异常: {checksum!r}")
            return None
        columns = mysql_table_columns(database, table)
        if not columns:
            report.fail(f"迁移后真实表不存在或没有字段: {database}.{table}")
            return None
        # 运行期二次确认：ORM 建模的每个字段都必须真实存在于 MySQL。
        # 静态检查可能被手改产物绕过；这里以数据库 INFORMATION_SCHEMA 为准。
        orm_columns = _js_orm_columns(_read_text(package_root / "server" / "db.js"))
        allowed = {column.lower() for column in columns}
        unknown = [column for column in orm_columns if column.lower() not in allowed]
        if unknown:
            report.fail(
                "ORM 字段在真实 MySQL 表中不存在（运行时会 500 Unknown column）："
                + "、".join(unknown)
                + f"；{database}.{table} 实际字段："
                + "、".join(columns)
            )
            return None
        report.ok(
            f"真实 MySQL 建表完成且与 ORM 字段一致：{database}.{table} "
            f"columns={','.join(columns[:6])}"
            f"{'...' if len(columns) > 6 else ''}，checksum={checksum[:12]}…"
        )

        # 3) 迁移后应用无需重启即可恢复 200；数据面也恢复可用。
        health = wait_health_status(base_url, container, expect_status=200)
        if (
            health.get("status") != "ok"
            or health.get("storage") != "mysql"
            or health.get("runtime") != "express-sequelize-mysql"
            or health.get("tableReady") is not True
        ):
            report.fail(f"迁移后健康检查语义异常: {health}")
            return None
        report.ok(
            "迁移后健康检查恢复 200："
            f"status=ok storage=mysql tableReady=true records={health.get('records')}"
        )

        created = verify_crud(
            base_url,
            collection_path or "/api/data",
            report,
            expected_table=expected_table,
        )
        if created is None:
            return None
        report.ok(f"核心业务表 {expected_table} 的 GET/POST 未因字段缺失返回 500")
        record_id = created.get("id")
        row = mysql_fetch_row(database, table, record_id)
        if row is None:
            report.fail(f"MySQL 直连查询未找到记录：table={table} id={record_id}")
            return None
        report.ok(f"MySQL 真实持久化：直连 {database}.{table} 查到 id={record_id}")

        # 4) 重复发布：迁移必须幂等，历史文件不得重复执行或改写 checksum。
        if not run_migration_container(
            tag, env_file, report, label="生产迁移重复执行", expect_applied=False
        ):
            return None
        rows_after = mysql_migration_rows(database)
        same_file = [row for row in rows_after if row.get("filename") == "001_init.sql"]
        if len(same_file) != 1:
            report.fail(f"迁移幂等失败：001_init.sql 记录数={len(same_file)}")
            return None
        if str(same_file[0].get("checksum") or "") != checksum:
            report.fail("重复迁移改写了 checksum，违反不可变迁移原则")
            return None
        report.ok("迁移幂等验证通过：重复发布未重复建表、checksum 未变化")

        keep_container = True
        return container
    except AcceptanceFailure as exc:
        report.fail(str(exc))
        return None
    finally:
        if not keep_container and container:
            run_command(["docker", "rm", "-f", container], timeout=30)
        if env_dir is not None:
            shutil.rmtree(env_dir, ignore_errors=True)
        # 验收库始终清理，避免污染宿主机 MySQL。
        mysql_drop(database)


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


def cleanup_docker(containers: list[str], tag: str | None) -> None:
    for container in containers:
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
    containers: list[str] = []
    image_tag = ""
    docker_ready = ensure_docker(report)

    try:
        stage("1/8 生成全栈代码")
        try:
            run_id, sandbox_id, payload, contract = generate_fullstack()
            print(
                f"   ✓ runId={run_id} sandboxId={sandbox_id} "
                f"collection={payload.get('collectionPath')}"
            )
        except Exception as exc:  # noqa: BLE001 - 统一转为验收失败
            report.fail(f"全栈代码生成失败: {exc}")
            return report.finish()

        stage("2/8 下载导出包")
        try:
            size = download_export(run_id, sandbox_id, archive_path)
            report.ok(f"已下载 project-export.tar.gz（{size / 1024:.1f} KB）")
        except Exception as exc:  # noqa: BLE001
            report.fail(f"导出包下载失败: {exc}")
            return report.finish()

        stage("3/8 解压与生产部署契约")
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
            report.ok(
                "DEPLOY.md / Dockerfile / render.yaml / .env.example / "
                "migrations 均存在"
            )
        try:
            contract_issues = check_production_contract(package_root)
            if contract_issues:
                for issue in contract_issues:
                    report.fail(issue)
            else:
                report.ok(
                    "环境变量隔离 / 显式迁移 / 严格 CORS / 503 健康检查 / "
                    "多阶段非 root 镜像 全部满足"
                )
        except Exception as exc:  # noqa: BLE001
            report.fail(f"生产部署契约检查失败: {exc}")

        stage("4/8 前后端路由一致性")
        try:
            backend_count, route_issues = check_route_consistency(package_root)
            if route_issues:
                for issue in route_issues:
                    report.fail(issue)
            else:
                report.ok(f"前端请求与后端 {backend_count} 条路由文本匹配，无 404 风险")
        except Exception as exc:  # noqa: BLE001
            report.fail(f"路由检查失败: {exc}")

        stage("5/8 业务语义断言")
        try:
            semantic_issues = check_business_semantics(package_root, contract)
            if semantic_issues:
                for issue in semantic_issues:
                    report.fail(issue)
            else:
                report.ok("契约路由 / 数据表 / 上传中间件 / 成功态与生成代码一致")

            orm_issues = check_orm_ddl_consistency(package_root, contract)
            if orm_issues:
                for issue in orm_issues:
                    report.fail(issue)
            else:
                core_table = core_business_table(package_root, contract)
                report.ok(
                    f"ORM 模型与迁移 DDL 字段一致（核心表 {core_table}），"
                    "不存在 Unknown column 风险"
                )
        except Exception as exc:  # noqa: BLE001
            report.fail(f"业务语义检查失败: {exc}")

        stage("6/8 纯净容器 npm install / build")
        if not docker_ready:
            report.fail("跳过容器内 npm 与 docker build（docker 不可用）")
        elif not (package_root / "Dockerfile").is_file():
            report.fail("缺少 Dockerfile，跳过 npm / docker build")
        else:
            npm_install_and_build(package_root, report)
            image_tag = docker_build(package_root, report) or ""

            if not image_tag:
                report.fail("docker build 未产出镜像，跳过运行验收")
            else:
                stage("7/8 无数据库配置运行 + SQLite + multipart")
                sqlite_container = docker_run_and_verify_sqlite(
                    image_tag,
                    payload.get("collectionPath"),
                    package_root,
                    contract,
                    report,
                )
                if sqlite_container:
                    containers.append(sqlite_container)

                stage("8/8 模拟云端部署：MySQL + Migration")
                if not MYSQL_ENABLED:
                    print("   - ACCEPTANCE_MYSQL=0，跳过真实 MySQL 验收")
                else:
                    mysql_container = docker_run_and_verify_mysql(
                        image_tag,
                        payload.get("collectionPath"),
                        package_root,
                        contract,
                        report,
                    )
                    if mysql_container:
                        containers.append(mysql_container)

        return report.finish()
    finally:
        cleanup_docker(containers, image_tag)
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
