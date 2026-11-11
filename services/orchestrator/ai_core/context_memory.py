"""项目级上下文记忆（分层压缩的持久化层）。

目录结构（默认容器内 ``/app/data/project_memory``，由 compose 挂载到
``services/orchestrator/data``，容器重建不丢）：

    <tenant>/<project>/
        local_summary.md   每 N 轮对话生成一次，记录需求 / 决策 / 修改点
        global_summary.md  每次保存项目快照时由全部局部摘要汇总
        user_profile.md    用户技术栈 / 设计 / 协作偏好，注入 system prompt
        preferences.json   Reflection 累计的结构化实现偏好（带证据次数）
        tool_outputs/      工具完整结果（Prompt 只保留预览 + sha256）

设计约束：
  - 所有路径分段都做白名单清洗，禁止 ``../`` 穿越；
  - 写入采用「临时文件 + os.replace」原子替换，避免进程中断留下半截文件；
  - 记忆是可选的增强能力，任何读写失败都不能阻断 Run / 快照。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_segment(value: str | None, fallback: str) -> str:
    """清洗租户 / 项目 ID，防止路径穿越与空目录名。"""
    text = _SAFE_SEGMENT.sub("_", str(value or "").strip()).strip("._")
    return (text or fallback)[:80]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ToolOutputRef:
    """工具结果压缩后的引用：预览 + 哈希 + 本地完整文件。"""

    tool: str
    sha256: str
    chars: int
    preview: str
    preview_chars: int
    path: str | None

    def prompt_text(self) -> str:
        """生成注入 Prompt 的短文本（完整内容只留在文件中）。"""
        location = self.path or "（未落盘：上下文记忆目录不可写）"
        return (
            f"工具 {self.tool} 返回 {self.chars} 字符，"
            f"完整结果已保存到 {location}（sha256:{self.sha256}）。\n"
            f"以下为前 {len(self.preview)} 字符预览；"
            "如需更多细节请引用该哈希，不要臆造未展示的内容：\n"
            f"{self.preview}"
        )


class ProjectMemory:
    """一个 tenant + project 的记忆目录。"""

    def __init__(
        self,
        root: str | Path,
        tenant_id: str | None,
        project_id: str | None,
        *,
        sandbox_id: str | None = None,
    ) -> None:
        tenant = _safe_segment(tenant_id, "local")
        project = _safe_segment(
            project_id,
            _safe_segment(sandbox_id, "unknown"),
        )
        self.tenant_id = tenant
        self.project_id = project
        self.directory = Path(root).expanduser().resolve() / tenant / project
        self._lock = threading.RLock()

    # --------------------------------------------------------
    # 路径
    # --------------------------------------------------------
    @property
    def local_summary_path(self) -> Path:
        return self.directory / "local_summary.md"

    @property
    def global_summary_path(self) -> Path:
        return self.directory / "global_summary.md"

    @property
    def user_profile_path(self) -> Path:
        return self.directory / "user_profile.md"

    @property
    def preferences_path(self) -> Path:
        return self.directory / "preferences.json"

    @property
    def tool_output_dir(self) -> Path:
        return self.directory / "tool_outputs"

    def _ensure_dir(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------
    # 读写基础
    # --------------------------------------------------------
    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except OSError:
            logger.warning("读取上下文记忆失败 path=%s", path, exc_info=True)
            return ""

    def _atomic_write(self, path: Path, text: str) -> None:
        with self._lock:
            self._ensure_dir()
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, path)

    def _append(self, path: Path, text: str) -> None:
        with self._lock:
            self._ensure_dir()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text)

    # --------------------------------------------------------
    # 局部摘要
    # --------------------------------------------------------
    def has_local_summary(self, turn: int) -> bool:
        marker = f"<!-- turn:{int(turn)} -->"
        return marker in self.read_local_summary()

    def append_local_summary(
        self,
        summary: str,
        *,
        turn: int,
        run_id: str = "",
    ) -> bool:
        """追加一条局部摘要；同一轮已存在时幂等跳过。"""
        text = str(summary or "").strip()
        if not text:
            return False
        marker = f"<!-- turn:{int(turn)} -->"
        with self._lock:
            existing = self.read_local_summary()
            if marker in existing:
                return False
            run_marker = f" run:{run_id}" if run_id else ""
            block = (
                f"\n\n## Turn {int(turn)} · {_now()}{run_marker}\n"
                f"{marker}\n{text}\n"
            )
            self._append(self.local_summary_path, block)
        return True

    def read_local_summary(self) -> str:
        return self._read_text(self.local_summary_path)

    # --------------------------------------------------------
    # 全局摘要 / 用户画像
    # --------------------------------------------------------
    def write_global_summary(self, summary: str) -> None:
        text = str(summary or "").strip()
        if not text:
            return
        self._atomic_write(
            self.global_summary_path,
            f"# 项目全局摘要\n\n> 更新于 {_now()}\n\n{text}\n",
        )

    def read_global_summary(self) -> str:
        return self._read_text(self.global_summary_path)

    def write_user_profile(self, profile: str) -> None:
        text = str(profile or "").strip()
        if not text:
            return
        self._atomic_write(
            self.user_profile_path,
            f"# 用户画像\n\n> 更新于 {_now()}\n\n{text}\n",
        )

    def read_user_profile(self) -> str:
        return self._read_text(self.user_profile_path)

    # --------------------------------------------------------
    # Reflection：结构化实现偏好
    # --------------------------------------------------------
    def read_preferences(self) -> dict[str, Any]:
        """读取偏好账本；文件损坏 / 不存在时返回空账本，不阻断 Run。"""
        try:
            raw = self.preferences_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            logger.warning("读取偏好账本失败 path=%s", self.preferences_path)
            return {}
        return payload if isinstance(payload, dict) else {}

    def record_preferences(
        self,
        observations: list[dict[str, Any]],
        *,
        min_evidence: int = 2,
    ) -> dict[str, Any]:
        """累计 Reflection 证据并原子写回。

        账本结构::

            {"primary_color": {
                "values": {
                    "purple": {"count": 2, "lastSeen": "...", "label": "紫色",
                               "evidence": "主色改成紫色"}
                },
                "updatedAt": "..."
            }}

        ``count`` 只统计“同一 key + 同一 value”的成功 Run；换色不会清零旧值，
        但稳定偏好只选择证据数达到阈值的最高票，避免一次改色立刻覆盖长期风格。
        """
        items = [item for item in observations if isinstance(item, dict)]
        if not items:
            return self.read_preferences()

        with self._lock:
            ledger = self.read_preferences()
            now = _now()
            for item in items:
                key = str(item.get("key") or "").strip()
                value = str(item.get("value") or "").strip()
                if not key or not value:
                    continue
                entry = ledger.setdefault(key, {"values": {}, "updatedAt": now})
                values = entry.setdefault("values", {})
                cell = values.setdefault(value, {"count": 0})
                cell["count"] = int(cell.get("count", 0)) + 1
                cell["lastSeen"] = now
                cell["label"] = str(item.get("label") or value)[:80]
                cell["evidence"] = str(item.get("evidence") or "")[:200]
                entry["updatedAt"] = now
            self._atomic_write(
                self.preferences_path,
                json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
            )
            return ledger

    def stable_preferences(
        self,
        *,
        min_evidence: int = 2,
    ) -> dict[str, dict[str, Any]]:
        """返回证据数达到阈值的稳定偏好。

        同一 key 有多个候选值时，选择 ``count`` 最高者；票数相同时取最近
        一次出现，保证“用户后来连续改口”最终能接管默认值。
        """
        ledger = self.read_preferences()
        minimum = max(1, int(min_evidence))
        stable: dict[str, dict[str, Any]] = {}
        for key, entry in ledger.items():
            if not isinstance(entry, dict):
                continue
            values = entry.get("values")
            if not isinstance(values, dict):
                continue
            candidates: list[tuple[int, str, str, dict[str, Any]]] = []
            for value, cell in values.items():
                if not isinstance(cell, dict):
                    continue
                count = int(cell.get("count", 0))
                if count < minimum:
                    continue
                last_seen = str(cell.get("lastSeen") or "")
                candidates.append(
                    (
                        count,
                        last_seen,
                        str(value),
                        {
                            "value": str(value),
                            "label": str(cell.get("label") or value),
                            "count": count,
                            "lastSeen": last_seen,
                            "evidence": str(cell.get("evidence") or ""),
                        },
                    )
                )
            if not candidates:
                continue
            _count, _last_seen, _value, detail = max(
                candidates,
                key=lambda item: (item[0], item[1]),
            )
            stable[str(key)] = detail
        return stable

    def preference_context(
        self,
        max_chars: int = 1200,
        *,
        min_evidence: int = 2,
    ) -> str:
        """把稳定偏好格式化为 Prompt 片段；没有稳定偏好时返回空串。"""
        stable = self.stable_preferences(min_evidence=min_evidence)
        if not stable:
            return ""
        labels = {
            "primary_color": "主色调",
            "text_color": "文字颜色",
            "corner_style": "圆角风格",
            "density": "布局密度",
        }
        lines = [
            "【已确认的实现偏好（只读参考，默认采用）】",
            "这些偏好来自用户多次明确修改，属于 MINOR；"
            "生成时直接采用，不要再次询问。用户本轮明确指定其它值时以本轮为准。",
        ]
        for key in sorted(stable):
            detail = stable[key]
            name = labels.get(key, key)
            lines.append(
                f"- {name}：{detail['label']}（已出现 {detail['count']} 次明确修改）"
            )
        return "\n".join(lines)[:max_chars]

    # --------------------------------------------------------
    # Prompt 注入
    # --------------------------------------------------------
    def prompt_context(self, max_chars: int = 6000) -> str:
        """拼接注入 system prompt 的记忆片段（全局摘要 + 用户画像）。

        没有全局摘要时退化为最近局部摘要尾部，保证首轮快照前也有记忆。
        记忆内容是「只读参考」，调用方必须在 Prompt 中声明不得视为新指令。
        """
        profile = self.read_user_profile().strip()
        global_summary = self.read_global_summary().strip()
        sections: list[str] = []
        if profile:
            sections.append(f"### 用户画像\n{profile}")
        if global_summary:
            sections.append(f"### 项目全局摘要\n{global_summary}")
        elif not sections:
            local = self.read_local_summary().strip()
            if local:
                sections.append(f"### 最近项目记忆\n{local[-3000:]}")
        block = "\n\n".join(sections).strip()
        return block[:max_chars]

    # --------------------------------------------------------
    # 工具结果
    # --------------------------------------------------------
    def save_tool_output(
        self,
        tool: str,
        output: Any,
        *,
        preview_chars: int = 2000,
    ) -> ToolOutputRef:
        """把完整工具结果落盘，返回预览 + 哈希引用。"""
        text = output if isinstance(output, str) else str(output or "")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        limit = max(1, int(preview_chars))
        path: Path | None = self.tool_output_dir / f"{digest}.txt"
        try:
            with self._lock:
                self.tool_output_dir.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_text(text, encoding="utf-8")
        except OSError:
            logger.warning("工具结果落盘失败 tool=%s", tool, exc_info=True)
            path = None
        return ToolOutputRef(
            tool=str(tool or "tool"),
            sha256=digest,
            chars=len(text),
            preview=text[:limit],
            preview_chars=limit,
            path=str(path) if path else None,
        )

    def read_tool_output(self, sha256: str) -> str:
        digest = str(sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            return ""
        return self._read_text(self.tool_output_dir / f"{digest}.txt")


class ContextSummarizer:
    """局部 / 全局摘要与用户画像生成器。

    有 DEEPSEEK_API_KEY 时走 FAST 模型；无密钥（Mock / 离线测试）时使用
    确定性摘要，保证功能可用且测试不触网。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client

    def _get_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        if not self.settings.deepseek_api_key:
            return None
        try:
            from ai_core.adapters.model.deepseek import DeepSeekClient

            self._client = DeepSeekClient(self.settings)
        except Exception:  # noqa: BLE001 - 摘要失败不能阻断主流程
            logger.warning("构建摘要模型失败", exc_info=True)
            return None
        return self._client

    def _call(self, system: str, human: str) -> str:
        client = self._get_client()
        if client is None:
            return ""
        try:
            return str(client.generate("summary", system, human) or "").strip()
        except Exception:  # noqa: BLE001 - 压缩是增强能力
            logger.warning("摘要模型调用失败", exc_info=True)
            return ""

    def local_summary(self, transcript: str, max_chars: int = 600) -> str:
        """把最近一段对话压成「需求 / 决策 / 修改点」。"""
        raw = str(transcript or "").strip()
        if not raw:
            return ""
        summary = self._call(
            "你是项目记忆压缩器。把给定对话压缩成结构化中文摘要，"
            "只保留：用户需求、已确认决策、代码修改点、待办与约束。"
            "使用 Markdown 小节（需求 / 决策 / 修改点 / 待办），"
            f"总长度不超过 {max_chars} 字，不要寒暄与重复解释。",
            f"对话记录：\n{raw[:12000]}",
        )
        if not summary:
            summary = _fallback_digest(raw, max_chars)
        return _truncate(summary, max_chars)

    def global_summary(self, local_markdown: str, max_chars: int = 1200) -> str:
        """基于全部局部摘要生成项目级全局摘要。"""
        raw = str(local_markdown or "").strip()
        if not raw:
            return ""
        summary = self._call(
            "你是项目记忆压缩器。基于按时间排列的局部摘要，生成项目全局摘要，"
            "必须覆盖：项目目标、技术栈 / 架构、重大变更、当前状态与待办。"
            f"使用 Markdown 小节，总长度不超过 {max_chars} 字；"
            "不要罗列每一轮细节，只保留长期有效的信息。",
            f"局部摘要：\n{raw[:16000]}",
        )
        if not summary:
            summary = _fallback_global(raw, max_chars)
        return _truncate(summary, max_chars)

    def user_profile(self, material: str, max_chars: int = 600) -> str:
        """从局部摘要 + 全局摘要中提取用户偏好。"""
        raw = str(material or "").strip()
        if not raw:
            return "- 暂无明确偏好（继续从后续对话中学习）"
        profile = self._call(
            "你是用户画像提取器。从项目记录中提取长期有效的用户偏好，"
            "只输出 Markdown 列表，分三节：技术栈偏好 / 设计偏好 / 协作约束。"
            "只写有证据的偏好；没有证据的节写“暂无”。"
            f"总长度不超过 {max_chars} 字，不要复述项目业务细节。",
            f"项目记录：\n{raw[:12000]}",
        )
        if not profile:
            profile = _fallback_profile(raw)
        return _truncate(profile, max_chars)


def get_project_memory(
    settings: Settings | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    *,
    sandbox_id: str | None = None,
) -> ProjectMemory:
    """按配置构造项目记忆目录。"""
    resolved = settings or get_settings()
    return ProjectMemory(
        resolved.context_memory_dir,
        tenant_id,
        project_id,
        sandbox_id=sandbox_id,
    )


def refresh_global_memory(
    memory: ProjectMemory,
    summarizer: ContextSummarizer | None = None,
    *,
    settings: Settings | None = None,
    latest_changes: str | None = None,
) -> bool:
    """快照保存后刷新全局摘要与用户画像。

    材料优先级：
      1. 已有 local_summary.md（每 N 轮自动生成的局部摘要）；
      2. 本次快照备注 / 最近一轮改动说明（latest_changes）。

    这样即使用户尚未累积到摘要周期，第一次保存快照也会生成
    global_summary.md，Architect 从第一轮全栈澄清就能拿到项目上下文。
    返回是否真正写入；没有任何材料时不生成空文件。
    调用方负责 try/except，快照本身绝不能因为记忆生成失败而失败。
    """
    local = memory.read_local_summary().strip()
    change_note = str(latest_changes or "").strip()
    material = local
    if change_note:
        block = f"## 本次快照改动\n{change_note}"
        material = f"{local}\n\n{block}".strip() if local else block
    if not material:
        return False
    resolved = settings or get_settings()
    generator = summarizer or ContextSummarizer(resolved)
    summary = generator.global_summary(
        material,
        resolved.context_global_summary_max_chars,
    )
    if summary:
        memory.write_global_summary(summary)
    profile = generator.user_profile(
        f"{material}\n\n{summary}",
        resolved.context_user_profile_max_chars,
    )
    if profile:
        memory.write_user_profile(profile)
    return bool(summary)


# ------------------------------------------------------------
# 无模型时的确定性回退
# ------------------------------------------------------------
def _truncate(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[:max_chars]


def _lines(text: str) -> list[str]:
    return [line.strip(" -#\t") for line in text.splitlines() if line.strip()]


def _fallback_digest(text: str, max_chars: int) -> str:
    lines = _lines(text)
    if not lines:
        return ""
    keywords = (
        "用户",
        "要求",
        "必须",
        "禁止",
        "不要",
        "改为",
        "约束",
        "决策",
        "修改",
        "todo",
        "待办",
    )
    picked = [line for line in lines if any(key in line for key in keywords)]
    if not picked:
        picked = lines
    merged = "\n".join(picked[-8:])
    return _truncate(merged, max_chars)


def _fallback_global(text: str, max_chars: int) -> str:
    lines = _lines(text)
    if not lines:
        return ""
    # 局部摘要按时间追加：越靠后越新。保留尾部并去重。
    seen: set[str] = set()
    picked: list[str] = []
    for line in reversed(lines):
        if line in seen or line.startswith("Turn ") or line.startswith("<!--"):
            continue
        seen.add(line)
        picked.append(line)
        if len(picked) >= 20:
            break
    return _truncate("\n".join(reversed(picked)), max_chars)


_TECH_HINTS = (
    "React",
    "Vue",
    "TypeScript",
    "JavaScript",
    "Java",
    "Spring",
    "Python",
    "FastAPI",
    "MySQL",
    "Redis",
    "MinIO",
    "Docker",
    "Tailwind",
)
_DESIGN_HINTS = (
    "极简",
    "深色",
    "浅色",
    "蓝色",
    "红色",
    "绿色",
    "圆角",
    "间距",
    "动画",
    "响应式",
    "字体",
)


def _fallback_profile(text: str) -> str:
    tech = [item for item in _TECH_HINTS if item.lower() in text.lower()]
    design = [item for item in _DESIGN_HINTS if item in text]
    constraints = [
        line
        for line in _lines(text)
        if any(key in line for key in ("必须", "禁止", "不要", "统一", "保持"))
    ][-4:]
    return "\n".join(
        [
            "### 技术栈偏好",
            "、".join(tech) if tech else "暂无",
            "### 设计偏好",
            "、".join(design) if design else "暂无",
            "### 协作约束",
            "\n".join(f"- {line}" for line in constraints) if constraints else "暂无",
        ]
    )


__all__ = [
    "ContextSummarizer",
    "ProjectMemory",
    "ToolOutputRef",
    "get_project_memory",
    "refresh_global_memory",
]
