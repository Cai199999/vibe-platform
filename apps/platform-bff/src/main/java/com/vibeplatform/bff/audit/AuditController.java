package com.vibeplatform.bff.audit;

import com.vibeplatform.bff.audit.AuditEventRepository.AuditEvent;
import com.vibeplatform.bff.identity.TenantContext;
import java.util.List;
import java.util.UUID;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 审计查询：只读、按租户隔离、仅管理员可见。
 */
@RestController
@RequestMapping("/api/v1/audit")
public class AuditController {

    private final AuditEventRepository repository;

    public AuditController(AuditEventRepository repository) {
        this.repository = repository;
    }

    @GetMapping("/events")
    @com.vibeplatform.bff.admin.IsAdmin
    public AuditEventsResponse events(
            @RequestParam(name = "limit", defaultValue = "50") int limit,
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        String tenantId = TenantContext.currentOrDefault();
        List<AuditEvent> events = repository.list(tenantId, limit);
        String effectiveRequestId =
                requestId == null || requestId.isBlank()
                        ? UUID.randomUUID().toString()
                        : requestId;
        return new AuditEventsResponse(tenantId, events, effectiveRequestId);
    }

    public record AuditEventsResponse(
            String tenantId,
            List<AuditEvent> events,
            String requestId
    ) {
    }
}
