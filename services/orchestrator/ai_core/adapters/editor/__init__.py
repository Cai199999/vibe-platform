"""统一代码编辑抽象（CodeEditor / FileCreator / TreeSitterEditor）。"""

from ai_core.adapters.editor.anchor import AnchorCodeEditor
from ai_core.ports.editor import (
    CodeEdit,
    CodeEditor,
    EditKind,
    EditLayout,
    EditResult,
    EditorError,
    EditorNotAvailableError,
)
from ai_core.adapters.editor.file_creator import FileCreator, normalize_workspace_path
from ai_core.adapters.editor.registry import EditorRegistry, default_editor_registry
from ai_core.adapters.editor.tree_sitter import TreeSitterEditor

__all__ = [
    "AnchorCodeEditor",
    "CodeEdit",
    "CodeEditor",
    "EditKind",
    "EditLayout",
    "EditResult",
    "EditorError",
    "EditorNotAvailableError",
    "EditorRegistry",
    "FileCreator",
    "TreeSitterEditor",
    "default_editor_registry",
    "normalize_workspace_path",
]
