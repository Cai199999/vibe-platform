package com.vibeplatform.bff.projectsource;

import com.vibeplatform.bff.projectsource.ProjectSourceDtos.ProjectSourceFileRecord;
import com.vibeplatform.bff.projectsource.ProjectSourceDtos.ProjectSourceRecord;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;
import java.util.Optional;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

/**
 * 项目源码元数据仓储。
 *
 * <p>表结构在首次访问时惰性创建，避免 BFF 启动强依赖 MySQL：即使数据库
 * 暂时不可用，健康检查与不涉及源码持久化的接口仍可工作。真正读写时
 * 失败会统一转成 {@code PROJECT_SOURCE_STORAGE_UNAVAILABLE}。</p>
 */
@Repository
public class ProjectSourceRepository {

    private static final String CREATE_PROJECT_TABLE = """
            CREATE TABLE IF NOT EXISTS vibe_project_source (
              tenant_id VARCHAR(64) NOT NULL,
              project_id VARCHAR(64) NOT NULL,
              project_name VARCHAR(200) NULL,
              entry_file VARCHAR(255) NOT NULL,
              source_name VARCHAR(255) NULL,
              file_count INT NOT NULL,
              total_bytes BIGINT NOT NULL,
              content_hash CHAR(64) NOT NULL,
              revision INT NOT NULL,
              object_prefix VARCHAR(512) NOT NULL,
              created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
              updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                ON UPDATE CURRENT_TIMESTAMP(6),
              PRIMARY KEY (tenant_id, project_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """;

    private static final String CREATE_FILE_TABLE = """
            CREATE TABLE IF NOT EXISTS vibe_project_source_file (
              tenant_id VARCHAR(64) NOT NULL,
              project_id VARCHAR(64) NOT NULL,
              path VARCHAR(512) NOT NULL,
              size_bytes BIGINT NOT NULL,
              content_hash CHAR(64) NOT NULL,
              object_key VARCHAR(1024) NOT NULL,
              PRIMARY KEY (tenant_id, project_id, path)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """;

    private final JdbcTemplate jdbcTemplate;
    private volatile boolean schemaReady = false;

    public ProjectSourceRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public Optional<ProjectSourceRecord> find(String tenantId, String projectId) {
        ensureSchema();
        try {
            List<ProjectSourceRecord> rows = jdbcTemplate.query(
                    """
                    SELECT tenant_id, project_id, project_name, entry_file,
                           source_name, file_count, total_bytes, content_hash,
                           revision, object_prefix, updated_at
                    FROM vibe_project_source
                    WHERE tenant_id = ? AND project_id = ?
                    """,
                    (rs, rowNum) -> mapProject(rs),
                    tenantId,
                    projectId
            );
            return rows.stream().findFirst();
        } catch (DataAccessException ex) {
            throw storageFailure("读取项目源码元数据失败", ex);
        }
    }

    public List<ProjectSourceFileRecord> findFiles(String tenantId, String projectId) {
        ensureSchema();
        try {
            return jdbcTemplate.query(
                    """
                    SELECT path, size_bytes, content_hash, object_key
                    FROM vibe_project_source_file
                    WHERE tenant_id = ? AND project_id = ?
                    ORDER BY path
                    """,
                    (rs, rowNum) -> new ProjectSourceFileRecord(
                            rs.getString("path"),
                            rs.getLong("size_bytes"),
                            rs.getString("content_hash"),
                            rs.getString("object_key")
                    ),
                    tenantId,
                    projectId
            );
        } catch (DataAccessException ex) {
            throw storageFailure("读取项目源码文件树失败", ex);
        }
    }

    @Transactional
    public void save(ProjectSourceRecord record, List<ProjectSourceFileRecord> files) {
        ensureSchema();
        try {
            jdbcTemplate.update(
                    """
                    INSERT INTO vibe_project_source (
                      tenant_id, project_id, project_name, entry_file,
                      source_name, file_count, total_bytes, content_hash,
                      revision, object_prefix
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                      project_name = VALUES(project_name),
                      entry_file = VALUES(entry_file),
                      source_name = VALUES(source_name),
                      file_count = VALUES(file_count),
                      total_bytes = VALUES(total_bytes),
                      content_hash = VALUES(content_hash),
                      revision = VALUES(revision),
                      object_prefix = VALUES(object_prefix)
                    """,
                    record.tenantId(),
                    record.projectId(),
                    record.projectName(),
                    record.entryFile(),
                    record.sourceName(),
                    record.fileCount(),
                    record.totalBytes(),
                    record.contentHash(),
                    record.revision(),
                    record.objectPrefix()
            );
            jdbcTemplate.update(
                    "DELETE FROM vibe_project_source_file WHERE tenant_id = ? AND project_id = ?",
                    record.tenantId(),
                    record.projectId()
            );
            for (ProjectSourceFileRecord file : files) {
                jdbcTemplate.update(
                        """
                        INSERT INTO vibe_project_source_file (
                          tenant_id, project_id, path, size_bytes, content_hash, object_key
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        record.tenantId(),
                        record.projectId(),
                        file.path(),
                        file.sizeBytes(),
                        file.contentHash(),
                        file.objectKey()
                );
            }
        } catch (DataAccessException ex) {
            throw storageFailure("保存项目源码元数据失败", ex);
        }
    }

    private void ensureSchema() {
        if (schemaReady) {
            return;
        }
        synchronized (this) {
            if (schemaReady) {
                return;
            }
            try {
                jdbcTemplate.execute(CREATE_PROJECT_TABLE);
                jdbcTemplate.execute(CREATE_FILE_TABLE);
                schemaReady = true;
            } catch (DataAccessException ex) {
                throw storageFailure("初始化项目源码表失败", ex);
            }
        }
    }

    private static ProjectSourceRecord mapProject(ResultSet rs) throws SQLException {
        return new ProjectSourceRecord(
                rs.getString("tenant_id"),
                rs.getString("project_id"),
                rs.getString("project_name"),
                rs.getString("entry_file"),
                rs.getString("source_name"),
                rs.getInt("file_count"),
                rs.getLong("total_bytes"),
                rs.getString("content_hash"),
                rs.getInt("revision"),
                rs.getString("object_prefix"),
                rs.getTimestamp("updated_at").toInstant().toString()
        );
    }

    private static ProjectSourceException storageFailure(
            String message,
            DataAccessException cause
    ) {
        return ProjectSourceException.unavailable(
                message + "，请稍后重试",
                cause
        );
    }
}
