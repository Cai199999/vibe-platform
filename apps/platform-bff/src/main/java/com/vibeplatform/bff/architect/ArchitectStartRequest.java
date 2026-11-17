package com.vibeplatform.bff.architect;

import com.fasterxml.jackson.databind.JsonNode;
import jakarta.validation.constraints.NotBlank;

/**
 * 启动全局架构澄清的请求。
 *
 * <p>source / uiHistory / options 保持 JsonNode 透传，由 Python Pydantic
 * 做单一真源校验；BFF 只校验路由必需字段。</p>
 */
public record ArchitectStartRequest(
        String runId,
        String tenantId,
        @NotBlank String projectId,
        String projectName,
        JsonNode source,
        JsonNode uiHistory,
        JsonNode options,
        String projectMode,
        String outputPhase,
        JsonNode visualDocument,
        JsonNode visualDelta
) {
}
