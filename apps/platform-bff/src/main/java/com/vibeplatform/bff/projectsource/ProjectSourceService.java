package com.vibeplatform.bff.projectsource;

import com.vibeplatform.bff.projectsource.ProjectSourceDtos.ProjectSourceFileRecord;
import com.vibeplatform.bff.projectsource.ProjectSourceDtos.ProjectSourceRecord;
import com.vibeplatform.bff.projectsource.ProjectSourceDtos.ProjectSourceResponse;
import com.vibeplatform.bff.projectsource.ProjectSourceDtos.SaveProjectSourceRequest;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Service;

/**
 * 项目源码持久化：MySQL 元数据 + MinIO 正文。
 *
 * <p>保存是“全量替换”语义：同一 projectId 每次 PUT 都会生成新 revision，
 * 文件树与内容哈希同步更新。读取时以 MySQL 文件树为准，从 MinIO 拉回
 * 每个文件正文，保证刷新 / 换浏览器后仍能得到同一份源码事实。</p>
 */
@Service
public class ProjectSourceService {

    private final ProjectSourceRepository repository;
    private final ProjectSourceObjectStore objectStore;

    public ProjectSourceService(
            ProjectSourceRepository repository,
            ProjectSourceObjectStore objectStore
    ) {
        this.repository = repository;
        this.objectStore = objectStore;
    }

    public synchronized ProjectSourceResponse save(
            String projectId,
            SaveProjectSourceRequest request
    ) {
        String safeProjectId = ProjectSourceValidator.normalizeProjectId(projectId);
        String tenantId = ProjectSourceValidator.normalizeTenant(request.tenantId());
        LinkedHashMap<String, String> files =
                ProjectSourceValidator.normalizeFiles(request.files());
        String entryFile = ProjectSourceValidator.resolveEntryFile(
                request.entryFile(),
                files
        );
        String sourceName = ProjectSourceValidator.normalizeSourceName(
                request.sourceName()
        );
        String projectName = ProjectSourceValidator.normalizeProjectName(
                request.projectName()
        );
        String contentHash = ProjectSourceValidator.contentHash(files);
        long totalBytes = files.values().stream()
                .mapToLong(value -> value.getBytes(StandardCharsets.UTF_8).length)
                .sum();

        int revision = repository.find(tenantId, safeProjectId)
                .map(existing -> existing.revision() + 1)
                .orElse(1);
        String prefix = ProjectSourceValidator.objectPrefix(
                tenantId,
                safeProjectId,
                revision
        );
        Map<String, String> objectKeys = objectStore.putFiles(prefix, files);
        List<ProjectSourceFileRecord> fileRecords = files.entrySet().stream()
                .map(entry -> new ProjectSourceFileRecord(
                        entry.getKey(),
                        entry.getValue().getBytes(StandardCharsets.UTF_8).length,
                        ProjectSourceValidator.sha256Text(entry.getValue()),
                        objectKeys.get(entry.getKey())
                ))
                .toList();
        String updatedAt = Instant.now().toString();
        ProjectSourceRecord record = new ProjectSourceRecord(
                tenantId,
                safeProjectId,
                projectName,
                entryFile,
                sourceName,
                files.size(),
                totalBytes,
                contentHash,
                revision,
                prefix,
                updatedAt
        );
        repository.save(record, fileRecords);
        return new ProjectSourceResponse(
                safeProjectId,
                tenantId,
                projectName,
                entryFile,
                sourceName,
                revision,
                files.size(),
                totalBytes,
                contentHash,
                updatedAt,
                null
        );
    }

    public ProjectSourceResponse get(String projectId, String tenantId) {
        String safeProjectId = ProjectSourceValidator.normalizeProjectId(projectId);
        String safeTenant = ProjectSourceValidator.normalizeTenant(tenantId);
        ProjectSourceRecord record = repository.find(safeTenant, safeProjectId)
                .orElseThrow(ProjectSourceException::notFound);
        LinkedHashMap<String, String> files = new LinkedHashMap<>();
        for (ProjectSourceFileRecord file : repository.findFiles(safeTenant, safeProjectId)) {
            files.put(file.path(), objectStore.readText(file.objectKey()));
        }
        return new ProjectSourceResponse(
                record.projectId(),
                record.tenantId(),
                record.projectName(),
                record.entryFile(),
                record.sourceName(),
                record.revision(),
                record.fileCount(),
                record.totalBytes(),
                record.contentHash(),
                record.updatedAt(),
                files
        );
    }

}
