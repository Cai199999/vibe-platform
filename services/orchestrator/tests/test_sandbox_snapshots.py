"""沙盒快照（Git checkpoint）单元测试。

真实 Git 操作依赖 Docker 沙盒，由 E2E 覆盖；这里锁定纯逻辑契约：
  - git log 自定义分隔符解析（含中文 / 时间戳 / 异常记录）；
  - 快照请求的空白校验；
  - 响应模型 camelCase 序列化。
"""

from __future__ import annotations

import unittest
from datetime import datetime

from pydantic import ValidationError

from ai_core.adapters.sandbox.local_docker import LocalDockerSandbox
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
