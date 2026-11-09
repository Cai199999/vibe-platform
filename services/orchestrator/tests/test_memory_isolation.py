"""记忆与状态隔离测试（Memory / State Isolation）。

覆盖三层边界：
  1. ThreadMemoryStore：同名的业务 ``threadId`` 在不同项目 / 租户下必须
     落在不同物理 key；
  2. LangGraph Checkpointer：UI Run 与 Architect Run 使用不同 kind，且叠加
     项目作用域，互相不可读取；
  3. Supervisor：Architect 只收到架构专属历史，UI 只收到画布标注，任何
     一方都不能读到另一方的上下文。

其中 ``MemoryIsolationE2ETest`` 会真实执行一次 UI Graph（Mock LLM + 内存
沙盒）和一次 Architect Graph（Mock LLM），然后在共享的 MemorySaver 中
断言两侧状态互不可见。
"""

from __future__ import annotations

import threading
import unittest
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from ai_core.agent.graph import AgentGraphRunner
from ai_core.agent.llm import MockAgentLLM
from ai_core.agent.service import RunExecutionService
from ai_core.agent.thread_memory import ThreadMemoryStore
from ai_core.adapters.checkpoint import MemoryCheckpointerProvider
from ai_core.architect.graph import ArchitectGraphRunner
from ai_core.architect.llm import MockArchitectLLM
from ai_core.domain.agent import AnnotationEnvelope, ExecuteRunRequest
from ai_core.domain.scope import (
    memory_scope,
    scoped_checkpoint_key,
    scoped_thread_key,
)
from ai_core.domain.state import AgentState, ArchitectState
from ai_core.domain.supervisor import SupervisorRequest
from ai_core.ports.sandbox import SandboxHandle, SandboxProvider, VerifyResult
from ai_core.supervisor.service import (
    ArchitectSubGraphDispatcher,
    UiSubGraphDispatcher,
)

# 带 <main> 容器的真实 JSX：Mock Coder 在没有 <style> 块时会把样式
# 插入主容器末尾，因此这里不能只保留裸 <button>，否则 Mock 无法产生
# 有效 diff，E2E 也就验证不到“UI 修改确实写入了 Coder 产物”。
UI_SOURCE = """export default function App() {
  return (
    <main className="page">
      <button className="primary">提交</button>
    </main>
  )
}
"""

# 用于跨图断言的唯一标记，避免误命中通用词。
UI_MARKER = "UI_ONLY_MARKER_blue_button"
ARCH_MARKER = "ARCH_ONLY_MARKER_order_ddl"


def _annotation(thread_id: str, text: str) -> AnnotationEnvelope:
    return AnnotationEnvelope.model_validate(
        {
            "annotationId": "ann_memory",
            "target": {
                "mode": "source",
                "source": {"file": "src/App.jsx", "line": 2},
                "selector": "button.primary",
            },
            "intent": {"kind": "UI_ONLY", "ui": {"summary": text}},
            "payload": {"text": text},
            "threadId": thread_id,
        }
    )


class _MemorySandbox(SandboxProvider):
    """只在内存里维护文件；不启动 Docker，不访问网络。"""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self._lock = threading.Lock()

    def create(
        self,
        project_id: str,
        files: dict[str, str],
        template: str = "react-vite",
    ) -> SandboxHandle:
        with self._lock:
            self.files = dict(files)
        now = datetime.now(timezone.utc)
        return SandboxHandle(
            sandbox_id=f"sbx_{uuid.uuid4().hex[:10]}",
            container_id="fake",
            container_name="fake",
            project_id=project_id,
            state="running",
            preview_url="http://127.0.0.1:9999",
            host_port=9999,
            internal_port=5173,
            template=template,
            created_at=now,
        )

    def apply_patch(
        self,
        sandbox_id: str,
        files: dict[str, str] | None = None,
        patch: str | None = None,
        deleted_paths: list[str] | None = None,
    ) -> list[str]:
        with self._lock:
            incoming = dict(files or {})
            self.files.update(incoming)
            for path in deleted_paths or []:
                self.files.pop(path, None)
            return sorted(incoming)

    def verify(self, sandbox_id: str, check: str = "build") -> VerifyResult:
        return VerifyResult(
            success=True,
            exit_code=0,
            timed_out=False,
            duration_ms=1,
            logs="ok",
            logs_truncated=False,
        )

    def read_file(self, sandbox_id: str, path: str) -> str:
        return self.files[path]

    def export_archive(self, sandbox_id: str) -> bytes:
        return b""

    def destroy(self, sandbox_id: str) -> None:
        return None


def _ui_initial_state(
    service: RunExecutionService,
    store: ThreadMemoryStore,
    *,
    run_id: str,
    tenant_id: str,
    project_id: str,
    thread_id: str,
) -> dict:
    text = "把按钮改成蓝色"
    annotation = _annotation(thread_id, text)
    request = ExecuteRunRequest.model_validate(
        {
            "runId": run_id,
            "tenantId": tenant_id,
            "projectId": project_id,
            "annotations": [annotation.model_dump(by_alias=True)],
            "files": {"src/App.jsx": UI_SOURCE},
        }
    )
    target_files = service._resolve_target_files(request)
    scope = memory_scope(tenant_id, project_id)
    thread_context = store.merge_context(thread_id, scope=scope)
    context = service._build_annotation_context(
        request,
        target_files,
        {thread_id: thread_context},
        {"src/App.jsx": UI_SOURCE},
    )
    return {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "sandbox_id": f"sbx_{run_id}",
        "preview_url": "http://127.0.0.1:9999",
        "target_file": "src/App.jsx",
        "target_files": target_files,
        "annotation_context": context,
        "messages": [],
        "current_code": UI_SOURCE,
        "current_files": {"src/App.jsx": UI_SOURCE},
        "initial_files": {"src/App.jsx": UI_SOURCE},
        "plan": "",
        "generated_patch": "",
        "generated_files": {},
        "verify_result": {},
        "apply_failed": False,
        "retry_count": 0,
        "max_retries": 0,
        "history_summary": "",
        "clarification_mode": "off",
        "max_clarification_rounds": 3,
        "confidence_threshold": 0.7,
        "clarification_round": 0,
        "clarification_answers": [],
        "clarification": None,
        "approval_required": False,
        "status": "RUNNING",
        "error": None,
        "error_code": None,
    }


def _architect_initial_state(
    *,
    run_id: str,
    tenant_id: str,
    project_id: str,
    history: list[dict],
) -> dict:
    return {
        "run_id": run_id,
        "project_id": project_id,
        "project_name": "隔离测试项目",
        "source_files": {"src/App.jsx": UI_SOURCE},
        "source_hash": "hash",
        "source_origin": "request",
        "source_warnings": [],
        "ui_history": history,
        "history_summary": "",
        "history_tokens_before": 0,
        "history_tokens_after": 0,
        "threshold": 0.85,
        "max_rounds": 5,
        "round": 0,
        "status": "INGESTING",
        "app_shape": "",
        "slots": {},
        "confidence_score": 0.0,
        "missing_slots": [],
        "questions": [],
        "analysis": {},
        "retrieved_chunks": [],
        "clarification": {},
        "contract": {},
        "contract_draft": {},
        "contract_issues": [],
        "contract_id": "",
        "contract_decision": "",
        "contract_feedback": "",
        "repair_count": 0,
        "repair_limit": 1,
        "contract_revision_count": 0,
        "asked_slots": [],
        "messages": [],
        "rag_provider": "markdown",
        "rag_top_k": 2,
        "error": "",
        "error_code": "",
    }


class ScopeKeyTest(unittest.TestCase):
    """作用域键必须稳定，且项目 / Agent 之间严格区分。"""

    def test_same_thread_id_gets_different_project_keys(self) -> None:
        key_a = scoped_thread_key("thread_demo", tenant_id="t1", project_id="p1")
        key_b = scoped_thread_key("thread_demo", tenant_id="t1", project_id="p2")
        self.assertNotEqual(key_a, key_b)
        self.assertEqual(
            key_a,
            scoped_thread_key("thread_demo", tenant_id="t1", project_id="p1"),
        )

    def test_tenant_changes_key_even_with_same_project(self) -> None:
        key_a = scoped_thread_key("thread_demo", tenant_id="t1", project_id="p1")
        key_b = scoped_thread_key("thread_demo", tenant_id="t2", project_id="p1")
        self.assertNotEqual(key_a, key_b)

    def test_agent_kind_changes_checkpoint_key(self) -> None:
        ui = scoped_checkpoint_key("ui_run", "run_x", tenant_id="t1", project_id="p1")
        architect = scoped_checkpoint_key(
            "architect_run", "run_x", tenant_id="t1", project_id="p1"
        )
        self.assertNotEqual(ui, architect)
        self.assertIn("ui_run", ui)
        self.assertIn("architect_run", architect)


class ThreadMemoryIsolationTest(unittest.TestCase):
    """ThreadMemoryStore 是进程内全局单例，必须按项目物理隔离。"""

    def test_same_thread_id_across_projects_does_not_share_history(self) -> None:
        store = ThreadMemoryStore()
        raw_thread_id = "thread_same"
        scope_a = memory_scope("tenant_a", "project_a")
        scope_b = memory_scope("tenant_b", "project_b")

        store.record_run(
            [raw_thread_id],
            run_id="run_a",
            user_messages={raw_thread_id: {"role": "user", "content": UI_MARKER}},
            assistant_message="已应用：按钮改成蓝色",
            success=True,
            sandbox_id="sbx_a",
            files={"src/App.jsx": UI_SOURCE},
            patch="",
            changed_files=["src/App.jsx"],
            retry_count=0,
            turn_counts={raw_thread_id: 1},
            commit_hash="a" * 40,
            scope=scope_a,
        )

        context_b = store.merge_context(raw_thread_id, scope=scope_b)
        self.assertEqual(context_b["chatHistory"], [])
        self.assertEqual(store.sandbox_id(raw_thread_id, scope=scope_b), None)
        self.assertEqual(store.seed_files([raw_thread_id], scope=scope_b), {})
        self.assertIsNone(
            store.checkpoint(
                raw_thread_id,
                commit_hash="a" * 40,
                scope=scope_b,
            )
        )

        context_a = store.merge_context(raw_thread_id, scope=scope_a)
        self.assertTrue(context_a["chatHistory"])
        self.assertEqual(store.sandbox_id(raw_thread_id, scope=scope_a), "sbx_a")

    def test_rollback_cannot_restore_other_project_checkpoint(self) -> None:
        store = ThreadMemoryStore()
        raw_thread_id = "thread_rollback"
        scope_a = memory_scope("tenant", "project_a")
        scope_b = memory_scope("tenant", "project_b")
        store.record_run(
            [raw_thread_id],
            run_id="run_a",
            user_messages={raw_thread_id: {"role": "user", "content": "A"}},
            assistant_message="A done",
            success=True,
            sandbox_id="sbx_a",
            files={"src/App.jsx": "A"},
            patch="",
            changed_files=["src/App.jsx"],
            retry_count=0,
            turn_counts={raw_thread_id: 1},
            commit_hash="b" * 40,
            scope=scope_a,
        )
        restored = store.rollback_to_checkpoint(
            raw_thread_id,
            commit_hash="b" * 40,
            scope=scope_b,
        )
        self.assertIsNone(restored)


class StateSchemaBoundaryTest(unittest.TestCase):
    """UI State 不得出现架构契约字段；Architect State 不得出现 Coder 产物。"""

    def test_ui_state_has_no_architecture_contract_fields(self) -> None:
        fields = set(AgentState.__annotations__)
        forbidden = {
            "database_ddl",
            "api_routes",
            "contract",
            "contract_draft",
            "app_shape",
            "slots",
            "retrieved_chunks",
            "architect_history",
        }
        self.assertEqual(fields & forbidden, set())

    def test_architect_state_has_no_coder_output_fields(self) -> None:
        fields = set(ArchitectState.__annotations__)
        forbidden = {
            "generated_files",
            "generated_patch",
            "patch_id",
            "current_files",
            "current_code",
            "initial_files",
            "target_files",
            "verify_result",
            "apply_failed",
        }
        self.assertEqual(fields & forbidden, set())


class SupervisorContextProjectionTest(unittest.TestCase):
    """Supervisor 派发时必须做专属 Context 投影。"""

    def setUp(self) -> None:
        self.ui_text = f"{UI_MARKER} 请把按钮改成蓝色"
        self.arch_text = f"{ARCH_MARKER} 订单表需要哪些字段"
        self.request = SupervisorRequest.model_validate(
            {
                "tenantId": "tenant_scope",
                "projectId": "project_scope",
                "projectName": "隔离项目",
                "message": "先设计架构，再改按钮",
                "annotations": [
                    {
                        "annotationId": "ann_1",
                        "threadId": "thread_ui",
                        "intent": {
                            "kind": "UI_ONLY",
                            "ui": {"summary": self.ui_text},
                        },
                        "payload": {"text": self.ui_text},
                        "chatHistory": [
                            {"role": "user", "content": self.ui_text},
                            {
                                "role": "assistant",
                                "content": "已应用：按钮蓝色",
                                "runId": "run_ui",
                            },
                        ],
                    }
                ],
                "files": {"src/App.jsx": UI_SOURCE},
                "architectHistory": [
                    {"role": "user", "content": self.arch_text},
                    {"role": "assistant", "content": "建议使用 MySQL 订单表"},
                ],
                # 旧字段即使被恶意塞入 UI 历史，也必须被忽略。
                "uiHistory": [{"role": "user", "content": self.ui_text}],
            }
        )

    @staticmethod
    def _service() -> tuple[SimpleNamespace, object]:
        captured: list[object] = []

        class _Service:
            def start_stream(self, request):
                captured.append(request)
                return SimpleNamespace(run_id="fake_run")

        return _Service(), captured

    def test_architect_receives_only_architect_history(self) -> None:
        service, captured = self._service()
        dispatcher = ArchitectSubGraphDispatcher(service_factory=lambda: service)
        dispatcher.start(
            self.request,
            SimpleNamespace(agent_id="architect_graph"),
        )
        architect_request = captured[0]
        self.assertEqual(architect_request.tenant_id, "tenant_scope")
        self.assertEqual(
            [item["content"] for item in architect_request.ui_history],
            [self.arch_text, "建议使用 MySQL 订单表"],
        )
        serialized = architect_request.model_dump_json(by_alias=True)
        self.assertNotIn(UI_MARKER, serialized)
        self.assertFalse(hasattr(architect_request, "annotations"))

    def test_ui_receives_only_canvas_annotations(self) -> None:
        service, captured = self._service()
        dispatcher = UiSubGraphDispatcher(service_factory=lambda: service)
        dispatcher.start(self.request, SimpleNamespace(agent_id="ui_graph"))
        ui_request = captured[0]
        self.assertEqual(ui_request.tenant_id, "tenant_scope")
        self.assertEqual(len(ui_request.annotations), 1)
        self.assertEqual(
            [item.content for item in ui_request.annotations[0].chat_history],
            [self.ui_text, "已应用：按钮蓝色"],
        )
        serialized = ui_request.model_dump_json(by_alias=True)
        self.assertNotIn(ARCH_MARKER, serialized)
        self.assertFalse(hasattr(ui_request, "contract"))


class MemoryIsolationE2ETest(unittest.TestCase):
    """真实执行 UI 修改与 Architect 澄清，验证共享 MemorySaver 不串数据。"""

    def test_ui_and_architect_threads_are_physically_isolated(self) -> None:
        provider = MemoryCheckpointerProvider()
        store = ThreadMemoryStore()
        sandbox = _MemorySandbox()
        # AgentGraphRunner 直接操作已存在的沙盒，这里预置入口文件。
        sandbox.files = {"src/App.jsx": UI_SOURCE}
        service = RunExecutionService(
            sandbox=sandbox,
            thread_store=store,
            checkpointer_provider=provider,
        )

        # ---- Thread A：真实 UI Graph 执行一次按钮改色 ----
        ui_run_id = "run_ui_iso"
        ui_project = "project_ui"
        ui_thread = "thread_ui_iso"
        ui_key = scoped_checkpoint_key(
            "ui_run",
            ui_run_id,
            tenant_id="tenant_a",
            project_id=ui_project,
        )
        ui_state = _ui_initial_state(
            service,
            store,
            run_id=ui_run_id,
            tenant_id="tenant_a",
            project_id=ui_project,
            thread_id=ui_thread,
        )
        ui_runner = AgentGraphRunner(
            sandbox=sandbox,
            llm=MockAgentLLM(scenario="none"),
            max_retries=0,
            checkpointer_provider=provider,
        )
        ui_outcome = ui_runner.run(ui_state, thread_id=ui_key)
        self.assertEqual(ui_outcome.state.get("status"), "SUCCEEDED")
        self.assertIn("src/App.jsx", ui_outcome.state.get("generated_files") or {})
        self.assertNotIn("contract", ui_outcome.state)

        # ---- Thread B：真实 Architect Graph 发起澄清 ----
        arch_run_id = "run_arch_iso"
        arch_project = "project_arch"
        arch_key = scoped_checkpoint_key(
            "architect_run",
            arch_run_id,
            tenant_id="tenant_b",
            project_id=arch_project,
        )
        arch_runner = ArchitectGraphRunner(
            MockArchitectLLM(),
            checkpointer_provider=provider,
        )
        arch_state = _architect_initial_state(
            run_id=arch_run_id,
            tenant_id="tenant_b",
            project_id=arch_project,
            history=[{"role": "user", "content": self._arch_prompt()}],
        )
        arch_outcome = arch_runner.run(
            arch_state,
            run_id=arch_run_id,
            thread_id=arch_key,
        )
        self.assertTrue(arch_outcome.interrupted)
        self.assertNotIn("generated_files", arch_outcome.state)
        self.assertNotIn("current_files", arch_outcome.state)
        # Architect 的源码只来自项目快照，不包含 UI Run 的生成结果。
        self.assertEqual(
            arch_outcome.state.get("source_files"),
            {"src/App.jsx": UI_SOURCE},
        )

        # ---- Checkpointer 物理隔离：B 的作用域读不到 A，反之亦然 ----
        saver = provider.get()
        ui_tuple = saver.get_tuple({"configurable": {"thread_id": ui_key}})
        arch_tuple = saver.get_tuple({"configurable": {"thread_id": arch_key}})
        self.assertIsNotNone(ui_tuple)
        self.assertIsNotNone(arch_tuple)

        # 同一 runId 换到另一个项目，必须是全新的空 checkpoint。
        cross_ui_key = scoped_checkpoint_key(
            "ui_run",
            ui_run_id,
            tenant_id="tenant_b",
            project_id=arch_project,
        )
        self.assertIsNone(
            saver.get_tuple({"configurable": {"thread_id": cross_ui_key}})
        )

        # UI 状态无架构契约；Architect 状态无 Coder 产物。
        self.assertNotIn("contract", ui_outcome.state)
        self.assertNotIn("generated_files", arch_outcome.state)
        self.assertNotIn(ARCH_MARKER, repr(ui_outcome.state))
        self.assertNotIn(UI_MARKER, repr(arch_outcome.state))

    @staticmethod
    def _arch_prompt() -> str:
        return f"{ARCH_MARKER} 我想做一个全栈订单系统，请先澄清架构"


if __name__ == "__main__":
    unittest.main()
