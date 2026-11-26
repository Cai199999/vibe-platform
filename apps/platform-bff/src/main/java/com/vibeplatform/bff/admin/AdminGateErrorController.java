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

/**
 * 管理中台：确定性门禁错误的目录与高频统计。
 *
 * <p>数据源为 Python AI Core 的 error_router 账本；接口只读，管理员可见。</p>
 */
@RestController
@RequestMapping("/api/v1/admin/gate-errors")
@IsAdmin
public class AdminGateErrorController {

    private final AdminGateErrorService service;

    public AdminGateErrorController(AdminGateErrorService service) {
        this.service = service;
    }

    @GetMapping("/stats")
    public Map<String, Object> stats(
            @RequestParam(defaultValue = "10") int top,
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        String effectiveRequestId =
                requestId == null || requestId.isBlank()
                        ? UUID.randomUUID().toString()
                        : requestId;
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("data", service.stats(top));
        response.put("requestId", effectiveRequestId);
        return response;
    }
}
