package com.vibeplatform.bff.project;

import com.vibeplatform.bff.identity.TenantContext;
import com.vibeplatform.bff.project.ProjectDtos.ContractListResponse;
import com.vibeplatform.bff.project.ProjectDtos.ContractResponse;
import com.vibeplatform.bff.project.ProjectDtos.CreateProjectRequest;
import com.vibeplatform.bff.project.ProjectDtos.ProjectListResponse;
import com.vibeplatform.bff.project.ProjectDtos.ProjectResponse;
import com.vibeplatform.bff.project.ProjectDtos.SaveContractRequest;
import com.vibeplatform.bff.project.ProjectDtos.UpdateProjectRequest;
import java.util.UUID;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 项目与契约持久化 API。
 *
 * <p>所有读写按 {@link TenantContext} 隔离；前端刷新后可通过 GET 列表恢复
 * 项目，契约确认后通过 POST /contracts 落库。</p>
 */
@RestController
@RequestMapping("/api/v1/projects")
public class ProjectController {

    private final ProjectService service;

    public ProjectController(ProjectService service) {
        this.service = service;
    }

    @GetMapping
    public ProjectListResponse list(
            @RequestParam(defaultValue = "100") int limit,
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        String tenantId = TenantContext.currentOrDefault();
        return new ProjectListResponse(
                tenantId,
                service.list(tenantId, limit),
                effectiveRequestId(requestId)
        );
    }

    @PostMapping
    public ProjectResponse create(
            @RequestBody CreateProjectRequest body,
            @RequestHeader(name = "X-User-Id", required = false) String userId,
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        String tenantId = TenantContext.currentOrDefault();
        return new ProjectResponse(
                service.create(tenantId, userId, body),
                effectiveRequestId(requestId)
        );
    }

    @GetMapping("/{projectId}")
    public ProjectResponse get(
            @PathVariable String projectId,
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        String tenantId = TenantContext.currentOrDefault();
        return new ProjectResponse(
                service.get(tenantId, projectId),
                effectiveRequestId(requestId)
        );
    }

    @PatchMapping("/{projectId}")
    public ProjectResponse update(
            @PathVariable String projectId,
            @RequestBody UpdateProjectRequest body,
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        String tenantId = TenantContext.currentOrDefault();
        return new ProjectResponse(
                service.update(tenantId, projectId, body),
                effectiveRequestId(requestId)
        );
    }

    @DeleteMapping("/{projectId}")
    public ProjectResponse delete(
            @PathVariable String projectId,
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        String tenantId = TenantContext.currentOrDefault();
        service.delete(tenantId, projectId);
        return new ProjectResponse(null, effectiveRequestId(requestId));
    }

    @GetMapping("/{projectId}/contracts")
    public ContractListResponse listContracts(
            @PathVariable String projectId,
            @RequestParam(defaultValue = "20") int limit,
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        String tenantId = TenantContext.currentOrDefault();
        return new ContractListResponse(
                projectId,
                service.listContracts(tenantId, projectId, limit),
                effectiveRequestId(requestId)
        );
    }

    @PostMapping("/{projectId}/contracts")
    public ContractResponse saveContract(
            @PathVariable String projectId,
            @RequestBody SaveContractRequest body,
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        String tenantId = TenantContext.currentOrDefault();
        return new ContractResponse(
                service.saveContract(tenantId, projectId, body),
                effectiveRequestId(requestId)
        );
    }

    private static String effectiveRequestId(String requestId) {
        return requestId == null || requestId.isBlank()
                ? UUID.randomUUID().toString()
                : requestId;
    }
}
