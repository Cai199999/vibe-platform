"""编辑器策略注册表。

调用方只提交 ``CodeEdit``，由 ``EditorRegistry`` 按 ``EditKind`` 选择实现：

| EditKind                         | 编辑器             | 阶段 |
|----------------------------------|--------------------|------|
| REPLACE_ANCHOR / REPLACE_ALL     | AnchorCodeEditor   | M2   |
| INSERT_BEFORE / INSERT_AFTER     | AnchorCodeEditor   | M2   |
| CREATE_FILE / REWRITE_FILE       | FileCreator        | M2   |
| STRUCTURED                       | TreeSitterEditor   | 全栈 |

后续新增编辑器只需 ``register()``，Graph / Tool 不感知具体类。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable, Mapping

from ai_core.editor.anchor import AnchorCodeEditor
from ai_core.editor.base import (
    CodeEdit,
    CodeEditor,
    EditKind,
    EditResult,
    EditorError,
)
from ai_core.editor.file_creator import FileCreator
from ai_core.editor.tree_sitter import TreeSitterEditor


class EditorRegistry:
    """按 ``EditKind`` 路由的编辑器集合。"""

    def __init__(self, editors: Iterable[CodeEditor] | None = None) -> None:
        if editors is None:
            editors = (AnchorCodeEditor(), FileCreator(), TreeSitterEditor())
        self._editors: list[CodeEditor] = list(editors)

    @property
    def editors(self) -> tuple[CodeEditor, ...]:
        return tuple(self._editors)

    def register(self, editor: CodeEditor, *, prepend: bool = False) -> None:
        """注册编辑器；后注册者优先匹配，便于覆盖默认实现。"""
        if prepend:
            self._editors.insert(0, editor)
        else:
            self._editors.append(editor)

    def resolve(self, kind: EditKind) -> CodeEditor:
        for editor in reversed(self._editors):
            if editor.supports(kind):
                return editor
        raise EditorError(f"没有编辑器支持 {kind.value}")

    def apply(self, edit: CodeEdit, files: Mapping[str, str]) -> EditResult:
        return self.resolve(edit.kind).apply(edit, files)

    def apply_all(
        self,
        edits: Iterable[CodeEdit],
        files: Mapping[str, str],
    ) -> EditResult:
        """顺序应用一组编辑，返回相对初始工作区的变化集合。"""
        current = dict(files)
        summaries: list[str] = []
        for edit in edits:
            result = self.apply(edit, current)
            current.update(result.files)
            if result.summary:
                summaries.append(result.summary)

        changed = {
            path: content
            for path, content in current.items()
            if files.get(path) != content
        }
        return EditResult(
            editor="registry",
            files=changed,
            changed=bool(changed),
            summary="；".join(summaries),
        )


@lru_cache
def default_editor_registry() -> EditorRegistry:
    """进程级默认注册表；无状态，可安全共享。"""
    return EditorRegistry()


__all__ = ["EditorRegistry", "default_editor_registry"]
