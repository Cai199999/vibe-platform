package com.vibeplatform.bff.admin;

import com.vibeplatform.bff.admin.AdminFullstackMetricsDtos.FullstackMetricsResponse;
import java.util.UUID;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 系统中台：全栈生成健康与缺陷拦截指标。
 *
 * <p>该接口只做透传，真正的聚合发生在 Python AI Core（Applier 与
 * 契约审计所在进程）。沿用 {@link IsAdmin} 权限切面，前端无法绕过
 * BFF 直接读取内部指标。</p>
 */
@RestController
@RequestMapping("/api/v1/admin/fullstack")
@IsAdmin
public class AdminFullstackMetricsController {

    private final AdminFullstackMetricsService metricsService;

    public AdminFullstackMetricsController(
            AdminFullstackMetricsService metricsService
    ) {
        this.metricsService = metricsService;
    }

    @GetMapping("/metrics")
    public FullstackMetricsResponse metrics(
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        String effectiveRequestId =
                requestId == null || requestId.isBlank()
                        ? UUID.randomUUID().toString()
                        : requestId;
        return new FullstackMetricsResponse(
                metricsService.snapshot(), effectiveRequestId);
    }
}
