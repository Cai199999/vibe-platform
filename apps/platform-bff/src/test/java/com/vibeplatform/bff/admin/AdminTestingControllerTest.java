package com.vibeplatform.bff.admin;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.EnableAspectJAutoProxy;
import org.springframework.context.annotation.Import;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.web.client.RestClient;

/** 管理中台测试指标：权限与透传契约。 */
@WebMvcTest(controllers = AdminTestingController.class)
@Import({AdminAuthorizationAspect.class, AdminTestingControllerTest.TestBeans.class})
class AdminTestingControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void rejectsMissingRole() throws Exception {
        mockMvc.perform(get("/api/v1/admin/testing/metrics"))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.code").value("ADMIN_ROLE_REQUIRED"));
    }

    @Test
    void adminReadsTestingMetrics() throws Exception {
        mockMvc.perform(get("/api/v1/admin/testing/metrics")
                        .header("X-User-Role", "admin")
                        .header("X-Request-Id", "req-testing"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.requestId").value("req-testing"))
                .andExpect(jsonPath("$.data.totalRuns").value(7))
                .andExpect(jsonPath("$.data.checkPassRate").value(0.9));
    }

    @TestConfiguration
    @EnableAspectJAutoProxy
    static class TestBeans {

        @Bean
        AdminTestingService adminTestingService() {
            return new AdminTestingService(RestClient.create()) {
                @Override
                public JsonNode metrics() {
                    ObjectMapper mapper = new ObjectMapper();
                    ObjectNode node = mapper.createObjectNode();
                    node.put("totalRuns", 7);
                    node.put("checkPassRate", 0.9);
                    return node;
                }
            };
        }
    }
}
