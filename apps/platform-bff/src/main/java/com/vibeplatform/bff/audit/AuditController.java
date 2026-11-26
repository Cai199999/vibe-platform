package com.vibeplatform.bff.audit;

import com.vibeplatform.bff.audit.AuditEventRepository.AuditEvent;
import com.vibeplatform.bff.identity.TenantContext;
import java.util.List;
import java.util.UUID;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
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

    /**
     * 审计导出：按当前租户导出最近 N 条为 CSV。
     *
     * <p>只读、仅管理员；不提供跨租户导出。上限 5000 条，避免一次请求
     * 拉走整张审计表。需要长期归档时由外部任务分页拉取。</p>
     */
    @GetMapping(value = "/events/export", produces = "text/csv;charset=UTF-8")
    @com.vibeplatform.bff.admin.IsAdmin
    public ResponseEntity<String> exportEvents(
            @RequestParam(name = "limit", defaultValue = "1000") int limit
    ) {
        int capped = Math.max(1, Math.min(5_000, limit));
        String tenantId = TenantContext.currentOrDefault();
        List<AuditEvent> events = repository.list(tenantId, capped);
        StringBuilder csv = new StringBuilder(
                "id,tenantId,traceId,actor,method,path,status,detail,createdAt\n"
        );
        for (AuditEvent event : events) {
            csv.append(event.id()).append(',')
                    .append(csvCell(event.tenantId())).append(',')
                    .append(csvCell(event.traceId())).append(',')
                    .append(csvCell(event.actor())).append(',')
                    .append(csvCell(event.method())).append(',')
                    .append(csvCell(event.path())).append(',')
                    .append(event.status()).append(',')
                    .append(csvCell(event.detail())).append(',')
                    .append(csvCell(event.createdAt())).append('\n');
        }
        return ResponseEntity.ok()
                .header(
                        HttpHeaders.CONTENT_DISPOSITION,
                        "attachment; filename=\"audit-events.csv\""
                )
                .contentType(MediaType.parseMediaType("text/csv;charset=UTF-8"))
                .body(csv.toString());
    }

    private static String csvCell(String value) {
        String text = value == null ? "" : value;
        if (text.contains(",") || text.contains("\"") || text.contains("\n")) {
            return "\"" + text.replace("\"", "\"\"") + "\"";
        }
        return text;
    }

    public record AuditEventsResponse(
            String tenantId,
            List<AuditEvent> events,
            String requestId
    ) {
    }
}
