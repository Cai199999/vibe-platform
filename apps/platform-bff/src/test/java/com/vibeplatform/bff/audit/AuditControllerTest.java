package com.vibeplatform.bff.audit;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.vibeplatform.bff.admin.AdminAuthorizationAspect;
import com.vibeplatform.bff.audit.AuditEventRepository.AuditEvent;
import com.vibeplatform.bff.identity.TenantFilter;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.EnableAspectJAutoProxy;
import org.springframework.context.annotation.Import;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(controllers = AuditController.class)
@EnableAspectJAutoProxy
@Import({AdminAuthorizationAspect.class, TenantFilter.class, AuditControllerTest.TestBeans.class})
class AuditControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void rejectsMissingRole() throws Exception {
        mockMvc.perform(get("/api/v1/audit/events"))
                .andExpect(status().isForbidden());
    }

    @Test
    void adminReadsTenantScopedEvents() throws Exception {
        mockMvc.perform(get("/api/v1/audit/events")
                        .header("X-User-Role", "admin")
                        .header("X-Tenant-Id", "tenant_audit")
                        .header("X-Request-Id", "req-audit"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.tenantId").value("tenant_audit"))
                .andExpect(jsonPath("$.requestId").value("req-audit"))
                .andExpect(jsonPath("$.events.length()").value(1))
                .andExpect(jsonPath("$.events[0].actor").value("user_audit"));
    }

    @Test
    void adminExportsTenantScopedCsv() throws Exception {
        mockMvc.perform(get("/api/v1/audit/events/export")
                        .header("X-User-Role", "admin")
                        .header("X-Tenant-Id", "tenant_audit"))
                .andExpect(status().isOk())
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers
                        .header().string("Content-Disposition",
                                "attachment; filename=\"audit-events.csv\""))
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers
                        .content().string(org.hamcrest.Matchers.containsString(
                                "id,tenantId,traceId,actor,method,path,status")))
                .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers
                        .content().string(org.hamcrest.Matchers.containsString(
                                "tenant_audit")));
    }

    @Test
    void exportRejectsMissingRole() throws Exception {
        mockMvc.perform(get("/api/v1/audit/events/export"))
                .andExpect(status().isForbidden());
    }

    @TestConfiguration
    static class TestBeans {

        @Bean
        AuditEventRepository auditEventRepository() {
            return new StubRepository();
        }
    }

    static class StubRepository extends AuditEventRepository {

        StubRepository() {
            super(null);
        }

        @Override
        public List<AuditEvent> list(String tenantId, int limit) {
            return List.of(new AuditEvent(
                    1L,
                    tenantId,
                    "trc_1",
                    "user_audit",
                    "POST",
                    "/api/v1/architect/documents/classify",
                    422,
                    "test-agent",
                    "2026-11-25T05:00:00Z"
            ));
        }
    }
}
