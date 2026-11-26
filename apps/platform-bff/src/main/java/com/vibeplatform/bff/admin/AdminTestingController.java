package com.vibeplatform.bff.admin;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/** 管理中台：TestAgent 指标与最近报告（只读代理）。 */
@RestController
@RequestMapping("/api/v1/admin/testing")
@IsAdmin
public class AdminTestingController {

    private final AdminTestingService service;

    public AdminTestingController(AdminTestingService service) {
        this.service = service;
    }

    @GetMapping("/metrics")
    public Map<String, Object> metrics(
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        return wrap(service.metrics(), requestId);
    }

    @GetMapping("/reports")
    public Map<String, Object> reports(
            @RequestParam(defaultValue = "10") int limit,
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        return wrap(service.reports(limit), requestId);
    }

    private Map<String, Object> wrap(JsonNode data, String requestId) {
        String effectiveRequestId =
                requestId == null || requestId.isBlank()
                        ? UUID.randomUUID().toString()
                        : requestId;
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("data", data);
        response.put("requestId", effectiveRequestId);
        return response;
    }
}
