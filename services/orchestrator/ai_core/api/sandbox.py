"""沙盒生命周期内部 API。

这些接口只供 Java BFF / 内部编排调用，不直接暴露给浏览器。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from starlette.concurrency import run_in_threadpool
from docker.errors import DockerException

from ai_core.api.errors import sandbox_error_to_http
from ai_core.config import get_settings
from ai_core.adapters.sandbox import get_sandbox_provider
from ai_core.ports.sandbox import SandboxError
from ai_core.sandbox.importer import import_zip
from ai_core.sandbox.vision_import import import_image
from ai_core.schemas import (
    ApplyPatchRequest,
    ApplyPatchResponse,
    CreateSandboxRequest,
    CreateSnapshotRequest,
    CreateSnapshotResponse,
    DestroySandboxResponse,
    ImportSandboxResponse,
    RestoreSnapshotResponse,
    SandboxInfo,
    SnapshotDiffResponse,
    SnapshotInfo,
    SnapshotListResponse,
    VerifyResponse,
)

router = APIRouter(prefix="/api/v1/internal/sandbox", tags=["sandbox"])

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ZIP_SUFFIXES = (".zip",)
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
# 快照 Diff 体积上限：避免单个巨型提交把 BFF / 浏览器内存打满。
_SNAPSHOT_DIFF_MAX_CHARS = 200_000


@router.post("/create", response_model=SandboxInfo, status_code=201)
def create_sandbox(request: CreateSandboxRequest) -> SandboxInfo:
    """创建并启动一个 React + Vite 单前端沙盒。"""
    provider = get_sandbox_provider()
    try:
        handle = provider.create(
            project_id=request.project_id,
            files=request.files,
            template=request.template,
        )
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "SANDBOX_DOCKER_UNAVAILABLE", "message": str(exc)},
        ) from exc

    return SandboxInfo(
        sandbox_id=handle.sandbox_id,
        container_id=handle.container_id,
        container_name=handle.container_name,
        project_id=handle.project_id,
        state=handle.state,
        preview_url=handle.preview_url,
        host_port=handle.host_port,
        internal_port=handle.internal_port,
        template=handle.template,
        created_at=handle.created_at,
    )


@router.post("/apply-patch", response_model=ApplyPatchResponse)
def apply_patch(request: ApplyPatchRequest) -> ApplyPatchResponse:
    """写入全量文件或 unified diff，触发 Vite 热更新。"""
    provider = get_sandbox_provider()
    try:
        changed = provider.apply_patch(
            sandbox_id=request.sandbox_id,
            files=request.files,
            patch=request.patch,
            deleted_paths=request.deleted_paths,
        )
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc

    return ApplyPatchResponse(
        sandbox_id=request.sandbox_id,
        changed_files=changed,
        hot_reload=True,
        applied_at=datetime.now(timezone.utc),
    )


@router.get("/verify", response_model=VerifyResponse)
def verify(
    sandbox_id: str = Query(min_length=1),
    check: Literal["build", "test"] = "build",
) -> VerifyResponse:
    """在沙盒内执行 npm run build / npm run test。"""
    provider = get_sandbox_provider()
    try:
        result = provider.verify(sandbox_id=sandbox_id, check=check)
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc

    return VerifyResponse(
        sandbox_id=sandbox_id,
        check=check,
        success=result.success,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
        logs=result.logs,
        logs_truncated=result.logs_truncated,
        finished_at=datetime.now(timezone.utc),
    )


@router.post("/import", response_model=ImportSandboxResponse, status_code=201)
async def import_project(
    project_id: str = Form(min_length=1, max_length=64),
    file: UploadFile = File(...),
) -> ImportSandboxResponse:
    """导入 zip / 图片并直接启动一个可预览沙盒。

    浏览器 -> Java BFF -> 本接口。所有安全校验（路径穿越、zip 炸弹、
    文件数量、体积、加密包、符号链接）都在服务端完成；上传内容绝不执行
    宿主机命令，只在隔离容器内解压、npm install 或起静态服务器。
    """
    if not _PROJECT_ID_RE.fullmatch(project_id):
        raise HTTPException(
            status_code=422,
            detail={"code": "IMPORT_INVALID_PROJECT_ID", "message": "非法 projectId"},
        )

    settings = get_settings()
    filename = Path(file.filename or "upload").name
    lowered = filename.lower()
    is_zip = lowered.endswith(_ZIP_SUFFIXES)
    is_image = lowered.endswith(_IMAGE_SUFFIXES)
    if not is_zip and not is_image:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "IMPORT_UNSUPPORTED_FILE",
                "message": "只支持 .zip / .png / .jpg / .jpeg",
            },
        )

    limit = (
        settings.import_max_archive_bytes if is_zip else settings.import_max_image_bytes
    )
    # 多读 1 字节用于判断是否超限，避免把超大文件完整读进内存。
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "IMPORT_FILE_TOO_LARGE",
                "message": f"上传文件超过限制（{limit} 字节）",
            },
        )
    if not data:
        raise HTTPException(
            status_code=422,
            detail={"code": "IMPORT_EMPTY_FILE", "message": "上传文件为空"},
        )

    provider = get_sandbox_provider()
    try:
        if is_zip:
            imported = await run_in_threadpool(import_zip, data, settings)
        else:
            imported = await run_in_threadpool(
                import_image,
                data,
                filename,
                file.content_type or "",
                settings,
            )
        handle = await run_in_threadpool(
            provider.create_imported,
            project_id,
            imported.files,
            imported.kind,
            imported.start_command,
            imported.template,
        )
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "SANDBOX_DOCKER_UNAVAILABLE", "message": str(exc)},
        ) from exc

    return ImportSandboxResponse(
        sandbox_id=handle.sandbox_id,
        preview_url=handle.preview_url,
        project_id=project_id,
        kind=imported.kind,
        entry_file=imported.entry_file,
        file_count=imported.file_count,
        files=imported.text_files,
        warnings=imported.warnings,
        source_name=filename,
        created_at=datetime.now(timezone.utc),
    )


@router.post(
    "/{sandbox_id}/snapshots",
    response_model=CreateSnapshotResponse,
    status_code=201,
)
def create_snapshot(
    sandbox_id: str,
    request: CreateSnapshotRequest,
) -> CreateSnapshotResponse:
    """把沙盒当前工作区提交为一次快照（git commit）。"""
    provider = get_sandbox_provider()
    try:
        commit_hash = provider.git_commit(sandbox_id, request.message)
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc

    return CreateSnapshotResponse(
        sandbox_id=sandbox_id,
        commit_hash=commit_hash,
        short_hash=commit_hash[:7],
        message=request.message,
        committed_at=datetime.now(timezone.utc),
    )


@router.get("/{sandbox_id}/snapshots", response_model=SnapshotListResponse)
def list_snapshots(
    sandbox_id: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> SnapshotListResponse:
    """按时间倒序列出沙盒全部快照，并标记当前 HEAD。"""
    provider = get_sandbox_provider()
    try:
        commits = provider.git_log(sandbox_id, limit=limit)
        head_commit = provider.git_head(sandbox_id)
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc

    return SnapshotListResponse(
        sandbox_id=sandbox_id,
        head_commit=head_commit,
        snapshots=[
            SnapshotInfo(
                commit_hash=item.commit_hash,
                short_hash=item.short_hash,
                message=item.message,
                author=item.author,
                committed_at=item.committed_at,
            )
            for item in commits
        ],
    )


@router.post(
    "/{sandbox_id}/snapshots/{commit_hash}/restore",
    response_model=RestoreSnapshotResponse,
)
def restore_snapshot(sandbox_id: str, commit_hash: str) -> RestoreSnapshotResponse:
    """``git reset --hard`` 到指定快照。

    注意：这是硬回滚语义，目标之后产生的 commit 将不再出现在历史中，
    与用户确认弹窗中「未保存的修改将丢失」的提示一致。
    """
    provider = get_sandbox_provider()
    try:
        provider.git_reset_hard(sandbox_id, commit_hash)
        # 回滚成功后从历史中反查备注；找不到时允许返回空字符串。
        message = ""
        for item in provider.git_log(sandbox_id, limit=200):
            if item.commit_hash == commit_hash.strip().lower():
                message = item.message
                break
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc

    return RestoreSnapshotResponse(
        sandbox_id=sandbox_id,
        commit_hash=commit_hash.strip().lower(),
        message=message,
        restored_at=datetime.now(timezone.utc),
    )


@router.get(
    "/{sandbox_id}/snapshots/{commit_hash}/diff",
    response_model=SnapshotDiffResponse,
)
def snapshot_diff(
    sandbox_id: str,
    commit_hash: str,
    mode: Literal["commit", "current", "baseline"] = "baseline",
) -> SnapshotDiffResponse:
    """返回快照只读视图的 unified diff。

    默认 ``baseline``：该版本相对沙盒初始代码的累积 Diff，适合展示
    “这个版本长什么样”；``commit`` 只看该提交自身改动，``current``
    对比当前 HEAD。
    """
    provider = get_sandbox_provider()
    try:
        raw = provider.git_diff(sandbox_id, commit_hash, mode=mode)
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc

    truncated = len(raw) > _SNAPSHOT_DIFF_MAX_CHARS
    diff = raw
    if truncated:
        diff = (
            raw[:_SNAPSHOT_DIFF_MAX_CHARS]
            + f"\n... [diff 已截断，共 {len(raw)} 字符] ...\n"
        )

    return SnapshotDiffResponse(
        sandbox_id=sandbox_id,
        commit_hash=commit_hash.strip().lower(),
        mode=mode,
        diff=diff,
        truncated=truncated,
    )


@router.get("/{sandbox_id}/export")
def export_project(sandbox_id: str) -> Response:
    """把沙盒当前项目打包为 tar.gz 并直接返回字节流。

    排除 node_modules / .git / dist；解压后执行 `npm install && npm run dev`
    即可得到与沙盒一致的可运行 Vite 项目。
    """
    provider = get_sandbox_provider()
    try:
        archive = provider.export_archive(sandbox_id)
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "SANDBOX_DOCKER_UNAVAILABLE", "message": str(exc)},
        ) from exc

    filename = "project-export.tar.gz"
    return Response(
        content=archive,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.delete("/{sandbox_id}", response_model=DestroySandboxResponse)
def destroy(sandbox_id: str) -> DestroySandboxResponse:
    """销毁沙盒容器，幂等语义由 404 表达。"""
    provider = get_sandbox_provider()
    try:
        provider.destroy(sandbox_id)
    except SandboxError as exc:
        raise sandbox_error_to_http(exc) from exc

    return DestroySandboxResponse(
        sandbox_id=sandbox_id,
        destroyed=True,
        destroyed_at=datetime.now(timezone.utc),
    )
