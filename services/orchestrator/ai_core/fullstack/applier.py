"""Applier：全栈产物落盘前的最后一道绝对门禁。

规则：

  - 任何 Coder 的产物都必须经过这里，**没有绕过开关**；
  - 门禁无条件调用底层 Babel AST 诊断（``check_orm_syntax``）；
  - 只要 ORM / Express 会话层与迁移 DDL 不一致（字段漂移、JOIN 别名
    写错、关系外键不存在），直接抛 ``ApplierSafetyBlock``，沙盒文件
    保持原样，绝不出现“半写盘”的中间状态；
  - 是否把这次拦截归因为代码缺陷还是契约缺陷，由编排层结合
    ``audit_contract`` 决定，Applier 自己不做业务裁决。
"""

from __future__ import annotations

import logging
from typing import Mapping

from ai_core.domain.architect import ArchitectContract
from ai_core.fullstack.diagnostics import OrmSyntaxReport, check_orm_syntax
from ai_core.observability.fullstack_metrics import FULLSTACK_METRICS
from ai_core.ports.sandbox import SandboxProvider

logger = logging.getLogger(__name__)

# 门禁需要合并“沙盒里已有文件 + 本次待写文件”，才能验证跨文件一致性。
_ORACLE_PATHS = (
    "server/db.js",
    "server/index.js",
    "server/migrations/001_init.sql",
    "server/schema.core.json",
    "server/api-routes.json",
)


class ApplierSafetyBlock(RuntimeError):
    """AST 门禁拒绝落盘。``report`` 是只读诊断，不包含任何业务裁决。"""

    code = "APPLIER_AST_BLOCK"

    def __init__(self, report: OrmSyntaxReport) -> None:
        super().__init__(report.summary())
        self.report = report


class Applier:
    """把生成产物安全写入沙盒。"""

    def __init__(self, sandbox: SandboxProvider, contract: ArchitectContract) -> None:
        self._sandbox = sandbox
        self._contract = contract

    def _merged_files(
        self,
        sandbox_id: str,
        incoming: Mapping[str, str],
    ) -> dict[str, str]:
        merged: dict[str, str] = {}
        for path in _ORACLE_PATHS:
            try:
                content = self._sandbox.read_file(sandbox_id, path)
            except Exception:  # noqa: BLE001 - 文件不存在是正常状态
                continue
            if isinstance(content, str):
                merged[path] = content
        for path, content in incoming.items():
            if isinstance(content, str) and path.startswith("server/"):
                merged[path] = content
        return merged

    @staticmethod
    def _touches_server(files: Mapping[str, str]) -> bool:
        """只有服务端产物才属于 AST 门禁的判定域。

        前端 / 部署产物同样必须经过 Applier（统一入口），但它们的写入
        不会改变 ORM ↔ DDL 事实，因此不重复触发 AST 检查，避免把
        ``Applier_Block_Rate`` 的分母掺入无关写入。
        """
        return any(path.startswith("server/") for path in files)

    def apply(
        self,
        sandbox_id: str,
        files: Mapping[str, str],
        *,
        mode: str = "fullstack",
    ) -> list[str]:
        """门禁通过后才写入；失败时异常向上抛，不触碰沙盒。"""
        if not self._touches_server(files):
            return self._sandbox.apply_patch(sandbox_id, files=dict(files))
        merged = self._merged_files(sandbox_id, files)
        report = check_orm_syntax(merged, self._contract, mode=mode)
        if report.applicable:
            FULLSTACK_METRICS.record_applier_attempt()
            if not report.ok:
                FULLSTACK_METRICS.record_applier_block(join_issue=report.has_join_issue)
                logger.warning(
                    "Applier AST 门禁拦截 mode=%s issues=%s",
                    mode,
                    [issue.message for issue in report.issues],
                )
                raise ApplierSafetyBlock(report)
        return self._sandbox.apply_patch(sandbox_id, files=dict(files))


__all__ = ["Applier", "ApplierSafetyBlock"]
