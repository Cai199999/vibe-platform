"""全栈生成核心监控指标。

四个指标直接对应“严格责任隔离 + 缺陷归因”的运行效果：

  - ``Applier_Block_Rate``：AST 门禁拦截次数 / 门禁检查次数；
  - ``Contract_Misretry_Count``：``audit_contract`` 抓出的契约缺陷次数
    （这些失败本可避免，不会被计入代码自愈重试）；
  - ``MultiTable_AST_Pass_Rate``：多表契约 AST 检查通过次数 / 总次数；
  - ``Contract_Revision_Rate``：前端契约回改次数 / 契约确认次数。

实现约定：
  - 进程内单例，线程安全；每次生成完成后通过 SSE 快照输出；
  - 只保存聚合计数，不保存 run_id / 源码等明细，避免指标变成日志仓库；
  - 任何记录异常都不得影响主流程。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

METRIC_APPLIER_BLOCK_RATE = "Applier_Block_Rate"
METRIC_CONTRACT_MISRETRY_COUNT = "Contract_Misretry_Count"
METRIC_MULTI_TABLE_AST_PASS_RATE = "MultiTable_AST_Pass_Rate"
METRIC_CONTRACT_REVISION_RATE = "Contract_Revision_Rate"


@dataclass
class _Ratio:
    numerator: int = 0
    denominator: int = 0

    @property
    def rate(self) -> float:
        if self.denominator <= 0:
            return 0.0
        return round(self.numerator / self.denominator, 6)

    def to_dict(self) -> dict[str, float | int]:
        return {
            "rate": self.rate,
            "numerator": self.numerator,
            "denominator": self.denominator,
        }


@dataclass
class FullstackMetrics:
    """聚合指标；所有方法均为线程安全。"""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _applier: _Ratio = field(default_factory=_Ratio, repr=False)
    _multi_table_ast: _Ratio = field(default_factory=_Ratio, repr=False)
    _contract_revision: _Ratio = field(default_factory=_Ratio, repr=False)
    _contract_misretry: int = 0
    # 细粒度计数便于排查，但不改变四个核心指标的语义。
    _applier_blocked_by_join: int = 0
    _applier_blocked_other: int = 0

    # ---- Applier_Block_Rate -------------------------------------------
    def record_applier_attempt(self) -> None:
        with self._lock:
            self._applier.denominator += 1

    def record_applier_block(self, *, join_issue: bool = False) -> None:
        with self._lock:
            self._applier.numerator += 1
            if join_issue:
                self._applier_blocked_by_join += 1
            else:
                self._applier_blocked_other += 1

    # ---- Contract_Misretry_Count --------------------------------------
    def record_contract_misretry(self) -> None:
        with self._lock:
            self._contract_misretry += 1

    # ---- MultiTable_AST_Pass_Rate -------------------------------------
    def record_multi_table_ast(self, *, passed: bool) -> None:
        with self._lock:
            self._multi_table_ast.denominator += 1
            if passed:
                self._multi_table_ast.numerator += 1

    # ---- Contract_Revision_Rate ---------------------------------------
    def record_contract_confirmation(self) -> None:
        with self._lock:
            self._contract_revision.denominator += 1

    def record_contract_revision(self) -> None:
        with self._lock:
            self._contract_revision.numerator += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                METRIC_APPLIER_BLOCK_RATE: self._applier.to_dict(),
                METRIC_CONTRACT_MISRETRY_COUNT: self._contract_misretry,
                METRIC_MULTI_TABLE_AST_PASS_RATE: self._multi_table_ast.to_dict(),
                METRIC_CONTRACT_REVISION_RATE: self._contract_revision.to_dict(),
                "details": {
                    "applierBlockedByJoin": self._applier_blocked_by_join,
                    "applierBlockedOther": self._applier_blocked_other,
                },
            }

    def reset(self) -> None:
        """仅用于测试；生产进程不做运行期重置。"""
        with self._lock:
            self._applier = _Ratio()
            self._multi_table_ast = _Ratio()
            self._contract_revision = _Ratio()
            self._contract_misretry = 0
            self._applier_blocked_by_join = 0
            self._applier_blocked_other = 0


FULLSTACK_METRICS = FullstackMetrics()


def snapshot_fullstack_metrics() -> dict[str, object]:
    return FULLSTACK_METRICS.snapshot()


__all__ = [
    "FULLSTACK_METRICS",
    "FullstackMetrics",
    "METRIC_APPLIER_BLOCK_RATE",
    "METRIC_CONTRACT_MISRETRY_COUNT",
    "METRIC_CONTRACT_REVISION_RATE",
    "METRIC_MULTI_TABLE_AST_PASS_RATE",
    "snapshot_fullstack_metrics",
]
