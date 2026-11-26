package com.vibeplatform.bff.project;

import com.fasterxml.jackson.databind.JsonNode;
import com.vibeplatform.bff.project.ProjectDtos.ContractView;
import com.vibeplatform.bff.project.ProjectDtos.CreateProjectRequest;
import com.vibeplatform.bff.project.ProjectDtos.ProjectView;
import com.vibeplatform.bff.project.ProjectDtos.SaveContractRequest;
import com.vibeplatform.bff.project.ProjectDtos.UpdateProjectRequest;
import com.vibeplatform.bff.project.ProjectRepository.ProjectException;
import java.util.List;
import java.util.UUID;
import org.springframework.stereotype.Service;

/** 项目 / 契约持久化业务规则。 */
@Service
public class ProjectService {

    private static final String ID_PATTERN = "^[A-Za-z0-9_-]{1,26}$";
    private static final int NAME_MAX = 80;

    private final ProjectRepository repository;

    public ProjectService(ProjectRepository repository) {
        this.repository = repository;
    }

    public List<ProjectView> list(String tenantId, int limit) {
        return repository.list(tenantId, limit);
    }

    public ProjectView get(String tenantId, String projectId) {
        return repository.get(tenantId, projectId).orElseThrow(
                () -> new ProjectException("PROJECT_NOT_FOUND", "项目不存在")
        );
    }

    public ProjectView create(
            String tenantId,
            String userId,
            CreateProjectRequest request
    ) {
        String id = blank(request.id())
                ? "proj_" + UUID.randomUUID().toString().replace("-", "").substring(0, 12)
                : request.id().trim();
        if (!id.matches(ID_PATTERN)) {
            throw new ProjectException("PROJECT_ID_INVALID", "项目 ID 不合法");
        }
        String name = requireName(request.name());
        return repository.create(
                tenantId,
                normalizeUserId(userId),
                id,
                name,
                normalizeAppType(request.appType()),
                normalizeSourceMode(request.sourceMode())
        );
    }

    public ProjectView update(
            String tenantId,
            String projectId,
            UpdateProjectRequest request
    ) {
        get(tenantId, projectId);
        String name = request.name() == null ? null : requireName(request.name());
        String status = request.status() == null ? null : request.status().trim();
        if (status != null && !status.matches("[A-Za-z_]{1,32}")) {
            throw new ProjectException("PROJECT_STATUS_INVALID", "项目状态不合法");
        }
        String sourceMode = request.sourceMode() == null
                ? null
                : normalizeSourceMode(request.sourceMode());
        return repository.update(tenantId, projectId, name, status, sourceMode)
                .orElseThrow(() -> new ProjectException(
                        "PROJECT_NOT_FOUND", "项目不存在"));
    }

    public void delete(String tenantId, String projectId) {
        get(tenantId, projectId);
        if (!repository.softDelete(tenantId, projectId)) {
            throw new ProjectException("PROJECT_NOT_FOUND", "项目不存在");
        }
    }

    public ContractView saveContract(
            String tenantId,
            String projectId,
            SaveContractRequest request
    ) {
        get(tenantId, projectId);
        String contractId = blank(request.contractId()) ? "" : request.contractId().trim();
        if (contractId.isEmpty() || contractId.length() > 128) {
            throw new ProjectException("CONTRACT_ID_INVALID", "契约 ID 不合法");
        }
        JsonNode payload = request.payload();
        if (payload == null || payload.isNull() || !payload.isObject()) {
            throw new ProjectException("CONTRACT_PAYLOAD_INVALID", "契约内容必须是 JSON 对象");
        }
        String status = blank(request.status()) ? "CONFIRMED" : request.status().trim();
        if (!status.matches("[A-Za-z_]{1,32}")) {
            throw new ProjectException("CONTRACT_STATUS_INVALID", "契约状态不合法");
        }
        return repository.saveContract(
                tenantId,
                projectId,
                contractId,
                status,
                payload.toString()
        );
    }

    public List<ContractView> listContracts(String tenantId, String projectId, int limit) {
        get(tenantId, projectId);
        return repository.listContracts(tenantId, projectId, limit);
    }

    private static String requireName(String value) {
        String name = value == null ? "" : value.trim();
        if (name.isEmpty() || name.length() > NAME_MAX) {
            throw new ProjectException(
                    "PROJECT_NAME_INVALID", "项目名称长度必须为 1-" + NAME_MAX);
        }
        return name;
    }

    private static String normalizeAppType(String value) {
        String appType = value == null ? "FRONTEND" : value.trim().toUpperCase();
        return "FULLSTACK".equals(appType) ? "FULLSTACK" : "FRONTEND";
    }

    private static String normalizeSourceMode(String value) {
        String mode = value == null ? "visual" : value.trim().toLowerCase();
        return "source".equals(mode) ? "source" : "visual";
    }

    private static String normalizeUserId(String value) {
        String userId = value == null ? "" : value.trim();
        if (userId.isEmpty()) {
            userId = "user_local";
        }
        return userId.length() > 26 ? userId.substring(0, 26) : userId;
    }

    private static boolean blank(String value) {
        return value == null || value.isBlank();
    }
}
