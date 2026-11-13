"""Phase 0 草稿隔离单元测试。

锁定三条核心契约：
  1. LocalDockerSandbox 的 worktree 生命周期：begin / discard / promote；
  2. Service 只允许审批通过后 promote，拒绝 / 失败只丢弃草稿；
  3. 审批 payload 必须把 iframe 指向草稿预览，并标记 ``isDraft``。
"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from ai_core.adapters.sandbox.local_docker import LocalDockerSandbox
from ai_core.agent.graph import _build_approval_payload
from ai_core.agent.service import RunExecutionService
from ai_core.domain.agent import Approval
from ai_core.ports.sandbox import SandboxDraft, SandboxError

MAIN_SHA = "a" * 40
DRAFT_SHA = "b" * 40


def _fake_docker_sandbox(
    *,
    main_sha: str = MAIN_SHA,
    status: str = "",
) -> tuple[LocalDockerSandbox, dict[str, Any]]:
    """构造只保留命令执行能力的 LocalDockerSandbox。

    真实容器操作由 E2E 覆盖；这里用状态化 fake ``_exec`` 验证命令序列与
    路由目录，避免单元测试依赖 Docker。
    """
    sandbox = object.__new__(LocalDockerSandbox)
    sandbox.settings = SimpleNamespace(  # type: ignore[assignment]
        sandbox_workdir="/app",
        sandbox_draft_root="/app-drafts",
        sandbox_draft_port=5174,
        sandbox_preview_host="127.0.0.1",
        sandbox_draft_start_timeout_seconds=1,
    )
    sandbox._drafts = {}
    sandbox._lock = threading.RLock()
    sandbox._git_lock = threading.RLock()
    container = SimpleNamespace()
    state: dict[str, Any] = {
        "head": main_sha,
        "calls": [],
        "starts": [],
        "stops": [],
        "removed": [],
    }

    def exec_(
        _container: Any,
        argv: list[str],
        workdir: str | None = None,
        **_: Any,
    ) -> tuple[int, bytes]:
        joined = " ".join(argv)
        state["calls"].append({"argv": argv, "workdir": workdir})
        if "status --porcelain" in joined:
            return 0, status.encode()
        if "commit --allow-empty" in joined:
            return 0, f"{DRAFT_SHA}\n".encode()
        if "merge --ff-only" in joined:
            state["head"] = DRAFT_SHA
            return 0, b""
        if "rev-parse HEAD" in joined:
            return 0, f"{state['head']}\n".encode()
        return 0, b""

    sandbox._exec = exec_  # type: ignore[method-assign]
    sandbox._get_container = lambda sandbox_id: container  # type: ignore[method-assign]
    sandbox._ensure_git_repo = lambda _container: None  # type: ignore[method-assign]
    sandbox._published_port = (  # type: ignore[method-assign]
        lambda _container, _port: 55074
    )
    sandbox._start_draft_preview = (  # type: ignore[method-assign]
        lambda _container, draft_id, path, port: state["starts"].append(
            (draft_id, path, port)
        )
    )
    sandbox._stop_draft_preview = (  # type: ignore[method-assign]
        lambda _container, draft_id: state["stops"].append(draft_id)
    )
    sandbox._remove_worktree = (  # type: ignore[method-assign]
        lambda _container, path, remove_dir=True: state["removed"].append(
            (path, remove_dir)
        )
    )
    return sandbox, state


class LocalDockerDraftLifecycleTest(unittest.TestCase):
    def test_begin_draft_creates_worktree_and_preview(self) -> None:
        sandbox, state = _fake_docker_sandbox()

        draft = sandbox.begin_draft("sbx_1")

        self.assertEqual(draft.base_sha, MAIN_SHA)
        self.assertEqual(draft.host_port, 55074)
        self.assertIn(f"/app-drafts/{draft.draft_id}", draft.workdir)
        self.assertIn("?draft=", draft.preview_url)

        worktree_call = next(
            call
            for call in state["calls"]
            if "git worktree add --detach" in " ".join(call["argv"])
        )
        self.assertEqual(worktree_call["workdir"], "/app")
        self.assertIn(MAIN_SHA, " ".join(worktree_call["argv"]))

        link_call = next(
            call
            for call in state["calls"]
            if "node_modules" in " ".join(call["argv"])
            and "ln -sfn" in " ".join(call["argv"])
        )
        link_command = " ".join(link_call["argv"])
        self.assertIn("/app/node_modules", link_command)
        self.assertIn(draft.workdir, link_command)

        self.assertEqual(state["starts"], [(draft.draft_id, draft.workdir, 55074)])
        # 草稿 ID 只能解析到登记过的影子目录，绝不能写任意路径。
        self.assertEqual(
            sandbox._resolve_workdir("sbx_1", draft.draft_id), draft.workdir
        )
        self.assertEqual(sandbox._resolve_workdir("sbx_1", None), "/app")
        with self.assertRaises(SandboxError):
            sandbox._resolve_workdir("sbx_1", "drf_ffffffffffffffff")

    def test_begin_draft_rejects_dirty_main_tree(self) -> None:
        sandbox, _state = _fake_docker_sandbox(status=" M src/App.jsx\n")

        with self.assertRaises(SandboxError) as ctx:
            sandbox.begin_draft("sbx_1")

        self.assertIn("未提交修改", str(ctx.exception))
        self.assertEqual(sandbox._drafts, {})

    def test_begin_draft_rejects_concurrent_draft_on_same_sandbox(self) -> None:
        sandbox, _state = _fake_docker_sandbox()
        sandbox.begin_draft("sbx_1")

        with self.assertRaises(SandboxError) as ctx:
            sandbox.begin_draft("sbx_1")

        self.assertIn("未完成的隔离草稿", str(ctx.exception))

    def test_discard_draft_is_idempotent(self) -> None:
        sandbox, state = _fake_docker_sandbox()
        draft = sandbox.begin_draft("sbx_1")

        sandbox.discard_draft("sbx_1", draft.draft_id)
        sandbox.discard_draft("sbx_1", draft.draft_id)

        self.assertEqual(state["stops"], [draft.draft_id])
        self.assertEqual(state["removed"], [(draft.workdir, True)])
        self.assertEqual(sandbox._drafts, {})

    def test_promote_draft_fast_forwards_and_cleans_up(self) -> None:
        sandbox, state = _fake_docker_sandbox()
        draft = sandbox.begin_draft("sbx_1")

        head = sandbox.promote_draft("sbx_1", draft.draft_id, draft.base_sha)

        self.assertEqual(head, DRAFT_SHA)
        commands = [" ".join(call["argv"]) for call in state["calls"]]
        self.assertTrue(any("merge-base --is-ancestor" in item for item in commands))
        self.assertTrue(any("merge --ff-only" in item for item in commands))
        self.assertEqual(state["stops"], [draft.draft_id])
        # promote 后 worktree 目录已被 git merge 使用，只 prune 不重复 rm。
        self.assertEqual(state["removed"], [(draft.workdir, False)])
        self.assertEqual(sandbox._drafts, {})

    def test_promote_draft_rejects_drifted_main_head(self) -> None:
        sandbox, state = _fake_docker_sandbox()
        draft = sandbox.begin_draft("sbx_1")
        state["head"] = "c" * 40

        with self.assertRaises(SandboxError) as ctx:
            sandbox.promote_draft("sbx_1", draft.draft_id, draft.base_sha)

        self.assertIn("已变化", str(ctx.exception))
        self.assertIn(draft.draft_id, sandbox._drafts)


class ApprovalPayloadDraftTest(unittest.TestCase):
    def test_preview_points_to_draft(self) -> None:
        state = {
            "run_id": "run_1",
            "sandbox_id": "sbx_1",
            "preview_url": "http://127.0.0.1:5173",
            "draft_id": "drf_0123456789abcdef0123",
            "draft_preview_url": "http://127.0.0.1:55074/?draft=drf_0123",
            "initial_files": {"src/App.jsx": "before\n"},
            "current_files": {"src/App.jsx": "after\n"},
            "generated_files": {"src/App.jsx": "after\n"},
            "generated_patch": "",
            "target_files": ["src/App.jsx"],
            "annotation_context": {"annotations": []},
            "verify_result": {"success": True},
        }

        payload = _build_approval_payload(state)
        approval = Approval.model_validate(payload)

        self.assertEqual(payload["previewUrl"], state["draft_preview_url"])
        self.assertTrue(payload["isDraft"])
        self.assertTrue(approval.is_draft)
        self.assertEqual(
            approval.model_dump(by_alias=True)["previewUrl"],
            state["draft_preview_url"],
        )


class _ServiceDraftSandbox:
    """仅实现 Service 收尾阶段需要的 Provider 方法。"""

    def __init__(self, *, fail_promote: bool = False) -> None:
        self.fail_promote = fail_promote
        self.promotions: list[tuple[str, str, str]] = []
        self.discards: list[tuple[str, str]] = []
        self.resets: list[tuple[str, str]] = []

    def promote_draft(self, sandbox_id: str, draft_id: str, base_sha: str) -> str:
        self.promotions.append((sandbox_id, draft_id, base_sha))
        if self.fail_promote:
            raise SandboxError("main changed")
        return DRAFT_SHA

    def discard_draft(self, sandbox_id: str, draft_id: str) -> None:
        self.discards.append((sandbox_id, draft_id))

    def git_reset_hard(self, sandbox_id: str, commit_hash: str) -> None:
        self.resets.append((sandbox_id, commit_hash))


def _prepared(draft: SandboxDraft | None) -> Any:
    return SimpleNamespace(
        run_id="run_1",
        handle=SimpleNamespace(
            sandbox_id="sbx_1",
            preview_url="http://127.0.0.1:5173",
        ),
        draft=draft,
        base_commit=MAIN_SHA,
    )


def _draft() -> SandboxDraft:
    return SandboxDraft(
        draft_id="drf_0123456789abcdef0123",
        sandbox_id="sbx_1",
        base_sha=MAIN_SHA,
        workdir="/app-drafts/drf_0123456789abcdef0123",
        preview_url="http://127.0.0.1:55074/?draft=drf_0123456789abcdef0123",
        host_port=55074,
        created_at=datetime.now(timezone.utc),
    )


class ServiceDraftLifecycleTest(unittest.TestCase):
    def test_promote_clears_draft_handle(self) -> None:
        sandbox = _ServiceDraftSandbox()
        service = object.__new__(RunExecutionService)
        service._sandbox = sandbox  # type: ignore[assignment]
        prepared = _prepared(_draft())

        service._promote_draft(prepared)

        self.assertEqual(sandbox.promotions, [("sbx_1", _draft().draft_id, MAIN_SHA)])
        self.assertIsNone(prepared.draft)

    def test_promote_failure_keeps_draft_for_explicit_discard(self) -> None:
        sandbox = _ServiceDraftSandbox(fail_promote=True)
        service = object.__new__(RunExecutionService)
        service._sandbox = sandbox  # type: ignore[assignment]
        prepared = _prepared(_draft())

        with self.assertRaises(SandboxError):
            service._promote_draft(prepared)

        self.assertIsNotNone(prepared.draft)
        self.assertEqual(sandbox.discards, [])

    def test_revert_discards_draft_without_touching_main(self) -> None:
        sandbox = _ServiceDraftSandbox()
        service = object.__new__(RunExecutionService)
        service._sandbox = sandbox  # type: ignore[assignment]
        prepared = _prepared(_draft())

        preview_url = service._revert_sandbox(prepared)

        self.assertEqual(sandbox.discards, [("sbx_1", _draft().draft_id)])
        self.assertEqual(sandbox.resets, [])
        self.assertTrue(preview_url.startswith("http://127.0.0.1:5173?v="))
        self.assertIsNone(prepared.draft)

    def test_discard_is_idempotent(self) -> None:
        sandbox = _ServiceDraftSandbox()
        service = object.__new__(RunExecutionService)
        service._sandbox = sandbox  # type: ignore[assignment]
        prepared = _prepared(_draft())

        service._discard_draft(prepared)
        service._discard_draft(prepared)

        self.assertEqual(sandbox.discards, [("sbx_1", _draft().draft_id)])


if __name__ == "__main__":
    unittest.main()
