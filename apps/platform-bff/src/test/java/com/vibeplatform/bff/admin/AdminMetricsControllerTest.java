package com.vibeplatform.bff.admin;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.vibeplatform.bff.run.RunSandboxRegistry;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.EnableAspectJAutoProxy;
import org.springframework.context.annotation.Import;
import org.springframework.test.web.servlet.MockMvc;

/**
 * 管理中台 M3.1 契约测试：
 *  - 缺失 / 非法角色返回 403 且不泄露数据；
 *  - Mock 管理员（X-User-Role: admin）可读取指标；
 *  - 指标口径：成功率排除取消 / 暂停，Agent 占比可复算。
 */
@WebMvcTest(controllers = AdminMetricsController.class)
@Import({
        AdminAuthorizationAspect.class,
        AdminMetricsControllerTest.TestBeans.class
})
class AdminMetricsControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private AdminMetricsService metricsService;

    @Test
    void rejectsMissingRole() throws Exception {
        mockMvc.perform(get("/api/v1/admin/metrics"))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.code").value("ADMIN_ROLE_REQUIRED"))
                .andExpect(jsonPath("$.data").doesNotExist());
    }

    @Test
    void rejectsMemberRole() throws Exception {
        mockMvc.perform(get("/api/v1/admin/metrics")
                        .header("X-User-Role", "member"))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.code").value("ADMIN_ROLE_REQUIRED"));
    }

    @Test
    void adminCanReadDemoMetrics() throws Exception {
        metricsService.seedDemoData();

        mockMvc.perform(get("/api/v1/admin/metrics")
                        .header("X-User-Role", "admin")
                        .header("X-Request-Id", "req-admin-test"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.requestId").value("req-admin-test"))
                .andExpect(jsonPath("$.data.demo").value(true))
                .andExpect(jsonPath("$.data.overview.activeSandboxes").value(3))
                .andExpect(jsonPath("$.data.overview.totalRunsToday").value(18))
                .andExpect(jsonPath("$.data.overview.totalRuns").value(18))
                .andExpect(jsonPath("$.data.overview.successRate").value(0.9333))
                .andExpect(jsonPath("$.data.overview.successRateSampleSize").value(15))
                .andExpect(jsonPath("$.data.agentDistribution.length()").value(2))
                .andExpect(jsonPath("$.data.recentRuns.length()").value(10))
                // 最近一条必须是仍在运行的 Run，前端据此显示 RUNNING。
                .andExpect(jsonPath("$.data.recentRuns[0].status").value("RUNNING"))
                // Token / 成本汇总：只统计带 usage 的终态 Run。
                .andExpect(jsonPath("$.data.usage.runsWithUsage").value(15))
                .andExpect(jsonPath("$.data.usage.totalTokens").value(125500))
                .andExpect(jsonPath("$.data.usage.cost").value(0.995))
                .andExpect(jsonPath("$.data.usage.currency").value("CNY"));
    }

    @Test
    void platformAdminAndTenantOperatorAreAllowed() throws Exception {
        mockMvc.perform(get("/api/v1/admin/metrics")
                        .header("X-User-Role", "platform_admin"))
                .andExpect(status().isOk());
        mockMvc.perform(get("/api/v1/admin/metrics")
                        .header("X-User-Role", "tenant_operator"))
                .andExpect(status().isOk());
    }

    @TestConfiguration
    @EnableAspectJAutoProxy
    static class TestBeans {

        @Bean
        RunSandboxRegistry runSandboxRegistry() {
            return new RunSandboxRegistry();
        }

        @Bean
        AdminMetricsService adminMetricsService(RunSandboxRegistry runSandboxRegistry) {
            return new AdminMetricsService(runSandboxRegistry);
        }
    }
}
