#!/usr/bin/env python3
"""GIS 基线 E2E：方案侦察 -> V0 -> 冻结 -> BACKEND_BINDING -> CodeGen -> 导出。

这是“故事基线”的生成脚本，不是普通单测：

    1. 调 /architect/solutions/discover 拿到复用 / 自研选项并选定一个；
    2. 用选定方案启动真实 DeepSeek Architect，驱动 V0 澄清；
    3. 把 V0 虚拟树作为冻结事实，启动 BACKEND_BINDING 业务审查；
    4. 确认契约 -> 全栈 CodeGen -> 构建 / 沙盒预览；
    5. 导出项目到 samples/gis-baseline/，并写 metadata.json 供评测复用。

环境变量：
    AI_BASE_URL=http://127.0.0.1:8080
    GIS_GOAL=...                  覆盖默认 GIS 需求
    GIS_SOLUTION_OPTION_ID=...    指定方案；默认用推荐项
    GIS_PROVIDER=auto|local|...   方案侦察 provider
    GIS_OUTPUT_DIR=samples/gis-baseline
    GIS_LLM_PROVIDER=deepseek
    GIS_ARCHITECT_TIMEOUT=900
    GIS_FULLSTACK_TIMEOUT=1200

用法：
    python3 scripts/gis_baseline_e2e.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("AI_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
VIA_BFF = ":8080" in BASE_URL
ARCHITECT_ROOT = "/api/v1/architect" if VIA_BFF else "/api/v1/internal/architect"
DISCOVER_PATH = f"{ARCHITECT_ROOT}/solutions/discover"
FULLSTACK_PATH = (
    "/api/v1/runs/fullstack" if VIA_BFF else "/api/v1/internal/fullstack/runs/stream"
)
EXPORT_ROOT = "/api/v1/runs" if VIA_BFF else "/api/v1/internal"

ARCHITECT_TIMEOUT = float(os.environ.get("GIS_ARCHITECT_TIMEOUT", "900"))
FULLSTACK_TIMEOUT = float(os.environ.get("GIS_FULLSTACK_TIMEOUT", "1200"))
MAX_CLARIFICATION_ROUNDS = int(os.environ.get("GIS_MAX_CLARIFICATION_ROUNDS", "8"))
LLM_PROVIDER = os.environ.get("GIS_LLM_PROVIDER", "deepseek")
QUALITY_TIER = os.environ.get("GIS_QUALITY_TIER", "L3_PRODUCTION")
PROVIDER = os.environ.get("GIS_PROVIDER", "auto")

DEFAULT_GOAL = (
    "做一个可上线的 GIS 任务管理平台，面向城市设施巡检团队。核心功能："
    "1) 地图总览，支持底图 / 设施点 / 巡检路线图层切换；"
    "2) 设施点位管理，支持增删改查、按类型与状态筛选；"
    "3) 空间查询，支持按当前位置或框选范围查询附近设施；"
    "4) 巡检任务工单，支持创建、指派、状态流转（待处理 / 进行中 / 已完成）和逾期标记；"
    "5) 影像上传与记录关联到设施；"
    "6) 登录与角色权限（管理员 / 巡检员）。"
    "要求使用真实数据库持久化、真实地图数据，交付可 Docker 部署的 Web 应用。"
)
GOAL = os.environ.get("GIS_GOAL", DEFAULT_GOAL)
PROJECT_NAME = os.environ.get("GIS_PROJECT_NAME", "GIS 任务管理平台")
# 全栈生成需要一个非空源码包来创建/复用沙盒；FrontendCoder 会覆盖它。
PLACEHOLDER_APP = """export default function App() {
  return <main>GIS baseline bootstrap</main>
}
"""
OUTPUT_DIR = Path(
    os.environ.get("GIS_OUTPUT_DIR", str(ROOT / "samples" / "gis-baseline"))
)

SLOT_VALUES = {
    "app.shape": "full_stack_system",
    "ui.primary_flow": (
        "打开地图总览 -> 切换图层 -> 选择设施 -> 创建巡检任务 -> "
        "指派巡检员 -> 更新状态 -> 在地图上查看任务分布"
    ),
    "business.key_flows": (
        "巡检员登录后查看分配给自己的任务；在地图上定位设施并更新任务状态；"
        "管理员创建设施、指派任务、查看逾期统计；所有状态变更写入数据库。"
    ),
    "data.entities": (
        "User(id,name,email,role); Facility(id,name,type,status,lng,lat,address); "
        "InspectionTask(id,facilityId,assigneeId,title,status,dueDate,createdAt); "
        "Attachment(id,facilityId,taskId,url,kind,createdAt)"
    ),
    "ui.layout": (
        "顶部导航（平台名 / 用户菜单）+ 左侧图层与筛选栏 + 中央地图 + "
        "右侧设施/任务详情抽屉；任务列表页提供表格与状态筛选。"
    ),
    "api.style": "RESTful JSON；资源路径使用复数名词；错误统一返回 code/message。",
    "auth.required": "yes",
    "tech.stack": (
        "React + Vite + MapLibre/Leaflet；Node.js + Express；MySQL 8 + Sequelize；"
        "Docker Compose 单机部署。"
    ),
    "deployment.target": "Docker 单机可上线；提供 Dockerfile、.env.example 与健康检查。",
    "map.provider": "OpenStreetMap / MapLibre 开源底图，避免商业地图 ToS 风险。",
    "data.source": "使用真实开放地理数据（OSM 设施点示例），不伪造业务统计。",
}


class E2EFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise E2EFailure(message)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 60,
) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def read_sse(
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    after_seq: int = 0,
    timeout: float = ARCHITECT_TIMEOUT,
    terminal_types: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    terminal = terminal_types or {
        "architect.clarification.required",
        "architect.contract.ready",
        "architect.visual_v0.ready",
        "architect.contract.confirmed",
        "architect.cancelled",
        "architect.failed",
        "fullstack.completed",
        "fullstack.failed",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    query = f"?afterSeq={after_seq}" if after_seq > 0 else ""
    request = urllib.request.Request(
        f"{BASE_URL}{path}{query}",
        data=data,
        method="POST" if payload is not None else "GET",
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Last-Event-ID": str(after_seq),
        },
    )
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    event_name = "message"
    deadline = time.monotonic() + timeout

    def flush() -> dict[str, Any] | None:
        nonlocal data_lines, event_name
        if not data_lines:
            event_name = "message"
            return None
        raw = "\n".join(data_lines)
        data_lines = []
        event_name = "message"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        parsed.setdefault("type", event_name)
        events.append(parsed)
        return parsed

    with urllib.request.urlopen(request, timeout=timeout) as response:
        check(response.status == 200, f"SSE HTTP {response.status}")
        for raw_line in response:
            if time.monotonic() > deadline:
                raise E2EFailure(f"SSE 读取超时（{timeout}s）")
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                event = flush()
                if event and event.get("type") in terminal:
                    return events, event
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value)
            elif field == "event":
                event_name = value
        event = flush()
        if event:
            return events, event
    raise E2EFailure("SSE 在终态事件前关闭")


# ------------------------------------------------------------
# 1. 方案侦察
# ------------------------------------------------------------
def discover_solutions() -> tuple[dict[str, Any], dict[str, Any]]:
    log("方案侦察：分析需求 + 检索可复用方案")
    status, body = request_json(
        "POST",
        DISCOVER_PATH,
        {
            "message": GOAL,
            "projectId": f"prj_gis_{uuid.uuid4().hex[:8]}",
            "projectName": PROJECT_NAME,
            "provider": PROVIDER,
            "maxOptions": 3,
        },
        timeout=60,
    )
    check(status == 200, f"方案侦察失败 HTTP {status}: {body}")
    check(bool(body.get("options")), "方案侦察没有返回任何选项")
    option_id = os.environ.get("GIS_SOLUTION_OPTION_ID") or body.get(
        "recommendedOptionId"
    )
    option = next(
        (item for item in body["options"] if item.get("optionId") == option_id),
        None,
    )
    if option is None:
        option = body["options"][0]
    log(
        f"选定方案：{option.get('title')} ({option.get('kind')}), "
        f"source={option.get('source')}, fit={option.get('fitScore')}"
    )
    return body, option


def freeze_document(document: dict[str, Any]) -> dict[str, Any]:
    """用领域层同一套规则冻结 V0 文档（写 FROZEN + contentHash）。

    脚本通常在 python-ai 容器内执行（``PYTHONPATH`` 指向 orchestrator），
    因此可直接复用 Python 的 freeze 实现，避免前后端哈希规则漂移。
    在宿主机执行且缺少依赖时降级为原样传递，并明确告警。
    """
    try:
        from ai_core.domain.visual_document import (
            VisualDocument,
            freeze_visual_document,
        )

        frozen = freeze_visual_document(
            VisualDocument.model_validate(document)
        )
        return frozen.model_dump(mode="json", by_alias=True)
    except Exception as exc:  # noqa: BLE001 - 宿主机降级路径
        log(f"WARN: 本地冻结失败，按原文档传递：{exc}")
        return document


def solution_choice(option: dict[str, Any]) -> dict[str, Any]:
    return {
        "optionId": option["optionId"],
        "kind": option.get("kind") or "BUILD",
        "title": option.get("title") or "自研方案",
        "sourceUrl": option.get("sourceUrl"),
        "license": option.get("license"),
        "summary": option.get("summary") or "",
        "integrationPlan": (
            f"以 {option.get('title')} 为"
            f"{'依赖' if option.get('kind') == 'REUSE' else '改造基线'}；"
            "业务模型、页面与权限按契约自研，不复制整站业务代码。"
        ),
        "notes": [
            f"来源：{option.get('source') or 'unknown'}",
            f"集成成本：{option.get('integrationCost') or 'unknown'}",
        ],
    }


# ------------------------------------------------------------
# 2. Architect 两阶段
# ------------------------------------------------------------
def answer_clarification(run_id: str, clarification: dict[str, Any]) -> None:
    answers = []
    for question in clarification.get("questions") or []:
        slot = question.get("slot") or ""
        options = question.get("options") or []
        if slot == "app.shape":
            value: Any = "full_stack_system"
        elif options:
            preferred = next(
                (
                    item
                    for item in options
                    if "推荐" in str(item.get("label") or "")
                ),
                options[0],
            )
            value = (
                [preferred.get("value")]
                if question.get("type") == "multi_choice"
                else preferred.get("value")
            )
        else:
            value = SLOT_VALUES.get(slot, f"{slot or '补充'}：按推荐方案实现")
        answer: dict[str, Any] = {
            "questionId": question.get("questionId"),
            "value": value,
        }
        if isinstance(value, str):
            answer["text"] = value
        answers.append(answer)
    status, body = request_json(
        "POST",
        f"{ARCHITECT_ROOT}/runs/{run_id}/clarifications/"
        f"{clarification['clarificationId']}/answers",
        {
            "answers": answers,
            "comment": (
                "基线运行：按推荐选项推进；真实数据、可 Docker 上线、"
                "登录与角色权限是硬约束。"
            ),
        },
        timeout=60,
    )
    check(status == 202, f"提交澄清失败 HTTP {status}: {body}")


def _architect_options() -> dict[str, Any]:
    return {
        "llmProvider": LLM_PROVIDER,
        "confidenceThreshold": 0.82,
        "maxClarificationRounds": MAX_CLARIFICATION_ROUNDS,
        "ragProvider": "markdown",
        "ragTopK": 5,
    }


def start_architect(
    run_id: str,
    *,
    phase: str,
    project_id: str,
    choice: dict[str, Any],
    visual_document: dict[str, Any] | None = None,
    visual_delta: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if phase == "VISUAL_V0":
        history = [{"role": "user", "content": GOAL}]
    else:
        history = [
            {
                "role": "system",
                "content": (
                    "【SEMANTIC_REVIEW_PACKET】已冻结 V0 虚拟界面。"
                    "只审查交互与数据绑定缺口，不要重新询问页面布局或视觉风格。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请开始业务审查：只针对这份已冻结虚拟界面的交互和数据绑定"
                    "缺口发起澄清；不要重新询问页面布局或视觉风格。"
                ),
            },
        ]
    payload = {
        "runId": run_id,
        "tenantId": "tenant_gis_e2e",
        "projectId": project_id,
        "projectName": PROJECT_NAME,
        "source": {"files": {}},
        "projectSummary": "GIS 任务管理平台；V0 虚拟树已冻结。",
        "latestChanges": "冻结 V0 虚拟树，进入后端绑定。",
        "qualityTier": QUALITY_TIER,
        "solutionChoice": choice,
        "projectMode": "NEW_PROJECT",
        "outputPhase": phase,
        "visualDocument": visual_document,
        "visualDelta": visual_delta or {},
        "uiHistory": history,
        "options": _architect_options(),
    }
    return read_sse(
        f"{ARCHITECT_ROOT}/runs/stream",
        payload,
        timeout=ARCHITECT_TIMEOUT,
    )


def drive_to_contract(
    run_id: str,
    *,
    phase: str,
    project_id: str,
    choice: dict[str, Any],
    visual_document: dict[str, Any] | None = None,
    visual_delta: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    events, terminal = start_architect(
        run_id,
        phase=phase,
        project_id=project_id,
        choice=choice,
        visual_document=visual_document,
        visual_delta=visual_delta,
    )
    all_events = list(events)
    last_seq = int(terminal.get("seq") or 0)
    for round_no in range(1, MAX_CLARIFICATION_ROUNDS + 1):
        if terminal.get("type") == "architect.contract.ready":
            contract = terminal.get("payload", {}).get("contract") or {}
            check(bool(contract.get("contractId")), "契约缺少 contractId")
            log(
                f"{phase} 契约就绪：{contract.get('contractId')}，"
                f"routes={len(contract.get('apiRoutes') or [])}，"
                f"tables={contract.get('databaseDdl', '').count('CREATE TABLE')}"
            )
            return contract, last_seq, all_events
        check(
            terminal.get("type") == "architect.clarification.required",
            f"{phase} 预期澄清或契约，实际 {terminal.get('type')}: "
            f"{json.dumps(terminal.get('payload'), ensure_ascii=False)[:600]}",
        )
        log(f"{phase} 澄清第 {round_no} 轮")
        answer_clarification(run_id, terminal["payload"])
        replay, terminal = read_sse(
            f"{ARCHITECT_ROOT}/runs/{run_id}/events/stream",
            after_seq=last_seq,
            timeout=ARCHITECT_TIMEOUT,
        )
        all_events.extend(replay)
        last_seq = max(
            [last_seq] + [int(event.get("seq") or 0) for event in replay + [terminal]]
        )
    raise E2EFailure(f"{phase} 超过 {MAX_CLARIFICATION_ROUNDS} 轮仍未产出契约")


def confirm_contract(
    run_id: str,
    contract: dict[str, Any],
    after_seq: int,
) -> dict[str, Any]:
    status, body = request_json(
        "POST",
        f"{ARCHITECT_ROOT}/runs/{run_id}/contracts/"
        f"{contract['contractId']}/decision",
        {
            "contractId": contract["contractId"],
            "decision": "CONFIRM",
            "comment": "GIS 基线确认，进入全栈生成",
            "qualityTier": QUALITY_TIER,
        },
        timeout=60,
    )
    check(status == 202, f"CONFIRM 失败 HTTP {status}: {body}")
    _, terminal = read_sse(
        f"{ARCHITECT_ROOT}/runs/{run_id}/events/stream",
        after_seq=after_seq,
        timeout=ARCHITECT_TIMEOUT,
    )
    check(
        terminal.get("type") == "architect.contract.confirmed",
        f"CONFIRM 未生效：{terminal.get('type')}",
    )
    confirmed = terminal.get("payload", {}).get("contract") or {}
    check(confirmed.get("status") == "CONFIRMED", "确认后的契约状态错误")
    return confirmed


# ------------------------------------------------------------
# 3. 全栈 CodeGen
# ------------------------------------------------------------
def start_fullstack(
    run_id: str,
    project_id: str,
    contract: dict[str, Any],
    visual_document: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = {
        "runId": run_id,
        "tenantId": "tenant_gis_e2e",
        "projectId": project_id,
        "contractId": contract["contractId"],
        "contract": contract,
        # 冻结文档是前端事实源；显式传入，避免契约装配时回退草稿树。
        "visualDocument": visual_document or contract.get("visualDocument"),
        # 全栈生成要求 sandboxId 或非空 source.files；占位 App.jsx 只用于
        # 创建沙盒，随后 FrontendCoder / Visual CodeGen 会覆盖它。
        "source": {"files": {"src/App.jsx": PLACEHOLDER_APP}},
        "options": {"verifyBuild": True, "restartSandbox": True},
    }
    return read_sse(
        FULLSTACK_PATH,
        payload,
        timeout=FULLSTACK_TIMEOUT,
        terminal_types={"fullstack.completed", "fullstack.failed"},
    )


# ------------------------------------------------------------
# 4. 导出
# ------------------------------------------------------------
def _safe_extract(tar_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as archive:
        for member in archive.getmembers():
            name = member.name
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts:
                raise E2EFailure(f"导出包包含非法路径：{name}")
            if member.issym() or member.islnk():
                raise E2EFailure(f"导出包包含符号链接：{name}")
            # 保留 dist：它是 BuildVerifier 的真实产物，评测需要构建证据。
            if any(part in {"node_modules", ".git", ".cache"} for part in parts):
                continue
            archive.extract(member, dest, filter="data")


def export_project(run_id: str, sandbox_id: str, output_dir: Path) -> Path:
    log("导出沙盒项目")
    query = urllib.parse.urlencode({"sandboxId": sandbox_id})
    url = f"{BASE_URL}/api/v1/runs/{urllib.parse.quote(run_id)}/export?{query}"
    request = urllib.request.Request(url, headers={"Accept": "application/gzip"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    check(len(data) > 100, f"导出包过小：{len(data)} bytes")
    archive_path = output_dir / "project-export.tar.gz"
    archive_path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    source_dir = output_dir / "source"
    if source_dir.exists():
        shutil.rmtree(source_dir)
    _safe_extract(archive_path, source_dir)
    # 如果 tar 只有一个顶层目录，提升一层，方便评测脚本直接指向项目根。
    entries = [entry for entry in source_dir.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        nested = entries[0]
        for child in nested.iterdir():
            shutil.move(str(child), source_dir / child.name)
        nested.rmdir()
    log(f"导出完成：{source_dir}（sha256={digest[:12]}…）")
    return source_dir


# ------------------------------------------------------------
# 5. 主流程
# ------------------------------------------------------------
def main() -> int:
    started = time.time()
    resume_dir = os.environ.get("GIS_RESUME_DIR", "").strip()
    if resume_dir:
        return _resume_fullstack(Path(resume_dir).resolve(), started)
    output_dir = OUTPUT_DIR
    if output_dir.exists() and any(output_dir.iterdir()):
        output_dir = output_dir.with_name(
            f"{output_dir.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    log(f"BASE_URL={BASE_URL} output={output_dir}")

    discovery, option = discover_solutions()
    (output_dir / "discovery.json").write_text(
        json.dumps(discovery, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    choice = solution_choice(option)

    project_id = f"prj_gis_{uuid.uuid4().hex[:10]}"
    v0_run_id = f"arc_gis_v0_{uuid.uuid4().hex[:10]}"
    backend_run_id = f"arc_gis_be_{uuid.uuid4().hex[:10]}"
    fullstack_run_id = f"fsk_gis_{uuid.uuid4().hex[:10]}"

    log("阶段 A：V0 虚拟树")
    v0_contract, v0_seq, v0_events = drive_to_contract(
        v0_run_id,
        phase="VISUAL_V0",
        project_id=project_id,
        choice=choice,
    )
    visual_document = v0_contract.get("visualDocument") or {}
    check(
        bool(
            visual_document.get("root")
            or visual_document.get("pages")
            or visual_document.get("nodes")
        ),
        "V0 契约没有可冻结的 VisualDocument",
    )
    visual_document = freeze_document(visual_document)
    frozen_hash = str(
        (visual_document.get("metadata") or {}).get("contentHash") or ""
    )
    log(f"V0 冻结：status={visual_document.get('status')} hash={frozen_hash[:24]}")
    (output_dir / "v0-contract.json").write_text(
        json.dumps(v0_contract, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "v0-visual-frozen.json").write_text(
        json.dumps(visual_document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log("阶段 B：冻结虚拟树 -> BACKEND_BINDING")
    backend_contract, backend_seq, backend_events = drive_to_contract(
        backend_run_id,
        phase="BACKEND_BINDING",
        project_id=project_id,
        choice=choice,
        visual_document=visual_document,
        visual_delta={},
    )
    confirmed = confirm_contract(backend_run_id, backend_contract, backend_seq)
    (output_dir / "contract.json").write_text(
        json.dumps(confirmed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log("阶段 C：全栈 CodeGen")
    fullstack_events, terminal = start_fullstack(
        fullstack_run_id,
        project_id,
        confirmed,
        visual_document=visual_document,
    )
    check(
        terminal.get("type") == "fullstack.completed",
        f"全栈生成失败：{terminal.get('type')} -> "
        f"{json.dumps(terminal.get('payload'), ensure_ascii=False)[:800]}",
    )
    payload = terminal.get("payload") or {}
    check(payload.get("status") == "SUCCEEDED", "全栈终态不是 SUCCEEDED")
    sandbox_id = payload.get("sandboxId") or ""
    check(bool(sandbox_id), "全栈终态缺少 sandboxId")

    source_dir = export_project(fullstack_run_id, sandbox_id, output_dir)
    metadata = _write_metadata(
        output_dir,
        discovery=discovery,
        choice=choice,
        project_id=project_id,
        v0_run_id=v0_run_id,
        backend_run_id=backend_run_id,
        fullstack_run_id=fullstack_run_id,
        confirmed=confirmed,
        visual_document=visual_document,
        frozen_hash=frozen_hash,
        fullstack_events=fullstack_events,
        terminal=terminal,
        source_dir=source_dir,
        started=started,
        v0_events=v0_events,
        backend_events=backend_events,
    )
    log(
        f"基线完成：{source_dir}，耗时 {metadata['elapsedSeconds']}s，"
        f"preview={metadata['previewUrl']}"
    )
    return 0


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pick_option(discovery: dict[str, Any]) -> dict[str, Any]:
    options = discovery.get("options") or []
    check(bool(options), "discovery.json 中没有方案选项")
    option_id = os.environ.get("GIS_SOLUTION_OPTION_ID") or discovery.get(
        "recommendedOptionId"
    )
    option = next(
        (item for item in options if item.get("optionId") == option_id), None
    )
    return option or options[0]


def _write_metadata(
    output_dir: Path,
    *,
    discovery: dict[str, Any],
    choice: dict[str, Any],
    project_id: str,
    v0_run_id: str,
    backend_run_id: str,
    fullstack_run_id: str,
    confirmed: dict[str, Any],
    visual_document: dict[str, Any],
    frozen_hash: str,
    fullstack_events: list[dict[str, Any]],
    terminal: dict[str, Any],
    source_dir: Path,
    started: float,
    v0_events: list[dict[str, Any]],
    backend_events: list[dict[str, Any]],
    resumed_from: str = "",
) -> dict[str, Any]:
    payload = terminal.get("payload") or {}
    metadata = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "baseUrl": BASE_URL,
        "goal": GOAL,
        "projectId": project_id,
        "projectName": PROJECT_NAME,
        "qualityTier": QUALITY_TIER,
        "llmProvider": LLM_PROVIDER,
        "solutionChoice": choice,
        "discovery": {
            "provider": discovery.get("provider"),
            "degraded": discovery.get("degraded"),
            "recommendedOptionId": discovery.get("recommendedOptionId"),
            "options": [
                {
                    "optionId": item.get("optionId"),
                    "kind": item.get("kind"),
                    "title": item.get("title"),
                    "source": item.get("source"),
                    "fitScore": item.get("fitScore"),
                }
                for item in discovery.get("options") or []
            ],
        },
        "v0RunId": v0_run_id,
        "backendRunId": backend_run_id,
        "fullstackRunId": fullstack_run_id,
        "contractId": confirmed.get("contractId"),
        "visualContentHash": frozen_hash,
        "sandboxId": payload.get("sandboxId") or "",
        "previewUrl": payload.get("previewUrl"),
        "collectionPath": payload.get("collectionPath"),
        "databaseEngine": payload.get("databaseEngine"),
        "orm": payload.get("orm"),
        "infraFiles": payload.get("infraFiles") or [],
        "fullstackStatus": payload.get("status"),
        "fullstackSteps": [
            {
                "node": step.get("node"),
                "status": step.get("status"),
                "summary": str(step.get("summary") or "")[:300],
            }
            for step in (payload.get("steps") or [])
        ],
        "contractSummary": {
            "appShape": confirmed.get("appShape"),
            "apiRoutes": len(confirmed.get("apiRoutes") or []),
            "tables": str(confirmed.get("databaseDdl") or "").count("CREATE TABLE"),
            "frontendRoutes": len(confirmed.get("frontendRoutes") or []),
            "components": len(confirmed.get("components") or []),
            "visualNodes": _count_visual_nodes(
                confirmed.get("visualDocument") or visual_document
            ),
        },
        "sourceDir": str(source_dir),
        "elapsedSeconds": round(time.time() - started, 1),
        "events": _event_summary(
            [*v0_events, *backend_events, *fullstack_events, terminal]
        ),
    }
    if resumed_from:
        metadata["resumedFrom"] = resumed_from
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metadata


def _resume_fullstack(resume_dir: Path, started: float) -> int:
    """跳过 V0 / BACKEND_BINDING，用已确认契约直接补跑全栈生成与导出。"""
    output_dir = resume_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    discovery = _load_json(output_dir / "discovery.json")
    confirmed = _load_json(output_dir / "contract.json")
    visual_document = _load_json(output_dir / "v0-visual-frozen.json")
    v0_contract = _load_json(output_dir / "v0-contract.json")
    check(bool(confirmed.get("contractId")), "恢复失败：contract.json 缺少 contractId")
    check(bool(visual_document), "恢复失败：v0-visual-frozen.json 为空")
    choice = solution_choice(_pick_option(discovery))
    project_id = os.environ.get("GIS_PROJECT_ID") or (
        f"prj_gis_resume_{uuid.uuid4().hex[:10]}"
    )
    fullstack_run_id = f"fsk_gis_{uuid.uuid4().hex[:10]}"
    frozen_hash = str(
        (visual_document.get("metadata") or {}).get("contentHash") or ""
    )
    log(f"恢复模式：跳过 V0/后端绑定，直接全栈生成 project={project_id}")
    fullstack_events, terminal = start_fullstack(
        fullstack_run_id,
        project_id,
        confirmed,
        visual_document=visual_document,
    )
    check(
        terminal.get("type") == "fullstack.completed",
        f"全栈生成失败：{terminal.get('type')} -> "
        f"{json.dumps(terminal.get('payload'), ensure_ascii=False)[:800]}",
    )
    payload = terminal.get("payload") or {}
    check(payload.get("status") == "SUCCEEDED", "全栈终态不是 SUCCEEDED")
    sandbox_id = payload.get("sandboxId") or ""
    check(bool(sandbox_id), "全栈终态缺少 sandboxId")
    source_dir = export_project(fullstack_run_id, sandbox_id, output_dir)
    metadata = _write_metadata(
        output_dir,
        discovery=discovery,
        choice=choice,
        project_id=project_id,
        v0_run_id=str(v0_contract.get("runId") or ""),
        backend_run_id=str(confirmed.get("runId") or ""),
        fullstack_run_id=fullstack_run_id,
        confirmed=confirmed,
        visual_document=visual_document,
        frozen_hash=frozen_hash,
        fullstack_events=fullstack_events,
        terminal=terminal,
        source_dir=source_dir,
        started=started,
        v0_events=[],
        backend_events=[],
        resumed_from=str(resume_dir),
    )
    log(
        f"恢复基线完成：{source_dir}，耗时 {metadata['elapsedSeconds']}s，"
        f"preview={metadata['previewUrl']}"
    )
    return 0


def _count_visual_nodes(document: dict[str, Any]) -> int:
    nodes = document.get("nodes")
    if isinstance(nodes, list):
        return len(nodes)
    pages = document.get("pages")
    if isinstance(pages, list):
        total = 0
        for page in pages:
            root = page.get("root") if isinstance(page, dict) else None
            if isinstance(root, dict):
                total += _count_tree(root)
        return total
    root = document.get("root")
    return _count_tree(root) if isinstance(root, dict) else 0


def _count_tree(root: dict[str, Any]) -> int:
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        count += 1
        stack.extend(node.get("children") or [])
    return count


def _event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("type") or "unknown")
        counts[event_type] = counts.get(event_type, 0) + 1
    return {
        "counts": counts,
        "total": len(events),
    }


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except E2EFailure as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
