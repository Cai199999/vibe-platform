"""全量文件生成器（全栈阶段的 ``FileCreator``）。

与锚点编辑不同，新建 Java / Python / SQL / 配置文件时不存在可依赖的锚点，
模型必须一次输出完整文件。本实现只做三件事：

1. **路径白名单校验**：拒绝绝对路径、``..``、反斜杠与 Windows 盘符，保证
   模型输出不能逃逸工作区；
2. **存在性校验**：``CREATE_FILE`` 已存在即报错（防止覆盖用户代码），
   ``REWRITE_FILE`` 不存在即报错（防止拼错路径悄悄生成孤儿文件）；
3. **原样写入**：不做格式化、不补换行，内容由上层 Prompt / 格式化步骤负责，
   避免隐式修改导致 diff 不可预测。

未来全栈模板（Spring Boot 骨架、Maven 依赖、Flyway 迁移）可以在此扩展
``metadata["template"]``，但语义仍是“写入完整文件”。
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Mapping

from ai_core.ports.editor import (
    CodeEdit,
    EditKind,
    EditResult,
    EditorError,
)


class FileCreator:
    """新建 / 整文件重写。"""

    name = "file_creator"

    _SUPPORTED = frozenset({EditKind.CREATE_FILE, EditKind.REWRITE_FILE})

    def supports(self, kind: EditKind) -> bool:
        return kind in self._SUPPORTED

    def apply(self, edit: CodeEdit, files: Mapping[str, str]) -> EditResult:
        if edit.kind not in self._SUPPORTED:
            raise EditorError(f"FileCreator 不支持 {edit.kind.value}", path=edit.path)
        path = normalize_workspace_path(edit.path)
        exists = path in files

        if edit.kind is EditKind.CREATE_FILE and exists:
            raise EditorError(
                f"文件已存在，拒绝覆盖: {path}；如需整体替换请使用 REWRITE_FILE",
                path=path,
            )
        if edit.kind is EditKind.REWRITE_FILE and not exists:
            raise EditorError(
                f"文件不存在，无法重写: {path}；新建文件请使用 CREATE_FILE",
                path=path,
            )

        changed = files.get(path) != edit.content
        summary = edit.label or (
            f"{'新建' if edit.kind is EditKind.CREATE_FILE else '重写'} {path}"
        )
        return EditResult(
            editor=self.name,
            files={path: edit.content},
            changed=changed,
            summary=summary,
        )


def normalize_workspace_path(path: str) -> str:
    """把模型给出的路径规范化为工作区相对 POSIX 路径。

    只接受形如 ``src/App.jsx``、``services/api/pom.xml`` 的路径；
    以下输入一律拒绝：

    - 空路径；
    - 绝对路径 ``/etc/passwd``；
    - 目录穿越 ``../../secret``；
    - 反斜杠或 Windows 盘符 ``C:\\...``；
    - 包含 ``.`` / ``..`` 段。
    """

    raw = (path or "").strip()
    if not raw:
        raise EditorError("文件路径不能为空")
    if raw.startswith("/") or "\\" in raw:
        raise EditorError(f"非法文件路径: {path!r}，必须是工作区相对路径")
    if ":" in raw.split("/", 1)[0]:
        raise EditorError(f"非法文件路径: {path!r}，不允许 Windows 盘符")
    if any(part in {"", ".", ".."} for part in raw.split("/")):
        raise EditorError(f"非法文件路径: {path!r}，不允许空段 / . / ..")

    return str(PurePosixPath(raw))


__all__ = ["FileCreator", "normalize_workspace_path"]
