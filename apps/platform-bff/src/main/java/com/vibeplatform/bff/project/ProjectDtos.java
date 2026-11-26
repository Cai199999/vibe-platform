package com.vibeplatform.bff.project;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.List;

/** 项目与契约持久化 API 契约（camelCase）。 */
public final class ProjectDtos {

    private ProjectDtos() {
    }

    public record ProjectView(
            String id,
            String name,
            String appType,
            String sourceMode,
            String status,
            String createdAt,
            String updatedAt
    ) {
    }

    public record CreateProjectRequest(
            String id,
            String name,
            String appType,
            String sourceMode
    ) {
    }

    public record UpdateProjectRequest(
            String name,
            String status,
            String sourceMode
    ) {
    }

    public record ProjectListResponse(
            String tenantId,
            List<ProjectView> projects,
            String requestId
    ) {
    }

    public record ProjectResponse(
            ProjectView project,
            String requestId
    ) {
    }

    public record SaveContractRequest(
            String contractId,
            String status,
            JsonNode payload
    ) {
    }

    public record ContractView(
            String id,
            String contractId,
            String projectId,
            String status,
            JsonNode payload,
            String createdAt,
            String updatedAt
    ) {
    }

    public record ContractListResponse(
            String projectId,
            List<ContractView> contracts,
            String requestId
    ) {
    }

    public record ContractResponse(
            ContractView contract,
            String requestId
    ) {
    }
}
