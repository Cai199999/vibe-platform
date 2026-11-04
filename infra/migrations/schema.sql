-- ============================================================
-- vibe-platform M1 初始 schema
--
-- 设计约束（见 docs/design/02.整体架构设计.md）：
--   1. 禁止物理外键：所有关联字段只建普通/唯一索引，不建 FOREIGN KEY。
--   2. 所有业务表必须带 tenant_id，服务端查询强制过滤。
--   3. 表名使用 P0 约定的 users/projects/runs；
--      对应 02 文档中的 user_account/project/run 逻辑实体。
--   4. M1 只建最小核心表，后续快照/审批/服务拓扑等表按里程碑补齐。
-- ============================================================

CREATE DATABASE IF NOT EXISTS geo_ai_db
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_0900_ai_ci;

USE geo_ai_db;

-- ------------------------------------------------------------
-- 用户
-- 逻辑实体：02 文档中的 user_account
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            CHAR(26)     NOT NULL COMMENT 'ULID/UUIDv7',
    tenant_id     CHAR(26)     NOT NULL COMMENT '租户 ID；M1 为 personal tenant',
    email         VARCHAR(255) NOT NULL COMMENT '登录邮箱',
    password_hash VARCHAR(255) NOT NULL COMMENT 'Argon2id 哈希，绝不存明文',
    display_name  VARCHAR(100) NOT NULL DEFAULT '',
    avatar_url    VARCHAR(512) NULL,
    status        VARCHAR(32)  NOT NULL DEFAULT 'ACTIVE' COMMENT 'ACTIVE/DISABLED/DELETED',
    created_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    version       BIGINT       NOT NULL DEFAULT 1 COMMENT '乐观锁',
    deleted_at    DATETIME(6)  NULL COMMENT '软删除',
    PRIMARY KEY (id),
    UNIQUE KEY uk_users_email (email),
    KEY idx_users_tenant_status (tenant_id, status),
    KEY idx_users_tenant_created (tenant_id, created_at)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '用户账号（无物理外键）';

-- ------------------------------------------------------------
-- 项目
-- 逻辑实体：02 文档中的 project
-- app_type 为全栈生成预留：M1 只写 FRONTEND，M2 可写 FULLSTACK。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id                  CHAR(26)     NOT NULL,
    tenant_id           CHAR(26)     NOT NULL,
    owner_user_id       CHAR(26)     NOT NULL COMMENT '逻辑关联 users.id，无物理外键',
    name                VARCHAR(80)  NOT NULL,
    app_type            VARCHAR(16)  NOT NULL DEFAULT 'FRONTEND' COMMENT 'FRONTEND/FULLSTACK',
    framework           VARCHAR(32)  NOT NULL DEFAULT 'vite-react',
    build_tool          VARCHAR(32)  NOT NULL DEFAULT 'vite',
    source_mode         VARCHAR(16)  NOT NULL DEFAULT 'source' COMMENT 'source/visual',
    status              VARCHAR(32)  NOT NULL DEFAULT 'ACTIVE' COMMENT 'ACTIVE/ARCHIVED/DELETED',
    current_snapshot_id CHAR(26)     NULL COMMENT '逻辑关联项目快照，无物理外键',
    created_at          DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at          DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    version             BIGINT       NOT NULL DEFAULT 1,
    deleted_at          DATETIME(6)  NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_projects_tenant_name (tenant_id, name),
    KEY idx_projects_tenant_owner_status (tenant_id, owner_user_id, status),
    KEY idx_projects_tenant_updated (tenant_id, updated_at)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '项目（无物理外键）';

-- ------------------------------------------------------------
-- Run
-- 逻辑实体：02 文档中的 run
-- clarification_id / app_type 为全栈多轮澄清预留。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    id              CHAR(26)     NOT NULL,
    tenant_id       CHAR(26)     NOT NULL,
    project_id      CHAR(26)     NOT NULL COMMENT '逻辑关联 projects.id，无物理外键',
    user_id         CHAR(26)     NOT NULL COMMENT '逻辑关联 users.id，无物理外键',
    status          VARCHAR(32)  NOT NULL DEFAULT 'QUEUED' COMMENT 'QUEUED/PLANNING/AWAITING_CLARIFICATION/...',
    app_type        VARCHAR(16)  NOT NULL DEFAULT 'FRONTEND' COMMENT 'FRONTEND/FULLSTACK',
    intent_summary  TEXT         NULL,
    scope_json      JSON         NULL COMMENT '元素/区域/项目范围',
    base_snapshot_id CHAR(26)    NULL COMMENT '逻辑关联项目快照，无物理外键',
    sandbox_id      VARCHAR(64)  NULL,
    clarification_id CHAR(26)    NULL COMMENT '当前澄清轮次，M2 启用',
    budget_json     JSON         NULL,
    result_json     JSON         NULL,
    error_code      VARCHAR(64)  NULL,
    last_event_seq  BIGINT       NOT NULL DEFAULT 0,
    created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    started_at      DATETIME(6)  NULL,
    finished_at     DATETIME(6)  NULL,
    version         BIGINT       NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    KEY idx_runs_tenant_project_created (tenant_id, project_id, created_at),
    KEY idx_runs_tenant_status (tenant_id, status),
    KEY idx_runs_sandbox (sandbox_id)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Run 执行记录（无物理外键）';
