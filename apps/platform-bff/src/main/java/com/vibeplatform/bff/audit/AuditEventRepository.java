package com.vibeplatform.bff.audit;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/**
 * 审计事件落库：只追加，不更新、不删除。
 *
 * <p>表按需创建，避免在 BFF 启动时强依赖数据库迁移已经执行。
 * 写入失败只记录日志，不阻断业务请求；是否 fail-closed 由后续策略控制。</p>
 */
@Repository
public class AuditEventRepository {

    private static final Logger log = LoggerFactory.getLogger(AuditEventRepository.class);

    private static final String CREATE_TABLE = """
            CREATE TABLE IF NOT EXISTS vibe_audit_event (
              id BIGINT NOT NULL AUTO_INCREMENT,
              tenant_id VARCHAR(64) NOT NULL,
              trace_id VARCHAR(128) NULL,
              actor VARCHAR(128) NULL,
              method VARCHAR(16) NOT NULL,
              path VARCHAR(512) NOT NULL,
              status INT NOT NULL,
              detail TEXT NULL,
              created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
              PRIMARY KEY (id),
              KEY idx_audit_tenant_created (tenant_id, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """;

    private final JdbcTemplate jdbcTemplate;
    private volatile boolean schemaReady = false;

    public AuditEventRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public void record(
            String tenantId,
            String traceId,
            String actor,
            String method,
            String path,
            int status,
            String detail
    ) {
        try {
            ensureSchema();
            jdbcTemplate.update(
                    "INSERT INTO vibe_audit_event "
                            + "(tenant_id, trace_id, actor, method, path, status, detail) "
                            + "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    tenantId,
                    traceId,
                    actor,
                    method,
                    path,
                    status,
                    detail
            );
        } catch (DataAccessException ex) {
            // 审计失败不能伪装成业务失败，但必须留下日志。
            log.warn("审计事件写入失败 tenant={} path={} error={}",
                    tenantId, path, ex.getMessage());
        }
    }

    public java.util.List<AuditEvent> list(String tenantId, int limit) {
        try {
            ensureSchema();
            return jdbcTemplate.query(
                    "SELECT id, tenant_id, trace_id, actor, method, path, status, "
                            + "detail, created_at FROM vibe_audit_event "
                            + "WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                    (rs, rowNum) -> new AuditEvent(
                            rs.getLong("id"),
                            rs.getString("tenant_id"),
                            rs.getString("trace_id"),
                            rs.getString("actor"),
                            rs.getString("method"),
                            rs.getString("path"),
                            rs.getInt("status"),
                            rs.getString("detail"),
                            rs.getTimestamp("created_at").toInstant().toString()
                    ),
                    tenantId,
                    Math.max(1, Math.min(limit, 200))
            );
        } catch (DataAccessException ex) {
            log.warn("审计事件查询失败 tenant={} error={}", tenantId, ex.getMessage());
            return java.util.List.of();
        }
    }

    public record AuditEvent(
            long id,
            String tenantId,
            String traceId,
            String actor,
            String method,
            String path,
            int status,
            String detail,
            String createdAt
    ) {
    }

    private void ensureSchema() {
        if (schemaReady) {
            return;
        }
        synchronized (this) {
            if (!schemaReady) {
                jdbcTemplate.execute(CREATE_TABLE);
                schemaReady = true;
            }
        }
    }
}
