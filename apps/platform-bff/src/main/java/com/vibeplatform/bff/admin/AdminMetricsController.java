package com.vibeplatform.bff.admin;

import com.vibeplatform.bff.admin.AdminMetricsDtos.MetricsResponse;
import java.util.UUID;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 管理中台指标 API。
 *
 * <p>整个 Controller 由 {@link IsAdmin} 保护，非管理员在进入方法前就会
 * 被切面拒绝（HTTP 403 + ADMIN_FORBIDDEN）。</p>
 */
@RestController
@RequestMapping("/api/v1/admin")
@IsAdmin
public class AdminMetricsController {

    private final AdminMetricsService metricsService;

    public AdminMetricsController(AdminMetricsService metricsService) {
        this.metricsService = metricsService;
    }

    /**
     * 聚合看板数据：活跃沙盒、今日 Run、成功率、平均耗时、Agent 占比与
     * 最近 10 次执行。
     */
    @GetMapping("/metrics")
    public MetricsResponse metrics(
            @RequestHeader(name = "X-Request-Id", required = false) String requestId
    ) {
        String effectiveRequestId =
                requestId == null || requestId.isBlank()
                        ? UUID.randomUUID().toString()
                        : requestId;
        return new MetricsResponse(metricsService.snapshot(), effectiveRequestId);
    }
}
