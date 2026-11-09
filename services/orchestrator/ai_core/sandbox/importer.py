"""项目导入：Zip 解包、类型识别与沙盒启动命令生成。

设计目标：
  - 浏览器只上传原始字节，所有安全校验在服务端完成；
  - 防 Zip Slip（``../`` / 绝对路径）、符号链接、zip 炸弹与超大文件；
  - 统一剥掉压缩包最外层目录，保证 ``package.json`` / ``index.html``
    落在沙盒工作目录根部；
  - React 项目自动 ``npm install``，纯 HTML 项目使用镜像内置静态服务器。

导入后的项目与普通沙盒完全一致：后续 Agent 增量修改、Git checkpoint、
导出、销毁都复用同一条链路。
"""

from __future__ import annotations

import io
import json
import logging
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from ai_core.config import Settings
from ai_core.ports.sandbox import SandboxValidationError

logger = logging.getLogger(__name__)

ProjectKind = Literal["react", "html"]

# 依赖 / 缓存目录不进入沙盒：镜像与 npm install 会重新生成。
_SKIP_DIR_PARTS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".vite",
    ".next",
    ".nuxt",
    "__pycache__",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    "__MACOSX",
}
_SKIP_FILENAMES = {".DS_Store", "Thumbs.db"}
# 绝不导入与部署环境相关的密钥文件。
_SECRET_FILENAMES = {".env", ".env.local", ".env.production", ".npmrc"}
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")

_TEXT_SUFFIXES = {
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".html",
    ".htm",
    ".json",
    ".md",
    ".txt",
    ".svg",
    ".yml",
    ".yaml",
    ".xml",
    ".vue",
    ".svelte",
    ".astro",
}
_ENTRY_CANDIDATES = (
    "src/App.tsx",
    "src/App.jsx",
    "src/App.ts",
    "src/App.js",
    "App.tsx",
    "App.jsx",
    "App.ts",
    "App.js",
    "src/main.tsx",
    "src/main.jsx",
    "src/index.tsx",
    "src/index.jsx",
    "index.html",
)


@dataclass(frozen=True)
class ImportedProject:
    """一次导入的最终产物。

    ``files`` 是写入沙盒的完整字节集（含图片），``text_files`` 是返回给
    浏览器的源码快照（只含文本、有体积上限），用于画布定位与刷新恢复。
    """

    kind: ProjectKind
    files: dict[str, bytes]
    text_files: dict[str, str]
    entry_file: str
    start_command: str
    file_count: int
    template: str = "react-vite"
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------
# 安全读取
# ------------------------------------------------------------
def _normalize_zip_name(raw_name: str) -> str | None:
    """校验并归一化 zip 条目路径；目录 / 不安全条目返回 None。"""
    name = raw_name.replace("\\", "/").strip()
    if not name or name.endswith("/"):
        return None
    if name.startswith("/") or "\x00" in name:
        raise SandboxValidationError(f"压缩包包含非法绝对路径: {raw_name}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SandboxValidationError(f"压缩包包含越界路径: {raw_name}")
    return str(path)


def _should_skip(rel_path: str) -> bool:
    path = PurePosixPath(rel_path)
    if any(part in _SKIP_DIR_PARTS for part in path.parts[:-1]):
        return True
    name = path.name
    if name in _SKIP_FILENAMES:
        return True
    if name in _SECRET_FILENAMES or name.startswith(".env."):
        return True
    if name.lower().endswith(_SECRET_SUFFIXES):
        return True
    return False


def _strip_common_root(entries: dict[str, bytes]) -> dict[str, bytes]:
    """剥掉压缩包的单一顶层目录。

    例如 GitHub 下载的 ``my-app-main/src/App.jsx`` 统一变成 ``src/App.jsx``；
    如果根目录还有 package.json 等文件，则不剥，避免误伤。
    """
    if not entries:
        return entries
    parts = [PurePosixPath(name).parts for name in entries]
    first = {item[0] for item in parts}
    if len(first) != 1:
        return entries
    root = next(iter(first))
    if any(len(item) == 1 for item in parts):
        # 根目录本身有文件（如 README.md），说明顶层不是包装目录。
        return entries
    if not root:
        return entries
    return {
        str(PurePosixPath(*item[1:])): content
        for item, content in zip(parts, entries.values())
    }


def _decode_text_files(
    entries: dict[str, bytes],
    settings: Settings,
) -> dict[str, str]:
    """抽取可展示 / 可定位的文本源码，严格限制数量与总字节。"""
    result: dict[str, str] = {}
    total = 0
    for path in sorted(entries):
        suffix = PurePosixPath(path).suffix.lower()
        if suffix not in _TEXT_SUFFIXES:
            continue
        if len(result) >= settings.import_max_text_files:
            break
        content = entries[path]
        if total + len(content) > settings.import_max_text_bytes:
            break
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        result[path] = text
        total += len(content)
    return result


def _find_entry(entries: dict[str, bytes], kind: ProjectKind) -> str:
    if kind == "html":
        if "index.html" in entries:
            return "index.html"
        html_files = sorted(
            path for path in entries if path.lower().endswith((".html", ".htm"))
        )
        if html_files:
            return html_files[0]
        return "index.html"
    for candidate in _ENTRY_CANDIDATES:
        if candidate in entries:
            return candidate
    # 非常规目录结构：退回到第一个 src 下的组件文件。
    for path in sorted(entries):
        if path.startswith("src/") and path.lower().endswith(
            (".jsx", ".tsx", ".js", ".ts")
        ):
            return path
    return "src/App.jsx"


def _react_start_command(entries: dict[str, bytes], port: int) -> str:
    """根据 package.json scripts 决定 dev / start，并保证先安装依赖。"""
    package_path = "package.json"
    if package_path not in entries:
        raise SandboxValidationError("React 项目缺少 package.json")
    try:
        package = json.loads(entries[package_path].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxValidationError(f"package.json 解析失败: {exc}") from exc
    scripts = package.get("scripts") if isinstance(package, dict) else None
    scripts = scripts if isinstance(scripts, dict) else {}

    install = "npm install --no-audit --no-fund"
    if "dev" in scripts:
        return f"{install} && npm run dev -- --host 0.0.0.0 --port {port}"
    if "start" in scripts:
        # CRA / react-scripts 读取 PORT / HOST 环境变量。
        return f"{install} && npm start"
    raise SandboxValidationError(
        "package.json 既没有 dev 也没有 start 脚本，无法启动预览"
    )


def _html_re_root(entries: dict[str, bytes], settings: Settings) -> dict[str, bytes]:
    """纯 HTML 项目可能把 index.html 放在子目录，统一提升到根目录。"""
    if "index.html" in entries:
        return entries
    candidates = sorted(
        (
            PurePosixPath(path)
            for path in entries
            if path.lower().endswith("index.html")
        ),
        key=lambda item: len(item.parts),
    )
    if not candidates:
        raise SandboxValidationError("HTML 项目缺少 index.html")
    root = candidates[0].parent
    if not root.parts:
        return entries
    prefix = f"{root.as_posix()}/"
    return {
        path[len(prefix) :]: content
        for path, content in entries.items()
        if path.startswith(prefix)
    }


# ------------------------------------------------------------
# 对外入口
# ------------------------------------------------------------
def import_zip(
    data: bytes,
    settings: Settings,
) -> ImportedProject:
    """解析 zip 并生成可直接写入沙盒的项目。"""
    if not data:
        raise SandboxValidationError("上传的压缩包为空")
    if len(data) > settings.import_max_archive_bytes:
        raise SandboxValidationError(
            f"压缩包超过大小限制（{len(data)} > {settings.import_max_archive_bytes} 字节）"
        )

    warnings: list[str] = []
    entries: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > settings.import_max_entries:
                raise SandboxValidationError(
                    f"压缩包条目过多（{len(infos)} > {settings.import_max_entries}）"
                )
            total_uncompressed = 0
            for info in infos:
                if info.is_dir():
                    continue
                if info.flag_bits & 0x1:
                    raise SandboxValidationError("不支持加密压缩包")
                mode = info.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    raise SandboxValidationError(f"压缩包包含符号链接: {info.filename}")

                rel_path = _normalize_zip_name(info.filename)
                if rel_path is None or _should_skip(rel_path):
                    continue

                total_uncompressed += info.file_size
                if total_uncompressed > settings.import_max_uncompressed_bytes:
                    raise SandboxValidationError("压缩包解压后体积超过限制")

                content = archive.read(info)
                if len(content) > settings.import_max_uncompressed_bytes:
                    raise SandboxValidationError(f"单个文件体积超过限制: {rel_path}")
                entries[rel_path] = content
    except zipfile.BadZipFile as exc:
        raise SandboxValidationError("不是有效的 zip 压缩包") from exc

    entries = _strip_common_root(entries)
    if not entries:
        raise SandboxValidationError("压缩包中没有可导入的源码文件")

    if "package.json" in entries:
        kind: ProjectKind = "react"
        start_command = _react_start_command(
            entries,
            settings.sandbox_dev_port,
        )
        template = "react-vite"
    else:
        entries = _html_re_root(entries, settings)
        kind = "html"
        start_command = (
            f"node /opt/vibe-static-server.mjs /app {settings.sandbox_dev_port}"
        )
        template = "html-static"

    entry_file = _find_entry(entries, kind)
    text_files = _decode_text_files(entries, settings)
    if entry_file not in text_files and entry_file in entries:
        # 入口文件必须返回给前端，否则画布无法定位源码行号。
        try:
            text_files[entry_file] = entries[entry_file].decode("utf-8")
        except UnicodeDecodeError:
            warnings.append(f"入口文件不是 UTF-8 文本: {entry_file}")

    if not text_files:
        warnings.append("未找到可返回前端的文本源码；沙盒内文件仍可正常预览")

    return ImportedProject(
        kind=kind,
        files=entries,
        text_files=text_files,
        entry_file=entry_file,
        start_command=start_command,
        file_count=len(entries),
        warnings=warnings,
        template=template,
    )
