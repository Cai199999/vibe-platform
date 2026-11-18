package com.vibeplatform.bff.projectsource;

import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.util.Map;

/**
 * 项目源码持久化 API 的请求 / 响应模型。
 *
 * <p>BFF 只保存导入源码的原始文件树与元数据，不做解析、不生成
 * VisualDocument。真实文件正文进 MinIO，文件路径与哈希进 MySQL。</p>
 */
public final class ProjectSourceDtos {

    private ProjectSourceDtos() {
    }

    /**
     * 全量替换一个项目的源码快照。
     *
     * @param tenantId   租户；缺省 tenant_local（当前鉴权仍为 Mock）
     * @param projectName 项目名称，仅用于展示
     * @param entryFile  入口文件；必须在 files 中
     * @param sourceName 原始上传文件名
     * @param files      路径 -> 文本源码
     */
    public record SaveProjectSourceRequest(
            @Size(max = 64, message = "tenantId 最多 64 字符")
            @Pattern(
                    regexp = "[A-Za-z0-9_-]*",
                    message = "tenantId 只允许字母、数字、下划线或短横线"
            )
            String tenantId,

            @Size(max = 200, message = "projectName 最多 200 字符")
            String projectName,

            @Size(max = 255, message = "entryFile 最多 255 字符")
            String entryFile,

            @Size(max = 255, message = "sourceName 最多 255 字符")
            String sourceName,

            @NotEmpty(message = "files 不能为空")
            Map<String, String> files
    ) {
    }

    /**
     * 源码快照响应。
     *
     * <p>{@code files} 只在 GET 时返回；PUT 响应只回元数据，避免重复传输。</p>
     */
    public record ProjectSourceResponse(
            String projectId,
            String tenantId,
            String projectName,
            String entryFile,
            String sourceName,
            int revision,
            int fileCount,
            long totalBytes,
            String contentHash,
            String updatedAt,
            Map<String, String> files
    ) {
    }

    record ProjectSourceRecord(
            String tenantId,
            String projectId,
            String projectName,
            String entryFile,
            String sourceName,
            int fileCount,
            long totalBytes,
            String contentHash,
            int revision,
            String objectPrefix,
            String updatedAt
    ) {
    }

    record ProjectSourceFileRecord(
            String path,
            long sizeBytes,
            String contentHash,
            String objectKey
    ) {
    }
}
