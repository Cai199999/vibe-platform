"""沙盒快照（Git checkpoint）单元测试。

真实 Git 操作依赖 Docker 沙盒，由 E2E 覆盖；这里锁定纯逻辑契约：
  - git log 自定义分隔符解析（含中文 / 时间戳 / 异常记录）；
  - 快照请求的空白校验；
  - 响应模型 camelCase 序列化。
"""

from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError

from ai_core.adapters.sandbox.local_docker import LocalDockerSandbox
from ai_core.ports.sandbox import SandboxPatchError
from ai_core.schemas import (
    CreateSnapshotRequest,
    SnapshotInfo,
    SnapshotListResponse,
)


class GitLogParserTest(unittest.TestCase):
    def test_parses_records_in_order(self) -> None:
        newer = "a" * 40
        older = "b" * 40
        raw = (
            f"{newer}\x1faaaaaaa\x1fAlice\x1f2026-11-09T10:30:00+08:00\x1f"
            "完成了登录页样式\x1e\n"
            f"{older}\x1fbbbbbbb\x1fBob\x1f2026-11-08T20:00:00+00:00\x1f"
            "初始化项目\x1e\n"
        )

        commits = LocalDockerSandbox._parse_git_log(raw)

        self.assertEqual([item.commit_hash for item in commits], [newer, older])
        self.assertEqual(commits[0].message, "完成了登录页样式")
        self.assertEqual(commits[0].author, "Alice")
        self.assertEqual(commits[0].short_hash, "aaaaaaa")
        self.assertEqual(commits[1].committed_at.tzinfo is not None, True)
        self.assertEqual(commits[1].committed_at.year, 2026)

    def test_ignores_malformed_records(self) -> None:
        raw = (
            "not-a-commit\x1fabc\x1fAlice\x1f2026-11-09T10:30:00+08:00\x1f坏数据\x1e\n"
            + ("c" * 40)
            + "\x1fccccccc\x1fAlice\x1fnot-a-date\x1f无时区\x1e\n"
        )

        commits = LocalDockerSandbox._parse_git_log(raw)

        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].commit_hash, "c" * 40)
        # 非法时间戳回退为当前时间，绝不能因为一条脏记录让整页历史 500。
        self.assertIsInstance(commits[0].committed_at, datetime)


class UnifiedDiffApplyTest(unittest.TestCase):
    """复现真实事故：hunk 头行数写错时，补丁仍必须能落盘。"""

    # 模型把 3 行插入声明成 ``+1,4``（实际应为 +1,6），严格 git apply 会
    # 报 ``corrupt patch``；这是线上“代码生成成功但按钮没出现”的根因之一。
    MISCCOUNT_PATCH = (
        "--- a/src/App.jsx\n"
        "+++ b/src/App.jsx\n"
        "@@ -1,3 +1,4 @@\n"
        " import { useMemo } from 'react'\n"
        "+import { Plus } from 'lucide-react'\n"
        "+const EXTRA_ONE = 1\n"
        "+const EXTRA_TWO = 2\n"
        " \n"
        " export default function App() {\n"
        " \n"
    )

    @staticmethod
    def _sandbox() -> tuple[LocalDockerSandbox, list[list[str]]]:
        sandbox = object.__new__(LocalDockerSandbox)
        sandbox.settings = SimpleNamespace(sandbox_workdir="/app")  # type: ignore[assignment]
        commands: list[list[str]] = []

        def exec_(container: Any, argv: list[str], workdir: str | None = None) -> Any:
            commands.append(argv)
            script = argv[-1]
            # 模拟 Git：没有 --recount 时拒绝计错补丁。
            if "git apply" in script and "--recount" not in script:
                return 128, b"error: corrupt patch at /tmp/vibe.patch.diff:9\n"
            return 0, b""

        sandbox._exec = exec_  # type: ignore[method-assign]
        return sandbox, commands

    def test_miscounted_hunk_applies_with_recount(self) -> None:
        sandbox, commands = self._sandbox()
        container = SimpleNamespace(put_archive=lambda path, data: True)

        sandbox._apply_unified_diff(container, self.MISCCOUNT_PATCH)

        self.assertTrue(commands)
        self.assertIn("--recount", commands[0][-1])


class AstPreflightTest(unittest.TestCase):
    """写盘前 AST 门禁：非法代码必须在触碰 /app 之前被拦截。"""

    @staticmethod
    def _sandbox(exit_code: int = 0, output: bytes = b"") -> tuple[Any, list[Any]]:
        sandbox = object.__new__(LocalDockerSandbox)
        sandbox.settings = SimpleNamespace(  # type: ignore[assignment]
            sandbox_workdir="/app",
            sandbox_template_dir="/nonexistent-template",
        )
        calls: list[Any] = []

        def exec_(
            container: Any, argv: list[str], workdir: str | None = None, **_: Any
        ) -> Any:
            calls.append((argv, workdir))
            if argv[0] == "node":
                return exit_code, output
            return 0, b""

        sandbox._exec = exec_  # type: ignore[method-assign]
        sandbox._ensure_ast_checker = lambda container: True  # type: ignore[method-assign]
        return sandbox, calls

    def test_invalid_files_are_blocked_before_live_write(self) -> None:
        sandbox, calls = self._sandbox(
            exit_code=1, output=b"src/App.jsx:2:1 unbound <Plus>\n"
        )
        writes: list[tuple[str, bytes]] = []
        container = SimpleNamespace(
            put_archive=lambda path, archive: writes.append((path, archive)) or True
        )

        with self.assertRaises(SandboxPatchError) as ctx:
            sandbox._preflight_code_check(
                container,
                {"src/App.jsx": "export default () => <Plus />"},
                None,
            )

        self.assertIn("未触发 HMR", str(ctx.exception))
        # 预检只写 /tmp 临时目录，绝不能写 /app。
        self.assertTrue(writes)
        self.assertTrue(all(path.startswith("/tmp") for path, _ in writes))
        node_calls = [call for call in calls if call[0][0] == "node"]
        self.assertEqual(len(node_calls), 1)
        self.assertIn("/tmp/vibe-ast-stage/src/App.jsx", node_calls[0][0])

    def test_apply_patch_preflight_failure_never_writes_live_files(self) -> None:
        sandbox, _ = self._sandbox()
        container = SimpleNamespace()
        sandbox._get_container = lambda sandbox_id: container  # type: ignore[method-assign]
        sandbox._validate_files = lambda files: dict(files)  # type: ignore[method-assign]

        def preflight(*_: Any, **__: Any) -> None:
            raise SandboxPatchError("syntax error")

        writes: list[dict[str, str]] = []
        sandbox._preflight_code_check = preflight  # type: ignore[method-assign]
        sandbox._put_files = lambda container, files: writes.append(dict(files))  # type: ignore[method-assign]

        with self.assertRaises(SandboxPatchError):
            sandbox.apply_patch(
                "sbx_demo", files={"src/App.jsx": "export default () => <Plus />"}
            )

        self.assertEqual(writes, [])

    def test_patch_target_paths_ignore_dev_null_and_traversal(self) -> None:
        patch = (
            "--- a/src/App.jsx\n"
            "+++ b/src/App.jsx\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "--- a/src/Old.jsx\n"
            "+++ /dev/null\n"
            "--- a/../escape.jsx\n"
            "+++ b/../escape.jsx\n"
        )

        self.assertEqual(LocalDockerSandbox._patch_target_paths(patch), ["src/App.jsx"])

    def test_missing_checker_fails_open_for_legacy_images(self) -> None:
        sandbox, calls = self._sandbox(exit_code=1, output=b"blocked")
        sandbox._ensure_ast_checker = lambda container: False  # type: ignore[method-assign]
        container = SimpleNamespace(put_archive=lambda path, archive: True)

        # 不应抛错，也不应执行 node。
        sandbox._preflight_code_check(container, {"src/App.jsx": "<Plus />"}, None)
        self.assertFalse([call for call in calls if call[0][0] == "node"])


class SnapshotSchemaTest(unittest.TestCase):
    def test_message_is_stripped(self) -> None:
        request = CreateSnapshotRequest(message="  完成了登录页样式  ")
        self.assertEqual(request.message, "完成了登录页样式")

    def test_blank_message_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            CreateSnapshotRequest(message="   ")

    def test_accepts_camel_case_context_fields(self) -> None:
        """BFF 透传 camelCase；字段必须真正落库到上下文记忆作用域。"""
        request = CreateSnapshotRequest.model_validate(
            {
                "message": "保存当前版本",
                "projectId": "proj_demo",
                "tenantId": "tenant_demo",
                "latestChanges": "新增登录按钮，并拖拽到卡片右上角",
            }
        )

        self.assertEqual(request.project_id, "proj_demo")
        self.assertEqual(request.tenant_id, "tenant_demo")
        self.assertEqual(request.latest_changes, "新增登录按钮，并拖拽到卡片右上角")

    def test_still_accepts_snake_case(self) -> None:
        """内部脚本 / 旧客户端使用 snake_case 时保持兼容。"""
        request = CreateSnapshotRequest.model_validate(
            {
                "message": "保存当前版本",
                "project_id": "proj_demo",
                "latest_changes": "调整按钮颜色",
            }
        )

        self.assertEqual(request.project_id, "proj_demo")
        self.assertEqual(request.latest_changes, "调整按钮颜色")

    def test_list_response_serializes_camel_case(self) -> None:
        response = SnapshotListResponse(
            sandbox_id="sbx_demo",
            head_commit="a" * 40,
            snapshots=[
                SnapshotInfo(
                    commit_hash="a" * 40,
                    short_hash="aaaaaaa",
                    message="完成了登录页样式",
                    author="Alice",
                    committed_at=datetime(2026, 11, 9, 10, 30),
                )
            ],
        )

        payload = response.model_dump(by_alias=True)

        self.assertEqual(payload["sandboxId"], "sbx_demo")
        self.assertEqual(payload["headCommit"], "a" * 40)
        self.assertEqual(payload["snapshots"][0]["commitHash"], "a" * 40)
        self.assertEqual(payload["snapshots"][0]["shortHash"], "aaaaaaa")
        self.assertEqual(
            payload["snapshots"][0]["committedAt"].isoformat(),
            "2026-11-09T10:30:00",
        )


if __name__ == "__main__":
    unittest.main()
