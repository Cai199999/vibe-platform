"""兼容层：编辑器注册表已迁移至 ``ai_core.adapters.editor``。"""

from ai_core.adapters.editor.registry import EditorRegistry, default_editor_registry

__all__ = ["EditorRegistry", "default_editor_registry"]
