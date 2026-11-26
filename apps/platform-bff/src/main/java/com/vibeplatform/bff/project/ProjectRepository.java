package com.vibeplatform.bff.project;

import com.vibeplatform.bff.project.ProjectDtos.ContractView;
import com.vibeplatform.bff.project.ProjectDtos.ProjectView;
import java.util.List;
import java.util.Optional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/**
 * 项目 / 契约持久化。
 *
 * <p>沿用 M1 schema 的 projects 表；project_contracts 由本仓库按需创建，
 * 避免 BFF 启动强依赖迁移已完成。所有查询按 tenant_id 过滤。</p>
 */
@Repository
public class ProjectRepository {

    private static final Logger log = LoggerFactory.getLogger(ProjectRepository.class);

    private static final String CREATE_CONTRACTS = """
            CREATE TABLE IF NOT EXISTS project_contracts (
              id CHAR(26) NOT NULL,
              tenant_id VARCHAR(64) NOT NULL,
              project_id VARCHAR(64) NOT NULL,
              contract_id VARCHAR(128) NOT NULL,
              status VARCHAR(32) NOT NULL DEFAULT 'CONFIRMED',
              payload JSON NOT NULL,
              created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
              updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                ON UPDATE CURRENT_TIMESTAMP(6),
              PRIMARY KEY (id),
              UNIQUE KEY uk_project_contract (tenant_id, contract_id),
              KEY idx_project_contracts_project (tenant_id, project_id, updated_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """;

    private final JdbcTemplate jdbcTemplate;
    private volatile boolean schemaReady = false;

    public ProjectRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    private void ensureSchema() {
        if (schemaReady) {
            return;
        }
        synchronized (this) {
            if (schemaReady) {
                return;
            }
            jdbcTemplate.execute(CREATE_CONTRACTS);
            schemaReady = true;
        }
    }

    public List<ProjectView> list(String tenantId, int limit) {
        ensureSchema();
        return jdbcTemplate.query(
                "SELECT id, name, app_type, source_mode, status, created_at, updated_at "
                        + "FROM projects WHERE tenant_id = ? AND deleted_at IS NULL "
                        + "ORDER BY updated_at DESC LIMIT ?",
                (rs, rowNum) -> new ProjectView(
                        rs.getString("id"),
                        rs.getString("name"),
                        rs.getString("app_type"),
                        rs.getString("source_mode"),
                        rs.getString("status"),
                        rs.getTimestamp("created_at").toInstant().toString(),
                        rs.getTimestamp("updated_at").toInstant().toString()
                ),
                tenantId,
                Math.max(1, Math.min(limit, 200))
        );
    }

    public Optional<ProjectView> get(String tenantId, String projectId) {
        ensureSchema();
        List<ProjectView> rows = jdbcTemplate.query(
                "SELECT id, name, app_type, source_mode, status, created_at, updated_at "
                        + "FROM projects WHERE tenant_id = ? AND id = ? "
                        + "AND deleted_at IS NULL",
                (rs, rowNum) -> new ProjectView(
                        rs.getString("id"),
                        rs.getString("name"),
                        rs.getString("app_type"),
                        rs.getString("source_mode"),
                        rs.getString("status"),
                        rs.getTimestamp("created_at").toInstant().toString(),
                        rs.getTimestamp("updated_at").toInstant().toString()
                ),
                tenantId,
                projectId
        );
        return rows.stream().findFirst();
    }

    public ProjectView create(
            String tenantId,
            String userId,
            String id,
            String name,
            String appType,
            String sourceMode
    ) {
        ensureSchema();
        jdbcTemplate.update(
                "INSERT INTO projects "
                        + "(id, tenant_id, owner_user_id, name, app_type, framework, "
                        + "build_tool, source_mode, status) "
                        + "VALUES (?, ?, ?, ?, ?, 'vite-react', 'vite', ?, 'ACTIVE') "
                        // 主键冲突只允许同租户更新；跨租户同 ID 不得被覆盖，
                        // 后续 get(tenant,id) 为空即返回 PROJECT_WRITE_FAILED。
                        + "ON DUPLICATE KEY UPDATE "
                        + "name = IF(tenant_id = VALUES(tenant_id), VALUES(name), name), "
                        + "app_type = IF(tenant_id = VALUES(tenant_id), "
                        + "VALUES(app_type), app_type), "
                        + "source_mode = IF(tenant_id = VALUES(tenant_id), "
                        + "VALUES(source_mode), source_mode), "
                        + "updated_at = IF(tenant_id = VALUES(tenant_id), "
                        + "CURRENT_TIMESTAMP(6), updated_at)",
                id,
                tenantId,
                userId,
                name,
                appType,
                sourceMode
        );
        return get(tenantId, id).orElseThrow(
                () -> new ProjectException(
                        "PROJECT_WRITE_FAILED", "项目 ID 冲突或写入失败")
        );
    }

    public Optional<ProjectView> update(
            String tenantId,
            String projectId,
            String name,
            String status,
            String sourceMode
    ) {
        ensureSchema();
        jdbcTemplate.update(
                "UPDATE projects SET name = COALESCE(?, name), "
                        + "status = COALESCE(?, status), "
                        + "source_mode = COALESCE(?, source_mode) "
                        + "WHERE tenant_id = ? AND id = ? AND deleted_at IS NULL",
                name,
                status,
                sourceMode,
                tenantId,
                projectId
        );
        return get(tenantId, projectId);
    }

    public boolean softDelete(String tenantId, String projectId) {
        ensureSchema();
        int updated = jdbcTemplate.update(
                "UPDATE projects SET status = 'DELETED', deleted_at = CURRENT_TIMESTAMP(6) "
                        + "WHERE tenant_id = ? AND id = ? AND deleted_at IS NULL",
                tenantId,
                projectId
        );
        return updated > 0;
    }

    public ContractView saveContract(
            String tenantId,
            String projectId,
            String contractId,
            String status,
            String payloadJson
    ) {
        ensureSchema();
        String id = "ctr_" + Integer.toHexString(
                (tenantId + projectId + contractId).hashCode()
        ) + Long.toHexString(System.nanoTime());
        jdbcTemplate.update(
                "INSERT INTO project_contracts "
                        + "(id, tenant_id, project_id, contract_id, status, payload) "
                        + "VALUES (?, ?, ?, ?, ?, CAST(? AS JSON)) "
                        + "ON DUPLICATE KEY UPDATE status = VALUES(status), "
                        + "payload = VALUES(payload), updated_at = CURRENT_TIMESTAMP(6)",
                id,
                tenantId,
                projectId,
                contractId,
                status,
                payloadJson
        );
        List<ContractView> rows = jdbcTemplate.query(
                "SELECT id, contract_id, project_id, status, payload, created_at, updated_at "
                        + "FROM project_contracts WHERE tenant_id = ? AND contract_id = ?",
                (rs, rowNum) -> new ContractView(
                        rs.getString("id"),
                        rs.getString("contract_id"),
                        rs.getString("project_id"),
                        rs.getString("status"),
                        readJson(rs.getString("payload")),
                        rs.getTimestamp("created_at").toInstant().toString(),
                        rs.getTimestamp("updated_at").toInstant().toString()
                ),
                tenantId,
                contractId
        );
        if (rows.isEmpty()) {
            throw new ProjectException("CONTRACT_WRITE_FAILED", "契约写入后读取失败");
        }
        return rows.get(0);
    }

    public List<ContractView> listContracts(String tenantId, String projectId, int limit) {
        ensureSchema();
        return jdbcTemplate.query(
                "SELECT id, contract_id, project_id, status, payload, created_at, updated_at "
                        + "FROM project_contracts WHERE tenant_id = ? AND project_id = ? "
                        + "ORDER BY updated_at DESC LIMIT ?",
                (rs, rowNum) -> new ContractView(
                        rs.getString("id"),
                        rs.getString("contract_id"),
                        rs.getString("project_id"),
                        rs.getString("status"),
                        readJson(rs.getString("payload")),
                        rs.getTimestamp("created_at").toInstant().toString(),
                        rs.getTimestamp("updated_at").toInstant().toString()
                ),
                tenantId,
                projectId,
                Math.max(1, Math.min(limit, 50))
        );
    }

    private static com.fasterxml.jackson.databind.JsonNode readJson(String value) {
        try {
            return new com.fasterxml.jackson.databind.ObjectMapper().readTree(value);
        } catch (Exception ex) {
            log.warn("契约 JSON 解析失败: {}", ex.getMessage());
            return new com.fasterxml.jackson.databind.ObjectMapper().createObjectNode();
        }
    }

    public static class ProjectException extends RuntimeException {
        private final String code;

        public ProjectException(String code, String message) {
            super(message);
            this.code = code;
        }

        public String code() {
            return code;
        }
    }
}
