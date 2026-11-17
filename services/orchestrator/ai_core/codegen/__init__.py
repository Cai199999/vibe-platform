"""离线 VisualDocument 的确定性 CodeGen 与编译门禁。"""

from ai_core.codegen.visual import (
    VisualCodegenError,
    VisualCompileResult,
    compile_visual_document,
    generate_visual_fallback,
    visual_document_hash,
)
from ai_core.codegen.visual_gate import (
    VisualCodegenGateError,
    VisualCodegenGateReport,
    VisualCodegenGateUnavailable,
    verify_visual_source,
)

__all__ = [
    "VisualCodegenError",
    "VisualCodegenGateError",
    "VisualCodegenGateReport",
    "VisualCodegenGateUnavailable",
    "VisualCompileResult",
    "compile_visual_document",
    "generate_visual_fallback",
    "verify_visual_source",
    "visual_document_hash",
]
