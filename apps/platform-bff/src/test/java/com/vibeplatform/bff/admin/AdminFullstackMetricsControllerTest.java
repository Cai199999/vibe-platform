package com.vibeplatform.bff.admin;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.vibeplatform.bff.admin.AdminFullstackMetricsDtos.BlockDetails;
import com.vibeplatform.bff.admin.AdminFullstackMetricsDtos.FullstackMetricsData;
import com.vibeplatform.bff.admin.AdminFullstackMetricsDtos.Ratio;
import java.time.Instant;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.EnableAspectJAutoProxy;
import org.springframework.context.annotation.Import;
import org.springframework.test.web.servlet.MockMvc;

/**
 * 全栈健康指标透传契约：
 *  - 非管理员 403；
 *  - 正常时输出 camelCase 指标；
 *  - AI Core 不可用时 502，禁止伪造成全零健康数据。
 *
 * <p>测试环境使用可编程 Stub Service，而不是 Mockito：CI 的 Java 25
 * 超出当前 Byte Buddy 支持范围，@MockBean 会直接失败。</p>
 */
@WebMvcTest(controllers = AdminFullstackMetricsController.class)
@Import({
        AdminAuthorizationAspect.class,
        AdminExceptionHandler.class,
        AdminFullstackMetricsControllerTest.TestBeans.class
})
class AdminFullstackMetricsControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private StubAdminFullstackMetricsService metricsService;

    @BeforeEach
    void resetStub() {
        metricsService.data = null;
        metricsService.failure = null;
    }

    @Test
    void rejectsMissingRole() throws Exception {
        mockMvc.perform(get("/api/v1/admin/fullstack/metrics"))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.code").value("ADMIN_ROLE_REQUIRED"));
    }

    @Test
    void adminReadsCamelCaseMetrics() throws Exception {
        metricsService.data = new FullstackMetricsData(
                Instant.parse("2026-11-11T08:00:00Z"),
                true,
                "python-ai",
                new Ratio(0.25, 1, 4),
                3,
                new Ratio(0.875, 7, 8),
                new Ratio(0.5, 1, 2),
                new BlockDetails(1, 0)
        );

        mockMvc.perform(get("/api/v1/admin/fullstack/metrics")
                        .header("X-User-Role", "admin")
                        .header("X-Request-Id", "req-health-test"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.requestId").value("req-health-test"))
                .andExpect(jsonPath("$.data.available").value(true))
                .andExpect(jsonPath("$.data.upstream").value("python-ai"))
                .andExpect(jsonPath("$.data.applierBlockRate.rate").value(0.25))
                .andExpect(jsonPath("$.data.applierBlockRate.numerator").value(1))
                .andExpect(jsonPath("$.data.applierBlockRate.denominator").value(4))
                .andExpect(jsonPath("$.data.contractMisretryCount").value(3))
                .andExpect(jsonPath("$.data.multiTableAstPassRate.rate").value(0.875))
                .andExpect(jsonPath("$.data.details.applierBlockedByJoin").value(1));
    }

    @Test
    void upstreamFailureReturnsBadGateway() throws Exception {
        metricsService.failure = new FullstackMetricsUnavailableException(
                "AI Core 全栈指标暂不可用", new RuntimeException("timeout"));

        mockMvc.perform(get("/api/v1/admin/fullstack/metrics")
                        .header("X-User-Role", "admin")
                        .header("X-Request-Id", "req-health-down"))
                .andExpect(status().isBadGateway())
                .andExpect(jsonPath("$.code").value("AI_CORE_METRICS_UNAVAILABLE"))
                .andExpect(jsonPath("$.requestId").value("req-health-down"))
                .andExpect(jsonPath("$.data").doesNotExist());
    }

    static class StubAdminFullstackMetricsService extends AdminFullstackMetricsService {

        FullstackMetricsData data;
        RuntimeException failure;

        StubAdminFullstackMetricsService() {
            super(null);
        }

        @Override
        public FullstackMetricsData snapshot() {
            if (failure != null) {
                throw failure;
            }
            return data;
        }
    }

    @TestConfiguration
    @EnableAspectJAutoProxy
    static class TestBeans {

        @Bean
        StubAdminFullstackMetricsService adminFullstackMetricsService() {
            return new StubAdminFullstackMetricsService();
        }
    }
}
