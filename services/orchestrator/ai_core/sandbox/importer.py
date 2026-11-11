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
import re
import stat
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from ai_core.config import Settings
from ai_core.ports.sandbox import SandboxValidationError

logger = logging.getLogger(__name__)

ProjectKind = Literal["react", "html", "fullstack"]
FULLSTACK_API_PORT = 3000

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
# 模板文件不含真实密钥，导入自己的导出包时应当保留。
_SAFE_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}
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
    # 全栈导入时实际拉起的服务；前端/后端分别对应 vite / express。
    services: list[str] = field(default_factory=list)
    # 注入到源码中的 data-source 标记数量，便于前端提示画布可圈选。
    data_source_count: int = 0


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
    if name in _SECRET_FILENAMES:
        return True
    if name.startswith(".env.") and name not in _SAFE_ENV_TEMPLATES:
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


def _is_fullstack(entries: dict[str, bytes]) -> bool:
    """平台导出包识别：根 package + server/ + 前端入口。"""
    if "package.json" not in entries:
        return False
    has_server = (
        "server/package.json" in entries
        or "server/index.js" in entries
        or "server/index.mjs" in entries
    )
    has_frontend = any(
        path in entries
        for path in ("src/main.jsx", "src/main.tsx", "src/App.jsx", "index.html")
    )
    return has_server and has_frontend


def _server_entry(entries: dict[str, bytes]) -> str:
    for path in ("server/index.js", "server/index.mjs", "server/index.cjs"):
        if path in entries:
            return path
    return ""


def _fullstack_start_command(entries: dict[str, bytes], port: int) -> str:
    """同时拉起 Express 与 Vite；Vite 独占对外 5173 端口。

    沙盒容器环境里 ``PORT`` 默认是 5173（供旧 CRA 使用），因此启动
    Express 时必须显式覆盖为内部端口，避免和 Vite 抢端口。
    """
    install = "npm install --no-audit --no-fund"
    commands = [install]
    if "server/package.json" in entries:
        commands.append(f"(cd server && {install})")
    entry = _server_entry(entries)
    if entry:
        server_cmd = (
            f"PORT={FULLSTACK_API_PORT} FULLSTACK_API_PORT={FULLSTACK_API_PORT} "
            f"node {entry}"
        )
    else:
        server_cmd = (
            f"PORT={FULLSTACK_API_PORT} FULLSTACK_API_PORT={FULLSTACK_API_PORT} "
            "npm --prefix server start"
        )
    commands.append(f"({server_cmd} > /tmp/vibe-fullstack-api.log 2>&1 &)")
    commands.append(f"./node_modules/.bin/vite --host 0.0.0.0 --port {port}")
    return " && ".join(commands)


def _ensure_vite_proxy(
    entries: dict[str, bytes],
    warnings: list[str],
) -> None:
    """全栈包若缺少 /api 代理，自动注入到 Vite 配置。"""
    config_path = next(
        (
            path
            for path in (
                "vite.config.js",
                "vite.config.ts",
                "vite.config.mjs",
                "vite.config.cjs",
            )
            if path in entries
        ),
        "",
    )
    if not config_path:
        warnings.append("未找到 vite.config.*，跳过 /api 代理注入")
        return
    try:
        content = entries[config_path].decode("utf-8")
    except UnicodeDecodeError:
        warnings.append(f"{config_path} 不是 UTF-8 文本，跳过代理注入")
        return
    if "proxy" in content and "/api" in content:
        return

    proxy = (
        "\n    // 全栈导入：把同源 /api 请求转发到沙盒内 Express。\n"
        "    '/api': {\n"
        f"      target: process.env.FULLSTACK_API_URL || 'http://127.0.0.1:{FULLSTACK_API_PORT}',\n"
        "      changeOrigin: true,\n"
        "    },"
    )
    target = ""
    if "proxy" in content:
        index = content.find("proxy")
        brace = content.find("{", index)
        if brace >= 0:
            content = content[: brace + 1] + proxy + content[brace + 1 :]
            target = "proxy"
    elif "server:" in content:
        index = content.find("server:")
        brace = content.find("{", index)
        if brace >= 0:
            content = (
                content[: brace + 1]
                + "\n    proxy: {"
                + proxy
                + "\n    },"
                + content[brace + 1 :]
            )
            target = "server"
    else:
        for marker in ("defineConfig({", "export default {"):
            index = content.find(marker)
            if index >= 0:
                insert_at = index + len(marker)
                content = (
                    content[:insert_at]
                    + "\n  server: {\n    proxy: {"
                    + proxy
                    + "\n    },\n  },"
                    + content[insert_at:]
                )
                target = marker
                break
    if target:
        entries[config_path] = content.encode("utf-8")
    else:
        warnings.append(f"{config_path} 结构无法识别，未能注入 /api 代理")


_JSX_OPEN = re.compile(r"<([a-z][a-zA-Z0-9-]*)(?=[\s/>])")
_MARKER_SKIP_TAGS = {
    "html",
    "head",
    "body",
    "script",
    "style",
    "meta",
    "link",
    "title",
    "path",
    "defs",
    "g",
    "circle",
    "rect",
    "line",
    "polyline",
    "polygon",
    "use",
    "symbol",
}
_RUNTIME_MARKER_FILE = "src/vibe-import-marker.js"

_RUNTIME_MARKER_SOURCE = r"""// 导入后运行时兜底：为动态渲染、源码扫描未命中的元素补 data-source。
// 仅用于画布定位，不改变业务逻辑；已有标记的节点绝不覆盖。
const SKIP_TAGS = new Set([
  'HTML', 'HEAD', 'BODY', 'SCRIPT', 'STYLE', 'META', 'LINK', 'TITLE',
])

const markRuntimeNodes = () => {
  if (!document.body) return
  let index = document.querySelectorAll('[data-source]').length
  const nodes = document.body.querySelectorAll('*:not([data-source])')
  nodes.forEach((node) => {
    if (SKIP_TAGS.has(node.tagName)) return
    node.setAttribute('data-source', `imp-runtime-${index++}`)
  })
}

const startMarker = () => {
  markRuntimeNodes()
  const observer = new MutationObserver(() => markRuntimeNodes())
  observer.observe(document.body, { childList: true, subtree: true })
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', startMarker, { once: true })
} else {
  startMarker()
}
"""


def _protected_line_numbers(source: str) -> set[int]:
    """找出“行首处于多行模板字符串 / 块注释内”的行号。

    单行字符串由 ``_inside_js_string_or_comment`` 按行判断；模板字符串与
    ``/* ... */`` 可以跨行，必须在扫描前整体标记，否则模板里的
    ``<div>`` 会被误注入。
    """
    protected: set[int] = set()
    in_template = False
    in_block_comment = False
    previous = ""
    for number, line in enumerate(source.splitlines(), start=1):
        if in_template or in_block_comment:
            protected.add(number)
        index = 0
        while index < len(line):
            char = line[index]
            if in_block_comment:
                end = line.find("*/", index)
                if end < 0:
                    break
                in_block_comment = False
                index = end + 2
                previous = "/"
                continue
            if in_template:
                if char == "\\":
                    index += 2
                    continue
                if char == "`":
                    in_template = False
                    previous = "`"
                index += 1
                continue
            if char == "/" and line[index : index + 2] == "/*":
                in_block_comment = True
                index += 2
                continue
            if char == "`" and (
                not previous
                or previous in "=([{,:?!>+-*/&|"
                or line[:index].rstrip().endswith("return")
            ):
                in_template = True
                index += 1
                previous = "`"
                continue
            if not char.isspace():
                previous = char
            index += 1
    return protected


def _inside_js_string_or_comment(
    source: str,
    position: int,
    protected_lines: set[int] | None = None,
) -> bool:
    """判断候选 ``<tag`` 是否位于 JS 字符串 / 注释中。

    生成代码里常见 ``const html = "<div>..."``、``title="a <b"`` 之类
    片段；只扫描 JSX 时若不加判断，会把字符串里的标签也改写。这里按
    行检查未闭合的引号（仅当引号出现在 ``=`` / ``(`` / ``[`` 等语法
    位置）以及 //、/* 注释，避免误伤 JSX 文案中的撇号。
    """
    line_start = source.rfind("\n", 0, position) + 1
    line_number = source.count("\n", 0, line_start) + 1
    if protected_lines and line_number in protected_lines:
        return True
    prefix = source[line_start:position]
    quote = ""
    escaped = False
    previous = ""
    for char in prefix:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'", "`"}:
            # 只有语法位置上的引号才当作字符串起点；JSX 文案里的 Don't
            # 前一个字符是字母，不会被误判。
            if not previous or previous in "=([{,:?!>+-*/&|":
                quote = char
            continue
        if not char.isspace():
            previous = char

    stripped = prefix.lstrip()
    if stripped.startswith("//"):
        return True
    if "//" in prefix and prefix.rfind("//") > prefix.rfind('"'):
        return True
    if prefix.rfind("/*") > prefix.rfind("*/"):
        return True
    return bool(quote)


def _inject_jsx_markers(source: str, slug: str) -> tuple[str, int]:
    """向 JSX / HTML 宿主元素注入 data-source。

    使用轻量扫描器而非正则替换，正确跳过属性字符串与 ``{...}`` 表达式里的
    ``>``（例如 onClick 箭头函数），避免生成非法 JSX。
    """
    result: list[str] = []
    index = 0
    count = 0
    length = len(source)
    protected_lines = _protected_line_numbers(source)
    while index < length:
        match = _JSX_OPEN.match(source, index)
        if not match:
            result.append(source[index])
            index += 1
            continue
        if _inside_js_string_or_comment(source, index, protected_lines):
            result.append(source[index])
            index += 1
            continue
        tag = match.group(1)
        if tag in {"script", "style"}:
            # 脚本 / 样式内部不是 JSX，整段跳过，避免把字符串里的 "<div"
            # 误当成宿主元素而注入标记、破坏源码。
            close_at = source.lower().find(f"</{tag}", match.end())
            if close_at >= 0:
                result.append(source[index:close_at])
                index = close_at
            else:
                result.append(source[index])
                index += 1
            continue
        if tag in _MARKER_SKIP_TAGS:
            result.append(source[index])
            index += 1
            continue
        cursor = match.end()
        quote = ""
        brace_depth = 0
        while cursor < length:
            char = source[cursor]
            if quote:
                if char == quote and source[cursor - 1] != "\\":
                    quote = ""
            elif char in {'"', "'"}:
                quote = char
            elif char == "{":
                brace_depth += 1
            elif char == "}":
                brace_depth = max(0, brace_depth - 1)
            elif char == ">" and brace_depth == 0:
                break
            cursor += 1
        if cursor >= length:
            result.append(source[index])
            index += 1
            continue
        tag_text = source[index : cursor + 1]
        if "data-source" in tag_text:
            result.append(tag_text)
            index = cursor + 1
            continue
        insert_at = cursor - 1 if tag_text.rstrip().endswith("/>") else cursor
        line = source.count("\n", 0, insert_at) + 1
        count += 1
        marker = f"imp-{slug}-{line}-{count}"
        result.append(source[index:insert_at])
        result.append(f' data-source="{marker}"')
        result.append(source[insert_at : cursor + 1])
        index = cursor + 1
        if count >= 200:
            result.append(source[index:])
            break
    return "".join(result), count


def _inject_data_sources(
    entries: dict[str, bytes],
    warnings: list[str],
) -> int:
    """扫描 JSX / TSX / HTML，注入画布定位标记并挂载运行时兜底脚本。"""
    total = 0
    for path in sorted(entries):
        suffix = PurePosixPath(path).suffix.lower()
        if suffix not in {".jsx", ".tsx", ".html", ".htm"}:
            continue
        if path.startswith("server/"):
            continue
        try:
            source = entries[path].decode("utf-8")
        except UnicodeDecodeError:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")[:48]
        patched, count = _inject_jsx_markers(source, slug or "page")
        if count:
            entries[path] = patched.encode("utf-8")
            total += count

    # 运行时兜底：动态列表、条件渲染产生的节点也能被画布选中。
    entries[_RUNTIME_MARKER_FILE] = _RUNTIME_MARKER_SOURCE.encode("utf-8")
    main_candidates = ("src/main.jsx", "src/main.tsx")
    main_path = next((path for path in main_candidates if path in entries), "")
    if main_path:
        try:
            main_source = entries[main_path].decode("utf-8")
        except UnicodeDecodeError:
            main_source = ""
        if main_source and "vibe-import-marker" not in main_source:
            entries[main_path] = (
                "import './vibe-import-marker.js'\n" + main_source
            ).encode("utf-8")
    else:
        html_path = next(
            (path for path in ("index.html",) if path in entries),
            "",
        )
        if html_path:
            try:
                html = entries[html_path].decode("utf-8")
            except UnicodeDecodeError:
                html = ""
            if html and "vibe-import-marker" not in html:
                script = f"<script>{_RUNTIME_MARKER_SOURCE}</script>"
                if "</body>" in html:
                    html = html.replace("</body>", script + "\n</body>", 1)
                else:
                    html += script
                entries[html_path] = html.encode("utf-8")
    if total == 0:
        warnings.append("源码中未找到可注入的 JSX/HTML 元素，已启用运行时标记兜底")
    return total


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
def _read_zip_entries(
    data: bytes,
    settings: Settings,
) -> dict[str, bytes]:
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
    return entries


def _read_tar_entries(
    data: bytes,
    settings: Settings,
) -> dict[str, bytes]:
    """安全读取平台导出的 tar.gz / tgz 全栈包。"""
    entries: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > settings.import_max_entries:
                raise SandboxValidationError(
                    f"压缩包条目过多（{len(members)} > {settings.import_max_entries}）"
                )
            total_uncompressed = 0
            for member in members:
                if member.isdir():
                    continue
                if not member.isfile():
                    # 符号链接 / 设备文件 / FIFO 一律拒绝，防止越界读取。
                    raise SandboxValidationError(
                        f"压缩包包含不支持的文件类型: {member.name}"
                    )
                rel_path = _normalize_zip_name(member.name)
                if rel_path is None or _should_skip(rel_path):
                    continue
                total_uncompressed += int(member.size)
                if total_uncompressed > settings.import_max_uncompressed_bytes:
                    raise SandboxValidationError("压缩包解压后体积超过限制")
                if member.size > settings.import_max_uncompressed_bytes:
                    raise SandboxValidationError(f"单个文件体积超过限制: {rel_path}")
                source = archive.extractfile(member)
                if source is None:
                    continue
                content = source.read()
                if len(content) > settings.import_max_uncompressed_bytes:
                    raise SandboxValidationError(f"单个文件体积超过限制: {rel_path}")
                entries[rel_path] = content
    except tarfile.TarError as exc:
        raise SandboxValidationError("不是有效的 tar.gz 压缩包") from exc
    return entries


def import_archive(
    data: bytes,
    filename: str,
    settings: Settings,
) -> ImportedProject:
    """统一解析 zip / tar.gz 并生成可直接写入沙盒的项目。

    平台导出的全栈包（根 package.json + server/ + src/）会自动识别为
    ``fullstack``：同时启动 Express 与 Vite、补齐 /api 代理，并向源码
    重新注入 data-source，导入后即可继续画布圈选。
    """
    if not data:
        raise SandboxValidationError("上传的压缩包为空")
    if len(data) > settings.import_max_archive_bytes:
        raise SandboxValidationError(
            f"压缩包超过大小限制（{len(data)} > {settings.import_max_archive_bytes} 字节）"
        )

    lowered = filename.lower()
    if lowered.endswith((".tar.gz", ".tgz")) or data[:2] == b"\x1f\x8b":
        entries = _read_tar_entries(data, settings)
    elif lowered.endswith(".zip") or data[:4] == b"PK\x03\x04":
        entries = _read_zip_entries(data, settings)
    else:
        raise SandboxValidationError("只支持 .zip / .tar.gz / .tgz 压缩包")

    warnings: list[str] = []
    entries = _strip_common_root(entries)
    if not entries:
        raise SandboxValidationError("压缩包中没有可导入的源码文件")

    services: list[str] = []
    data_source_count = 0
    if _is_fullstack(entries):
        kind: ProjectKind = "fullstack"
        _ensure_vite_proxy(entries, warnings)
        data_source_count = _inject_data_sources(entries, warnings)
        start_command = _fullstack_start_command(
            entries,
            settings.sandbox_dev_port,
        )
        template = "fullstack-export"
        services = ["vite", "express"]
    elif "package.json" in entries:
        kind = "react"
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
        services=services,
        data_source_count=data_source_count,
    )


def import_zip(
    data: bytes,
    settings: Settings,
) -> ImportedProject:
    """兼容旧调用：按 zip 解析。"""
    return import_archive(data, "upload.zip", settings)


def import_project_file(
    data: bytes,
    filename: str,
    settings: Settings,
) -> ImportedProject:
    """API 层入口别名，语义上同时支持 zip 与 tar.gz。"""
    return import_archive(data, filename, settings)
